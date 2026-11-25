"""Paying the listing fee: the check before Telegram takes the money, and the payment itself."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from ...money import fmt_money
from ...repo import RepoError
from ..callbacks import MyCb
from ..context import Ctx
from ..notify import Notifier
from ..payments import CURRENCY, announce_paid, from_subunits, parse_payload, send_fee_invoice
from ..ui import main_menu

log = logging.getLogger(__name__)

router = Router(name="pay")


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, ctx: Ctx) -> None:
    """Telegram waits up to 10 seconds for this answer; «no» means the seller is not charged."""
    listing_id = parse_payload(query.invoice_payload)
    problem = "not_found" if listing_id is None or query.currency != CURRENCY else \
        await ctx.repo.fee_problem(listing_id, ctx.user_id, from_subunits(query.total_amount))
    if problem:
        await query.answer(ok=False, error_message=ctx.t(f"pay.err.{problem}"))
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, ctx: Ctx, notifier: Notifier) -> None:
    sp = message.successful_payment
    listing_id = parse_payload(sp.invoice_payload)
    if listing_id is None:
        log.error("payment with an unknown payload: %s (charge %s)", sp.invoice_payload, sp.telegram_payment_charge_id)
        return
    try:
        listing, first = await ctx.repo.pay_fee(
            listing_id, ctx.user_id, from_subunits(sp.total_amount), sp.currency,
            sp.telegram_payment_charge_id, sp.provider_payment_charge_id,
        )
    except RepoError:
        log.error("payment for a missing listing %s (charge %s)", listing_id, sp.telegram_payment_charge_id)
        await message.answer(ctx.t("pay.err.lost"), reply_markup=main_menu(ctx))
        return
    if not first:
        return  # the same payment reported again
    await message.answer(ctx.t("pay.done", id=listing_id, fee=fmt_money(from_subunits(sp.total_amount))),
                         reply_markup=main_menu(ctx))
    await announce_paid(notifier, ctx, listing, first, from_subunits(sp.total_amount), tell_seller=False)


@router.callback_query(MyCb.filter(F.action == "pay"))
async def on_pay_again(call: CallbackQuery, callback_data: MyCb, ctx: Ctx) -> None:
    """«Pay» on an unpaid listing in «My listings»: a fresh invoice (the old one may be lost or expired)."""
    listing = await ctx.repo.get_listing(callback_data.id)
    problem = await ctx.repo.fee_problem(callback_data.id, ctx.user_id)
    if problem or not ctx.cfg.can_take_fee:
        await call.answer(ctx.t(f"pay.err.{problem or 'off'}"), show_alert=True)
        return
    await call.answer()
    await send_fee_invoice(call.bot, ctx, ctx.user_id, listing)
