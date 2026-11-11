"""Builds the dispatcher. Kept separate from __main__ so tests can drive it without a real token."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from .config import Config, load_config
from .i18n import I18n
from .repo import Repo
from .storage import SQLiteStorage
from .bot.handlers import build_router
from .bot.middlewares import ContextMiddleware
from .bot.notify import Notifier
from .webapp.server import start_webapp

log = logging.getLogger(__name__)


def create_dispatcher(repo: Repo, i18n: I18n, cfg: Config, bot: Bot) -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage(repo))
    mw = ContextMiddleware(repo, i18n, cfg)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp["notifier"] = Notifier(bot)
    dp.include_router(build_router())
    return dp


async def setup_bot_profile(bot: Bot, i18n: I18n, cfg: Config) -> None:
    """Commands and descriptions in both languages. Failures here never stop the bot."""
    try:
        for lang_code, lang in (("ru", "ru"), ("uz", "uz"), (None, "ru")):
            commands = [
                BotCommand(command="start", description=i18n.t(lang, "menu.hint").replace(" 👇", "")),
                BotCommand(command="support", description=i18n.t(lang, "menu.support")[2:].strip()),
            ]
            if cfg.webapp_enabled:
                commands.insert(1, BotCommand(command="app", description=i18n.t(lang, "app.cmd")))
            await bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code=lang_code)
        for admin_id in cfg.admin_ids:
            await bot.set_my_commands(
                [BotCommand(command="admin", description="Admin"), BotCommand(command="start", description="Menu")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        # The button next to the message field opens the Mini App (or goes back to the default one).
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Two Fit", web_app=WebAppInfo(url=cfg.webapp_url))
            if cfg.webapp_enabled else MenuButtonDefault()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not set bot commands: %s", e)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can moderate listings. Send /id to the bot to learn your id.")
    for path in (cfg.welcome_image, cfg.logo):
        if not path.exists():
            log.warning("image not found: %s (the bot works without it)", path)

    repo = Repo(cfg.db_path, cfg.commission_percent)
    await repo.connect()
    i18n = I18n()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = create_dispatcher(repo, i18n, cfg, bot)
    await setup_bot_profile(bot, i18n, cfg)
    web_runner = None
    if cfg.webapp_enabled:
        web_runner = await start_webapp(repo, i18n, cfg, bot, dp["notifier"])
    elif cfg.webapp_url:
        log.warning("WEBAPP_URL must start with https:// — the Mini App is off")
    try:
        log.info("Two Fit bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if web_runner is not None:
            await web_runner.cleanup()
        await repo.close()
        await bot.session.close()
