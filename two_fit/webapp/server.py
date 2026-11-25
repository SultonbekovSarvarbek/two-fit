"""Telegram Mini App: one static page plus a JSON API.

The API is a thin layer over ``Repo`` — the same rules as in the bot apply (who may do what,
the listing fee before moderation, no contacts in descriptions). Every request is authenticated with the
``initData`` string signed by Telegram; nothing else identifies the user.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import hmac
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from ..clubs import CLUBS, FIT, is_fit
from ..bot.callbacks import AdmCb
from ..bot.context import Ctx
from ..bot.notify import Notifier
from ..bot.payments import announce_awaiting, announce_paid, announce_refunded, fee_invoice_link
from ..bot.ui import btn, kb, price_advice
from ..config import Config
from .. import ocr
from ..payme import Payme
from ..price_watch import PriceWatcher
from ..money import fmt_money, round_k
from ..ocr import MAX_SCREENSHOTS
from ..i18n import CITIES, LANGS, LEGACY_CITIES, I18n
from ..repo import LK, LS, Repo, RepoError, today_local
from ..pricing import DISCOUNT_PERCENT, advise
from ..usage import card_end, remaining, usage
from ..validators import (
    DESC_MAX,
    MAX_YEARS_AHEAD,
    MAX_YEARS_BACK,
    NAME_MAX,
    PAYMENTS_LEFT_MAX,
    PRICE_MAX,
    PRICE_MIN,
    TARIFF_MAX,
    TARIFF_PLANS,
    ValidationError,
    check_plan_end,
    check_plan_span,
    normalize_phone,
    parse_description,
    parse_end_date,
    parse_bonus_days,
    parse_freeze_days,
    parse_monthly,
    parse_payments_left,
    parse_start_date,
    parse_name,
    parse_price,
    parse_promo_code,
    parse_tariff,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"
LEGAL_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — 2FIT</title>
<style>
:root {{ color-scheme: light dark; --fg: #1d1d1f; --bg: #fff; --muted: #6e6e73; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg: #f2f2f2; --bg: #111; --muted: #a1a1a6; }} }}
body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; overflow-wrap: anywhere; }}
h1 {{ font-size: 24px; line-height: 1.25; }} h2 {{ font-size: 18px; margin-top: 28px; }}
a {{ color: inherit; }} .langs {{ color: var(--muted); font-size: 14px; }}
</style></head><body><main><p class="langs"><a href="?lang=ru">Русский</a> · <a href="?lang=uz">Ўзбекча</a></p>
{body}</main></body></html>"""
INIT_DATA_MAX_AGE = 24 * 3600  # seconds; Telegram re-signs initData on every launch
SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024
MY_LIMIT = 100
CATALOG_PAGE_MAX = 50
# Texts the page needs, by key prefix. Everything else stays on the server.
CLIENT_TEXT_PREFIXES = ("app.", "city.", "status.")

# Per-request context. Newer aiohttp wants typed keys; older versions only know strings.
CTX = web.RequestKey("ctx", Ctx) if hasattr(web, "RequestKey") else "ctx"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(code)
        self.status, self.code, self.message, self.field = status, code, message, field


# ------------------------------------------------------------------ serializers
def month_cost(listing: dict[str, Any], fit_prices: dict[int, int] | None) -> dict[str, Any] | None:
    """What the buyer pays per month of use next to what 1Fit charges; None when it is not cheaper.
    A price far too low to be the whole term is flagged ``suspicious`` instead of shown as a discount."""
    if listing["kind"] == LK.PROMO or not is_fit(listing):  # compared with 1Fit's prices: 1Fit only
        return None
    installment = listing["paid_in_full"] == 0
    a = advise(listing["end_date"], today_local(), fit_prices,
               listing["monthly_payment"] if installment else None, listing["payments_left"] if installment else None,
               plan_days=listing["tariff_days"])
    if a and a.suspicious(listing["price"]):
        return {"suspicious": True}
    if not a or a.saving(listing["price"]) <= 0:
        return None
    price = listing["price"]
    return {"total": round_k(a.total(price)), "fit_total": round_k(a.fit_value), "saving": a.saving(price),
            "saved": round_k(a.saved(price))}


