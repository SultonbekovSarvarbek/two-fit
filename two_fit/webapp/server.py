"""Telegram Mini App: one static page plus a JSON API.

The API is a thin layer over ``Repo`` — the same rules as in the bot apply (who may do what,
commission consent, no contacts in public texts). Every request is authenticated with the
``initData`` string signed by Telegram; nothing else identifies the user.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from ..bot.callbacks import AdmCb
from ..bot.context import Ctx
from ..bot.notify import Notifier
from ..bot.ui import btn, esc, kb
from ..config import Config
from ..i18n import CITIES, LANGS, I18n
from ..repo import LS, Repo, RepoError, today_local
from ..validators import (
    DESC_MAX,
    MAX_YEARS_AHEAD,
    NAME_MAX,
    PRICE_MAX,
    PRICE_MIN,
    TARIFF_MAX,
    ValidationError,
    normalize_phone,
    parse_description,
    parse_end_date,
    parse_name,
    parse_price,
    parse_tariff,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INIT_DATA_MAX_AGE = 24 * 3600  # seconds; Telegram re-signs initData on every launch
SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024
MY_LIMIT = 100
CATALOG_PAGE_MAX = 50
# Texts the page needs, by key prefix. Everything else stays on the server.
CLIENT_TEXT_PREFIXES = ("app.", "city.", "status.", "commission.note")

# Per-request context. Newer aiohttp wants typed keys; older versions only know strings.
CTX = web.RequestKey("ctx", Ctx) if hasattr(web, "RequestKey") else "ctx"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(code)
        self.status, self.code, self.message, self.field = status, code, message, field


# ------------------------------------------------------------------ serializers
def public_view(listing: dict[str, Any]) -> dict[str, Any]:
    """Only what a buyer may see — never the seller's identity, contacts or screenshot."""
    return {k: listing[k] for k in ("id", "city", "tariff", "end_date", "price", "description")}


def own_view(ctx: Ctx, listing: dict[str, Any]) -> dict[str, Any]:
    status = listing["status"]
    expired = status == LS.APPROVED and Repo.is_expired(listing)
    c = ctx.commission(listing["price"])
    return {
        **public_view(listing),
        "status": "expired" if expired else status,
        "reject_reason": listing["reject_reason"] if status == LS.REJECTED else None,
        "sale_reported": bool(listing["sale_reported_at"]) and status in (LS.APPROVED, LS.RESERVED),
        "seller_name": listing["seller_name"],
        "seller_phone": listing["seller_phone"],
        "has_screenshot": bool(listing["screenshot_file_id"]),
        "commission": c.commission,
        "net": c.net,
        "can_edit": status in LS.SELLER_EDITABLE,
        "can_withdraw": status in (LS.PENDING, LS.APPROVED),
        "can_report": status in (LS.APPROVED, LS.RESERVED) and not listing["sale_reported_at"],
    }


