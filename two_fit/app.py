"""Builds the dispatcher. Kept separate from __main__ so tests can drive it without a real token."""
from __future__ import annotations

import asyncio
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

from . import ocr
from .config import Config, load_config
from .i18n import I18n
from .price_watch import PriceWatcher, next_run
from .repo import TZ, Repo
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
    dp.pre_checkout_query.outer_middleware(mw)
    dp["notifier"] = Notifier(bot)
    dp["price_watcher"] = PriceWatcher(repo, i18n, cfg, dp["notifier"])
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
                [BotCommand(command="admin", description="Admin"), BotCommand(command="prices", description="1Fit prices"),
                 BotCommand(command="askprice", description="Ask sellers to update prices"),
                 BotCommand(command="start", description="Menu")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        # The button next to the message field opens the Mini App (or goes back to the default one).
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="2FIT", web_app=WebAppInfo(url=cfg.webapp_url))
            if cfg.webapp_enabled else MenuButtonDefault()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not set bot commands: %s", e)


REPRICE_AT = [(0, 10)]  # every day just after midnight in Tashkent


async def reprice_daily(repo: Repo) -> None:
    """Automatic prices follow the days left: a membership listed today is worth less in a month."""
    from datetime import datetime
    while True:
        try:
            changed = await repo.reprice()
            log.info("repriced %d listings", changed)
        except Exception:  # noqa: BLE001 — a failed run must not stop tomorrow's
            log.exception("reprice failed")
        at = next_run(datetime.now(TZ), REPRICE_AT)
        await asyncio.sleep(max(60.0, (at - datetime.now(TZ)).total_seconds()))


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can moderate listings. Send /id to the bot to learn your id.")
    if cfg.listing_fee > 0 and not cfg.payments_on:
        log.warning("neither PAYME_MERCHANT_ID/PAYME_KEY nor PAYMENT_PROVIDER_TOKEN is set: listings are placed for free")
    if cfg.payme_enabled and not cfg.payme_ikpu:
        log.warning("PAYME_IKPU is empty: Payme receipts go without the fiscal details")
    for path in (cfg.welcome_image, cfg.logo):
        if not path.exists():
            log.warning("image not found: %s (the bot works without it)", path)

    repo = Repo(cfg.db_path, cfg.commission_percent, cfg.promo_commission_percent, promo_enabled=cfg.promo_enabled)
    await repo.connect()
    i18n = I18n()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = create_dispatcher(repo, i18n, cfg, bot)
    await setup_bot_profile(bot, i18n, cfg)
    web_runner = None
    if cfg.webapp_enabled or cfg.payme_enabled:  # Payme calls POST /payme on the same server
        web_runner = await start_webapp(repo, i18n, cfg, bot, dp["notifier"])
    if cfg.webapp_url and not cfg.webapp_enabled:
        log.warning("WEBAPP_URL must start with https:// — the Mini App is off")
    reprice_task = asyncio.create_task(reprice_daily(repo))  # runs once now, then every night
    watch_task = None
    if cfg.fit_watch:
        watch_task = asyncio.create_task(dp["price_watcher"].run())
    try:
        log.info("Two Fit bot started")
        log.info("screenshot reading (Tesseract): %s", "on" if ocr.available() else "off")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reprice_task.cancel()
        if watch_task is not None:
            watch_task.cancel()
        if web_runner is not None:
            await web_runner.cleanup()
        await repo.close()
        await bot.session.close()
