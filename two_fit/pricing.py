"""Price advice for a membership: what the seller should ask so it sells.

A buyer does not compare a listing with «7 890 000», but with what 1Fit would charge him for the same time.
That value is drawn as a line through 1Fit's tariffs — 0 days cost nothing, 180 days cost the 6-month price,
365 days the year, 730 days two years — and read at the days that are left; then a discount is knocked off.

The line never goes down and never jumps, so one day more never costs less (prorating «the cheapest tariff
that covers the days» priced 91 days 1.5 million below 90, and 250 days below 180). 1Fit's 3-month tariff is
left out: per day it costs half as much again as 6 months, and a buyer of 50 days compares with those.

When the membership is paid in installments the buyer keeps paying 1Fit every month after the deal, so those
payments are part of what the membership costs him: they come off the price the seller may ask.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .usage import DAYS_PER_MONTH
from .validators import PRICE_MIN

# 1Fit prices in Tashkent (so'm), tariff length in days -> actual price (with 1Fit's own discount).
# These are only the starting values: the price watcher keeps the current ones in the database.
DEFAULT_FIT_PRICES: dict[int, int] = {90: 4_890_000, 180: 5_890_000, 365: 7_890_000, 730: 12_190_000}

MIN_BASE_DAYS = 180  # shorter 1Fit tariffs (3 months) are not what a buyer compares with: far dearer per day
DISCOUNT_PERCENT = 20  # how much cheaper than 1Fit we advise to sell
MAX_PERCENT = 85       # a seller may not ask more than this share of the 1Fit value: it would not sell
MIN_PERCENT = 40       # below this the seller is warned (a typo or a giveaway)
SUSPICIOUS_PERCENT = 25  # below this it is almost surely the price of one month, not of the whole term
ROUND_TO = 10_000      # advised prices are rounded down to 10 000 so'm


@dataclass(frozen=True)
class Advice:
    days_left: int
    base_days: int       # the 1Fit tariff the buyer would otherwise buy
    base_price: int
    fit_value: int       # what these days cost at 1Fit
    installments: int    # what the buyer still pays 1Fit after the deal
    recommended: int     # advised price for the seller
    max_price: int       # the most the seller may ask (0 = no limit applies)
    min_price: int       # below this the seller is warned
    fit_month: int       # 1Fit, per month of use

    def month_cost(self, price: int) -> int:
        """What the buyer pays per month of use: the price plus the installments he takes over."""
        return round((price + self.installments) / (self.days_left / DAYS_PER_MONTH))

    def total(self, price: int) -> int:
        """What the buyer pays in all for the days left: the price plus the installments he takes over."""
        return price + self.installments

    def saved(self, price: int) -> int:
        """How much the buyer saves in all against buying the same days at 1Fit."""
        return max(0, round(self.fit_value - price - self.installments))

    def saving(self, price: int) -> int:
        """How much cheaper than 1Fit, in percent (may be negative)."""
        return round((1 - self.month_cost(price) / self.fit_month) * 100) if self.fit_month else 0

    def too_high(self, price: int) -> bool:
        return bool(self.max_price) and price > self.max_price

    def too_low(self, price: int) -> bool:
        return price < self.min_price

    def suspicious(self, price: int) -> bool:
        """«485 000 for 9 months» next to 5 800 000 at 1Fit: almost surely the monthly price, not the total.
        Buyers are told to check it with the operator instead of seeing it as a 92% discount."""
        return price + self.installments < self.fit_value * SUSPICIOUS_PERCENT / 100


def _round_down(amount: float) -> int:
    return int(amount // ROUND_TO * ROUND_TO)


def normalize_prices(prices: dict | None) -> dict[int, int]:
    """Keys may come from JSON as strings; anything broken falls back to the defaults."""
    try:
        clean = {int(k): int(v) for k, v in (prices or {}).items() if int(k) > 0 and int(v) > 0}
    except (TypeError, ValueError):
        clean = {}
    return clean or dict(DEFAULT_FIT_PRICES)


def fit_value_of(days: int, prices: dict[int, int]) -> tuple[float, int]:
    """What ``days`` of 1Fit are worth, and the 1Fit tariff they are compared with (the first that covers them).
    Linear between the tariffs (0 → 0, 180 → 5 890 000, 365 → 7 890 000, 730 → 12 190 000); past the longest
    tariff it goes on as the last stretch does."""
    plans = [d for d in sorted(prices) if d >= MIN_BASE_DAYS] or sorted(prices)
    points, top = [(0, 0)], 0
    for d in plans:
        top = max(top, prices[d])  # a longer tariff never counts as cheaper than a shorter one
        points.append((d, top))
    base = next((d for d in plans if d >= days), plans[-1])
    for (d0, p0), (d1, p1) in zip(points, points[1:]):
        if days <= d1:
            return p0 + (p1 - p0) * (days - d0) / (d1 - d0), base
    (d0, p0), (d1, p1) = points[-2], points[-1]
    return p1 + (p1 - p0) * (days - d1) / (d1 - d0), base


def advise(end_iso: str, today: date, fit_prices: dict[int, int] | None = None,
           monthly_payment: int | None = None, payments_left: int | None = None,
           discount: int = DISCOUNT_PERCENT, max_percent: int = MAX_PERCENT,
           plan_days: int | None = None) -> Advice | None:
    """Advice for a membership that ends on ``end_iso``; ``None`` if it has already ended.
    The value of the days left comes from 1Fit's tariffs (``fit_value_of``). ``plan_days`` (the tariff the seller
    bought) does not change it: 147 days left of a 12-month membership are worth 147 days, whatever was bought.
    It is kept for the callers."""
    days_left = (date.fromisoformat(end_iso) - today).days
    if days_left <= 0:
        return None
    prices = normalize_prices(fit_prices)
    fit_value, base_days = fit_value_of(days_left, prices)
    base_price = prices[base_days]
    installments = (monthly_payment or 0) * (payments_left or 0) if monthly_payment and payments_left else 0

    recommended = max(PRICE_MIN, _round_down(fit_value * (100 - discount) / 100 - installments))
    max_price = _round_down(fit_value * max_percent / 100 - installments) if max_percent else 0
    if max_price < recommended:
        # The installments alone already cost about as much as 1Fit: a cap would leave the seller nothing to ask.
        max_price = 0
    min_price = min(recommended, _round_down(fit_value * MIN_PERCENT / 100 - installments))
    return Advice(
        days_left=days_left,
        base_days=base_days,
        base_price=base_price,
        fit_value=round(fit_value),
        installments=installments,
        recommended=recommended,
        max_price=max_price,
        min_price=max(PRICE_MIN, min_price),
        fit_month=round(fit_value / (days_left / DAYS_PER_MONTH)),  # what a month of these days costs at 1Fit
    )
