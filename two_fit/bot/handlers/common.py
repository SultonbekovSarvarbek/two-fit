"""Start, language, main menu buttons, support, «how it works» and fallbacks."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from ..callbacks import NOOP, LangCb
from ..context import Ctx
from ..middlewares import Btn
from ..ui import btn, kb, lang_kb, main_menu, open_app_kb

menu_router = Router(name="menu")
fallback_router = Router(name="fallback")

_welcome_file_id: str | None = None  # cached after the first upload


async def send_welcome(message: Message, ctx: Ctx) -> None:
    global _welcome_file_id
    image = ctx.cfg.welcome_image
    app = ctx.cfg.webapp_enabled
    text = ctx.t("app.welcome" if app else "welcome")
    if _welcome_file_id or image.exists():
        photo = _welcome_file_id or FSInputFile(image)
        sent = await message.answer_photo(photo, caption=text, reply_markup=main_menu(ctx))
        if sent.photo:
            _welcome_file_id = sent.photo[-1].file_id
    else:
        await message.answer(text, reply_markup=main_menu(ctx))
    if app:
        # The welcome removes the old reply keyboard, so the button needs a message of its own.
        await send_open_app(message, ctx)


async def send_open_app(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("app.open_text"), reply_markup=open_app_kb(ctx))


async def send_menu(message: Message, ctx: Ctx, key: str = "menu.hint") -> None:
    if ctx.cfg.webapp_enabled:
        await send_open_app(message, ctx)
        return
    await message.answer(ctx.t(key), reply_markup=main_menu(ctx))


@menu_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await send_welcome(message, ctx)


@menu_router.callback_query(LangCb.filter())
async def on_lang(call: CallbackQuery, callback_data: LangCb, state: FSMContext, ctx: Ctx) -> None:
    first_time = ctx.user["lang"] is None
    await ctx.repo.set_lang(ctx.user_id, callback_data.code)
    ctx.user["lang"] = callback_data.code
    await call.answer()
    await call.message.edit_text(ctx.t("lang.changed"))
    if first_time:
        await send_welcome(call.message, ctx)
    else:
        await state.clear()
        await send_menu(call.message, ctx)


@menu_router.message(Btn("menu.lang"))
async def menu_lang(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer(ctx.t("lang.choose"), reply_markup=lang_kb(ctx.i18n))


@menu_router.message(Btn("menu.support"))
async def menu_support(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer(ctx.t("support.text"), reply_markup=kb([btn(ctx.t("support.btn"), url=ctx.cfg.support_url)]))


@menu_router.message(Btn("menu.how"))
async def menu_how(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer(
        ctx.t("how.text"), reply_markup=kb([btn(ctx.t("support.btn"), url=ctx.cfg.support_url)])
    )


@menu_router.message(Command("menu", "cancel"))
async def cmd_menu(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await send_menu(message, ctx)


@menu_router.message(Command("app"))
async def cmd_app(message: Message, state: FSMContext, ctx: Ctx) -> None:
    if not ctx.cfg.webapp_enabled:
        await cmd_menu(message, state, ctx)
        return
    await send_open_app(message, ctx)


@menu_router.message(Command("id"))
async def cmd_id(message: Message, ctx: Ctx) -> None:
    await message.answer(ctx.t("common.id", id=ctx.user_id))


@menu_router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await menu_support(message, state, ctx)


# ---------------------------------------------------------------- fallbacks
@fallback_router.callback_query(F.data == NOOP)
async def on_noop(call: CallbackQuery) -> None:
    await call.answer()


@fallback_router.callback_query()
async def on_stale(call: CallbackQuery, ctx: Ctx) -> None:
    await call.answer(ctx.t("common.stale"), show_alert=True)


@fallback_router.message()
async def on_unknown(message: Message, ctx: Ctx) -> None:
    await send_menu(message, ctx, "common.unknown")
