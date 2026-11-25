"""Closed admin panel. Every handler here is behind IsAdmin: other users never reach it
(their /admin falls through to «unknown command», their stray admin buttons to «stale button»)."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message

from ...money import fmt_money
from ... import ocr
from ...clubs import is_fit
from ...repo import LS, Repo, RepoError
from ...usage import card_end
from ...validators import clean_text
from ..callbacks import AdmCb, FitPriceCb
from ..context import Ctx
from ..middlewares import IsAdmin
from ..notify import Notifier
from ..payments import announce_paid
from ..ui import (
    btn, esc, fmt_date, fmt_dt, kb, listing_public, pager, pages_count, status_label, tg_ref,
)

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    reason = State()
    find = State()


def A(action: str, id: int = 0, page: int = 0, extra: str = "") -> AdmCb:  # noqa: N802
    return AdmCb(action=action, id=id, page=page, extra=extra)


# ------------------------------------------------------------------ rendering
CONTACT_ICONS = (("calls", "📞"), ("telegram", "✈️"), ("copies", "📋"))


def buyer_link(row: dict) -> str:
    """The buyer's name as a link that opens their Telegram chat (by id, so it works without a username)."""
    name = esc(row["contact_name"] or row["tg_name"] or str(row["user_id"]))
    ref = f' @{esc(row["username"])}' if row["username"] else ""
    return f'<a href="tg://user?id={row["user_id"]}">{name}</a>{ref}'


