"""The listing fee through Telegram Payments (Click or Payme as the provider).

The bot sends an invoice (in the chat) or makes an invoice link (for the Mini App). Telegram asks the bot
to confirm before it takes the money (pre-checkout), then reports the payment as a message — both are
handled in ``handlers/pay.py``. The payload names the listing: ``fee:<listing id>``.
"""
from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import LabeledPrice

from ..money import fmt_money
from .context import Ctx

CURRENCY = "UZS"
SUBUNITS = 100  # Telegram counts UZS in tiyin: 30 000 сум is 3 000 000
PREFIX = "fee:"


def payload(listing_id: int) -> str:
    return f"{PREFIX}{listing_id}"


def parse_payload(value: str | None) -> int | None:
    if not value or not value.startswith(PREFIX):
        return None
    try:
        return int(value[len(PREFIX):])
    except ValueError:
        return None


def to_subunits(amount: int) -> int:
    return amount * SUBUNITS


def from_subunits(amount: int) -> int:
    return amount // SUBUNITS


def invoice_args(ctx: Ctx, listing: dict[str, Any]) -> dict[str, Any]:
    fee = listing["fee_amount"]
    return {
        "title": ctx.t("pay.title", id=listing["id"]),
        "description": ctx.t("pay.description", id=listing["id"], fee=fmt_money(fee)),
        "payload": payload(listing["id"]),
        "provider_token": ctx.cfg.payment_token,
        "currency": CURRENCY,
        "prices": [LabeledPrice(label=ctx.t("pay.label"), amount=to_subunits(fee))],
    }


async def send_fee_invoice(bot: Bot, ctx: Ctx, chat_id: int, listing: dict[str, Any]) -> None:
    await bot.send_invoice(chat_id=chat_id, **invoice_args(ctx, listing))


async def fee_invoice_link(bot: Bot, ctx: Ctx, listing: dict[str, Any]) -> str:
    """For the Mini App: Telegram.WebApp.openInvoice(link) shows the same payment form."""
    return await bot.create_invoice_link(**invoice_args(ctx, listing))
