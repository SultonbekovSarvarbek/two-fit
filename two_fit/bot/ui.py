"""Message formatting and keyboards shared by handlers."""
from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from ..i18n import LANGS
from ..money import fmt_money
from ..repo import LS, TZ, Repo
from .callbacks import NOOP, LangCb
from .context import Ctx


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def btn(text: str, cb: Any = None, url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    data = cb.pack() if hasattr(cb, "pack") else cb
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def main_menu(ctx: Ctx) -> ReplyKeyboardMarkup:
    b = lambda key: KeyboardButton(text=ctx.t(key))  # noqa: E731
    return ReplyKeyboardMarkup(
        keyboard=[
            [b("menu.catalog")],
            [b("menu.sell"), b("menu.my")],
            [b("menu.support"), b("menu.how")],
            [b("menu.lang")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def phone_kb(ctx: Ctx) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ctx.t("sell.share_phone"), request_contact=True)],
            [KeyboardButton(text=ctx.t("common.back")), KeyboardButton(text=ctx.t("common.cancel"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def lang_kb(i18n) -> InlineKeyboardMarkup:
    return kb([btn(i18n.t(code, f"lang.{code}"), LangCb(code=code)) for code in LANGS])


def operator_btn(ctx: Ctx) -> InlineKeyboardButton:
    return btn(ctx.t("common.operator_btn"), url=ctx.cfg.support_url)


def noop_btn(text: str) -> InlineKeyboardButton:
    return btn(text, NOOP)


def pager(ctx: Ctx, page: int, pages: int, make_cb) -> list[InlineKeyboardButton] | None:
    if pages <= 1:
        return None
    row = []
    row.append(btn(ctx.t("common.prev"), make_cb(page - 1)) if page > 0 else noop_btn(" "))
    row.append(noop_btn(ctx.t("common.page", page=page + 1, pages=pages)))
    row.append(btn(ctx.t("common.next"), make_cb(page + 1)) if page + 1 < pages else noop_btn(" "))
    return row


def pages_count(total: int, size: int) -> int:
    return max(1, -(-total // size))


def status_label(ctx: Ctx, listing: dict[str, Any]) -> str:
    if listing["status"] == LS.APPROVED and Repo.is_expired(listing):
        return ctx.t("status.expired")
    return ctx.t(f"status.{listing['status']}")


def listing_public(ctx: Ctx, data: dict[str, Any], listing_id: int | str = "—") -> str:
    """Public part of a listing: only what a buyer may see. Labels in the viewer's language,
    the seller's own words (tariff, description) as written."""
    text = ctx.t(
        "card.body",
        id=listing_id,
        city=esc(ctx.i18n.city(ctx.lang, data["city"])),
        tariff=esc(data["tariff"]),
        date=fmt_date(data["end_date"]),
        price=fmt_money(data["price"]),
    )
    if data.get("description"):
        text += ctx.t("card.desc", desc=esc(data["description"]))
    return text


def commission_block(ctx: Ctx, price: int) -> str:
    c = ctx.commission(price)
    return ctx.t(
        "commission.block", price=fmt_money(c.price), commission=fmt_money(c.commission), net=fmt_money(c.net)
    )


def tg_ref(user: dict[str, Any] | None, user_id: int) -> str:
    if user and user.get("username"):
        return f"{user_id} (@{esc(user['username'])})"
    return str(user_id)
