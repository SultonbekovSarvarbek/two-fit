"""How much of a membership is already used: «8 of 12 months», shown when the seller gave the start date."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

DAYS_PER_MONTH = 30.4375
MONTHS_FROM_DAYS = 60  # shorter memberships are counted in days


def remaining(end_iso: str, today: date) -> dict[str, Any]:
    """What the buyer actually gets: «7 months» or «45 days» from today to the end date.
    It replaces the tariff name: «12 months» made buyers think they got all 12."""
    days = max((date.fromisoformat(end_iso) - today).days, 0)
    if days >= MONTHS_FROM_DAYS:
        return {"n": max(1, round(days / DAYS_PER_MONTH)), "unit": "m"}
    return {"n": days, "unit": "d"}


def payments_guess(end_iso: str, today: date, most: int = 60) -> int:
    """How many monthly installments are probably left: one per month until the end date.
    Only a suggestion — freezes move the end date and a bank plan may be shorter."""
    days = (date.fromisoformat(end_iso) - today).days
    return min(most, max(1, round(days / DAYS_PER_MONTH)))


def usage(start_iso: str | None, end_iso: str, today: date, plan_days: int | None = None) -> dict[str, Any] | None:
    """With no start date, the tariff gives it: a 12-month membership ending in May started a year before."""
    if not start_iso and plan_days:
        start_iso = (date.fromisoformat(end_iso) - timedelta(days=plan_days)).isoformat()
    if not start_iso:
        return None
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    total_days = (end - start).days
    if total_days <= 0:
        return None
    used_days = min(max((today - start).days, 0), total_days)
    if total_days >= MONTHS_FROM_DAYS:
        unit = "m"
        total = max(1, round(total_days / DAYS_PER_MONTH))
        # «left» is rounded the way ``remaining`` rounds it, so «Осталось 10 мес.» above never meets «осталось 9»
        left = round(max((end - max(today, start)).days, 0) / DAYS_PER_MONTH)
        used = min(total, max(0, total - left))
    else:
        unit, total, used = "d", total_days, used_days
    return {"used": used, "total": total, "unit": unit, "pct": round(used_days * 100 / total_days)}
