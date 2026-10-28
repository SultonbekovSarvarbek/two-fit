"""«I want to buy»: collect the buyer's contact for the admin and create a request."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...repo import RepoError
from ...validators import ValidationError, normalize_phone, parse_name
from ..callbacks import AdmCb, BuyCb, BuyStepCb
from ..context import Ctx
from ..notify import Notifier
from ..ui import btn, esc, kb, main_menu, operator_btn, phone_kb

router = Router(name="buy")


class BuyForm(StatesGroup):
    name = State()
    phone = State()


def _cancel_kb(ctx: Ctx):
    return kb([btn(ctx.t("common.cancel"), BuyStepCb(action="cancel"))])


async def _precheck(ctx: Ctx, listing_id: int) -> str | None:
    listing = await ctx.repo.get_listing(listing_id)
    if not ctx.repo.is_available(listing):
        return ctx.t("common.not_available")
    if listing["seller_id"] == ctx.user_id:
        return ctx.t("buy.own")
    if await ctx.repo.active_request(listing_id, ctx.user_id):
        return ctx.t("buy.duplicate", id=listing_id)
    return None


@router.callback_query(BuyCb.filter())
async def on_buy(call: CallbackQuery, callback_data: BuyCb, state: FSMContext, ctx: Ctx) -> None:
    problem = await _precheck(ctx, callback_data.id)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.update_data(listing_id=callback_data.id)
    await call.message.answer(ctx.t("buy.intro", id=callback_data.id))
    user = ctx.user
    if user.get("contact_name") and user.get("contact_phone"):
        await state.set_state(BuyForm.name)
        await call.message.answer(
            ctx.t("buy.use_saved_q"),
            reply_markup=kb(
                [btn(ctx.t("sell.use_saved", name=user["contact_name"], phone=user["contact_phone"]),
                     BuyStepCb(action="saved"))],
                [btn(ctx.t("buy.other_contacts"), BuyStepCb(action="other"))],
                [btn(ctx.t("common.cancel"), BuyStepCb(action="cancel"))],
            ),
        )
    else:
        await state.set_state(BuyForm.name)
        await call.message.answer(ctx.t("buy.ask_name"), reply_markup=_cancel_kb(ctx))


@router.callback_query(BuyStepCb.filter(F.action == "cancel"))
async def on_cancel(call: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("buy.cancelled"), reply_markup=main_menu(ctx))


@router.callback_query(BuyForm.name, BuyStepCb.filter(F.action == "other"))
async def on_other(call: CallbackQuery, ctx: Ctx) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(ctx.t("buy.ask_name"), reply_markup=_cancel_kb(ctx))


@router.callback_query(BuyForm.name, BuyStepCb.filter(F.action == "saved"))
async def on_saved(call: CallbackQuery, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await _finish(call.message, state, ctx, notifier, ctx.user["contact_name"], ctx.user["contact_phone"])


@router.message(BuyForm.name)
async def on_name(message: Message, state: FSMContext, ctx: Ctx) -> None:
    try:
        name = parse_name(message.text)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=_cancel_kb(ctx))
        return
    await state.update_data(buyer_name=name)
    await state.set_state(BuyForm.phone)
    await message.answer(ctx.t("sell.ask_phone"), reply_markup=phone_kb(ctx))


@router.message(BuyForm.phone)
async def on_phone(message: Message, state: FSMContext, ctx: Ctx, notifier: Notifier) -> None:
    text = message.text or ""
    if text in ctx.i18n.variants("common.cancel"):
        await state.clear()
        await message.answer(ctx.t("buy.cancelled"), reply_markup=main_menu(ctx))
        return
    if text in ctx.i18n.variants("common.back"):
        await state.set_state(BuyForm.name)
        await message.answer(ctx.t("buy.ask_name"), reply_markup=main_menu(ctx))
        return
    raw = message.contact.phone_number if message.contact else text
    try:
        phone = normalize_phone(raw)
    except ValidationError as e:
        await message.answer(ctx.t(f"err.{e.code}"), reply_markup=phone_kb(ctx))
        return
    data = await state.get_data()
    await message.answer(ctx.t("sell.phone_ok"), reply_markup=main_menu(ctx))
    await _finish(message, state, ctx, notifier, data["buyer_name"], phone)


async def _finish(message: Message, state: FSMContext, ctx: Ctx, notifier: Notifier, name: str, phone: str) -> None:
    data = await state.get_data()
    listing_id = data.get("listing_id")
    await state.clear()
    if not listing_id:
        await message.answer(ctx.t("common.stale"), reply_markup=main_menu(ctx))
        return
    await ctx.repo.save_contact(ctx.user_id, name, phone)
    try:
        request_id = await ctx.repo.create_request(listing_id, ctx.user_id, name, phone)
    except RepoError as e:
        key = {"own": "buy.own", "duplicate": "buy.duplicate"}.get(e.code, "common.not_available")
        await message.answer(ctx.t(key, id=listing_id), reply_markup=main_menu(ctx))
        return

    await message.answer(ctx.t("buy.done", id=listing_id), reply_markup=kb([operator_btn(ctx)]))
    await notifier.admins(ctx, lambda a: (
        a.t("admin.n.new_request", rid=request_id, id=listing_id, name=esc(name), phone=esc(phone)),
        kb([btn(a.t("admin.open_btn", id=listing_id), AdmCb(action="reqs", id=listing_id))]),
    ))
