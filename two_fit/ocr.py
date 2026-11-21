"""Reading a 1Fit membership screenshot: the end date, the tariff and the freeze days left.

Tesseract does the reading (``pytesseract`` + the ``tesseract`` program with the rus/uzb_cyrl languages).
Without them nothing breaks: ``read_text`` returns ``None`` and the listing is checked by the admin's eyes only.

What is read is a hint, never the truth: the seller's form is filled with it (and can be corrected), and the
admin sees where it differs from what the seller wrote.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

MAX_SCREENSHOTS = 3
WANTED_LANGS = ("rus", "uzb_cyrl", "uzb", "eng")

_MONTHS = {  # Russian (genitive and nominative) and Uzbek Cyrillic month names -> month number
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
_WORD_DATE = re.compile(r"(\d{1,2})\s+" + _MONTH_RE + r"\s+(\d{4})", re.I)
_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_END_WORDS = re.compile(r"(до|действ|оконч|истека|гача|тугаш|амал)", re.I)
_START_WORDS = re.compile(r"(\bс\b|начал|актив|купл|покупк|бошлан|фаол)", re.I)
_PLAN = re.compile(r"(?<!\d)(6|12|24)\s*(месяц\w*|мес\.?|ой\w*)", re.I)
_PLAN_DAYS = re.compile(r"(?<!\d)(180|365|730)\s*(дн\w*|кун\w*)", re.I)
_FREEZE_LINE = re.compile(r"(заморо\w*|музлат\w*)", re.I)
_DAYS = re.compile(r"(?<!\d)(\d{1,3})\s*(дн\w*|день|дня|кун\w*)", re.I)

PLAN_BY_MONTHS = {6: 180, 12: 365, 24: 730}


def _month(word: str) -> int | None:
    w = word.lower()
    if w.startswith("ма"):  # «мая», «май» — but not «март»
        return 3 if w.startswith("март") else 5
    for stem, n in _MONTHS.items():
        if w.startswith(stem):
            return n
    return None


def _dates(line: str) -> list[date]:
    found = []
    for d, m, y in _NUM_DATE.findall(line):
        year = int(y) + 2000 if len(y) == 2 else int(y)
        try:
            found.append(date(year, int(m), int(d)))
        except ValueError:
            pass
    for d, word, y in _WORD_DATE.findall(line):
        month = _month(word)
        if month:
            try:
                found.append(date(int(y), month, int(d)))
            except ValueError:
                pass
    return found


def parse_card(text: str | None, today: date) -> dict[str, Any]:
    """Fields found in the text of a 1Fit membership card; only what was actually found is returned."""
    if not text:
        return {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result: dict[str, Any] = {}

    ends, starts, others = [], [], []
    for i, line in enumerate(lines):
        dates = _dates(line)
        if not dates:
            continue
        context = line + " " + (lines[i - 1] if i else "")  # the label is often on the line above
        if _END_WORDS.search(context):
            ends += dates
        elif _START_WORDS.search(context):
            starts += dates
        else:
            others += dates
    future = [d for d in ends + others if d > today]
    if ends and max(ends) > today:
        result["end_date"] = max(ends).isoformat()
    elif future:
        result["end_date"] = max(future).isoformat()
    past = [d for d in starts + others if d <= today]
    if starts and min(starts) <= today:
        result["start_date"] = min(starts).isoformat()
    elif past:
        result["start_date"] = min(past).isoformat()

    whole = " ".join(lines)
    m = _PLAN.search(whole)
    if m:
        result["tariff_days"] = PLAN_BY_MONTHS[int(m.group(1))]
    else:
        m = _PLAN_DAYS.search(whole)
        if m:
            result["tariff_days"] = int(m.group(1))

    for i, line in enumerate(lines):
        if _FREEZE_LINE.search(line):
            near = " ".join(lines[i:i + 2])  # «Заморозка» and «20 дней» may be on two lines
            d = _DAYS.search(near)
            if d and 0 < int(d.group(1)) <= 365:
                result["freeze_days"] = int(d.group(1))
                break
    return result


def merge(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Several screenshots of one card: the first value found for each field wins."""
    merged: dict[str, Any] = {}
    for r in results:
        for k, v in (r or {}).items():
            merged.setdefault(k, v)
    return merged


def mismatches(read: dict[str, Any], listing: dict[str, Any]) -> list[str]:
    """Fields where the screenshot and the listing disagree: «end_date», «tariff_days», «freeze_days»."""
    out = []
    for key in ("end_date", "tariff_days", "freeze_days"):
        if read.get(key) is None or listing.get(key) is None:
            continue
        if key == "end_date":
            if abs((date.fromisoformat(read[key]) - date.fromisoformat(listing[key])).days) > 3:
                out.append(key)
        elif read[key] != listing[key]:
            out.append(key)
    return out


# ------------------------------------------------------------------ Tesseract
@lru_cache(maxsize=1)
def available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 — not installed: reading is simply off
        return False


def read_text(image: bytes) -> str | None:
    """The text on a screenshot, or None when Tesseract is not installed or the picture is unreadable.
    Blocking: call it through ``asyncio.to_thread``."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(image))
        img = ImageOps.exif_transpose(img).convert("L")
        if sum(img.getdata()) / (img.width * img.height) < 110:  # dark theme: white text on black
            img = ImageOps.invert(img)
        img = ImageOps.autocontrast(img)
        if img.width < 1400:  # small phone screenshots read better enlarged
            k = 1400 / img.width
            img = img.resize((round(img.width * k), round(img.height * k)))
        langs = [lang for lang in WANTED_LANGS if lang in set(pytesseract.get_languages(config=""))] or ["eng"]
        return pytesseract.image_to_string(img, lang="+".join(langs))
    except Exception as e:  # noqa: BLE001 — a bad picture must not break selling
        log.warning("screenshot not read: %s", e)
        return None


def read_card(image: bytes, today: date) -> dict[str, Any]:
    return parse_card(read_text(image), today)
