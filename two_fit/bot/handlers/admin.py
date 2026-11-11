"""Closed admin panel. Every handler here is behind IsAdmin: other users never reach it
(their /admin falls through to «unknown command», their stray admin buttons to «stale button»)."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...money import fmt_money
from ...repo import CS, LS, RS, RepoError
from ...validators import ValidationError, clean_text, parse_price
from ..callbacks import AdmCb, PriceCb
from ..context import Ctx
from ..middlewares import IsAdmin
from ..notify import Notifier
from ..ui import (
    btn, commission_block, esc, fmt_date, fmt_dt, kb, listing_public, pager, pages_count, status_label, tg_ref,
)

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    reason = State()
    final_price = State()
    find = State()


def A(action: str, id: int = 0, page: int = 0, extra: str = "") -> AdmCb:  # noqa: N802
    return AdmCb(action=action, id=id, page=page, extra=extra)


# ------------------------------------------------------------------ rendering
async def render_home(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    pending = await ctx.repo.count_status(LS.PENDING)
    _, active_req = await ctx.repo.active_requests(1, 0)
    return ctx.t("admin.title"), kb(
        [btn(ctx.t("admin.btn.moderation", n=pending), A("lst", extra=LS.PENDING))],
        [btn(ctx.t("admin.btn.listings"), A("st"))],
        [btn(ctx.t("admin.btn.requests", n=active_req), A("areqs"))],
        [btn(ctx.t("admin.btn.deals"), A("deals"))],
        [btn(ctx.t("admin.btn.stats"), A("stats")), btn(ctx.t("admin.btn.find"), A("find"))],
    )


def render_status_menu(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[btn(ctx.t(f"status.{s}"), A("lst", extra=s))] for s in LS.ALL]
    rows.append([btn(ctx.t("admin.btn.all"), A("lst", extra="all"))])
    rows.append([btn(ctx.t("admin.btn.home"), A("home"))])
    return ctx.t("admin.choose_status"), kb(*rows)


async def render_list(ctx: Ctx, status: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    size = 8
    rows, total = await ctx.repo.listings_by_status(None if status == "all" else status, size, page * size)
    title = ctx.t("admin.btn.all") if status == "all" else ctx.t(f"status.{status}")
    items = [
        [btn(ctx.t("admin.list_item", id=r["id"], status=status_label(ctx, r), price=fmt_money(r["price"])),
             A("open", id=r["id"], extra=status))]
        for r in rows
    ]
    text = ctx.t("admin.list_title", status=title, count=total)
    if not total:
        text += "\n\n" + ctx.t("admin.empty")
    return text, kb(
        *items,
        pager(ctx, page, pages_count(total, size), lambda p: A("lst", page=p, extra=status)),
        [btn(ctx.t("admin.back"), A("st")), btn(ctx.t("admin.btn.home"), A("home"))],
    )


async def render_card(ctx: Ctx, listing: dict, back: str = "") -> tuple[str, InlineKeyboardMarkup]:
    seller = await ctx.repo.get_user(listing["seller_id"])
    requests = await ctx.repo.listing_requests(listing["id"])
    open_requests = [r for r in requests if r["status"] in RS.ACTIVE]
    text = ctx.t(
        "admin.card",
        status=status_label(ctx, listing),
        public=listing_public(ctx, listing, listing["id"]),
        name=esc(listing["seller_name"]),
        phone=esc(listing["seller_phone"]),
        tg=tg_ref(seller, listing["seller_id"]),
        lang=listing["lang"],
        created=fmt_dt(listing["created_at"]),
    )
    extra = [ctx.t("admin.card.accepted", price=fmt_money(listing["commission_accepted_price"]))]
    if listing["status"] == LS.APPROVED and ctx.repo.is_expired(listing):
        extra.append(ctx.t("admin.card.expired"))
    if listing["reject_reason"] and listing["status"] == LS.REJECTED:
        extra.append(ctx.t("admin.card.reason", reason=esc(listing["reject_reason"])))
    if listing["sale_reported_at"]:
        extra.append(ctx.t("admin.card.sale_reported", date=fmt_dt(listing["sale_reported_at"])))
    if listing["status"] == LS.RESERVED and listing["reserved_request_id"]:
        req = await ctx.repo.get_request(listing["reserved_request_id"])
        buyer = await ctx.repo.get_user(req["buyer_id"])
        extra.append(ctx.t("admin.card.reserved_for", rid=req["id"], name=esc(req["buyer_name"]),
                           phone=esc(req["buyer_phone"]), tg=tg_ref(buyer, req["buyer_id"])))
        if listing["final_price"]:
            state = ctx.t("admin.card.final_ok" if listing["final_price_confirmed"] else "admin.card.final_wait")
            extra.append(ctx.t("admin.card.final_price", price=fmt_money(listing["final_price"]), state=state))
    extra.append(ctx.t("admin.card.requests", n=len(requests)))
    text += "\n\n" + "\n".join(extra)

    lid, st = listing["id"], listing["status"]
    rows = []
    if st == LS.PENDING:
        rows.append([btn(ctx.t("admin.approve"), A("appr", id=lid))])
        if listing["description"]:
            rows.append([btn(ctx.t("admin.approve_nodesc"), A("apprnd", id=lid))])
        rows.append([btn(ctx.t("admin.reject"), A("rej", id=lid))])
    if st == LS.RESERVED:
        rows.append([btn(ctx.t("admin.final_price_btn"), A("fp", id=lid))])
        rows.append([btn(ctx.t("admin.close_btn"), A("close", id=lid))])
        rows.append([btn(ctx.t("admin.release_btn"), A("rel", id=lid))])
    if requests:
        rows.append([btn(ctx.t("admin.requests_btn", n=len(open_requests)), A("reqs", id=lid))])
    if st == LS.SOLD:
        rows.append([btn(ctx.t("admin.deal_btn"), A("dealL", id=lid))])
    if listing["screenshot_file_id"]:
        rows.append([btn(ctx.t("admin.screenshot_btn"), A("shot", id=lid))])
    if st in LS.ADMIN_REMOVABLE:
        rows.append([btn(ctx.t("admin.remove"), A("rm", id=lid))])
    rows.append([btn(ctx.t("admin.back"), A("lst", extra=back or st)), btn(ctx.t("admin.btn.home"), A("home"))])
    return text, kb(*rows)


async def render_requests(ctx: Ctx, listing: dict) -> tuple[str, InlineKeyboardMarkup]:
    requests = await ctx.repo.listing_requests(listing["id"])
    blocks = [ctx.t("admin.requests_title", id=listing["id"])]
    rows = []
    for r in requests:
        buyer = await ctx.repo.get_user(r["buyer_id"])
        blocks.append(ctx.t("admin.request_item", rid=r["id"], status=ctx.t(f"rstatus.{r['status']}"),
                            date=fmt_dt(r["created_at"]), name=esc(r["buyer_name"]), phone=esc(r["buyer_phone"]),
                            tg=tg_ref(buyer, r["buyer_id"])))
        if r["status"] == RS.NEW and listing["status"] == LS.APPROVED and not ctx.repo.is_expired(listing):
            rows.append([btn(ctx.t("admin.select_btn", rid=r["id"]), A("sel", id=r["id"]))])
    if not requests:
        blocks.append(ctx.t("admin.empty"))
    rows.append([btn(ctx.t("admin.back"), A("open", id=listing["id"])), btn(ctx.t("admin.btn.home"), A("home"))])
    return "\n\n".join(blocks), kb(*rows)


async def render_active_requests(ctx: Ctx, page: int) -> tuple[str, InlineKeyboardMarkup]:
    size = 8
    rows, total = await ctx.repo.active_requests(size, page * size)
    items = [
        [btn(ctx.t("admin.req_list_item", rid=r["id"], id=r["listing_id"], name=r["buyer_name"],
                   status=ctx.t(f"rstatus.{r['status']}")), A("reqs", id=r["listing_id"]))]
        for r in rows
    ]
    text = ctx.t("admin.all_requests_title", count=total)
    if not total:
        text += "\n\n" + ctx.t("admin.empty")
    return text, kb(*items, pager(ctx, page, pages_count(total, size), lambda p: A("areqs", page=p)),
                    [btn(ctx.t("admin.btn.home"), A("home"))])


def _cstatus(ctx: Ctx, deal: dict) -> str:
    return ctx.t(f"cstatus.{deal['commission_status']}")


async def render_deals(ctx: Ctx, page: int) -> tuple[str, InlineKeyboardMarkup]:
    size = 8
    rows, total = await ctx.repo.deals(size, page * size)
    items = [
        [btn(ctx.t("admin.deal_item", deal=d["id"], id=d["listing_id"], price=fmt_money(d["sale_price"]),
                   cstatus=_cstatus(ctx, d)), A("deal", id=d["id"]))]
        for d in rows
    ]
    text = ctx.t("admin.deals_title", count=total)
    if not total:
        text += "\n\n" + ctx.t("admin.empty")
    return text, kb(*items, pager(ctx, page, pages_count(total, size), lambda p: A("deals", page=p)),
                    [btn(ctx.t("admin.btn.stats"), A("stats")), btn(ctx.t("admin.btn.home"), A("home"))])


async def render_deal(ctx: Ctx, deal: dict) -> tuple[str, InlineKeyboardMarkup]:
    seller = await ctx.repo.get_user(deal["seller_id"])
    buyer = await ctx.repo.get_user(deal["buyer_id"]) if deal["buyer_id"] else None
    seller_s = f'{esc(deal["seller_name"])}, {esc(deal["seller_phone"])}, {tg_ref(seller, deal["seller_id"])}'
    buyer_s = (f'{esc(deal["buyer_name"])}, {esc(deal["buyer_phone"])}, {tg_ref(buyer, deal["buyer_id"])}'
               if deal["buyer_id"] else "—")
    text = ctx.t(
        "admin.deal_card", deal=deal["id"], id=deal["listing_id"], seller=seller_s, buyer=buyer_s,
        price=fmt_money(deal["sale_price"]), percent=deal["commission_percent"],
        commission=fmt_money(deal["commission_amount"]), net=fmt_money(deal["seller_amount"]),
        closed=fmt_dt(deal["closed_at"]), cstatus=_cstatus(ctx, deal),
    )
    if deal["commission_paid_at"]:
        text += ctx.t("admin.paid_at", date=fmt_dt(deal["commission_paid_at"]))
    rows = []
    if deal["commission_status"] == CS.PENDING:
        rows.append([btn(ctx.t("admin.mark_paid_btn"), A("paid", id=deal["id"]))])
    rows.append([btn(ctx.t("admin.open_btn", id=deal["listing_id"]), A("open", id=deal["listing_id"]))])
    rows.append([btn(ctx.t("admin.back"), A("deals")), btn(ctx.t("admin.btn.home"), A("home"))])
    return text, kb(*rows)


async def render_stats(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    s = await ctx.repo.stats()
    money = {k: fmt_money(s[k]) for k in ("volume", "accrued", "paid", "due")}
    return ctx.t("admin.stats", **{**s, **money}), kb(
        [btn(ctx.t("admin.btn.deals"), A("deals")), btn(ctx.t("admin.btn.home"), A("home"))]
    )


def confirm_kb(ctx: Ctx, yes: AdmCb, no: AdmCb) -> InlineKeyboardMarkup:
    return kb([btn(ctx.t("admin.confirm_yes"), yes)], [btn(ctx.t("admin.back"), no)])


async def _show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, reply_markup=markup)


async def _send(message: Message, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await message.answer(view[0], reply_markup=view[1])


# ------------------------------------------------------------------ entry
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await _send(message, await render_home(ctx))


@router.callback_query(AdmCb.filter())
async def on_admin(call: CallbackQuery, callback_data: AdmCb, state: FSMContext, ctx: Ctx,
                   notifier: Notifier) -> None:
    a, oid, page, extra = callback_data.action, callback_data.id, callback_data.page, callback_data.extra
    repo = ctx.repo
    try:
        if a == "home":
            await state.clear()
            await call.answer()
            await _show(call, *await render_home(ctx))
        elif a == "st":
            await call.answer()
            await _show(call, *render_status_menu(ctx))
        elif a == "lst":
            await call.answer()
            await _show(call, *await render_list(ctx, extra or "all", max(0, page)))
        elif a == "areqs":
            await call.answer()
            await _show(call, *await render_active_requests(ctx, max(0, page)))
        elif a == "deals":
            await call.answer()
            await _show(call, *await render_deals(ctx, max(0, page)))
        elif a == "stats":
            await call.answer()
            await _show(call, *await render_stats(ctx))
        elif a == "find":
            await call.answer()
            await state.set_state(AdminForm.find)
            await call.message.answer(ctx.t("admin.ask_find"), reply_markup=kb([btn(ctx.t("admin.back"), A("home"))]))
        elif a == "deal":
            deal = await repo.get_deal(oid)
            if not deal:
                raise RepoError("not_found")
            await call.answer()
            await _show(call, *await render_deal(ctx, deal))
        elif a == "dealL":
            deal = await repo.deal_for_listing(oid)
            if not deal:
                raise RepoError("not_found")
            await call.answer()
            await _show(call, *await render_deal(ctx, deal))
        elif a == "paid":
            deal = await repo.get_deal(oid)
            if not deal:
                raise RepoError("not_found")
            if deal["commission_status"] != CS.PENDING:
                raise RepoError("already")
            await call.answer()
            await _show(call, ctx.t("admin.confirm_paid", deal=oid, commission=fmt_money(deal["commission_amount"])),
                        confirm_kb(ctx, A("paid!", id=oid), A("deal", id=oid)))
        elif a == "paid!":
            deal = await repo.mark_commission_paid(oid, ctx.user_id)
            await call.answer(ctx.t("admin.paid_ok", deal=oid), show_alert=True)
            await _show(call, *await render_deal(ctx, deal))
        elif a == "sel":
            listing, req = await repo.reserve(oid, ctx.user_id)
            lid = listing["id"]
            await call.answer(ctx.t("admin.reserved_ok", id=lid, rid=oid), show_alert=True)
            await _show(call, *await render_card(ctx, listing))
            await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.reserved_seller", id=lid), None))
            await notifier.user(ctx, req["buyer_id"], lambda u: (u.t("notify.reserved_buyer", id=lid), None))
        else:
            await _listing_action(call, ctx, state, notifier, a, oid, extra)
    except RepoError as e:
        await call.answer(ctx.t(f"admin.err.{e.code}") if e.code in _ERRS else ctx.t("admin.err.bad_status"),
                          show_alert=True)


_ERRS = {"not_found", "bad_status", "already", "price_not_confirmed", "stale"}


async def _listing_action(call: CallbackQuery, ctx: Ctx, state: FSMContext, notifier: Notifier,
                          a: str, lid: int, extra: str) -> None:
    repo = ctx.repo
    listing = await repo.get_listing(lid)
    if not listing:
        raise RepoError("not_found")

    if a == "open":
        await state.clear()
        await call.answer()
        await _show(call, *await render_card(ctx, listing, extra))
    elif a == "reqs":
        await call.answer()
        await _show(call, *await render_requests(ctx, listing))
    elif a == "shot":
        await call.answer()
        await call.message.answer_photo(listing["screenshot_file_id"],
                                        caption=ctx.t("admin.screenshot_caption", id=lid))
    elif a in ("appr", "apprnd"):
        listing = await repo.approve(lid, ctx.user_id, clear_description=(a == "apprnd"))
        await call.answer(ctx.t("admin.approved_ok", id=lid))
        await _show(call, *await render_card(ctx, listing))
        await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.approved", id=lid), None))
    elif a == "rej":
        if listing["status"] != LS.PENDING:
            raise RepoError("bad_status")
        await call.answer()
        await state.set_state(AdminForm.reason)
        await state.update_data(listing_id=lid)
        await call.message.answer(ctx.t("admin.ask_reason", id=lid),
                                  reply_markup=kb([btn(ctx.t("admin.back"), A("open", id=lid))]))
    elif a == "rm":
        if listing["status"] not in LS.ADMIN_REMOVABLE:
            raise RepoError("bad_status")
        await call.answer()
        await _show(call, ctx.t("admin.confirm_remove", id=lid), confirm_kb(ctx, A("rm!", id=lid), A("open", id=lid)))
    elif a == "rm!":
        listing, buyers = await repo.remove(lid, ctx.user_id)
        await call.answer(ctx.t("admin.removed_ok", id=lid))
        await _show(call, *await render_card(ctx, listing))
        await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.removed", id=lid), None))
        await notifier.users(ctx, buyers, lambda u: (u.t("notify.request_closed", id=lid), None))
    elif a == "rel":
        if listing["status"] != LS.RESERVED:
            raise RepoError("bad_status")
        await call.answer()
        await _show(call, ctx.t("admin.confirm_release", id=lid), confirm_kb(ctx, A("rel!", id=lid), A("open", id=lid)))
    elif a == "rel!":
        listing, req = await repo.release_reserve(lid, ctx.user_id)
        await call.answer(ctx.t("admin.released_ok", id=lid))
        await _show(call, *await render_card(ctx, listing))
        await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.released_seller", id=lid), None))
        if req:
            await notifier.user(ctx, req["buyer_id"], lambda u: (u.t("notify.released_buyer", id=lid), None))
    elif a == "fp":
        if listing["status"] != LS.RESERVED:
            raise RepoError("bad_status")
        await call.answer()
        await state.set_state(AdminForm.final_price)
        await state.update_data(listing_id=lid)
        await call.message.answer(
            ctx.t("admin.ask_final_price", id=lid, price=fmt_money(listing["commission_accepted_price"])),
            reply_markup=kb([btn(ctx.t("admin.back"), A("open", id=lid))]),
        )
    elif a == "close":
        if listing["status"] != LS.RESERVED:
            raise RepoError("bad_status")
        if not listing["final_price"] or not listing["final_price_confirmed"]:
            raise RepoError("price_not_confirmed")
        await call.answer()
        await _show(call, ctx.t("admin.confirm_close", id=lid, block=commission_block(ctx, listing["final_price"])),
                    confirm_kb(ctx, A("close!", id=lid), A("open", id=lid)))
    elif a == "close!":
        deal, others = await repo.close_deal(lid, ctx.user_id)
        await call.answer(ctx.t("admin.closed_ok", deal=deal["id"], id=lid,
                                commission=fmt_money(deal["commission_amount"])), show_alert=True)
        await _show(call, *await render_deal(ctx, deal))
        await notifier.user(ctx, deal["seller_id"], lambda u: (
            u.t("notify.deal_closed_seller", id=lid, block=commission_block(u, deal["sale_price"])), None))
        if deal["buyer_id"]:
            await notifier.user(ctx, deal["buyer_id"], lambda u: (u.t("notify.deal_closed_buyer", id=lid), None))
        await notifier.users(ctx, others, lambda u: (u.t("notify.request_closed", id=lid), None))
    else:
        await call.answer(ctx.t("common.stale"), show_alert=True)


# ------------------------------------------------------------------ text inputs
@router.message(AdminForm.reason)
async def on_reason(message: Message, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    reason = clean_text(message.text)[:300]
    if len(reason) < 3:
        await message.answer(ctx.t("admin.ask_reason", id=(await state.get_data()).get("listing_id")))
        return
    lid = (await state.get_data())["listing_id"]
    await state.clear()
    try:
        listing = await ctx.repo.reject(lid, ctx.user_id, reason)
    except RepoError as e:
        await message.answer(ctx.t(f"admin.err.{e.code}" if e.code in _ERRS else "admin.err.bad_status"))
        return
    await message.answer(ctx.t("admin.rejected_ok", id=lid))
    await _send(message, await render_card(ctx, listing))
    await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.rejected", id=lid, reason=esc(reason)), None))


@router.message(AdminForm.final_price)
async def on_final_price(message: Message, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    lid = (await state.get_data())["listing_id"]
    try:
        price = parse_price(message.text)
    except ValidationError:
        await message.answer(ctx.t("err.bad_price"))
        return
    await state.clear()
    try:
        listing, needs_confirm = await ctx.repo.set_final_price(lid, ctx.user_id, price)
    except RepoError as e:
        await message.answer(ctx.t(f"admin.err.{e.code}" if e.code in _ERRS else "admin.err.bad_status"))
        return
    if needs_confirm:
        await message.answer(ctx.t("admin.final_wait", price=fmt_money(price)))
        await notifier.user(ctx, listing["seller_id"], lambda u: (
            u.t("notify.price_confirm", id=lid, block=commission_block(u, price)),
            kb([btn(u.t("notify.price_yes"), PriceCb(id=lid, price=price, ok=1))],
               [btn(u.t("notify.price_no"), PriceCb(id=lid, price=price, ok=0))]),
        ))
    else:
        await message.answer(ctx.t("admin.final_same", price=fmt_money(price)))
    await _send(message, await render_card(ctx, listing))


@router.message(AdminForm.find)
async def on_find(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = (message.text or "").strip().lstrip("№#")
    listing = await ctx.repo.get_listing(int(raw)) if raw.isdigit() else None
    if not listing:
        await message.answer(ctx.t("admin.err.not_found"))
        return
    await state.clear()
    await _send(message, await render_card(ctx, listing))


@router.message(Command("l"))
async def cmd_listing(message: Message, ctx: Ctx) -> None:
    """/l 12 — open listing №12 straight away."""
    parts = (message.text or "").split()
    listing = await ctx.repo.get_listing(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
    if not listing:
        await message.answer(ctx.t("admin.err.not_found"))
        return
    await _send(message, await render_card(ctx, listing))


__all__ = ["router", "fmt_date"]
