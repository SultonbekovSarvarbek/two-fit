"""Reading a 1Fit membership screenshot: the end date, the tariff, the freeze days and the bonus days left.

Tesseract does the reading (``pytesseract`` + the ``tesseract`` program with the rus/uzb_cyrl languages).
Without them nothing breaks: ``read_words`` returns ``None`` and the listing is checked by the admin's eyes only.

What is read is a hint, never the truth: the seller's form is filled with it (and can be corrected), and the
admin sees where it differs from what the seller wrote.
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import date
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

MAX_SCREENSHOTS = 3
WANTED_LANGS = ("rus", "uzb_cyrl", "uzb", "eng")

_MONTHS = {  # Russian (genitive and nominative), Uzbek Cyrillic and English month names -> month number
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # Uzbek Latin
    "yanvar": 1, "fevral": 2, "aprel": 4, "iyun": 6, "iyul": 7, "avgust": 8,
    "sentyabr": 9, "oktyabr": 10, "noyabr": 11, "dekabr": 12,
}
_EN_MONTH_RE = r"(jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)"
# The real card (1Fit app in English): «until January 13, 2027»
_EN_DATE = re.compile(_EN_MONTH_RE + r"\s+(\d{1,2}),?\s+(\d{4})", re.I)
_EN_DATE_DAY_FIRST = re.compile(r"(\d{1,2})\s+" + _EN_MONTH_RE + r",?\s+(\d{4})", re.I)
_MONTH_RE = (r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*"
             r"|декабр\w*|yanvar\w*|fevral\w*|mart\w*|aprel\w*|may\w*|iyun\w*|iyul\w*|avgust\w*|sentyabr\w*"
             r"|oktyabr\w*|noyabr\w*|dekabr\w*)")
_WORD_DATE = re.compile(r"(\d{1,2})[\s-]+" + _MONTH_RE + r",?\s+(\d{4})", re.I)  # «13 января 2027», «13-yanvar, 2027»
_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_END_WORDS = re.compile(r"(до|действ|оконч|истека|гача|тугаш|амал|until|valid|expire|gacha|tugash|amal)", re.I)
# «52 days left», «осталось 52 дня», «52 kun qoldi»: the end date when the date itself was not read
_DAYS_LEFT = re.compile(r"(?<!\d)(\d{1,4})\s*(?:days?\s+left|дн\w*\s+остал|kun\s+qol|кун\s+қол)|остал\w*\s+(\d{1,4})\s*дн",
                        re.I)
_START_WORDS = re.compile(r"(\bс\b|начал|актив|купл|покупк|бошлан|фаол)", re.I)
_PLAN = re.compile(r"(?<!\d)(6|12|24)\s*(месяц\w*|мес\.?|ой\w*|months?|oy\w*)", re.I)
_PLAN_DAYS = re.compile(r"(?<!\d)(180|365|730)\s*(дн\w*|кун\w*|days?|kun\w*)", re.I)  # «for 365 days»
# «Бонусные дни / B 60 дней», «Bonusli kunlar / B 60 kun»: given after the main membership, not in its end date
_BONUS_LINE = re.compile(r"(бонус\w*|bonus\w*)", re.I)
_FREEZE_LINE = re.compile(r"(заморо\w*|музлат\w*|freez\w*|muzlat\w*)", re.I)
_DAYS = re.compile(r"(?<!\d)(\d{1,3})\s*(дн\w*|день|дня|кун\w*|days?|kun\w*)", re.I)
# «100 / 100» of the Attendance tile next to Freezing; Tesseract may read the slash as «|», «I», «l» or «\\»
_FRACTION = re.compile(r"\d+\s*[/|Il\\]\s*\d+")
_DAYS_WORD = re.compile(r"(дн\w*|день|дня|кун\w*|days?|kun\w*)", re.I)
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3})(?![\d.,])")

PLAN_BY_MONTHS = {6: 180, 12: 365, 24: 730}

# The installment contract and its payment schedule are screenshots too, but not of the membership card:
# «Контракт · ПОКУПКА В 1Fit · 6 400 000 сум · Заявка №690984 от 12.08.2026 · График платежей».
# Their dates are the purchase and the payment days — never the membership's start and end
# (listing #21 got «действует до 01.08.2027» from the last payment).
_CONTRACT = re.compile(r"(контракт|заявк\w*\s*№|график\s+платеж|покупка\s+в\s+1\s*fit|платеж\w*\s+по\s+график"
                       r"|shartnoma|to'?lov\w*\s+jadval|шартнома|тўлов\w*\s+жадвал|payment\s+schedule)", re.I)
_MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ \u00a0]\d{3})+|\d{4,9})(?:[.,]\d{2})?\s*(?:сум|сўм|so['ʻ’]?m|sum|uzs)",
                    re.I)
_PURCHASE = re.compile(r"покупк|xarid|харид|purchase", re.I)
_PAID = re.compile(r"(оплачен|погашен|to'?langan|тўланган|paid)", re.I)


def _money(text: str) -> list[int]:
    return [int(re.sub(r"\D", "", m)) for m in _MONEY.findall(text)]


def parse_contract(lines: list[str], today: date) -> dict[str, Any]:
    """The installment contract and its schedule: what was paid for, when, and the payments still due."""
    result: dict[str, Any] = {"installment": True}
    whole = " ".join(lines)
    m = re.search(r"№\s*\d+\s*(?:от|dan|дан)?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", whole, re.I)
    if m and _dates(m.group(1)):
        result["contract_date"] = _dates(m.group(1))[0].isoformat()
    for i, line in enumerate(lines):  # «ПОКУПКА В 1Fit» and the amount on the same or the next line
        if _PURCHASE.search(line):
            amounts = _money(" ".join(lines[i:i + 2]))
            if amounts:
                result["contract_total"] = amounts[0]
                break
    # The schedule: one row per payment, a date and an amount. Rows still to pay are dated after today and
    # not marked as paid.
    due = []
    for line in lines:
        dates, amounts = _dates(line), _money(line)
        if len(dates) == 1 and amounts and dates[0] > today and not _PAID.search(line):
            due.append(amounts[0])
    if due:
        result["payments_left"] = len(due)
        result["monthly_payment"] = max(set(due), key=due.count)
    return result


def _month(word: str) -> int | None:
    w = word.lower()
    if w.startswith("ма"):  # «мая», «май» — but not «март»
        return 3 if w.startswith("март") else 5
    if w.startswith("mar"):  # English «March» / Uzbek «mart», not Russian «ма…»
        return 3
    if w.startswith("may"):
        return 5
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
    triples = [(d, w, y) for d, w, y in _WORD_DATE.findall(line)]
    triples += [(d, w, y) for w, d, y in _EN_DATE.findall(line)]
    triples += [(d, w, y) for d, w, y in _EN_DATE_DAY_FIRST.findall(line)]
    for d, word, y in triples:
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
    if _CONTRACT.search(" ".join(lines)):
        return parse_contract(lines, today)
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
    if "end_date" not in result:
        m = _DAYS_LEFT.search(" ".join(lines))
        if m:
            left = int(m.group(1) or m.group(2))
            if 0 < left <= 1000:
                result["end_date"] = date.fromordinal(today.toordinal() + left).isoformat()
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
        m = _FREEZE_LINE.search(line)
        if not m:
            continue
        # «Freezing» / «18» / «days left» may be on separate lines, and two tiles side by side are read as one
        # row («Attendance > Freezing >», «100 /100 18», «Perfect days left»): look after the word, without
        # the attendance fraction.
        after = _FRACTION.sub(" ", " ".join(lines[i:i + 4])[m.start() + len(m.group()):])
        d = _DAYS.search(after)  # «18 days» written together
        if d:
            n = int(d.group(1))
        else:  # «… 18 / Perfect days left»: the last number before the word «days»
            w = _DAYS_WORD.search(after)
            numbers = [int(x) for x in _NUMBER.findall(after[:w.start()] if w else after) if 0 < int(x) <= 365]
            n = numbers[-1] if numbers else None
        if n and 0 < n <= 365:
            result["freeze_days"] = n
            break

    for i, line in enumerate(lines):
        m = _BONUS_LINE.search(line)
        if not m:
            continue
        # «Bonusli kunlar» has the word «kun» in it: only a number followed by «days» counts, on this line or the next
        d = _DAYS.search(" ".join(lines[i:i + 2])[m.end():])
        if d and 0 < int(d.group(1)) <= 365:  # «0 days»: nothing to add, nothing to compare
            result["bonus_days"] = int(d.group(1))
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
    """Fields where the screenshot and the listing disagree: «end_date», «tariff_days», «freeze_days», «bonus_days»,
    and «installment» when the seller says «paid in full» next to an installment contract."""
    out = []
    if read.get("installment") and listing.get("paid_in_full"):
        out.append("installment")
    for key in ("end_date", "tariff_days", "freeze_days", "bonus_days"):
        if read.get(key) is None or listing.get(key) is None:
            continue
        if key == "end_date":
            if abs((date.fromisoformat(read[key]) - date.fromisoformat(listing[key])).days) > 3:
                out.append(key)
        elif read[key] != listing[key]:
            out.append(key)
    if contract_after_start(read):
        out.append("contract_date")
    if not listing.get("paid_in_full") and read.get("payments_left") and listing.get("payments_left") \
            and read["payments_left"] != listing["payments_left"]:
        out.append("payments_left")
    return out


CONTRACT_SLACK_DAYS = 14  # bought a little before it was activated is normal; months after is another purchase


def contract_after_start(read: dict[str, Any]) -> bool:
    """The contract is dated long after the card's membership began: it belongs to another purchase.
    The card's end minus its tariff is the start or later (freezes only push the end), never earlier,
    so a contract after it cannot be the one this membership was bought with."""
    if not (read.get("contract_date") and read.get("end_date") and read.get("tariff_days")):
        return False
    start = date.fromordinal(date.fromisoformat(read["end_date"]).toordinal() - read["tariff_days"])
    return (date.fromisoformat(read["contract_date"]) - start).days > CONTRACT_SLACK_DAYS


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


def freeze_from_words(words: list[dict[str, Any]]) -> int | None:
    """The freeze days by their place on the card: the number right under the word «Freezing», in its own tile.
    The «100 / 100» of the Attendance tile beside it is to the left and never taken, however it is read."""
    label = next((w for w in words if _FREEZE_LINE.fullmatch(w["text"].strip(" >:.,»«"))), None)
    if not label:
        return None
    lx, ly, lw, lh = label["left"], label["top"], label["width"], max(label["height"], 1)
    best = None
    for w in words:
        t = w["text"].strip(" >:.,")
        if not t.isdigit() or not 0 < int(t) <= 365:
            continue
        cx = w["left"] + w["width"] / 2
        below = w["top"] - (ly + lh)
        if not (0 <= below <= 12 * lh and lx - 0.6 * lw <= cx <= lx + 2.5 * lw):
            continue
        if best is None or below < best[0]:
            best = (below, int(t))
    return best[1] if best else None


def _prepare(image: bytes):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(image))
    img = ImageOps.exif_transpose(img).convert("L")
    if sum(img.getdata()) / (img.width * img.height) < 110:  # dark theme: white text on black
        img = ImageOps.invert(img)
    img = ImageOps.autocontrast(img)
    if img.width < 1400:  # small phone screenshots read better enlarged
        k = 1400 / img.width
        img = img.resize((round(img.width * k), round(img.height * k)))
    return img


def read_words(image: bytes) -> tuple[str, list[dict[str, Any]]] | None:
    """Tesseract's text (in lines) and every word with its box; None when it is not installed or fails.
    Blocking: call it through ``asyncio.to_thread``."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        img = _prepare(image)
        langs = [lang for lang in WANTED_LANGS if lang in set(pytesseract.get_languages(config=""))] or ["eng"]
        data = pytesseract.image_to_data(img, lang="+".join(langs), output_type=pytesseract.Output.DICT)
    except Exception as e:  # noqa: BLE001 — a bad picture must not break selling
        log.warning("screenshot not read: %s", e)
        return None
    words, lines, key = [], [], None
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        words.append({k: data[k][i] for k in ("left", "top", "width", "height")} | {"text": text})
        line_key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if line_key != key:
            lines.append([])
            key = line_key
        lines[-1].append(text)
    return "\n".join(" ".join(ln) for ln in lines), words


def read_card(image: bytes, today: date) -> dict[str, Any]:
    got = read_words(image)
    if not got:
        return {}
    text, words = got
    if os.getenv("OCR_LOG"):  # OCR_LOG=1: see what Tesseract read, to tune the parser
        log.info("screenshot text:\n%s", text)
    result = parse_card(text, today)
    freeze = freeze_from_words(words)  # the place on the card beats the order of the text
    if freeze:
        result["freeze_days"] = freeze
    return result
