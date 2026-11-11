"""Commission math and money formatting.

All amounts are whole sums (UZS) stored as integers.
"""
from __future__ import annotations

from dataclasses import dataclass

NBSP = " "


@dataclass(frozen=True)
class Commission:
    price: int
    percent: int
    commission: int
    net: int  # what the seller keeps


def calc_commission(price: int, percent: int) -> Commission:
    """Commission is taken from the actual sale price, rounded half up to 1 sum."""
    if price <= 0:
        raise ValueError("price must be positive")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be 0..100")
    commission = (price * percent + 50) // 100
    return Commission(price=price, percent=percent, commission=commission, net=price - commission)


def fmt_money(amount: int) -> str:
    """2000000 -> '2 000 000' (non-breaking spaces so it never wraps)."""
    return f"{amount:,}".replace(",", NBSP)
