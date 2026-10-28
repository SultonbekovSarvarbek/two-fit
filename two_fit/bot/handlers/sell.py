"""«Sell a membership» wizard. Also used to edit an existing listing (re-moderation)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...i18n import CITIES
from ...repo import LS, RepoError, today_local
from ...validators import (
    ValidationError,
    normalize_phone,
    parse_description,
    parse_end_date,
    parse_name,
    parse_price,
    parse_tariff,
)
from ..callbacks import AdmCb, SellCb
from ..context import Ctx
from ..middlewares import Btn
from ..notify import Notifier
from ..ui import btn, commission_block, esc, kb, listing_public, main_menu, phone_kb

router = Router(name="sell")


class SellForm(StatesGroup):
    city = State()
    tariff = State()
    date = State()
    price = State()
    desc = State()
    photo = State()
    name = State()
    phone = State()
    preview = State()
    choose = State()


STEPS = ["city", "tariff", "date", "price", "desc", "photo", "name", "phone"]
STATE_OF = {s: getattr(SellForm, s) for s in STEPS}
FIELDS = {  # field in «Change» menu -> first step to ask
    "city": "city", "tariff": "tariff", "date": "date", "price": "price",
    "desc": "desc", "photo": "photo", "contacts": "name",
}


# ------------------------------------------------------------------ prompts
def _nav(ctx: Ctx, step: str, extra: list | None = None, single: bool = False) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    bottom = []
    if step != "city" or single:
        bottom.append(btn(ctx.t("common.back"), SellCb(action="back")))
    bottom.append(btn(ctx.t("common.cancel"), SellCb(action="cancel")))
    rows.append(bottom)
    return kb(*rows)


async def ask(message: Message, state: FSMContext, ctx: Ctx, step: str) -> None:
    await state.set_state(STATE_OF[step])
    data = await state.get_data()
    if step == "city":
        names = [btn(ctx.i18n.city(ctx.lang, c), SellCb(action="city", value=c)) for c in CITIES]
        rows = [names[i:i + 2] for i in range(0, len(names), 2)]
        await message.answer(ctx.t("sell.ask_city"), reply_markup=_nav(ctx, step, rows, bool(data.get("single"))))
    elif step == "tariff":
        await message.answer(ctx.t("sell.ask_tariff"), reply_markup=_nav(ctx, step))
    elif step == "date":
        await message.answer(ctx.t("sell.ask_date"), reply_markup=_nav(ctx, step))
    elif step == "price":
        await message.answer(ctx.t("sell.ask_price"), reply_markup=_nav(ctx, step))
    elif step == "desc":
        skip = [[btn(ctx.t("common.skip"), SellCb(action="skip"))]]
        await message.answer(ctx.t("sell.ask_desc"), reply_markup=_nav(ctx, step, skip))
    elif step == "photo":
        skip = [[btn(ctx.t("common.skip"), SellCb(action="skip"))]]
        await message.answer(ctx.t("sell.ask_photo"), reply_markup=_nav(ctx, step, skip))
    elif step == "name":
        extra = []
        u = ctx.user
        if u.get("contact_name") and u.get("contact_phone"):
            extra = [[btn(ctx.t("sell.use_saved", name=u["contact_name"], phone=u["contact_phone"]),
                          SellCb(action="saved"))]]
        await message.answer(ctx.t("sell.ask_name"), reply_markup=_nav(ctx, step, extra))
    elif step == "phone":
        await message.answer(ctx.t("sell.ask_phone"), reply_markup=phone_kb(ctx))


async def advance(message: Message, state: FSMContext, ctx: Ctx, step: str) -> None:
    """Go to the step after ``step`` — or back to the preview when editing a single field."""
    data = await state.get_data()
    if step == "name":
        await ask(message, state, ctx, "phone")
        return
    if data.get("single") or step == "phone":
        await show_preview(message, state, ctx)
        return
    await ask(message, state, ctx, STEPS[STEPS.index(step) + 1])


async def draft_update(state: FSMContext, **values) -> dict:
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    draft.update(values)
    await state.update_data(draft=draft)
    return draft


# ------------------------------------------------------------------ preview
def preview_text(ctx: Ctx, data: dict) -> str:
    d = data["draft"]
    parts = [
        ctx.t("sell.preview_title"),
        listing_public(ctx, d, data.get("listing_id") or "—"),
        "—",
        commission_block(ctx, d["price"]),
        ctx.t("commission.note"),
        ctx.t(
            "sell.private_block",
            name=esc(d["seller_name"]),
            phone=esc(d["seller_phone"]),
            photo=ctx.t("sell.photo_yes" if d.get("screenshot_file_id") else "sell.photo_no"),
        ),
    ]
    return "\n\n".join(parts)


def preview_kb(ctx: Ctx, accepted: bool) -> InlineKeyboardMarkup:
    return kb(
        [btn(ctx.t("sell.accept_on" if accepted else "sell.accept_off"), SellCb(action="accept"))],
        [btn(ctx.t("sell.send"), SellCb(action="send"))],
        [btn(ctx.t("sell.edit"), SellCb(action="edit")), btn(ctx.t("sell.cancel"), SellCb(action="cancel"))],
    )


async def show_preview(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.set_state(SellForm.preview)
    await state.update_data(single=False)
    data = await state.get_data()
    await message.answer(preview_text(ctx, data), reply_markup=preview_kb(ctx, bool(data.get("accepted"))))


# ------------------------------------------------------------------ entry points
@router.message(Btn("menu.sell"))
async def menu_sell(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await state.set_data({"mode": "new", "draft": {}, "accepted": False})
    await message.answer(ctx.t("sell.intro"), reply_markup=main_menu(ctx))
    await ask(message, state, ctx, "city")


async def start_edit(message: Message, state: FSMContext, ctx: Ctx, listing: dict) -> None:
    """Called from «My listings»: open the preview of an existing listing for editing."""
    draft = {k: listing[k] for k in (
        "city", "tariff", "end_date", "price", "description", "screenshot_file_id", "seller_name", "seller_phone"
    )}
    await state.clear()
    await state.set_data({
        "mode": "edit",
        "listing_id": listing["id"],
        "draft": draft,
        # Already accepted for this exact price; any price change resets it.
        "accepted": listing["commission_accepted_price"] == listing["price"],
    })
    await message.answer(ctx.t("sell.edit_intro", id=listing["id"]))
    await show_preview(message, state, ctx)


# ------------------------------------------------------------------ navigation
_WIZARD = (SellForm.city, SellForm.tariff, SellForm.date, SellForm.price, SellForm.desc, SellForm.photo,
           SellForm.name, SellForm.phone, SellForm.preview, SellForm.choose)


@router.callback_query(SellCb.filter(F.action == "cancel"), F.message)
async def on_cancel(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    if await state.get_state() not in {s.state for s in _WIZARD}:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("sell.cancelled"), reply_markup=main_menu(ctx))


@router.callback_query(SellCb.filter(F.action == "back"))
async def on_back(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    current = await state.get_state()
    step = next((s for s in STEPS if STATE_OF[s].state == current), None)
    if step is None:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if data.get("single") and step != "phone":
        await show_preview(call.message, state, ctx)
    elif step == "city":
        await show_preview(call.message, state, ctx)
    else:
        await ask(call.message, state, ctx, STEPS[STEPS.index(step) - 1])


async def _phone_text_nav(message: Message, state: FSMContext, ctx: Ctx) -> bool:
    """«Back»/«Cancel» typed via the reply keyboard on the phone step."""
    text = message.text or ""
    if text in ctx.i18n.variants("common.cancel"):
        await state.clear()
        await message.answer(ctx.t("sell.cancelled"), reply_markup=main_menu(ctx))
        return True
    if text in ctx.i18n.variants("common.back"):
        await message.answer(ctx.t("menu.hint"), reply_markup=main_menu(ctx))
        await ask(message, state, ctx, "name")
        return True
    return False


# ------------------------------------------------------------------ steps
@router.callback_query(SellForm.city, SellCb.filter(F.action == "city"))
async def on_city(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    if callback_data.value not in CITIES:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        ctx.t("sell.ask_city") + " <b>" + esc(ctx.i18n.city(ctx.lang, callback_data.value)) + "</b>"
    )
    await draft_update(state, city=callback_data.value)
    await advance(call.message, state, ctx, "city")


async def _text_step(message: Message, state: FSMContext, ctx: Ctx, step: str, parser, field: str) -> None:
    if not message.text:
        await message.answer(ctx.t("err.text_expected"))
        return
    try:
        value = parser(message.text)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=_nav(ctx, step))
        return
    await draft_update(state, **{field: value})
    await advance(message, state, ctx, step)


@router.message(SellForm.tariff)
async def on_tariff(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "tariff", parse_tariff, "tariff")


@router.message(SellForm.date)
async def on_date(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "date", lambda t: parse_end_date(t, today_local()).isoformat(), "end_date")


@router.message(SellForm.price)
async def on_price(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not message.text:
        await message.answer(ctx.t("err.text_expected"))
        return
    try:
        price = parse_price(message.text)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=_nav(ctx, "price"))
        return
    data = await state.get_data()
    old = (data.get("draft") or {}).get("price")
    if price != old:
        # New price -> new commission -> the seller must confirm again.
        if data.get("accepted"):
            await message.answer(ctx.t("sell.accept_reset"))
        await state.update_data(accepted=False)
    await draft_update(state, price=price)
    await message.answer(ctx.t("sell.price_calc", block=commission_block(ctx, price)))
    await advance(message, state, ctx, "price")


@router.message(SellForm.desc)
async def on_desc(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "desc", parse_description, "description")


@router.callback_query(SellForm.desc, SellCb.filter(F.action == "skip"))
async def on_desc_skip(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, description="")
    await advance(call.message, state, ctx, "desc")


@router.message(SellForm.photo, F.photo)
async def on_photo(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await draft_update(state, screenshot_file_id=message.photo[-1].file_id)
    await advance(message, state, ctx, "photo")


@router.message(SellForm.photo)
async def on_photo_wrong(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("err.bad_photo"), reply_markup=_nav(ctx, "photo", [
        [btn(ctx.t("common.skip"), SellCb(action="skip"))]
    ]))


@router.callback_query(SellForm.photo, SellCb.filter(F.action == "skip"))
async def on_photo_skip(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, screenshot_file_id=None)
    await advance(call.message, state, ctx, "photo")


@router.message(SellForm.name)
async def on_name(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await _text_step(message, state, ctx, "name", parse_name, "seller_name")


@router.callback_query(SellForm.name, SellCb.filter(F.action == "saved"))
async def on_saved_contact(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    u = ctx.user
    if not (u.get("contact_name") and u.get("contact_phone")):
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await draft_update(state, seller_name=u["contact_name"], seller_phone=u["contact_phone"])
    await show_preview(call.message, state, ctx)


@router.message(SellForm.phone)
async def on_phone(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not message.contact and await _phone_text_nav(message, state, ctx):
        return
    raw = message.contact.phone_number if message.contact else message.text
    try:
        phone = normalize_phone(raw)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=phone_kb(ctx))
        return
    draft = await draft_update(state, seller_phone=phone)
    await ctx.repo.save_contact(ctx.user_id, draft["seller_name"], phone)
    await message.answer(ctx.t("sell.phone_ok"), reply_markup=main_menu(ctx))
    await advance(message, state, ctx, "phone")


# ------------------------------------------------------------------ preview actions
@router.callback_query(SellForm.preview, SellCb.filter(F.action == "accept"))
async def on_accept(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    data = await state.get_data()
    accepted = not data.get("accepted")
    await state.update_data(accepted=accepted)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=preview_kb(ctx, accepted))


@router.callback_query(SellForm.preview, SellCb.filter(F.action == "edit"))
async def on_edit(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.set_state(SellForm.choose)
    names = [btn(ctx.t(f"sell.field.{f}"), SellCb(action="field", value=f)) for f in FIELDS]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append([btn(ctx.t("sell.to_preview"), SellCb(action="preview"))])
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("sell.choose_field"), reply_markup=kb(*rows))


@router.callback_query(SellForm.choose, SellCb.filter(F.action == "field"))
async def on_field(call: CallbackQuery, callback_data: SellCb, state: FSMContext, ctx: Ctx) -> None:
    step = FIELDS.get(callback_data.value)
    if not step:
        await call.answer(ctx.t("common.stale"), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await state.update_data(single=True)
    await ask(call.message, state, ctx, step)


@router.callback_query(SellForm.choose, SellCb.filter(F.action == "preview"))
async def on_to_preview(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await show_preview(call.message, state, ctx)


@router.callback_query(SellForm.preview, SellCb.filter(F.action == "send"))
async def on_send(call: CallbackQuery, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    if not data.get("accepted"):
        await call.answer(ctx.t("sell.need_accept"), show_alert=True)
        return
    draft["commission_accepted_price"] = draft.get("price")
    try:
        if data.get("mode") == "edit":
            listing_id = data["listing_id"]
            await ctx.repo.update_listing_by_seller(listing_id, ctx.user_id, ctx.lang, draft)
            done_key, admin_key = "sell.resent", "admin.n.edited_listing"
        else:
            listing_id = await ctx.repo.create_listing(ctx.user_id, ctx.lang, draft)
            done_key, admin_key = "sell.sent", "admin.n.new_listing"
    except RepoError as e:
        await call.answer()
        await state.clear()
        key = "sell.edit_blocked" if e.code in ("bad_status", "not_found") else "common.error"
        await call.message.answer(ctx.t(key), reply_markup=main_menu(ctx))
        return

    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t(done_key, id=listing_id), reply_markup=main_menu(ctx))
    await notifier.admins(ctx, lambda a: (
        a.t(admin_key, id=listing_id),
        kb([btn(a.t("admin.open_btn", id=listing_id), AdmCb(action="open", id=listing_id))]),
    ))


# Text typed while a button is expected (city / preview / field menu).
@router.message(SellForm.city)
@router.message(SellForm.preview)
@router.message(SellForm.choose)
async def on_text_instead_of_button(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("common.unknown"))


__all__ = ["router", "start_edit", "LS"]