async def render_contacts(ctx: Ctx, listing: dict, back: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Who reached the seller of this listing: ask them whether they bought it."""
    rows = await ctx.repo.contact_buyers(listing["id"])
    lines = [ctx.t("admin.contacts.title", id=listing["id"], n=len(rows))]
    for r in rows:
        how = " ".join(f"{icon}{r[key] if r[key] > 1 else ''}" for key, icon in CONTACT_ICONS if r[key])
        line = f"• {buyer_link(r)} — {how or ctx.t('admin.contacts.viewed')} · {fmt_dt(r['last_at'])}"
        if r["contact_phone"]:
            line += f" · {esc(r['contact_phone'])}"
        lines.append(line)
    lines.append(ctx.t("admin.contacts.legend"))
    lid = listing["id"]
    return "\n".join(lines), kb([btn(ctx.t("admin.back"), A("open", id=lid, extra=back))])


async def render_home(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    pending = await ctx.repo.count_status(LS.PENDING)
    return ctx.t("admin.title"), kb(
        [btn(ctx.t("admin.btn.moderation", n=pending), A("lst", extra=LS.PENDING))],
        [btn(ctx.t("admin.btn.listings"), A("st"))],
        [btn(ctx.t("admin.btn.stats"), A("stats")), btn(ctx.t("admin.btn.find"), A("find"))],
        [btn(ctx.t("admin.btn.prices"), FitPriceCb(action="show"))],
    )


def render_status_menu(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[btn(ctx.t(f"status.{s}"), A("lst", extra=s))] for s in LS.ALL if s != LS.RESERVED]
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


def screen_check(ctx: Ctx, listing: dict) -> list[str]:
    """For moderation: what the screenshots say and where it differs from the listing."""
    if not Repo.screenshot_ids(listing) or listing["kind"] == "promo" or not is_fit(listing):
        return []  # the reader knows only 1Fit's card: other clubs are checked by eye
    read = Repo.ocr_of(listing)
    if not read:
        return [ctx.t("admin.card.ocr_none")] if ocr.available() else []
    parts = []
    if read.get("end_date"):
        parts.append(ctx.t("app.until", date=fmt_date(read["end_date"])))
    if read.get("tariff_days"):
        parts.append(ctx.t("sell.screen.tariff_days") + " " + ctx.t(f"app.plan.short.{read['tariff_days']}"))
    if read.get("freeze_days"):
        parts.append(ctx.t("sell.screen.freeze_days") + " " + ctx.t("app.freeze_row", days=read["freeze_days"]))
    if read.get("bonus_days"):
        parts.append(ctx.t("sell.screen.bonus_days") + " " + ctx.t("app.bonus_row", days=read["bonus_days"]))
    if read.get("start_date"):
        parts.append(ctx.t("app.f.start").lower() + " " + fmt_date(read["start_date"]))
    if read.get("installment"):
        parts.append(ctx.t("admin.card.ocr_contract",
                           total=fmt_money(read["contract_total"]) if read.get("contract_total") else "—",
                           date=fmt_date(read["contract_date"]) if read.get("contract_date") else "—"))
    if read.get("payments_left"):
        parts.append(ctx.t("admin.card.ocr_payments", monthly=fmt_money(read.get("monthly_payment") or 0),
                           left=read["payments_left"]))
    lines = [ctx.t("admin.card.ocr", read=" · ".join(parts))]
    diff = ocr.mismatches(read, {**listing, "freeze_days": listing["freeze_days"] or 0, "end_date": card_end(listing)})
    if diff:
        lines.append(ctx.t("admin.card.ocr_diff", fields=", ".join(ctx.t(f"sell.screen.{k}") for k in diff)))
    return lines


async def render_card(ctx: Ctx, listing: dict, back: str = "") -> tuple[str, InlineKeyboardMarkup]:
    seller = await ctx.repo.get_user(listing["seller_id"])
    text = ctx.t(
        "admin.card",
        status=status_label(ctx, listing),
        public=listing_public(ctx, listing, listing["id"], installments=True),
        name=esc(listing["seller_name"]),
        phone=esc(listing["seller_phone"] or "—"),  # Mini App sellers are reached through Telegram
        tg=tg_ref(seller, listing["seller_id"]),
        lang=listing["lang"],
        created=fmt_dt(listing["created_at"]),
    )
    if listing["fee_paid_at"]:
        extra = [ctx.t("admin.card.fee_paid", fee=fmt_money(listing["fee_amount"] or 0),
                       date=fmt_dt(listing["fee_paid_at"]))]
    elif listing["fee_amount"]:
        extra = [ctx.t("admin.card.fee_unpaid", fee=fmt_money(listing["fee_amount"]))]
    else:
        extra = [ctx.t("admin.card.fee_free")]
    if listing["promo_code"]:
        extra.insert(0, ctx.t("admin.card.promo_code", code=esc(listing["promo_code"])))
    extra[0:0] = screen_check(ctx, listing)
    if listing["status"] == LS.APPROVED and ctx.repo.is_expired(listing):
        extra.append(ctx.t("admin.card.expired"))
    if listing["reject_reason"] and listing["status"] == LS.REJECTED:
        extra.append(ctx.t("admin.card.reason", reason=esc(listing["reject_reason"])))
    if listing["status"] == LS.SOLD and listing["sale_reported_at"]:
        extra.append(ctx.t("admin.card.sold", date=fmt_dt(listing["sale_reported_at"])))
    counts = await ctx.repo.contact_counts(listing["id"])
    extra.append(ctx.t("admin.card.contacts", **counts) if counts["buyers"] else ctx.t("admin.card.no_contacts"))
    text += "\n\n" + "\n".join(extra)

    lid, st = listing["id"], listing["status"]
    rows = []
    if st == LS.UNPAID and Repo.fee_due(listing):  # paid by hand through the operator
        rows.append([btn(ctx.t("admin.fee_received", fee=fmt_money(listing["fee_amount"])), A("fee", id=lid))])
    if st == LS.PENDING:
        rows.append([btn(ctx.t("admin.approve"), A("appr", id=lid))])
        if listing["description"]:
            rows.append([btn(ctx.t("admin.approve_nodesc"), A("apprnd", id=lid))])
        rows.append([btn(ctx.t("admin.reject"), A("rej", id=lid))])
    if listing["screenshot_file_id"]:
        rows.append([btn(ctx.t("admin.screenshot_btn"), A("shot", id=lid))])
    if counts["buyers"]:
        rows.append([btn(ctx.t("admin.contacts_btn", n=counts["buyers"]), A("cont", id=lid, extra=back))])
    if st in LS.ADMIN_REMOVABLE:
        rows.append([btn(ctx.t("admin.remove"), A("rm", id=lid))])
    rows.append([btn(ctx.t("admin.back"), A("lst", extra=back or st)), btn(ctx.t("admin.btn.home"), A("home"))])
    return text, kb(*rows)


async def render_stats(ctx: Ctx) -> tuple[str, InlineKeyboardMarkup]:
    s = await ctx.repo.stats()
    v = await ctx.repo.visit_stats(exclude=ctx.cfg.admin_ids)  # the admins are not counted
    days = "\n".join(ctx.t("admin.stats.day", day=fmt_date(d["day"])[:5], came=d["came"], new=d["new"]) for d in v["days"])
    visits = ctx.t("admin.stats.visits", **{k: x for k, x in v.items() if k not in ("days", "since")}, days=days,
                   since=fmt_date(v["since"]) if v["since"] else "—")
    return ctx.t("admin.stats", **{**s, "fees": fmt_money(s["fees"])}) + "\n\n" + visits, kb(
        [btn(ctx.t("admin.stats.refresh"), A("stats"))],
        [btn(ctx.t("admin.btn.home"), A("home"))],
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
        elif a == "askp!":
            await call.answer(ctx.t("admin.askprice.sending"))
            await call.message.edit_reply_markup(reply_markup=None)
            sent, failed = await send_askprice(ctx, notifier)
            await call.message.answer(ctx.t("admin.askprice.done", sent=sent, failed=failed))
        elif a == "stats":
            await call.answer()
            await _show(call, *await render_stats(ctx))
        elif a == "find":
            await call.answer()
            await state.set_state(AdminForm.find)
            await call.message.answer(ctx.t("admin.ask_find"), reply_markup=kb([btn(ctx.t("admin.back"), A("home"))]))
        else:
            await _listing_action(call, ctx, state, notifier, a, oid, extra)
    except RepoError as e:
        await call.answer(ctx.t(f"admin.err.{e.code}") if e.code in _ERRS else ctx.t("admin.err.bad_status"),
                          show_alert=True)


_ERRS = {"not_found", "bad_status", "already", "stale"}


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
    elif a == "cont":
        await call.answer()
        await _show(call, *await render_contacts(ctx, listing, extra))
    elif a == "shot":
        await call.answer()
        ids = Repo.screenshot_ids(listing)
        if len(ids) == 1:
            await call.message.answer_photo(ids[0], caption=ctx.t("admin.screenshot_caption", id=lid))
        elif ids:
            await call.message.answer_media_group([
                InputMediaPhoto(media=i, caption=ctx.t("admin.screenshot_caption", id=lid) if n == 0 else None)
                for n, i in enumerate(ids)])
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
    elif a == "fee":
        if listing["status"] != LS.UNPAID or not Repo.fee_due(listing):
            raise RepoError("already")
        await call.answer()
        await _show(call, ctx.t("admin.confirm_fee", id=lid, fee=fmt_money(listing["fee_amount"])),
                    confirm_kb(ctx, A("fee!", id=lid), A("open", id=lid)))
    elif a == "fee!":
        listing, amount = await repo.pay_fee_manually(lid, ctx.user_id)
        await call.answer(ctx.t("admin.fee_ok", id=lid))
        await _show(call, *await render_card(ctx, listing))
        await announce_paid(notifier, ctx, listing, True, amount)  # the seller hears; admins get it to moderate
    elif a == "rm":
        if listing["status"] not in LS.ADMIN_REMOVABLE:
            raise RepoError("bad_status")
        await call.answer()
        await _show(call, ctx.t("admin.confirm_remove", id=lid), confirm_kb(ctx, A("rm!", id=lid), A("open", id=lid)))
    elif a == "rm!":
        listing, _ = await repo.remove(lid, ctx.user_id)
        await call.answer(ctx.t("admin.removed_ok", id=lid))
        await _show(call, *await render_card(ctx, listing))
        await notifier.user(ctx, listing["seller_id"], lambda u: (u.t("notify.removed", id=lid), None))
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


@router.message(AdminForm.find)
async def on_find(message: Message, state: FSMContext, ctx: Ctx) -> None:
    raw = (message.text or "").strip().lstrip("№#")
    listing = await ctx.repo.get_listing(int(raw)) if raw.isdigit() else None
    if not listing:
        await message.answer(ctx.t("admin.err.not_found"))
        return
    await state.clear()
    await _send(message, await render_card(ctx, listing))


# ------------------------------------------------------------------ «update your price» to every seller
ASK_PRICE_PAUSE = 0.05  # Telegram allows about 30 messages a second


@router.message(Command("askprice"))
async def cmd_askprice(message: Message, ctx: Ctx) -> None:
    """Shows how many sellers get the message; nothing is sent before «Send»."""
    sellers = await ctx.repo.live_listings_by_seller()
    if not sellers:
        await message.answer(ctx.t("admin.askprice.none"))
        return
    n_listings = sum(len(ids) for ids in sellers.values())
    await message.answer(
        ctx.t("admin.askprice.confirm", sellers=len(sellers), listings=n_listings)
        + "\n\n" + ctx.t("notify.update_price"),
        reply_markup=confirm_kb(ctx, A("askp!"), A("home")))


async def send_askprice(ctx: Ctx, notifier: Notifier) -> tuple[int, int]:
    """Each seller once, in their language, with a button to each of their listings. Returns (sent, failed)."""
    import asyncio

    from ..callbacks import MyCb
    sent = failed = 0
    for seller_id, ids in (await ctx.repo.live_listings_by_seller()).items():
        ok = await notifier.user(ctx, seller_id, lambda u: (
            u.t("notify.update_price"),
            kb(*[[btn(u.t("notify.update_price_btn", id=i), MyCb(action="open", id=i))] for i in ids]),
        ))
        sent, failed = sent + ok, failed + (not ok)
        await asyncio.sleep(ASK_PRICE_PAUSE)
    return sent, failed


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
