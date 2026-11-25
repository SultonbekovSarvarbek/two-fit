"""1Fit price watcher: reads 1Fit's prices for Tashkent and keeps the bot's ones current.

They are read several times a day on a schedule and again whenever a seller lists a membership (at most every
few minutes), so a price is always worked out from what 1Fit charges today.

A usual change (every tariff within ±AUTO_APPLY_PERCENT) is taken at once and the admins are told. A bigger
jump, or a tariff that appears or disappears, waits for an admin's «Update prices in the bot» / «Leave as is»:
a misread page must never turn into odd advice for sellers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import aiohttp

from .bot.callbacks import FitPriceCb
from .bot.context import Ctx
from .bot.notify import Notifier
from .bot.ui import btn, kb
from .config import Config
from .i18n import I18n
from .money import fmt_money
from .repo import TZ, Repo
from .usage import DAYS_PER_MONTH

log = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/157.0.0.0 Mobile Safari/537.36")
PRICE_RANGE = (1_000_000, 50_000_000)  # a price outside this is a misread, not a real price
REQUIRED_PLANS = (180, 365, 730)       # memberships that can be re-registered; without them the advice breaks
ERROR_ALERT_AFTER = 2                  # failed checks in a row before the admins hear about it
AUTO_APPLY_PERCENT = 30                # a bigger jump than this waits for an admin
LIVE_TTL = 600                         # seconds: prices read for a seller are reused this long
LIVE_TIMEOUT = 6                       # seconds a seller may wait for 1Fit; then the stored prices are used
PENDING_KEY, NOTIFIED_KEY, ALERTED_KEY = "fit_prices_pending", "fit_prices_notified", "fit_prices_error_alerted"

Fetch = Callable[[], Awaitable[Any]]


class PriceError(Exception):
    pass


def parse_prices(payload: Any) -> tuple[dict[int, int], dict[int, int]]:
    """1Fit's answer -> (actual prices, prices without 1Fit's discount), by tariff length in days.

    The actual price is ``price_with_discount`` when 1Fit gives one, else ``price``. ``monthly_price`` is not
    used: for 24 months it is the installment, not the price of a month of use."""
    items = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise PriceError("no result list")
    actual: dict[int, int] = {}
    full: dict[int, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            days = int(item["days"])
            price = int(item["price"])
            discounted = item.get("price_with_discount")
            current = int(discounted) if discounted else price
        except (KeyError, TypeError, ValueError):
            continue
        if not PRICE_RANGE[0] <= current <= PRICE_RANGE[1]:
            raise PriceError(f"odd price {current} for {days} days")
        actual[days], full[days] = current, price
    missing = [d for d in REQUIRED_PLANS if d not in actual]
    if missing:
        raise PriceError("no tariff for " + ", ".join(map(str, missing)) + " days")
    return actual, full


def changes(old: dict[int, int], new: dict[int, int]) -> list[tuple[int, int | None, int | None]]:
    """[(days, old price or None, new price or None)] for every tariff that differs."""
    return [(d, old.get(d), new.get(d)) for d in sorted(set(old) | set(new)) if old.get(d) != new.get(d)]


def small(diff: list[tuple[int, int | None, int | None]]) -> bool:
    """Every tariff is still there and none moved by more than AUTO_APPLY_PERCENT."""
    return all(old and new and abs(new - old) * 100 <= AUTO_APPLY_PERCENT * old for _, old, new in diff)


def token(prices: dict[int, int]) -> str:
    """Short id of a price set, so an «Update» button from an older message cannot apply newer prices."""
    raw = json.dumps({str(k): prices[k] for k in sorted(prices)})
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def parse_times(raw: str) -> list[tuple[int, int]]:
    times = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        h, m = int(hh), int(mm or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad time {part!r}")
        times.append((h, m))
    return sorted(set(times))


def next_run(now: datetime, times: list[tuple[int, int]]) -> datetime:
    """The next of the given Tashkent times after ``now``."""
    local = now.astimezone(TZ)
    for day in (0, 1):
        base = (local + timedelta(days=day)).replace(second=0, microsecond=0)
        for h, m in times:
            at = base.replace(hour=h, minute=m)
            if at > local:
                return at
    raise ValueError("no times")


def plan_name(ctx: Ctx, days: int) -> str:
    return ctx.t("prices.plan", n=max(1, round(days / DAYS_PER_MONTH)))


class PriceWatcher:
    def __init__(self, repo: Repo, i18n: I18n, cfg: Config, notifier: Notifier, fetch: Fetch | None = None):
        self.repo, self.i18n, self.cfg, self.notifier = repo, i18n, cfg, notifier
        self._fetch = fetch or self._http_fetch
        self.fails = 0
        self._lock = asyncio.Lock()
        self._fresh_at: float | None = None
        self._task: asyncio.Task | None = None

    def ctx(self) -> Ctx:
        return Ctx(self.repo, self.i18n, self.cfg, {"id": 0, "lang": "ru"})

    async def _http_fetch(self) -> Any:
        headers = {"Accept-Language": "ru", "Referer": "https://1fit.app/", "User-Agent": USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=30)
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(self.cfg.fit_api_url) as resp:
                        if resp.status != 200:
                            raise PriceError(f"HTTP {resp.status}")
                        return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, PriceError, ValueError) as e:
                last = e
                await asyncio.sleep(5 * (attempt + 1))
        raise PriceError(str(last) or type(last).__name__)

    # ------------------------------------------------------------------ one check
    async def check(self, notify: bool = True) -> dict[str, Any]:
        """Read 1Fit's prices once. Returns {"status": ok|applied|changed|error, ...}."""
        try:
            actual, full = parse_prices(await self._fetch())
        except Exception as e:  # noqa: BLE001 — any failure is reported, never crashes the bot
            error = str(e) or type(e).__name__
            self.fails += 1
            await self.repo.log_price_check("error", error=error[:300])
            log.warning("1Fit price check failed (%s in a row): %s", self.fails, error)
            if notify and self.fails >= ERROR_ALERT_AFTER:
                await self._alert_error(error)
            return {"status": "error", "error": error}

        self.fails = 0
        return await self._compare(actual, full, notify)

    async def _compare(self, actual: dict[int, int], full: dict[int, int], notify: bool) -> dict[str, Any]:
        diff = changes(self.repo.fit_prices, actual)
        if not diff:
            await self.repo.log_price_check("ok", actual, full)
            await self.repo.set_setting(PENDING_KEY, None)
            await self.repo.set_setting(NOTIFIED_KEY, None)
            return {"status": "ok", "prices": actual}

        if small(diff):  # an ordinary change of 1Fit's prices: sellers get it right away
            await self.repo.set_fit_prices(actual)
            await self.repo.log_price_check("applied", actual, full)
            await self.repo.set_setting(PENDING_KEY, None)
            await self.repo.set_setting(NOTIFIED_KEY, None)
            log.info("1Fit prices taken: %s", actual)
            if notify:
                await self.notifier.admins(self.ctx(), lambda a: (self.applied_message(a, diff), None))
            return {"status": "applied", "prices": actual, "changes": diff}

        await self.repo.log_price_check("changed", actual, full)
        pending = {"token": token(actual), "prices": {str(k): v for k, v in actual.items()},
                   "full": {str(k): v for k, v in full.items()}}
        await self.repo.set_setting(PENDING_KEY, pending)
        # The same new prices are announced once, not five times a day.
        if notify and await self.repo.get_setting(NOTIFIED_KEY) != pending["token"]:
            await self.notifier.admins(self.ctx(), lambda a: self.change_message(a, diff, pending["token"]))
            await self.repo.set_setting(NOTIFIED_KEY, pending["token"])
        return {"status": "changed", "prices": actual, "changes": diff, "token": pending["token"]}

    async def refresh(self) -> None:
        """1Fit's prices right now, before a seller's price is worked out. Read at most every LIVE_TTL seconds;
        when 1Fit does not answer within LIVE_TIMEOUT the stored prices are used and nobody waits longer."""
        if not self.cfg.fit_watch:
            return
        async with self._lock:
            if self._fresh_at is not None and time.monotonic() - self._fresh_at < LIVE_TTL:
                return
            self._fresh_at = time.monotonic()  # a failure also waits: a 1Fit outage must not slow every seller
            try:
                actual, full = parse_prices(await asyncio.wait_for(self._fetch(), LIVE_TIMEOUT))
            except Exception as e:  # noqa: BLE001 — the scheduled checks report failures
                log.warning("1Fit prices for a seller not read, the stored ones are used: %s", str(e) or type(e).__name__)
                return
            self.fails = 0
            await self._compare(actual, full, notify=True)

    def refresh_soon(self) -> None:
        """Start reading 1Fit's prices in the background (a seller opened the form; the price comes later)."""
        if self.cfg.fit_watch and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.refresh())

    def change_lines(self, ctx: Ctx, diff: list[tuple[int, int | None, int | None]]) -> str:
        lines = []
        for days, old, new in diff:
            if old is None:
                lines.append(ctx.t("prices.change_line", name=plan_name(ctx, days), old="—", new=fmt_money(new),
                                   diff=ctx.t("prices.new_plan")))
            elif new is None:
                lines.append(ctx.t("prices.change_line", name=plan_name(ctx, days), old=fmt_money(old), new="—",
                                   diff=ctx.t("prices.gone_plan")))
            else:
                pct = (new - old) * 100 / old
                lines.append(ctx.t("prices.change_line", name=plan_name(ctx, days), old=fmt_money(old),
                                   new=fmt_money(new), diff=f"{pct:+.1f}%".replace(".", ",")))
        return "\n".join(lines)

    def applied_message(self, ctx: Ctx, diff: list[tuple[int, int | None, int | None]]) -> str:
        return ctx.t("prices.auto_applied", lines=self.change_lines(ctx, diff), percent=AUTO_APPLY_PERCENT)

    def change_message(self, ctx: Ctx, diff: list[tuple[int, int | None, int | None]], tok: str):
        return ctx.t("prices.changed", lines=self.change_lines(ctx, diff)), kb(
            [btn(ctx.t("prices.apply_btn"), FitPriceCb(action="apply", tok=tok))],
            [btn(ctx.t("prices.ignore_btn"), FitPriceCb(action="ignore", tok=tok))],
        )

    async def _alert_error(self, error: str) -> None:
        today = datetime.now(TZ).date().isoformat()
        if await self.repo.get_setting(ALERTED_KEY) == today:
            return  # once a day is enough
        await self.repo.set_setting(ALERTED_KEY, today)
        await self.notifier.admins(self.ctx(), lambda a: (a.t("prices.error", n=self.fails, error=error[:200]), None))

    # ------------------------------------------------------------------ admin actions
    async def apply(self, tok: str, admin_id: int) -> bool:
        pending = await self.repo.get_setting(PENDING_KEY)
        if not pending or pending.get("token") != tok:
            return False
        await self.repo.set_fit_prices({int(k): v for k, v in pending["prices"].items()}, admin_id)
        await self.repo.set_setting(PENDING_KEY, None)
        return True

    async def ignore(self, tok: str) -> bool:
        pending = await self.repo.get_setting(PENDING_KEY)
        if not pending or pending.get("token") != tok:
            return False
        await self.repo.set_setting(PENDING_KEY, None)
        return True

    # ------------------------------------------------------------------ schedule
    async def run(self) -> None:
        times = parse_times(self.cfg.fit_watch_times)
        log.info("1Fit price watcher: checks at %s (Tashkent)", ", ".join(f"{h:02d}:{m:02d}" for h, m in times))
        while True:
            at = next_run(datetime.now(TZ), times)
            await asyncio.sleep(max(1.0, (at - datetime.now(TZ)).total_seconds()))
            try:
                await self.check()
            except Exception:  # noqa: BLE001
                log.exception("1Fit price watcher failed")
