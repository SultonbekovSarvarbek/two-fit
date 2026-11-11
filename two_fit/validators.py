"""Input parsing and validation for the user-facing wizards."""
from __future__ import annotations

import re
from datetime import date

PRICE_MIN = 10_000
PRICE_MAX = 100_000_000
DESC_MAX = 500
TARIFF_MIN, TARIFF_MAX = 2, 60
NAME_MIN, NAME_MAX = 2, 60
MAX_YEARS_AHEAD = 3


class ValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def clean_text(text: str | None) -> str:
    return re.sub(r"[ \t]+", " ", (text or "").strip())


def parse_tariff(text: str | None) -> str:
    value = clean_text(text)
    if not TARIFF_MIN <= len(value) <= TARIFF_MAX:
        raise ValidationError("bad_tariff")
    if contains_contacts(value):
        raise ValidationError("bad_contacts")
    return value


def parse_name(text: str | None) -> str:
    value = clean_text(text)
    if not NAME_MIN <= len(value) <= NAME_MAX:
        raise ValidationError("bad_name")
    return value


_DATE_RE = re.compile(r"^\s*(\d{1,2})[./\-\s](\d{1,2})[./\-\s](\d{2}|\d{4})\s*$")


def parse_end_date(text: str | None, today: date) -> date:
    m = _DATE_RE.match(text or "")
    if not m:
        raise ValidationError("bad_date_format")
    day, month, year = (int(x) for x in m.groups())
    if year < 100:
        year += 2000
    try:
        value = date(year, month, day)
    except ValueError:
        raise ValidationError("bad_date_format") from None
    if value < today:
        raise ValidationError("bad_date_past")
    if value.year > today.year + MAX_YEARS_AHEAD:
        raise ValidationError("bad_date_far")
    return value


def parse_price(text: str | None) -> int:
    raw = re.sub(r"[\s .,'’_]", "", (text or "").lower())
    raw = re.sub(r"(сум|сўм|so'm|som|sum|uzs)$", "", raw)
    if not raw.isdigit():
        raise ValidationError("bad_price")
    value = int(raw)
    if not PRICE_MIN <= value <= PRICE_MAX:
        raise ValidationError("bad_price")
    return value


def normalize_phone(text: str | None) -> str:
    """Return '+998901234567'-style phone or raise."""
    raw = (text or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:  # local Uzbek number without country code
        digits = "998" + digits
    if not 9 <= len(digits) <= 15:
        raise ValidationError("bad_phone")
    return "+" + digits


def parse_description(text: str | None) -> str:
    value = (text or "").strip()
    value = re.sub(r"\n{3,}", "\n\n", value)
    if len(value) > DESC_MAX:
        raise ValidationError("bad_desc_long")
    if contains_contacts(value):
        raise ValidationError("bad_contacts")
    return value


_CONTACT_PATTERNS = [
    re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,}"),  # @username
    re.compile(r"(?i)\b(?:t\.me|telegram\.me|wa\.me|instagram\.com)\b"),
    re.compile(r"(?i)\bhttps?://|\bwww\."),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # e-mail
]
# 9+ digits possibly separated by spaces, dashes, brackets -> looks like a phone number
_PHONE_LIKE = re.compile(r"\+?\d(?:[\s\-()]*\d){8,}")


def contains_contacts(text: str) -> bool:
    if any(p.search(text) for p in _CONTACT_PATTERNS):
        return True
    for m in _PHONE_LIKE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        # Prices like "1 500 000" have <= 9 digits and no leading +/998/0;
        # treat as phone if it has a country code or 9+ digits starting like a mobile.
        if m.group().startswith("+") or digits.startswith("998") or len(digits) >= 10:
            return True
        if len(digits) == 9 and not re.fullmatch(r"\d{1,3}(?:[  ]\d{3})+", m.group().strip()):
            return True
    return False