def price_total(listing: dict[str, Any]) -> int:
    """The price buyers see. Installments: the total left to pay, monthly × payments left (557 000 × 11);
    otherwise exactly what the seller set."""
    if listing["paid_in_full"] == 0 and listing["monthly_payment"] and listing["payments_left"]:
        return listing["monthly_payment"] * listing["payments_left"]
    return listing["price"]


def seller_view(listing: dict[str, Any], seller: dict[str, Any] | None) -> dict[str, Any]:
    """How buyers reach the seller: the name and phone from the listing, today's Telegram username."""
    return {"name": listing["seller_name"], "phone": listing["seller_phone"],
            "username": (seller or {}).get("username"), "user_id": listing["seller_id"]}


def public_view(listing: dict[str, Any], fit_prices: dict[int, int] | None = None) -> dict[str, Any]:
    """What a buyer sees in the list — the contacts come with the listing itself, the screenshot never."""
    view = {k: listing[k] for k in ("id", "city", "tariff", "end_date", "start_date", "price", "description")}
    view["price_negotiable"] = bool(listing["price_negotiable"])
    view["kind"] = listing["kind"]  # the promo code itself is never public
    view["club"] = listing["club"] or FIT  # whose membership: 1fit, ozone, embassy, ufc
    view["sold"] = listing["status"] == LS.SOLD  # shown a month with the «Sold» badge, never with contacts
    view["tariff_days"] = listing["tariff_days"]  # 180 / 365 / 730, None on old listings
    view["freeze_days"] = listing["freeze_days"]  # unused freeze days the buyer gets; 0 none, None not asked
    view["bonus_days"] = listing["bonus_days"] or 0  # 1Fit bonus days, already in end_date
    view["card_end_date"] = card_end(listing)       # the end date on the 1Fit card, without them
    # the tariff counts from the card's end: bonus days are not part of it
    view["usage"] = usage(listing["start_date"], view["card_end_date"], today_local(), listing["tariff_days"])
    view["left"] = remaining(listing["end_date"], today_local())
    # Buyers never see the installments, only the whole sum they pay: the price plus the payments still due to 1Fit
    view["price_total"] = price_total(listing)
    view["month_cost"] = month_cost(listing, fit_prices)
    return view


def own_view(ctx: Ctx, listing: dict[str, Any]) -> dict[str, Any]:
    status = listing["status"]
    expired = status == LS.APPROVED and Repo.is_expired(listing)
    unpaid = status == LS.UNPAID and Repo.fee_due(listing)
    return {
        **public_view(listing, ctx.repo.fit_prices),
        # the seller's own answers, to edit them: never in the public view
        "paid_in_full": None if listing["paid_in_full"] is None else bool(listing["paid_in_full"]),
        "monthly_payment": listing["monthly_payment"] if listing["paid_in_full"] == 0 else None,
        "payments_left": listing["payments_left"] if listing["paid_in_full"] == 0 else None,
        "promo_code": listing["promo_code"],
        "status": "expired" if expired else status,
        "reject_reason": listing["reject_reason"] if status == LS.REJECTED else None,
        "fee": listing["fee_amount"] or 0,
        "fee_due": Repo.fee_due(listing),  # an edit sends it back unpaid until it is paid
        "can_pay": unpaid and ctx.cfg.can_take_fee,
        "seller_name": listing["seller_name"],
        "seller_phone": listing["seller_phone"],
        "has_screenshot": bool(listing["screenshot_file_id"]),
        "screenshot_count": len(Repo.screenshot_ids(listing)),
        "can_edit": status in LS.SELLER_EDITABLE,
        "can_withdraw": status in LS.SELLER_WITHDRAWABLE,
        "can_sold": status in LS.SELLER_SELLABLE and not expired,
    }


