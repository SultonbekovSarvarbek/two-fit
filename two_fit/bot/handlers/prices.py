"""Admin view of 1Fit prices: what the price advice uses, the last check of 1Fit's site, update or ignore."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...money import fmt_money
from ...price_watch import PENDING_KEY, PriceWatcher, changes, plan_name
from ..callbacks import AdmCb, FitPriceCb
from ..context import Ctx
from ..middlewares import IsAdmin
from ..ui import btn, fmt_dt, kb

router = Router(name="prices")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


async def render_prices(ctx: Ctx, watcher: PriceWatcher) -> tuple[str, InlineKeyboardMarkup]:
    current = "\n".join(ctx.t("prices.line", name=plan_name(ctx, d), price=fmt_money(p))
                        for d, p in sorted(ctx.repo.fit_prices.items()))
    checks = await ctx.repo.last_price_checks(1)
    if checks:
        checked, status = fmt_dt(checks[0]["checked_at"]), ctx.t(f"prices.status.{checks[0]['status']}")
    else:
        checked, status = ctx.t("prices.never"), ""
    text = ctx.t("prices.title", current=current, checked=checked, status=status,
                 times=ctx.cfg.fit_watch_times.replace(",", ", "))
    rows = []
    pending = await ctx.repo.get_setting(PENDING_KEY)
    if pending:
        new = {int(k): v for k, v in pending["prices"].items()}
        msg, _ = watcher.change_message(ctx, changes(ctx.repo.fit_prices, new), pending["token"])
        text += "\n\n" + msg
        rows.append([btn(ctx.t("prices.apply_btn"), FitPriceCb(action="apply", tok=pending["token"]))])
        rows.append([btn(ctx.t("prices.ignore_btn"), FitPriceCb(action="ignore", tok=pending["token"]))])
    rows.append([btn(ctx.t("prices.check_btn"), FitPriceCb(action="check"))])
    rows.append([btn(ctx.t("admin.btn.home"), AdmCb(action="home"))])
    return text, kb(*rows)


async def _show(call: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    try:
        await call.message.edit_text(view[0], reply_markup=view[1])
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(view[0], reply_markup=view[1])


@router.message(Command("prices"))
async def cmd_prices(message: Message, ctx: Ctx, price_watcher: PriceWatcher) -> None:
    text, markup = await render_prices(ctx, price_watcher)
    await message.answer(text, reply_markup=markup)


@router.callback_query(FitPriceCb.filter(), F.message)
async def on_prices(call: CallbackQuery, callback_data: FitPriceCb, ctx: Ctx, price_watcher: PriceWatcher) -> None:
    action, tok = callback_data.action, callback_data.tok
    if action == "check":
        await call.answer()
        result = await price_watcher.check(notify=False)  # the admin sees the result right here
        if result["status"] == "ok":
            await call.message.answer(ctx.t("prices.same"))
        elif result["status"] == "applied":
            await call.message.answer(price_watcher.applied_message(ctx, result["changes"]))
        elif result["status"] == "error":
            await call.message.answer(ctx.t("prices.failed", error=result["error"][:200]))
    elif action == "apply":
        if not await price_watcher.apply(tok, ctx.user_id):
            await call.answer(ctx.t("prices.stale"), show_alert=True)
            return
        await call.answer(ctx.t("prices.applied"), show_alert=True)
    elif action == "ignore":
        if not await price_watcher.ignore(tok):
            await call.answer(ctx.t("prices.stale"), show_alert=True)
            return
        await call.answer(ctx.t("prices.ignored"), show_alert=True)
    else:
        await call.answer()
    await _show(call, await render_prices(ctx, price_watcher))
