"""Input parsing and validation for the user-facing wizards."""
from __future__ import annotations

import re
from datetime import date

PRICE_MIN = 10_000
PRICE_MAX = 100_000_000
PAYMENTS_LEFT_MAX = 60  # monthly installment payments still to make
# 1Fit's standard tariffs in Tashkent, in days: 6, 12 and 24 months. The seller picks one instead of typing a name.
# Only a 12- or 24-month membership can be re-registered to another person in 1Fit: 6 months cannot be sold.
TARIFF_PLANS = (365, 730)
PLAN_FREEZE_DAYS = 90  # freezes push the end date past the tariff length; more than this is a mistake
DESC_MAX = 500
TARIFF_MIN, TARIFF_MAX = 2, 60
NAME_MIN, NAME_MAX = 2, 60
MAX_YEARS_AHEAD = 3
MAX_YEARS_BACK = 5


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


def _parse_dmy(text: str | None) -> date:
    m = _DATE_RE.match(text or "")
    if not m:
        raise ValidationError("bad_date_format")
    day, month, year = (int(x) for x in m.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        raise ValidationError("bad_date_format") from None


def parse_end_date(text: str | None, today: date) -> date:
    value = _parse_dmy(text)
    if value < today:
        raise ValidationError("bad_date_past")
    if value.year > today.year + MAX_YEARS_AHEAD:
        raise ValidationError("bad_date_far")
    return value


def parse_start_date(text: str | None, today: date, end: date) -> date:
    """When the membership was activated: optional, lets buyers see how much of it is used."""
    value = _parse_dmy(text)
    if value > today:
        raise ValidationError("bad_start_future")
    if value >= end:
        raise ValidationError("bad_start_order")
    if value.year < today.year - MAX_YEARS_BACK:
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


PLAN_SHORT_DAYS = 15  # the start date may be a little after the purchase: a slightly shorter span is fine


def check_plan_span(start: date | None, end: date, plan_days: int | None) -> None:
    """From the start date to the end date there must be about as long as the tariff: a 12-month tariff
    from 01.10.2026 cannot end on 31.12.2027 (15 months). Freezes may add up to PLAN_FREEZE_DAYS."""
    if not (start and plan_days):
        return
    span = (end - start).days
    if not plan_days - PLAN_SHORT_DAYS <= span <= plan_days + PLAN_FREEZE_DAYS:
        raise ValidationError("bad_dates_for_plan")


def check_plan_end(end: date, today: date, plan_days: int | None) -> None:
    """A 12-month tariff cannot have 20 months left: either the date or the tariff is wrong."""
    if plan_days and (end - today).days > plan_days + PLAN_FREEZE_DAYS:
        raise ValidationError("bad_end_for_plan")


FREEZE_DAYS_MAX = 365


def parse_freeze_days(text: str | None) -> int:
    """Unused freeze days the buyer gets with the membership: «20» or «20 дней»."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*[^\d]*", text or "")
    if not m or not 1 <= int(m.group(1)) <= FREEZE_DAYS_MAX:
        raise ValidationError("bad_freeze_days")
    return int(m.group(1))


def parse_bonus_days(text: str | None) -> int:
    """1Fit bonus days, turned on after the main membership: «60» or «60 дней»; 0 = none."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*[^\d]*", text or "")
    if not m or not 0 <= int(m.group(1)) <= FREEZE_DAYS_MAX:
        raise ValidationError("bad_bonus_days")
    return int(m.group(1))


def parse_monthly(text: str | None) -> int:
    """Installment: how much the seller pays 1Fit every month; the buyer takes these payments over."""
    try:
        return parse_price(text)
    except ValidationError:
        raise ValidationError("bad_monthly") from None


def parse_payments_left(text: str | None) -> int:
    """Installment: how many monthly payments are still due, «9» or «9 месяцев»."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*[^\d]*", text or "")
    if not m or not 1 <= int(m.group(1)) <= PAYMENTS_LEFT_MAX:
        raise ValidationError("bad_payments_left")
    return int(m.group(1))


PROMO_CODE_MAX = 40
_PROMO_CODE = re.compile(r"[0-9A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ_\-]{3,%d}" % PROMO_CODE_MAX)


def parse_promo_code(text: str | None) -> str:
    """The 1Fit promo code being sold: one word of letters, digits, «-» and «_»."""
    value = (text or "").strip()
    if not _PROMO_CODE.fullmatch(value):
        raise ValidationError("bad_promo_code")
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
