"""Loads the user, builds ``ctx`` and asks for a language on first contact."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..config import Config
from ..i18n import I18n
from ..repo import Repo
from .context import Ctx
from .ui import lang_kb


class ContextMiddleware(BaseMiddleware):
    def __init__(self, repo: Repo, i18n: I18n, cfg: Config):
        self.repo, self.i18n, self.cfg = repo, i18n, cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        chat = data.get("event_chat")
        if tg_user is None or tg_user.is_bot:
            return None
        if chat is not None and chat.type != "private":
            return None  # the bot works only in private chats

        user = await self.repo.touch_user(tg_user.id, tg_user.username, tg_user.full_name)
        data["ctx"] = Ctx(self.repo, self.i18n, self.cfg, user)

        if user["lang"] is None:
            is_lang_choice = isinstance(event, CallbackQuery) and (event.data or "").startswith("lang:")
            if not is_lang_choice:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                bot = data["bot"]
                await bot.send_message(tg_user.id, self.i18n.t("ru", "lang.choose"), reply_markup=lang_kb(self.i18n))
                return None
        return await handler(event, data)


class Btn(Filter):
    """Filter: the message is a reply-keyboard button with this text key, in any language."""

    def __init__(self, *keys: str):
        self.keys = keys

    async def __call__(self, message: Message, ctx: Ctx) -> bool:
        return bool(message.text) and any(message.text in ctx.i18n.variants(k) for k in self.keys)


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, ctx: Ctx | None = None) -> bool:
        return ctx is not None and ctx.is_admin
