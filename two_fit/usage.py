"""How much of a membership is already used: «8 of 12 months», shown when the seller gave the start date."""
from __future__ import annotations

from datetime import date
from typing import Any

DAYS_PER_MONTH = 30.4375
MONTHS_FROM_DAYS = 60  # shorter memberships are counted in days


def usage(start_iso: str | None, end_iso: str, today: date) -> dict[str, Any] | None:
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
        used = min(total, round(used_days / DAYS_PER_MONTH))
    else:
        unit, total, used = "d", total_days, used_days
    return {"used": used, "total": total, "unit": unit, "pct": round(used_days * 100 / total_days)}