class WebApp:
    def __init__(self, repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier):
        self.repo, self.i18n, self.cfg, self.bot, self.notifier = repo, i18n, cfg, bot, notifier

    # ------------------------------------------------------------------ plumbing
    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.errors_mw, self.auth_mw], client_max_size=64 * 1024)
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/bootstrap", self.bootstrap)
        r.add_post("/api/lang", self.set_lang)
        r.add_get("/api/catalog", self.catalog)
        r.add_get("/api/my", self.my)
        r.add_post("/api/screenshot", self.screenshot)
        r.add_post("/api/listings", self.create)
        r.add_get(r"/api/listings/{id:\d+}", self.listing)
        r.add_put(r"/api/listings/{id:\d+}", self.update)
        r.add_post(r"/api/listings/{id:\d+}/request", self.request_purchase)
        r.add_post(r"/api/listings/{id:\d+}/withdraw", self.withdraw)
        r.add_post(r"/api/listings/{id:\d+}/report", self.report)
        return app

    @web.middleware
    async def errors_mw(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as e:
            body = {"error": e.code, "message": e.message}
            if e.field:
                body["field"] = e.field
            return web.json_response(body, status=e.status)
        except web.HTTPException as e:
            if not request.path.startswith("/api/"):
                raise
            return web.json_response({"error": "http", "message": e.reason}, status=e.status)
        except Exception:  # noqa: BLE001
            log.exception("webapp request failed: %s %s", request.method, request.path)
            ctx = request.get(CTX)
            message = ctx.t("common.error") if ctx else self.i18n.t(None, "app.err.network")
            return web.json_response({"error": "internal", "message": message}, status=500)

    @web.middleware
    async def auth_mw(self, request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        scheme, _, init_data = request.headers.get("Authorization", "").partition(" ")
        denied = ApiError(401, "unauthorized", self.i18n.t(None, "app.err.auth"))
        if scheme.lower() != "tma" or not init_data:
            raise denied
        try:
            data = safe_parse_webapp_init_data(self.cfg.bot_token, init_data)
        except ValueError:
            raise denied from None
        age = (datetime.now(timezone.utc) - data.auth_date).total_seconds()
        tg_user = data.user
        if tg_user is None or tg_user.is_bot or not -300 <= age <= INIT_DATA_MAX_AGE:
            raise denied
        full_name = " ".join(x for x in (tg_user.first_name, tg_user.last_name) if x)
        user = await self.repo.touch_user(tg_user.id, tg_user.username, full_name)
        if user["lang"] is None:
            # Opened before choosing a language in the chat: follow the Telegram client language
            # for this request only — the bot will still ask once.
            user = {**user, "lang": "uz" if tg_user.language_code == "uz" else "ru", "lang_guessed": True}
        request[CTX] = Ctx(self.repo, self.i18n, self.cfg, user)
        return await handler(request)

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "Bad request")
        return body

    def _sign(self, user_id: int, file_id: str) -> str:
        key = hashlib.sha256(b"screenshot:" + self.cfg.bot_token.encode()).digest()
        return hmac.new(key, f"{user_id}:{file_id}".encode(), hashlib.sha256).hexdigest()[:32]

    async def _notify_admins(self, ctx: Ctx, key: str, listing_id: int, action: str = "open", **fmt: Any) -> None:
        await self.notifier.admins(ctx, lambda a: (
            a.t(key, id=listing_id, **fmt),
            kb([btn(a.t("admin.open_btn", id=listing_id), AdmCb(action=action, id=listing_id))]),
        ))

    # ------------------------------------------------------------------ pages
    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    # ------------------------------------------------------------------ profile
    def _bootstrap(self, ctx: Ctx) -> dict[str, Any]:
        today = today_local()
        texts = {k: v for k, v in self.i18n.texts[ctx.lang].items() if k.startswith(CLIENT_TEXT_PREFIXES)}
        return {
            "lang": ctx.lang,
            "langs": [{"code": code, "name": self.i18n.t(code, f"lang.{code}")} for code in LANGS],
            "texts": texts,
            "vars": {"operator": self.cfg.operator, "percent": self.cfg.commission_percent},
            "support_url": self.cfg.support_url,
            "cities": list(CITIES),
            "user": {"name": ctx.user.get("contact_name") or "", "phone": ctx.user.get("contact_phone") or ""},
            "limits": {
                "price_min": PRICE_MIN, "price_max": PRICE_MAX, "desc_max": DESC_MAX,
                "tariff_max": TARIFF_MAX, "name_max": NAME_MAX,
                "date_min": today.isoformat(), "date_max": f"{today.year + MAX_YEARS_AHEAD}-12-31",
            },
        }

    async def bootstrap(self, request: web.Request) -> web.Response:
        return web.json_response(self._bootstrap(request[CTX]))

    async def set_lang(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        lang = (await self._json(request)).get("lang")
        if lang not in LANGS:
            raise ApiError(400, "bad_lang", "Unknown language")
        await self.repo.set_lang(ctx.user_id, lang)
        ctx.user["lang"] = lang
        return web.json_response(self._bootstrap(ctx))

    # ------------------------------------------------------------------ catalog
    async def catalog(self, request: web.Request) -> web.Response:
        q = request.query
        city = q.get("city", "")
        if city and city not in CITIES:
            city = ""
        try:
            offset = max(0, int(q.get("offset", "0")))
            limit = min(CATALOG_PAGE_MAX, max(1, int(q.get("limit", "20"))))
        except ValueError:
            raise ApiError(400, "bad_request", "Bad request") from None
        rows, total = await self.repo.catalog(city or None, limit, offset)
        cities = await self.repo.catalog_cities()
        return web.json_response({
            "items": [public_view(r) for r in rows],
            "total": total,
            "city": city,
            "cities": [{"code": code, "count": n} for code, n in cities],
        })

    async def listing(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        listing = await self.repo.get_listing(listing_id)
        if listing and listing["seller_id"] == ctx.user_id:
            return web.json_response({"listing": own_view(ctx, listing), "own": True})
        if not self.repo.is_available(listing):
            raise ApiError(404, "not_available", ctx.t("common.not_available"))
        requested = bool(await self.repo.active_request(listing_id, ctx.user_id))
        return web.json_response({"listing": public_view(listing), "own": False, "requested": requested})

    async def request_purchase(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        body = await self._json(request)
        name = self._field(ctx, "name", parse_name, body.get("name"))
        phone = self._field(ctx, "phone", normalize_phone, body.get("phone"))
        try:
            request_id = await self.repo.create_request(listing_id, ctx.user_id, name, phone)
        except RepoError as e:
            key = {"own": "buy.own", "duplicate": "buy.duplicate"}.get(e.code, "common.not_available")
            raise ApiError(409, e.code, ctx.t(key, id=listing_id)) from None
        await self.repo.save_contact(ctx.user_id, name, phone)
        await self._notify_admins(ctx, "admin.n.new_request", listing_id, action="reqs",
                                  rid=request_id, name=esc(name), phone=esc(phone))
        return web.json_response({"request_id": request_id})

    # ------------------------------------------------------------------ selling
    @staticmethod
    def _field(ctx: Ctx, field: str, parse, value: Any, *args: Any) -> Any:
        try:
            return parse(value if isinstance(value, str) else None, *args)
        except ValidationError as e:
            fmt = {"max": DESC_MAX} if e.code == "bad_desc_long" else {}
            raise ApiError(400, e.code, ctx.t(f"err.{e.code}", **fmt), field) from None

    @staticmethod
    def _iso_date(value: str | None, today: date) -> str:
        try:
            parsed = date.fromisoformat(value or "")
        except ValueError:
            raise ValidationError("bad_date_format") from None
        return parse_end_date(parsed.strftime("%d.%m.%Y"), today).isoformat()

    def _draft(self, ctx: Ctx, body: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        city = body.get("city")
        if city not in CITIES:
            raise ApiError(400, "bad_city", ctx.t("app.err.city"), "city")
        raw_price = body.get("price")
        if isinstance(raw_price, int) and not isinstance(raw_price, bool):
            raw_price = str(raw_price)
        draft = {
            "city": city,
            "tariff": self._field(ctx, "tariff", parse_tariff, body.get("tariff")),
            "end_date": self._field(ctx, "end_date", self._iso_date, body.get("end_date"), today_local()),
            "price": self._field(ctx, "price", parse_price, raw_price),
            "description": self._field(ctx, "description", parse_description, body.get("description") or ""),
            "seller_name": self._field(ctx, "name", parse_name, body.get("name")),
            "seller_phone": self._field(ctx, "phone", normalize_phone, body.get("phone")),
        }
        # The seller ticks the consent for one exact price; a different price means no consent.
        if body.get("accepted_price") != draft["price"]:
            raise ApiError(400, "commission_not_accepted", ctx.t("app.err.accept"), "accept")
        draft["commission_accepted_price"] = draft["price"]

        shot = body.get("screenshot")
        if shot == "keep":
            draft["screenshot_file_id"] = existing["screenshot_file_id"] if existing else None
        elif isinstance(shot, dict):
            file_id, sig = str(shot.get("file_id", "")), str(shot.get("sig", ""))
            if not file_id or not hmac.compare_digest(sig, self._sign(ctx.user_id, file_id)):
                raise ApiError(400, "bad_photo", ctx.t("app.err.photo"), "screenshot")
            draft["screenshot_file_id"] = file_id
        else:
            draft["screenshot_file_id"] = None
        return draft

    async def screenshot(self, request: web.Request) -> web.Response:
        """The picture goes to the seller's own chat with the bot; Telegram then stores it for the admin."""
        ctx = request[CTX]
        bad = ApiError(400, "bad_photo", ctx.t("app.err.photo"), "screenshot")
        if not request.content_type.startswith("multipart/"):
            raise bad
        part = await (await request.multipart()).next()
        if part is None or part.name != "file" or not part.headers.get("Content-Type", "").startswith("image/"):
            raise bad
        buf = bytearray()
        while chunk := await part.read_chunk(64 * 1024):
            buf += chunk
            if len(buf) > SCREENSHOT_MAX_BYTES:
                raise bad
        if not buf:
            raise bad
        try:
            sent = await self.bot.send_photo(
                ctx.user_id, BufferedInputFile(bytes(buf), filename="screenshot.jpg"),
                caption=ctx.t("app.screenshot_caption"),
            )
        except TelegramAPIError as e:
            log.warning("screenshot upload for %s failed: %s", ctx.user_id, e)
            raise bad from None
        if not sent.photo:
            raise bad
        file_id = sent.photo[-1].file_id
        return web.json_response({"file_id": file_id, "sig": self._sign(ctx.user_id, file_id)})

    async def create(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        draft = self._draft(ctx, await self._json(request))
        listing_id = await self.repo.create_listing(ctx.user_id, ctx.lang, draft)
        await self.repo.save_contact(ctx.user_id, draft["seller_name"], draft["seller_phone"])
        await self._notify_admins(ctx, "admin.n.new_listing", listing_id)
        return web.json_response({"id": listing_id}, status=201)

    async def _own(self, ctx: Ctx, listing_id: int) -> dict[str, Any]:
        listing = await self.repo.get_listing(listing_id)
        if not listing or listing["seller_id"] != ctx.user_id:  # other people's listings do not exist here
            raise ApiError(404, "not_found", ctx.t("common.not_available"))
        return listing

    async def update(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        existing = await self._own(ctx, listing_id)
        draft = self._draft(ctx, await self._json(request), existing)
        try:
            await self.repo.update_listing_by_seller(listing_id, ctx.user_id, ctx.lang, draft)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("sell.edit_blocked")) from None
        await self.repo.save_contact(ctx.user_id, draft["seller_name"], draft["seller_phone"])
        await self._notify_admins(ctx, "admin.n.edited_listing", listing_id)
        return web.json_response({"id": listing_id})

    # ------------------------------------------------------------------ my listings
    async def my(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        rows, total = await self.repo.seller_listings(ctx.user_id, MY_LIMIT, 0)
        return web.json_response({"items": [own_view(ctx, r) for r in rows], "total": total})

    async def withdraw(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        await self._own(ctx, listing_id)
        try:
            buyers = await self.repo.withdraw_by_seller(listing_id, ctx.user_id)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("my.cant_withdraw")) from None
        await self.notifier.users(ctx, buyers, lambda u: (u.t("notify.request_closed", id=listing_id), None))
        await self._notify_admins(ctx, "admin.n.withdrawn", listing_id)
        return web.json_response({"listing": own_view(ctx, await self.repo.get_listing(listing_id))})

    async def report(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        await self._own(ctx, listing_id)
        try:
            first = await self.repo.report_sale(listing_id, ctx.user_id)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("my.cant_report")) from None
        if first:
            await self._notify_admins(ctx, "admin.n.sale_reported", listing_id)
        return web.json_response({"listing": own_view(ctx, await self.repo.get_listing(listing_id))})


def create_webapp(repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier) -> web.Application:
    return WebApp(repo, i18n, cfg, bot, notifier).build()


async def start_webapp(repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier) -> web.AppRunner:
    runner = web.AppRunner(create_webapp(repo, i18n, cfg, bot, notifier), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.webapp_host, cfg.webapp_port).start()
    log.info("Mini App is served on %s:%s for %s", cfg.webapp_host, cfg.webapp_port, cfg.webapp_url)
    return runner
