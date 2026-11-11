"""«My listings»: statuses, edit, withdraw, report a sale; seller's answer on a changed price."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...money import fmt_money
from ...repo import LS, RepoError
from ..callbacks import AdmCb, MyCb, PriceCb
from ..context import Ctx
from ..middlewares import Btn
from ..notify import Notifier
from ..ui import btn, commission_block, esc, kb, listing_public, operator_btn, pager, pages_count, status_label
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


def card(ctx: Ctx, listing: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    parts = [
        ctx.t("my.status_line", status=status_label(ctx, listing)),
        listing_public(ctx, listing, listing["id"]),
        commission_block(ctx, listing["price"]),
    ]
    if listing["status"] == LS.REJECTED and listing["reject_reason"]:
        parts.append(ctx.t("my.reason", reason=esc(listing["reject_reason"])))
    if listing["sale_reported_at"] and listing["status"] in (LS.APPROVED, LS.RESERVED):
        parts.append(ctx.t("my.sale_reported"))

    lid, st = listing["id"], listing["status"]
    rows = []
    if st in LS.SELLER_EDITABLE:
        rows.append([btn(ctx.t("my.edit_btn"), MyCb(action="edit", id=lid, page=page))])
    if st in (LS.PENDING, LS.APPROVED):
        rows.append([btn(ctx.t("my.withdraw_btn"), MyCb(action="wd", id=lid, page=page))])
    if st in (LS.APPROVED, LS.RESERVED) and not listing["sale_reported_at"]:
        rows.append([btn(ctx.t("my.report_btn"), MyCb(action="rep", id=lid, page=page))])
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
        await _edit(call, *card(ctx, listing, page))

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
            buyers = await ctx.repo.withdraw_by_seller(lid, ctx.user_id)
        except RepoError:
            await call.answer(ctx.t("my.cant_withdraw"), show_alert=True)
            return
        await call.answer()
        listing = await ctx.repo.get_listing(lid)
        text, markup = card(ctx, listing, page)
        await _edit(call, ctx.t("my.withdrawn", id=lid) + "\n\n" + text, markup)
        await notifier.users(ctx, buyers, lambda u: (u.t("notify.request_closed", id=lid), None))
        await notifier.admins(ctx, lambda adm: (
            adm.t("admin.n.withdrawn", id=lid),
            kb([btn(adm.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
        ))

    elif a == "rep":
        await call.answer()
        await _edit(call, ctx.t("my.report_confirm", id=lid), kb(
            [btn(ctx.t("my.report_yes"), MyCb(action="rep!", id=lid, page=page))],
            [btn(ctx.t("common.back"), MyCb(action="open", id=lid, page=page))],
        ))

    elif a == "rep!":
        try:
            first = await ctx.repo.report_sale(lid, ctx.user_id)
        except RepoError:
            await call.answer(ctx.t("my.cant_report"), show_alert=True)
            return
        await call.answer()
        if not first:
            await _edit(call, ctx.t("my.report_already"), kb([operator_btn(ctx)]))
            return
        await _edit(call, ctx.t("my.report_done", id=lid), kb(
            [operator_btn(ctx)], [btn(ctx.t("common.back"), MyCb(action="open", id=lid, page=page))]
        ))
        await notifier.admins(ctx, lambda adm: (
            adm.t("admin.n.sale_reported", id=lid),
            kb([btn(adm.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
        ))
    else:
        await call.answer(ctx.t("common.stale"), show_alert=True)


@router.callback_query(PriceCb.filter())
async def on_price_answer(call: CallbackQuery, callback_data: PriceCb, ctx: Ctx, notifier: Notifier) -> None:
    lid, price, ok = callback_data.id, callback_data.price, bool(callback_data.ok)
    try:
        await ctx.repo.seller_answer_price(lid, ctx.user_id, price, ok)
    except RepoError:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        ctx.t("notify.price_confirmed" if ok else "notify.price_declined", id=lid), reply_markup=kb([operator_btn(ctx)])
    )
    key = "admin.n.price_confirmed" if ok else "admin.n.price_declined"
    await notifier.admins(ctx, lambda adm: (
        adm.t(key, id=lid, price=fmt_money(price)),
        kb([btn(adm.t("admin.open_btn", id=lid), AdmCb(action="open", id=lid))]),
    ))
