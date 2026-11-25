"""«All memberships»: list, club and city filters, pages and the listing card.

The filter travels in the buttons' ``city`` field as «city.club» (both optional), so the buttons already
sent in chats keep working.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...clubs import CLUBS, club_name
from ...money import fmt_money
from ..callbacks import CardCb, CatCb, CityMenuCb
from ..context import Ctx
from ..middlewares import Btn
from ..ui import btn, buyer_total, esc, kb, listing_public, pager, pages_count, seller_contacts

router = Router(name="catalog")
CLUB_MENU = "~club"  # CityMenuCb.city starting with it: the club menu instead of the city menu


def split_filter(value: str) -> tuple[str, str]:
    city, _, club = (value or "").partition(".")
    return city, club if club in CLUBS else ""


def join_filter(city: str, club: str) -> str:
    return f"{city}.{club}" if club else city


async def render_catalog(ctx: Ctx, page: int, flt: str) -> tuple[str, InlineKeyboardMarkup]:
    city, club = split_filter(flt)
    flt = join_filter(city, club)
    size = ctx.cfg.page_size
    rows, total = await ctx.repo.catalog(city or None, size, page * size, club=club or None)
    pages = pages_count(total, size)
    if total and page >= pages:  # list got shorter since the button was drawn
        page = pages - 1
        rows, total = await ctx.repo.catalog(city or None, size, page * size, club=club or None)

    city_name = esc(ctx.i18n.city(ctx.lang, city)) if city else ""
    filters = ([ctx.t("catalog.filter_club", club=esc(club_name(club)))] if club else []) + \
              ([ctx.t("catalog.filter_on", city=city_name)] if city else [])
    if total == 0:
        text = ctx.t("catalog.title") + "\n\n"
        text += "\n".join(filters) + "\n" if club else ""
        text += ctx.t("catalog.empty_city", city=city_name) if city else ctx.t("catalog.empty")
        markup = kb(
            [btn(ctx.t("catalog.reset_btn"), CatCb(page=0, city=""))] if flt else None,
            [btn(ctx.t("support.btn"), url=ctx.cfg.support_url)],
        )
        return text, markup

    lines = [ctx.t("catalog.title"), ctx.t("catalog.count", count=total), *filters, "", ctx.t("catalog.hint")]
    item_rows = [
        [btn(
            ctx.t("catalog.item", id=r["id"], club=club_name(r["club"]), city=ctx.i18n.city(ctx.lang, r["city"]),
                  price=fmt_money(buyer_total(r)))
            + (ctx.t("catalog.verified") if r["fee_paid_at"] and r["status"] != "sold" else "")
            + (ctx.t("catalog.promo") if r["kind"] == "promo" else "")
            + (ctx.t("catalog.sold") if r["status"] == "sold" else ""),
            CardCb(id=r["id"], page=page, city=flt),
        )]
        for r in rows
    ]
    filter_row = [btn(ctx.t("catalog.club_btn"), CityMenuCb(city=CLUB_MENU + flt)),
                  btn(ctx.t("catalog.filter_btn"), CityMenuCb(city=flt))]
    reset = [btn(ctx.t("catalog.reset_btn"), CatCb(page=0, city=""))] if flt else None
    markup = kb(*item_rows, pager(ctx, page, pages, lambda p: CatCb(page=p, city=flt)), filter_row, reset)
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
    value = callback_data.city
    await call.answer()
    if value.startswith(CLUB_MENU):  # which club: keeps the city chosen
        city, club = split_filter(value[len(CLUB_MENU):])
        clubs = await ctx.repo.catalog_clubs()
        rows = [[btn(ctx.t("catalog.club_item", club=club_name(code), count=n), CatCb(page=0, city=join_filter(city, code)))]
                for code, n in clubs]
        if club:
            rows.append([btn(ctx.t("app.catalog.all_clubs"), CatCb(page=0, city=city))])
        rows.append([btn(ctx.t("catalog.to_list"), CatCb(page=0, city=join_filter(city, club)))])
        await _edit(call, ctx.t("catalog.choose_club") if clubs else ctx.t("catalog.empty"), kb(*rows))
        return
    city, club = split_filter(value)
    cities = await ctx.repo.catalog_cities()
    rows = [
        [btn(ctx.t("catalog.city_item", city=ctx.i18n.city(ctx.lang, code), count=n), CatCb(page=0, city=join_filter(code, club)))]
        for code, n in cities
    ]
    rows.append([btn(ctx.t("catalog.to_list"), CatCb(page=0, city=value))])
    text = ctx.t("catalog.choose_city") if cities else ctx.t("catalog.empty")
    await _edit(call, text, kb(*rows))


def card_markup(ctx: Ctx, tg_row: list | None, page: int, flt: str) -> InlineKeyboardMarkup:
    return kb(tg_row, [btn(ctx.t("catalog.to_list"), CatCb(page=page, city=flt))])


@router.callback_query(CardCb.filter())
async def on_card(call: CallbackQuery, callback_data: CardCb, ctx: Ctx) -> None:
    listing = await ctx.repo.get_listing(callback_data.id)
    if ctx.repo.is_sold_shown(listing):  # sold: the card without the seller's contacts
        await call.answer()
        text = ctx.t("catalog.sold_card") + "\n\n" + listing_public(ctx, listing, listing["id"])
        await _edit(call, text, card_markup(ctx, None, callback_data.page, callback_data.city))
        return
    if not ctx.repo.is_available(listing):
        await call.answer(ctx.t("common.not_available"), show_alert=True)
        text, markup = await render_catalog(ctx, callback_data.page, callback_data.city)
        await _edit(call, text, markup)
        return
    contacts, tg_row = await seller_contacts(ctx, listing)
    await ctx.repo.log_contact(listing["id"], ctx.user_id, "view")  # the card shows the seller's contacts
    text = listing_public(ctx, listing, listing["id"]) + "\n\n" + contacts + "\n\n" + ctx.t("catalog.safety")
    await call.answer()
    await _edit(call, text, card_markup(ctx, tg_row, callback_data.page, callback_data.city))
