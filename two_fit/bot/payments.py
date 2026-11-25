"""The listing fee: Payme directly (Merchant API, ``two_fit/payme.py``) when it is set up,
otherwise Telegram Payments (Click or Payme as the provider).

With Payme the seller gets a button to Payme's checkout page; Payme then calls our server and
``announce_paid`` tells the seller and the admins. With Telegram Payments:

The bot sends an invoice (in the chat) or makes an invoice link (for the Mini App). Telegram asks the bot
to confirm before it takes the money (pre-checkout), then reports the payment as a message — both are
handled in ``handlers/pay.py``. The payload names the listing: ``fee:<listing id>``.
"""
from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import LabeledPrice

from .. import payme
from ..money import fmt_money
from ..repo import LS
from .callbacks import AdmCb
from .context import Ctx
from .notify import Notifier
from .ui import btn, kb, main_menu

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


async def bot_link(bot: Bot) -> str | None:
    """Where Payme's page sends the seller back: the bot's chat."""
    try:
        username = (await bot.me()).username
    except Exception:  # noqa: BLE001 — without it Payme simply shows its own «done» page
        return None
    return f"https://t.me/{username}" if username else None


async def payme_url(bot: Bot, ctx: Ctx, listing: dict[str, Any]) -> str:
    return payme.checkout_url(ctx.cfg, listing["id"], listing["fee_amount"], ctx.lang, await bot_link(bot))


async def send_fee_invoice(bot: Bot, ctx: Ctx, chat_id: int, listing: dict[str, Any]) -> None:
    if ctx.cfg.fee_manual:  # by hand: the seller writes to the operator, an admin confirms the money
        await bot.send_message(
            chat_id, ctx.t("pay.manual_text", id=listing["id"], fee=fmt_money(listing["fee_amount"]),
                           operator=ctx.cfg.operator),
            reply_markup=kb([btn(ctx.t("pay.manual_btn"), url=ctx.cfg.support_url)]))
        return
    if ctx.cfg.payme_enabled:
        await bot.send_message(
            chat_id, ctx.t("pay.payme_text", id=listing["id"], fee=fmt_money(listing["fee_amount"])),
            reply_markup=kb([btn(ctx.t("pay.payme_btn", fee=fmt_money(listing["fee_amount"])),
                                 url=await payme_url(bot, ctx, listing))]))
        return
    await bot.send_invoice(chat_id=chat_id, **invoice_args(ctx, listing))


async def fee_invoice_link(bot: Bot, ctx: Ctx, listing: dict[str, Any]) -> str:
    """For the Mini App: a Telegram invoice link (openInvoice) or Payme's checkout page (openLink)."""
    if ctx.cfg.payme_enabled:
        return await payme_url(bot, ctx, listing)
    return await bot.create_invoice_link(**invoice_args(ctx, listing))


async def announce_paid(notifier: Notifier, ctx: Ctx, listing: dict[str, Any], first: bool, amount: int,
                        tell_seller: bool = True) -> None:
    """The fee came in: the seller hears it went to moderation, the admins get it to check."""
    if not first:
        return  # the same payment reported again
    lid = listing["id"]
    if tell_seller:
        await notifier.user(ctx, listing["seller_id"], lambda c: (
            c.t("pay.done", id=lid, fee=fmt_money(amount)), main_menu(c)))
    key = "admin.n.new_listing" if listing["status"] == LS.PENDING else "admin.n.paid_not_pending"
    await notifier.admins(ctx, lambda a: (
        a.t(key, id=lid) + "\n" + a.t("admin.n.fee_paid", fee=fmt_money(amount)),
        kb([btn(a.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
    ))


async def announce_awaiting(notifier: Notifier, ctx: Ctx, listing: dict[str, Any]) -> None:
    """Paid by hand: the admins hear a listing waits for its fee and confirm it once the money is in."""
    lid, fee = listing["id"], fmt_money(listing["fee_amount"])
    await notifier.admins(ctx, lambda a: (
        a.t("admin.n.awaiting_fee", id=lid, fee=fee),
        kb([btn(a.t("admin.fee_received", fee=fee), AdmCb(action="fee", id=lid))],
           [btn(a.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
    ))


async def announce_refunded(notifier: Notifier, ctx: Ctx, listing: dict[str, Any], amount: int) -> None:
    """Payme returned the money: the listing waits for its fee again."""
    lid = listing["id"]
    await notifier.user(ctx, listing["seller_id"], lambda c: (c.t("pay.refunded", id=lid, fee=fmt_money(amount)), None))
    await notifier.admins(ctx, lambda a: (
        a.t("admin.n.fee_refunded", id=lid, fee=fmt_money(amount)),
        kb([btn(a.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
    ))