class WebApp:
    def __init__(self, repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier,
                 prices: PriceWatcher | None = None):
        self.repo, self.i18n, self.cfg, self.bot, self.notifier = repo, i18n, cfg, bot, notifier
        # 1Fit's prices are read again before a seller's price is worked out
        self.prices = prices or PriceWatcher(repo, i18n, cfg, notifier)
        self.ocr_on = ocr.available()  # Tesseract installed? Without it screenshots are only looked at by the admin
        if not self.ocr_on:
            log.info("Tesseract is not installed: screenshots are not read automatically")
        system = Ctx(repo, i18n, cfg, {"id": 0, "lang": "ru"})  # notifications go out in each one's language
        self.payme = Payme(
            repo, cfg, receipt_title=lambda lid: f'{i18n.t("ru", "pay.label")} №{lid}',
            on_paid=lambda listing, first, amount: announce_paid(notifier, system, listing, first, amount),
            on_refunded=lambda listing, amount: announce_refunded(notifier, system, listing, amount),
        )

    # ------------------------------------------------------------------ plumbing
    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.errors_mw, self.auth_mw], client_max_size=64 * 1024)
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/clubs", STATIC_DIR / "clubs")  # the clubs' logos
        r.add_get("/offer", self.legal)  # opened in the browser: no Telegram login needed
        r.add_get("/terms", self.legal)
        r.add_get("/api/bootstrap", self.bootstrap)
        r.add_post("/api/lang", self.set_lang)
        r.add_get("/api/catalog", self.catalog)
        r.add_get("/api/my", self.my)
        r.add_post("/api/screenshot", self.screenshot)
        r.add_post("/api/listings", self.create)
        r.add_post("/api/advice", self.advice)
        r.add_get(r"/api/listings/{id:\d+}", self.listing)
        r.add_put(r"/api/listings/{id:\d+}", self.update)
        r.add_post(r"/api/listings/{id:\d+}/invoice", self.invoice)
        r.add_post(r"/api/listings/{id:\d+}/withdraw", self.withdraw)
        r.add_post(r"/api/listings/{id:\d+}/sold", self.sold)
        r.add_post(r"/api/listings/{id:\d+}/contact", self.contact)
        if self.cfg.payme_enabled:
            r.add_post("/payme", self.payme_rpc)  # Payme's own Basic auth, not initData
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
    async def legal(self, request: web.Request) -> web.Response:
        """The public offer or the user agreement, with the requisites from the settings."""
        doc = request.path.strip("/")
        lang = request.query.get("lang", "ru")
        lang = lang if lang in LANGS else "ru"
        body = (LEGAL_DIR / f"{doc}.{lang}.html").read_text(encoding="utf-8")
        missing = "[не указано]" if lang == "ru" else "[кўрсатилмаган]"
        c = self.cfg
        values = {"LEGAL_NAME": c.legal_name, "LEGAL_INN": c.legal_inn, "LEGAL_ADDRESS": c.legal_address,
                  "LEGAL_BANK": c.legal_bank, "LEGAL_ACCOUNT": c.legal_account, "LEGAL_MFO": c.legal_mfo,
                  "LEGAL_PHONE": c.legal_phone, "LEGAL_EMAIL": c.legal_email, "EFFECTIVE_DATE": c.legal_date,
                  "FEE": fmt_money(c.listing_fee), "OPERATOR": c.operator}
        for key, value in values.items():
            if not value:  # a requisite not given: its own line goes, a mention inside a sentence is marked
                body = re.sub(r"<p>[^<]*\{\{" + key + r"\}\}[^<]*</p>\n?", "", body)
            body = body.replace("{{" + key + "}}", html.escape(value) if value else missing)
        # the documents link to each other as «offer» / «terms»: keep the language
        body = body.replace('href="offer"', f'href="offer?lang={lang}"').replace('href="terms"', f'href="terms?lang={lang}"')
        title = self.i18n.t(lang, "legal.offer" if doc == "offer" else "legal.terms")
        return web.Response(text=LEGAL_PAGE.format(lang=lang, title=html.escape(title), body=body),
                            content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })

    async def payme_rpc(self, request: web.Request) -> web.Response:
        """Payme Merchant API. Always HTTP 200 with a JSON-RPC result or error, as Payme expects."""
        answer = await self.payme.handle(await request.read(), request.headers.get("Authorization"))
        return web.json_response(answer)

    # ------------------------------------------------------------------ profile
    def _bootstrap(self, ctx: Ctx) -> dict[str, Any]:
        today = today_local()
        texts = {k: v for k, v in self.i18n.texts[ctx.lang].items() if k.startswith(CLIENT_TEXT_PREFIXES)}
        return {
            "lang": ctx.lang,
            "langs": [{"code": code, "name": self.i18n.t(code, f"lang.{code}")} for code in LANGS],
            "texts": texts,
            "vars": {"operator": self.cfg.operator, "fee": self.cfg.listing_fee if self.cfg.fee_enabled else 0,
                     "fee_text": ctx.fee_text(), "instagram": self.cfg.instagram_url,
                     "promo_enabled": self.cfg.promo_enabled, "auto_price": self.cfg.auto_price,
                     "discount": DISCOUNT_PERCENT, "shot_required": self.cfg.screenshot_required,
                     "ocr": self.ocr_on, "clubs": [{"code": k, "name": v} for k, v in CLUBS.items()],
                     "pay_manual": self.cfg.fee_manual and self.cfg.fee_enabled},
            "support_url": self.cfg.support_url,
            "cities": list(CITIES),
            "user": {"name": ctx.user.get("contact_name") or ctx.user.get("tg_name") or "",
                     "phone": ctx.user.get("contact_phone") or "", "username": ctx.user.get("username") or ""},
            "limits": {
                "price_min": PRICE_MIN, "price_max": PRICE_MAX, "desc_max": DESC_MAX,
                "tariff_max": TARIFF_MAX, "name_max": NAME_MAX,
                "date_min": today.isoformat(), "date_max": f"{today.year + MAX_YEARS_AHEAD}-12-31",
                "start_min": f"{today.year - MAX_YEARS_BACK}-01-01", "start_max": today.isoformat(),
                "payments_left_max": PAYMENTS_LEFT_MAX, "plans": list(TARIFF_PLANS),
            },
        }

    async def bootstrap(self, request: web.Request) -> web.Response:
        self.prices.refresh_soon()  # the seller may be about to list: today's 1Fit prices get ready
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
        if city and city not in CITIES + LEGACY_CITIES:
            city = ""
        try:
            offset = max(0, int(q.get("offset", "0")))
            limit = min(CATALOG_PAGE_MAX, max(1, int(q.get("limit", "20"))))
        except ValueError:
            raise ApiError(400, "bad_request", "Bad request") from None
        club = q.get("club", "")
        if club not in CLUBS:
            club = ""
        rows, total = await self.repo.catalog(city or None, limit, offset, club=club or None)
        cities = await self.repo.catalog_cities()
        clubs = await self.repo.catalog_clubs()
        return web.json_response({
            "items": [public_view(r, self.repo.fit_prices) for r in rows],
            "total": total,
            "city": city,
            "club": club,
            "cities": [{"code": code, "count": n} for code, n in cities],
            "clubs": [{"code": code, "count": n} for code, n in clubs],
        })

    async def listing(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        listing = await self.repo.get_listing(listing_id)
        if listing and listing["seller_id"] == ctx.user_id:
            return web.json_response({"listing": own_view(ctx, listing), "own": True})
        if self.repo.is_sold_shown(listing):  # still in the list as «Sold»: the seller is no longer reachable
            return web.json_response({"listing": public_view(listing, self.repo.fit_prices), "own": False})
        if not self.repo.is_available(listing):
            raise ApiError(404, "not_available", ctx.t("common.not_available"))
        view = public_view(listing, self.repo.fit_prices)
        view["seller"] = seller_view(listing, await self.repo.get_user(listing["seller_id"]))
        await self.repo.log_contact(listing_id, ctx.user_id, "view")  # the card shows the contacts
        return web.json_response({"listing": view, "own": False})

    async def contact(self, request: web.Request) -> web.Response:
        """A buyer tapped the seller's phone, Telegram or «Call»: the admin can later ask them about the deal."""
        ctx = request[CTX]
        action = (await self._json(request)).get("action")
        if action not in ("call", "telegram", "copy"):
            raise ApiError(400, "bad_request", "Bad request")
        listing_id = int(request.match_info["id"])
        if not self.repo.is_available(await self.repo.get_listing(listing_id)):
            raise ApiError(404, "not_available", ctx.t("common.not_available"))
        await self.repo.log_contact(listing_id, ctx.user_id, action)
        return web.json_response({"ok": True})

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

    @staticmethod
    def _iso_start(value: str | None, today: date, end_iso: str) -> str | None:
        if not value:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValidationError("bad_date_format") from None
        return parse_start_date(parsed.strftime("%d.%m.%Y"), today, date.fromisoformat(end_iso)).isoformat()

    def _draft(self, ctx: Ctx, body: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
        city = body.get("city")
        if city not in CITIES:
            raise ApiError(400, "bad_city", ctx.t("app.err.city"), "city")
        def number(key: str) -> Any:  # the form may send 2000000 or "2 000 000"
            value = body.get(key)
            return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

        raw_price = number("price")
        draft = {
            "city": city,
            "end_date": self._field(ctx, "end_date", self._iso_date, body.get("end_date"), today_local()),
            "price_negotiable": body.get("price_negotiable") is True,
            "description": self._field(ctx, "description", parse_description, body.get("description") or ""),
        }
        draft["seller_name"], draft["seller_phone"] = self._seller_contact(ctx, body, existing)
        # What is sold stays as it was listed; a new listing must say it.
        kind = existing["kind"] if existing else body.get("kind")
        if kind not in LK.ALL or (kind == LK.PROMO and not existing and not self.cfg.promo_enabled):
            raise ApiError(400, "bad_kind", ctx.t("app.err.kind"), "kind")
        draft["kind"] = kind
        if kind == LK.PROMO:
            draft["tariff"] = self._field(ctx, "tariff", parse_tariff, body.get("tariff"))  # what it gives
            draft["promo_code"] = self._field(ctx, "promo_code", parse_promo_code, body.get("promo_code"))
        else:
            # Older app versions do not ask: everything was 1Fit then. The new form makes the seller pick.
            club = body.get("club") or (existing["club"] if existing else FIT)
            if club not in CLUBS:
                raise ApiError(400, "bad_club", ctx.t("app.err.club"), "club")
            draft["club"] = club
            if club == FIT:
                self._membership_fields(ctx, body, draft, number)
            else:
                self._club_fields(ctx, body, draft)
        a = price_advice(ctx, draft)  # 1Fit only: other clubs name their own price
        if kind != LK.PROMO and draft.get("paid_in_full") is False:
            # Installments: the price is what is left to pay 1Fit, worked out here — the app sends no price.
            total = draft["monthly_payment"] * draft["payments_left"]
            draft["price"] = self._field(ctx, "monthly_payment", parse_price, str(total))
            a = None  # no recommended price, no limits: the sum is fixed by 1Fit
        elif a and self.cfg.auto_price:
            # The price is the recommended one, worked out here: whatever the app sent is not trusted.
            # It then keeps following the days left: Repo.reprice lowers it every day.
            draft["price"] = a.recommended
            draft["price_auto"] = True
        else:
            draft["price"] = self._field(ctx, "price", parse_price, raw_price)
        # The seller's own price is taken as it is: no suggested price, no limits against 1Fit.

        # Up to 3 screenshots: {file_id, sig} for a new upload, "keep" for the ones already on the listing.
        shots = body.get("screenshots")
        if shots is None:  # older app versions send one
            shots = [body.get("screenshot")] if body.get("screenshot") else []
        if not isinstance(shots, list) or len(shots) > MAX_SCREENSHOTS:
            raise ApiError(400, "bad_photo", ctx.t("app.err.photo"), "screenshot")
        ids, new_ids, keep = [], [], False
        for shot in shots:
            if shot == "keep":
                keep = True
                ids += Repo.screenshot_ids(existing) if existing else []
            elif isinstance(shot, dict):
                file_id, sig = str(shot.get("file_id", "")), str(shot.get("sig", ""))
                if not file_id or not hmac.compare_digest(sig, self._sign(ctx.user_id, file_id)):
                    raise ApiError(400, "bad_photo", ctx.t("app.err.photo"), "screenshot")
                ids.append(file_id)
                new_ids.append(file_id)
            else:
                raise ApiError(400, "bad_photo", ctx.t("app.err.photo"), "screenshot")
        ids = list(dict.fromkeys(ids))[:MAX_SCREENSHOTS]
        draft["screenshot_file_ids"] = ids
        draft["screenshot_file_id"] = ids[0] if ids else None
        draft["new_screenshots"] = new_ids  # what was read from them is added in create/update
        draft["ocr"] = Repo.ocr_of(existing) if keep and existing else {}
        if self.cfg.screenshot_required and not draft["screenshot_file_id"]:
            # The admin checks the end date and the freeze days against the 1Fit card: only they see it.
            raise ApiError(400, "screenshot_required", ctx.t("app.err.shot_required"), "screenshot")
        return draft

    def _seller_contact(self, ctx: Ctx, body: dict[str, Any], existing: dict[str, Any] | None) -> tuple[str, str]:
        """The name and phone every buyer sees: both required, both checked like in the bot."""
        return (self._field(ctx, "name", parse_name, body.get("name")),
                self._field(ctx, "phone", normalize_phone, body.get("phone")))

    def _bonus(self, ctx: Ctx, body: dict[str, Any]) -> int:
        """1Fit bonus days, turned on after the main membership: they come on top of the card's end date."""
        bonus = body.get("bonus_days")
        if bonus in (None, 0, "", "0"):
            return 0
        return self._field(ctx, "bonus_days", parse_bonus_days, str(bonus) if isinstance(bonus, int) else bonus)

    def _freeze(self, ctx: Ctx, body: dict[str, Any]) -> int:
        """Freeze days: 0 = none; a number = that many unused days go to the buyer."""
        freeze = body.get("freeze_days")
        if freeze in (None, 0, "", "0"):
            return 0
        return self._field(ctx, "freeze_days", parse_freeze_days, str(freeze) if isinstance(freeze, int) else freeze)

    def _club_fields(self, ctx: Ctx, body: dict[str, Any], draft: dict[str, Any]) -> None:
        """Another club than 1Fit: the end date and the freeze days; no 1Fit tariff, bonus days or installments."""
        draft["freeze_days"] = self._freeze(ctx, body)
        draft["start_date"] = None
        draft["paid_in_full"] = True
        draft["bonus_days"] = 0

    def _membership_fields(self, ctx: Ctx, body: dict[str, Any], draft: dict[str, Any], number) -> None:
        """The 1Fit tariff, the start date and how the membership is paid: fully or in installments."""
        plan = body.get("tariff_days")
        if plan not in TARIFF_PLANS or isinstance(plan, bool):
            raise ApiError(400, "bad_plan", ctx.t("app.err.plan"), "tariff_days")
        draft["tariff_days"] = plan
        draft["freeze_days"] = self._freeze(ctx, body)
        draft["bonus_days"] = self._bonus(ctx, body)
        try:
            check_plan_end(date.fromisoformat(draft["end_date"]), today_local(), plan)
        except ValidationError as e:
            raise ApiError(400, e.code, ctx.t(f"err.{e.code}"), "end_date") from None
        draft["start_date"] = self._field(ctx, "start_date", self._iso_start, body.get("start_date") or "",
                                          today_local(), draft["end_date"])
        try:  # 15 months between the dates on a 12-month tariff: a wrong date or a wrong tariff
            check_plan_span(date.fromisoformat(draft["start_date"]) if draft["start_date"] else None,
                            date.fromisoformat(draft["end_date"]), plan)
        except ValidationError as e:
            raise ApiError(400, e.code, ctx.t(f"err.{e.code}"), "start_date") from None
        paid = body.get("paid_in_full")
        if not isinstance(paid, bool):
            raise ApiError(400, "bad_payment", ctx.t("app.err.payment"), "payment")
        draft["paid_in_full"] = paid
        if not paid:
            draft["monthly_payment"] = self._field(ctx, "monthly_payment", parse_monthly, number("monthly_payment"))
            draft["payments_left"] = self._field(ctx, "payments_left", parse_payments_left, number("payments_left"))

    async def advice(self, request: web.Request) -> web.Response:
        """Recommended price for the sell form, from the end date and the installments typed so far.
        Half-filled forms are fine: anything not valid yet simply gives no advice."""
        ctx = request[CTX]
        await self.prices.refresh()
        body = await self._json(request)
        if (body.get("club") or FIT) != FIT:  # the advice comes from 1Fit's prices
            return web.json_response({"advice": None})
        try:
            end = self._iso_date(body.get("end_date"), today_local())
        except ValidationError:
            return web.json_response({"advice": None})
        plan = body.get("tariff_days")
        draft: dict[str, Any] = {"kind": LK.MEMBERSHIP, "end_date": end, "paid_in_full": body.get("paid_in_full") is not False,
                                 "tariff_days": plan if plan in TARIFF_PLANS else None}
        try:
            draft["bonus_days"] = self._bonus(ctx, body)
        except ApiError:
            return web.json_response({"advice": None})
        if not draft["paid_in_full"]:
            try:
                draft["monthly_payment"] = parse_monthly(str(body.get("monthly_payment") or ""))
                draft["payments_left"] = parse_payments_left(str(body.get("payments_left") or ""))
            except ValidationError:
                return web.json_response({"advice": None})
        a = price_advice(ctx, draft)
        if not a:
            return web.json_response({"advice": None})
        return web.json_response({"advice": {
            "recommended": a.recommended, "max": a.max_price, "min": a.min_price, "installments": a.installments,
            "total": round_k(a.total(a.recommended)), "fit_total": round_k(a.fit_value),
            "saving": a.saving(a.recommended),
            "days_left": a.days_left, "base_days": a.base_days,
        }})

    async def _add_reads(self, ctx: Ctx, draft: dict[str, Any]) -> None:
        """What was read from the new screenshots joins what the kept ones gave (the admin compares it)."""
        reads = await self.repo.screenshot_reads(draft.pop("new_screenshots", []), ctx.user_id)
        draft["ocr"] = ocr.merge([draft.get("ocr") or {}, *reads])

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
        # Read the 1Fit card (end date, tariff, freeze days): the form fills itself, the admin compares.
        read = await asyncio.to_thread(ocr.read_card, bytes(buf), today_local()) if self.ocr_on else {}
        await self.repo.save_screenshot_read(file_id, ctx.user_id, read)
        return web.json_response({"file_id": file_id, "sig": self._sign(ctx.user_id, file_id), "read": read})

    async def create(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        await self.prices.refresh()
        draft = self._draft(ctx, await self._json(request))
        await self._add_reads(ctx, draft)
        fee = self.cfg.listing_fee if self.cfg.fee_enabled else 0
        listing_id = await self.repo.create_listing(ctx.user_id, ctx.lang, draft, fee=fee)
        await self.repo.save_contact(ctx.user_id, draft["seller_name"], draft["seller_phone"])
        listing = await self.repo.get_listing(listing_id)
        if listing["status"] == LS.UNPAID:  # the admins hear about it once it is paid
            if self.cfg.fee_manual:  # ... or now, to confirm the money when the seller pays the operator
                await announce_awaiting(self.notifier, ctx, listing)
            return web.json_response({"id": listing_id, "invoice_url": await self._invoice_url(ctx, listing)},
                                     status=201)
        await self._notify_admins(ctx, "admin.n.new_listing", listing_id)
        return web.json_response({"id": listing_id}, status=201)

    async def _invoice_url(self, ctx: Ctx, listing: dict[str, Any]) -> str | None:
        """None when Telegram does not answer: the listing is saved, the seller pays later from «My listings».
        None too when the fee is paid by hand: the Mini App sends the seller to the operator."""
        if self.cfg.fee_manual:
            return None
        try:
            return await fee_invoice_link(self.bot, ctx, listing)
        except TelegramAPIError as e:
            log.warning("could not make an invoice for listing %s: %s", listing["id"], e)
            return None

    async def invoice(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        listing = await self._own(ctx, listing_id)
        problem = await self.repo.fee_problem(listing_id, ctx.user_id)
        if problem or not self.cfg.can_take_fee:
            raise ApiError(409, problem or "off", ctx.t(f"pay.err.{problem or 'off'}"))
        if self.cfg.fee_manual:
            return web.json_response({"invoice_url": None, "manual": True})
        url = await self._invoice_url(ctx, listing)
        if not url:
            raise ApiError(502, "invoice", ctx.t("common.error"))
        return web.json_response({"invoice_url": url})

    async def _own(self, ctx: Ctx, listing_id: int) -> dict[str, Any]:
        listing = await self.repo.get_listing(listing_id)
        if not listing or listing["seller_id"] != ctx.user_id:  # other people's listings do not exist here
            raise ApiError(404, "not_found", ctx.t("common.not_available"))
        return listing

    async def update(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        existing = await self._own(ctx, listing_id)
        await self.prices.refresh()
        draft = self._draft(ctx, await self._json(request), existing)
        await self._add_reads(ctx, draft)
        try:
            await self.repo.update_listing_by_seller(listing_id, ctx.user_id, ctx.lang, draft)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("sell.edit_blocked")) from None
        await self.repo.save_contact(ctx.user_id, draft["seller_name"], draft["seller_phone"])
        listing = await self.repo.get_listing(listing_id)
        if listing["status"] == LS.UNPAID:
            return web.json_response({"id": listing_id, "invoice_url": await self._invoice_url(ctx, listing)})
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
        was_unpaid = (await self._own(ctx, listing_id))["status"] == LS.UNPAID
        try:
            await self.repo.withdraw_by_seller(listing_id, ctx.user_id)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("my.cant_withdraw")) from None
        if not was_unpaid:  # the admins never saw an unpaid one
            await self._notify_admins(ctx, "admin.n.withdrawn", listing_id)
        return web.json_response({"listing": own_view(ctx, await self.repo.get_listing(listing_id))})

    async def sold(self, request: web.Request) -> web.Response:
        ctx = request[CTX]
        listing_id = int(request.match_info["id"])
        await self._own(ctx, listing_id)
        try:
            await self.repo.mark_sold(listing_id, ctx.user_id)
        except RepoError:
            raise ApiError(409, "bad_status", ctx.t("my.cant_sold")) from None
        await self._notify_admins(ctx, "admin.n.sold", listing_id, **await self.repo.contact_counts(listing_id))
        return web.json_response({"listing": own_view(ctx, await self.repo.get_listing(listing_id))})


def create_webapp(repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier,
                  prices: PriceWatcher | None = None) -> web.Application:
    return WebApp(repo, i18n, cfg, bot, notifier, prices).build()


async def start_webapp(repo: Repo, i18n: I18n, cfg: Config, bot: Bot, notifier: Notifier,
                       prices: PriceWatcher | None = None) -> web.AppRunner:
    runner = web.AppRunner(create_webapp(repo, i18n, cfg, bot, notifier, prices), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.webapp_host, cfg.webapp_port).start()
    log.info("web server on %s:%s (Mini App: %s, Payme: %s)", cfg.webapp_host, cfg.webapp_port,
             cfg.webapp_url or "off", "POST /payme" if cfg.payme_enabled else "off")
    return runner
