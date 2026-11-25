"""«My listings»: statuses, pay the fee, edit, withdraw, mark as sold."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...money import fmt_money
from ...repo import LS, Repo, RepoError
from ..callbacks import AdmCb, MyCb
from ..context import Ctx
from ..middlewares import Btn
from ..notify import Notifier
from ..ui import btn, esc, kb, listing_public, operator_btn, pager, pages_count, seller_contacts, status_label
from . import sell

router = Router(name="my")


async def render_list(ctx: Ctx, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    size = ctx.cfg.page_size
    rows, total = await ctx.repo.seller_listings(ctx.user_id, size, page * size)
    if total == 0:
        return ctx.t("my.title") + "\n\n" + ctx.t("my.empty"), None
    pages = pages_count(total, size)
    items = [
        [btn(f'{ctx.t("my.item", id=r["id"], status=status_label(ctx, r))} · {fmt_money(r["price"])}',
             MyCb(action="open", id=r["id"], page=page))]
        for r in rows
    ]
    return ctx.t("my.title"), kb(*items, pager(ctx, page, pages, lambda p: MyCb(action="list", page=p)))


async def card(ctx: Ctx, listing: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    contacts, _ = await seller_contacts(ctx, listing)
    parts = [
        ctx.t("my.status_line", status=status_label(ctx, listing)),
        listing_public(ctx, listing, listing["id"], installments=True),
        contacts,
    ]
    if Repo.fee_due(listing):
        parts.append(ctx.t("my.unpaid", fee=fmt_money(listing["fee_amount"])))
    if listing["promo_code"]:
        parts.append(ctx.t("my.promo_code", code=esc(listing["promo_code"])))
    if listing["status"] == LS.REJECTED and listing["reject_reason"]:
        parts.append(ctx.t("my.reason", reason=esc(listing["reject_reason"])))

    lid, st = listing["id"], listing["status"]
    rows = []
    if st == LS.UNPAID and Repo.fee_due(listing) and ctx.cfg.can_take_fee:
        rows.append([btn(ctx.t("my.pay_btn", fee=fmt_money(listing["fee_amount"])), MyCb(action="pay", id=lid, page=page))])
    if st in LS.SELLER_EDITABLE:
        rows.append([btn(ctx.t("my.edit_btn"), MyCb(action="edit", id=lid, page=page))])
    if st in LS.SELLER_SELLABLE:
        rows.append([btn(ctx.t("my.sold_btn"), MyCb(action="sold", id=lid, page=page))])
    if st in LS.SELLER_WITHDRAWABLE:
        rows.append([btn(ctx.t("my.withdraw_btn"), MyCb(action="wd", id=lid, page=page))])
    rows.append([operator_btn(ctx)])
    rows.append([btn(ctx.t("common.back"), MyCb(action="list", page=page))])
    return "\n\n".join(parts), kb(*rows)


async def _edit(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, reply_markup=markup)


async def _own(call: CallbackQuery, ctx: Ctx, listing_id: int) -> dict | None:
    listing = await ctx.repo.get_listing(listing_id)
    if not listing or listing["seller_id"] != ctx.user_id:  # never show other people's listings
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return None
    return listing


@router.message(Btn("menu.my"))
async def menu_my(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    text, markup = await render_list(ctx, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(MyCb.filter())
async def on_my(call: CallbackQuery, callback_data: MyCb, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    a, lid, page = callback_data.action, callback_data.id, callback_data.page

    if a == "list":
        await call.answer()
        text, markup = await render_list(ctx, max(0, page))
        await _edit(call, text, markup)
        return

    listing = await _own(call, ctx, lid)
    if listing is None:
        return

    if a == "open":
        await call.answer()
        await _edit(call, *await card(ctx, listing, page))

    elif a == "edit":
        if listing["status"] not in LS.SELLER_EDITABLE:
            await call.answer(ctx.t("sell.edit_blocked"), show_alert=True)
            return
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await sell.start_edit(call.message, state, ctx, listing)

    elif a == "wd":
        await call.answer()
        await _edit(call, ctx.t("my.withdraw_confirm", id=lid), kb(
            [btn(ctx.t("my.withdraw_yes"), MyCb(action="wd!", id=lid, page=page))],
            [btn(ctx.t("common.back"), MyCb(action="open", id=lid, page=page))],
        ))

    elif a == "wd!":
        try:
            await ctx.repo.withdraw_by_seller(lid, ctx.user_id)
        except RepoError:
            await call.answer(ctx.t("my.cant_withdraw"), show_alert=True)
            return
        await call.answer()
        was_unpaid = listing["status"] == LS.UNPAID
        listing = await ctx.repo.get_listing(lid)
        text, markup = await card(ctx, listing, page)
        await _edit(call, ctx.t("my.withdrawn", id=lid) + "\n\n" + text, markup)
        if not was_unpaid:  # the admins never saw an unpaid one
            await notifier.admins(ctx, lambda adm: (
                adm.t("admin.n.withdrawn", id=lid),
                kb([btn(adm.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
            ))

    elif a == "sold":
        await call.answer()
        await _edit(call, ctx.t("my.sold_confirm", id=lid), kb(
            [btn(ctx.t("my.sold_yes"), MyCb(action="sold!", id=lid, page=page))],
            [btn(ctx.t("common.back"), MyCb(action="open", id=lid, page=page))],
        ))

    elif a == "sold!":
        try:
            await ctx.repo.mark_sold(lid, ctx.user_id)
        except RepoError:
            await call.answer(ctx.t("my.cant_sold"), show_alert=True)
            return
        await call.answer()
        counts = await ctx.repo.contact_counts(lid)
        await notifier.admins(ctx, lambda adm: (
            adm.t("admin.n.sold", id=lid, **counts),
            kb([btn(adm.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
        ))
        await _edit(call, ctx.t("my.sold_done", id=lid), kb(
            [btn(ctx.t("common.back"), MyCb(action="list", page=page))]
        ))
    else:
        await call.answer(ctx.t("common.stale"), show_alert=True)

