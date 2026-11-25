"""The clubs whose memberships are sold here.

1Fit is special: its tariffs, bonus days, installments and the price advice (``pricing.py``) are 1Fit's own,
and the screenshot is read as a 1Fit card. The other clubs only re-register a membership to another person:
the seller gives the end date, the freeze days and names the price.
"""
from __future__ import annotations

FIT = "1fit"

# key -> name as clubs write it; the order is the order of the buttons
CLUBS: dict[str, str] = {
    FIT: "1Fit",
    "ozone": "Ozone Fitness Club",
    "embassy": "Fitness Embassy",
    "ufc": "UFC Gym",
}


def club_name(key: str | None) -> str:
    return CLUBS.get(key or FIT, CLUBS[FIT])


def is_fit(data: dict) -> bool:
    """A listing or a draft of a 1Fit membership (old listings have no club: they are all 1Fit)."""
    return (data.get("club") or FIT) == FIT
