"""«All memberships»: list, city filter, pages and the listing card."""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...money import fmt_money
from ..callbacks import BuyCb, CardCb, CatCb, CityMenuCb
from ..context import Ctx
from ..middlewares import Btn
from ..ui import btn, esc, kb, listing_public, operator_btn, pager, pages_count

router = Router(name="catalog")


async def render_catalog(ctx: Ctx, page: int, city: str) -> tuple[str, InlineKeyboardMarkup]:
    size = ctx.cfg.page_size
    rows, total = await ctx.repo.catalog(city or None, size, page * size)
    pages = pages_count(total, size)
    if total and page >= pages:  # list got shorter since the button was drawn
        page = pages - 1
        rows, total = await ctx.repo.catalog(city or None, size, page * size)

    city_name = esc(ctx.i18n.city(ctx.lang, city)) if city else ""
    if total == 0:
        text = ctx.t("catalog.title") + "\n\n"
        text += ctx.t("catalog.empty_city", city=city_name) if city else ctx.t("catalog.empty")
        markup = kb(
            [btn(ctx.t("catalog.reset_btn"), CatCb(page=0, city=""))] if city else None,
            [btn(ctx.t("support.btn"), url=ctx.cfg.support_url)],
        )
        return text, markup

    lines = [ctx.t("catalog.title"), ctx.t("catalog.count", count=total)]
    if city:
        lines.append(ctx.t("catalog.filter_on", city=city_name))
    lines += ["", ctx.t("catalog.hint")]
    item_rows = [
        [btn(
            ctx.t("catalog.item", id=r["id"], city=ctx.i18n.city(ctx.lang, r["city"]), price=fmt_money(r["price"])),
            CardCb(id=r["id"], page=page, city=city),
        )]
        for r in rows
    ]
    filter_row = [btn(ctx.t("catalog.filter_btn"), CityMenuCb(city=city))]
    if city:
        filter_row.append(btn(ctx.t("catalog.reset_btn"), CatCb(page=0, city="")))
    markup = kb(*item_rows, pager(ctx, page, pages, lambda p: CatCb(page=p, city=city)), filter_row)
    return "\n".join(lines), markup


async def _edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            # e.g. the old message was a photo: send a fresh one instead
            await call.message.answer(text, reply_markup=markup)


@router.message(Btn("menu.catalog"))
async def menu_catalog(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    text, markup = await render_catalog(ctx, 0, "")
    await message.answer(text, reply_markup=markup)


@router.callback_query(CatCb.filter())
async def on_page(call: CallbackQuery, callback_data: CatCb, ctx: Ctx) -> None:
    text, markup = await render_catalog(ctx, max(0, callback_data.page), callback_data.city)
    await call.answer()
    await _edit(call, text, markup)


@router.callback_query(CityMenuCb.filter())
async def on_city_menu(call: CallbackQuery, callback_data: CityMenuCb, ctx: Ctx) -> None:
    cities = await ctx.repo.catalog_cities()
    rows = [
        [btn(ctx.t("catalog.city_item", city=ctx.i18n.city(ctx.lang, code), count=n), CatCb(page=0, city=code))]
        for code, n in cities
    ]
    rows.append([btn(ctx.t("catalog.to_list"), CatCb(page=0, city=callback_data.city))])
    await call.answer()
    text = ctx.t("catalog.choose_city") if cities else ctx.t("catalog.empty")
    await _edit(call, text, kb(*rows))


def card_markup(ctx: Ctx, listing_id: int, page: int, city: str) -> InlineKeyboardMarkup:
    return kb(
        [btn(ctx.t("catalog.buy_btn"), BuyCb(id=listing_id))],
        [operator_btn(ctx)],
        [btn(ctx.t("catalog.to_list"), CatCb(page=page, city=city))],
    )


@router.callback_query(CardCb.filter())
async def on_card(call: CallbackQuery, callback_data: CardCb, ctx: Ctx) -> None:
    listing = await ctx.repo.get_listing(callback_data.id)
    if not ctx.repo.is_available(listing):
        await call.answer(ctx.t("common.not_available"), show_alert=True)
        text, markup = await render_catalog(ctx, callback_data.page, callback_data.city)
        await _edit(call, text, markup)
        return
    text = listing_public(ctx, listing, listing["id"]) + "\n\n" + ctx.t("catalog.op_hint", id=listing["id"])
    await call.answer()
    await _edit(call, text, card_markup(ctx, listing["id"], callback_data.page, callback_data.city))
