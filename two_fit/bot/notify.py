"""Notifications to users and admins, each in the recipient's own language."""
from __future__ import annotations

import logging
from typing import Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

from .context import Ctx

log = logging.getLogger(__name__)

Builder = Callable[[Ctx], tuple[str, InlineKeyboardMarkup | None]]


class Notifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def user(self, ctx: Ctx, user_id: int, build: Builder) -> bool:
        row = await ctx.repo.get_user(user_id)
        target = ctx.for_lang(row["lang"] if row else None, user_id)
        text, markup = build(target)
        try:
            await self.bot.send_message(user_id, text, reply_markup=markup)
            return True
        except TelegramAPIError as e:  # user blocked the bot, etc. — never break the flow
            log.warning("notify user %s failed: %s", user_id, e)
            return False

    async def users(self, ctx: Ctx, user_ids: list[int], build: Builder) -> None:
        for uid in dict.fromkeys(user_ids):
            await self.user(ctx, uid, build)

    async def admins(self, ctx: Ctx, build: Builder) -> None:
        langs = await ctx.repo.admins_langs(ctx.cfg.admin_ids)
        for admin_id, lang in langs.items():
            text, markup = build(ctx.for_lang(lang, admin_id))
            try:
                await self.bot.send_message(admin_id, text, reply_markup=markup)
            except TelegramAPIError as e:
                log.warning("notify admin %s failed: %s", admin_id, e)
