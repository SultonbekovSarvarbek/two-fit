"""SQLite storage and all business rules of the marketplace.

Every state change goes through this module, so the rules (who may do what,
one payment per listing fee) live in one place and are covered by tests.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .clubs import CLUBS, FIT, is_fit
from .pricing import advise, normalize_prices
from .usage import DAYS_PER_MONTH, with_bonus
from .validators import TARIFF_PLANS

# Uzbekistan has no DST: UTC+5 all year round.
TZ = timezone(timedelta(hours=5))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_local() -> date:
    return datetime.now(TZ).date()


class LS:  # listing statuses
    UNPAID = "unpaid"        # filled in, the listing fee is not paid yet: nobody but the seller sees it
    PENDING = "pending"      # waiting for moderation
    APPROVED = "approved"    # published in the catalog
    REJECTED = "rejected"    # rejected by admin with a reason
    WITHDRAWN = "withdrawn"  # taken down by the seller
    REMOVED = "removed"      # taken down by an admin
    RESERVED = "reserved"    # the operator's deals before the marketplace: kept for old listings, no longer set
    SOLD = "sold"            # the seller marked it sold

    ALL = (UNPAID, PENDING, APPROVED, REJECTED, WITHDRAWN, REMOVED, RESERVED, SOLD)
    SELLER_EDITABLE = (UNPAID, PENDING, APPROVED, REJECTED, WITHDRAWN, REMOVED)
    SELLER_WITHDRAWABLE = (UNPAID, PENDING, APPROVED, REJECTED)
    SELLER_SELLABLE = (APPROVED,)  # «Sold»: the seller takes it out of the catalog for good
    ADMIN_REMOVABLE = (PENDING, APPROVED, REJECTED)


class LK:  # what a listing sells
    MEMBERSHIP = "membership"  # a 1Fit membership, re-registered to the buyer
    PROMO = "promo"            # a 1Fit promo code, handed to the buyer by the operator
    ALL = (MEMBERSHIP, PROMO)


class RepoError(Exception):
    """Business rule violation. ``code`` is mapped to a user/admin message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,           -- Telegram user id
    lang          TEXT,
    username      TEXT,
    tg_name       TEXT,
    contact_name  TEXT,
    contact_phone TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Who came on which day (Tashkent time), through the bot or the Mini App: one row per person, day and way.
CREATE TABLE IF NOT EXISTS visits (
    day      TEXT NOT NULL,      -- YYYY-MM-DD
    user_id  INTEGER NOT NULL,
    source   TEXT NOT NULL,      -- bot | app
    PRIMARY KEY (day, user_id, source)
);

CREATE TABLE IF NOT EXISTS listings (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id                 INTEGER NOT NULL REFERENCES users(id),
    lang                      TEXT NOT NULL,
    kind                      TEXT NOT NULL DEFAULT 'membership',  -- membership | promo
    club                      TEXT NOT NULL DEFAULT '1fit',  -- whose membership: clubs.CLUBS (1fit, ozone, ...)
    promo_code                TEXT,               -- promo: the code itself, only the admin and the seller see it
    city                      TEXT NOT NULL,
    tariff                    TEXT NOT NULL,
    end_date                  TEXT NOT NULL,      -- YYYY-MM-DD
    start_date                TEXT,               -- YYYY-MM-DD, optional: when the membership was activated
    price                     INTEGER NOT NULL CHECK (price > 0),
    price_negotiable          INTEGER NOT NULL DEFAULT 0,  -- 1: price is open to negotiation
    paid_in_full              INTEGER,            -- 1: fully paid, 0: installment, NULL: not asked (old listings)
    monthly_payment           INTEGER,            -- installment: what the buyer keeps paying every month
    payments_left             INTEGER,            -- installment: how many of those payments are left
    tariff_days               INTEGER,            -- membership: the 1Fit tariff, 180 / 365 / 730 days; NULL on old listings
    freeze_days               INTEGER,            -- membership: unused freeze days (0 = none); NULL on old listings
    bonus_days                INTEGER NOT NULL DEFAULT 0,  -- membership: 1Fit bonus days, already added to end_date
    price_auto                INTEGER NOT NULL DEFAULT 0,  -- 1: the recommended price, lowered every day as days pass
    payments_as_of            TEXT,               -- YYYY-MM-DD: the day payments_left was true; one fewer each month after
    screenshots               TEXT,               -- JSON list of up to 3 Telegram file ids (the first is screenshot_file_id)
    ocr                       TEXT,               -- JSON: what was read from the screenshots (end_date, tariff_days, …)
    description               TEXT NOT NULL DEFAULT '',
    screenshot_file_id        TEXT,
    seller_name               TEXT NOT NULL,
    seller_phone              TEXT NOT NULL,
    commission_accepted_price INTEGER NOT NULL,   -- price the seller explicitly accepted the commission for
    commission_percent        INTEGER,            -- the rate the seller accepted; a later change of rate does not touch it
    status                    TEXT NOT NULL,
    reject_reason             TEXT,
    reserved_request_id       INTEGER,
    final_price               INTEGER,
    final_price_confirmed     INTEGER NOT NULL DEFAULT 0,
    sale_reported_at          TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    published_at              TEXT,
    fee_amount                INTEGER,            -- the listing fee to pay, сум; NULL or 0: placed for free
    fee_paid_at               TEXT,               -- when the seller paid it
    fee_charge_id             TEXT                -- Telegram's payment charge id: one payment, one listing
);
CREATE INDEX IF NOT EXISTS ix_listings_catalog ON listings(status, end_date, city, published_at);
CREATE INDEX IF NOT EXISTS ix_listings_seller ON listings(seller_id);

CREATE TABLE IF NOT EXISTS fee_payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          INTEGER NOT NULL REFERENCES listings(id),
    user_id             INTEGER NOT NULL,
    amount              INTEGER NOT NULL,          -- сум
    currency            TEXT NOT NULL,
    charge_id           TEXT NOT NULL UNIQUE,      -- telegram_payment_charge_id: never counted twice
    provider_charge_id  TEXT,
    paid_at             TEXT NOT NULL,
    refunded_at         TEXT                       -- Payme cancelled a performed payment (money returned)
);

-- Buyers reaching a seller: opening the listing's contacts, tapping the phone, Telegram or «Call».
-- Lets the admin ask the buyers themselves whether a deal happened (sellers may not report a sale).
CREATE TABLE IF NOT EXISTS contact_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    action      TEXT NOT NULL,                     -- view | call | telegram | copy
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contact_listing ON contact_events(listing_id, user_id);

-- Payme Merchant API: one row per Payme transaction. Times are Unix milliseconds, as Payme sends them.
CREATE TABLE IF NOT EXISTS payme_transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- ours: «transaction» in the answers to Payme
    payme_id      TEXT NOT NULL UNIQUE,
    listing_id    INTEGER NOT NULL,
    amount        INTEGER NOT NULL,                   -- tiyin
    payme_time    INTEGER NOT NULL,                   -- when Payme made it (GetStatement filters by it)
    create_time   INTEGER NOT NULL,
    perform_time  INTEGER NOT NULL DEFAULT 0,
    cancel_time   INTEGER NOT NULL DEFAULT 0,
    state         INTEGER NOT NULL,                   -- 1 created, 2 performed, -1 cancelled, -2 cancelled after perform
    reason        INTEGER
);
CREATE INDEX IF NOT EXISTS ix_payme_listing ON payme_transactions(listing_id, state);
CREATE INDEX IF NOT EXISTS ix_payme_time ON payme_transactions(payme_time);

CREATE TABLE IF NOT EXISTS purchase_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    buyer_id    INTEGER NOT NULL REFERENCES users(id),
    buyer_name  TEXT NOT NULL,
    buyer_phone TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- one active request per buyer per listing
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_request
    ON purchase_requests(listing_id, buyer_id) WHERE status IN ('new', 'selected');

CREATE TABLE IF NOT EXISTS deals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id           INTEGER NOT NULL UNIQUE REFERENCES listings(id),  -- no double sale
    request_id           INTEGER REFERENCES purchase_requests(id),
    seller_id            INTEGER NOT NULL,
    seller_name          TEXT NOT NULL,
    seller_phone         TEXT NOT NULL,
    buyer_id             INTEGER,
    buyer_name           TEXT,
    buyer_phone          TEXT,
    sale_price           INTEGER NOT NULL,
    commission_percent   INTEGER NOT NULL,
    commission_amount    INTEGER NOT NULL,
    seller_amount        INTEGER NOT NULL,
    closed_at            TEXT NOT NULL,
    closed_by            INTEGER NOT NULL,
    commission_status    TEXT NOT NULL DEFAULT 'pending',
    commission_paid_at   TEXT,
    commission_marked_by INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    details    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  TEXT
);

-- What was read from a screenshot uploaded through the Mini App, until the listing that uses it is sent.
CREATE TABLE IF NOT EXISTS screenshot_reads (
    file_id    TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fit_price_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    status     TEXT NOT NULL,          -- ok | changed | error
    prices     TEXT,                   -- {"365": 7890000, ...} as read from 1Fit
    full       TEXT,                   -- prices without 1Fit's discount
    error      TEXT
);
"""


# The price buyers see, in SQL (the same rule as ui.buyer_total and the Mini App's price_total).
SHOWN_PRICE = ("CASE WHEN paid_in_full = 0 AND monthly_payment > 0 AND payments_left > 0"
               " THEN monthly_payment * payments_left ELSE price END")


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Repo:
    def __init__(self, db_path: str | Path, commission_percent: int = 15, promo_commission_percent: int = 30,
                 promo_enabled: bool = True):
        self.db_path = str(db_path)
        self.commission_percent = commission_percent
        self.promo_commission_percent = promo_commission_percent
        self.promo_enabled = promo_enabled  # off: promo code listings are hidden from the catalog and not for sale
        self._db: aiosqlite.Connection | None = None
        # 1Fit prices used for the price advice; read from the database on connect, updated by the price watcher.
        self.fit_prices: dict[int, int] = normalize_prices(None)
        # One connection shared by all handlers: the lock keeps transactions from interleaving.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    async def connect(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        self.fit_prices = normalize_prices(await self.get_setting("fit_prices"))

    async def _migrate(self) -> None:
        """Columns added after the first release: CREATE TABLE IF NOT EXISTS does not add them."""
        async with self.db.execute("PRAGMA table_info(listings)") as cur:
            columns = {r[1] for r in await cur.fetchall()}
        if "price_negotiable" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN price_negotiable INTEGER NOT NULL DEFAULT 0")
        if "start_date" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN start_date TEXT")
        for column in ("paid_in_full", "monthly_payment", "payments_left"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} INTEGER")
        if "kind" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN kind TEXT NOT NULL DEFAULT 'membership'")
        if "promo_code" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN promo_code TEXT")
        for column in ("tariff_days", "freeze_days"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} INTEGER")
        if "bonus_days" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN bonus_days INTEGER NOT NULL DEFAULT 0")
        if "price_auto" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN price_auto INTEGER NOT NULL DEFAULT 0")
        if "payments_as_of" not in columns:
            await self.db.execute("ALTER TABLE listings ADD COLUMN payments_as_of TEXT")
        for column in ("screenshots", "ocr"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")
        if "commission_percent" not in columns:
            # Listings made before the rate was stored were accepted at 10% (membership) and 30% (promo code).
            await self.db.execute("ALTER TABLE listings ADD COLUMN commission_percent INTEGER")
            await self.db.execute(
                "UPDATE listings SET commission_percent = CASE kind WHEN 'promo' THEN 30 ELSE 10 END")
        if "club" not in columns:  # every listing before other clubs came was a 1Fit one
            await self.db.execute("ALTER TABLE listings ADD COLUMN club TEXT NOT NULL DEFAULT '1fit'")
        if "fee_amount" not in columns:  # listings placed before the fee were free
            await self.db.execute("ALTER TABLE listings ADD COLUMN fee_amount INTEGER")
        async with self.db.execute("PRAGMA table_info(fee_payments)") as cur:
            fee_columns = {r[1] for r in await cur.fetchall()}
        if "refunded_at" not in fee_columns:
            await self.db.execute("ALTER TABLE fee_payments ADD COLUMN refunded_at TEXT")
        for column in ("fee_paid_at", "fee_charge_id"):
            if column not in columns:
                await self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")
        # Installment memberships cost monthly × payments left. Listings made before that rule get it once,
        # so the price, the catalog order and the nightly countdown agree (sold ones keep their history).
        if not await self._scalar("SELECT 1 FROM settings WHERE key='installment_price_v1'"):
            await self.db.execute(
                "UPDATE listings SET price = monthly_payment * payments_left WHERE kind='membership' AND paid_in_full=0"
                " AND monthly_payment > 0 AND payments_left > 0 AND status NOT IN ('sold', 'reserved')")
            await self.db.execute("INSERT INTO settings(key, value, updated_at) VALUES ('installment_price_v1', 'true', ?)",
                                  (now_iso(),))

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Repo.connect() was not called"
        return self._db

    async def _one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        async with self.db.execute(sql, params) as cur:
            return _row(await cur.fetchone())

    async def _all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def _log(self, listing_id: int | None, actor_id: int | None, action: str, **details: Any) -> None:
        await self.db.execute(
            "INSERT INTO events(listing_id, actor_id, action, details, created_at) VALUES (?,?,?,?,?)",
            (listing_id, actor_id, action, json.dumps(details, ensure_ascii=False) if details else None, now_iso()),
        )

    class _Tx:
        def __init__(self, repo: "Repo"):
            self.repo = repo

        async def __aenter__(self):
            await self.repo._lock.acquire()
            await self.repo.db.execute("BEGIN IMMEDIATE")
            return self.repo

        async def __aexit__(self, exc_type, exc, tb):
            try:
                await self.repo.db.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.repo._lock.release()
            return False

    def tx(self) -> "Repo._Tx":
        return Repo._Tx(self)

    # ------------------------------------------------------------------ users
    async def touch_user(self, user_id: int, username: str | None, tg_name: str | None,
                         source: str = "bot") -> dict[str, Any]:
        """Every message, button or Mini App request: the user's profile, and today's visit (counted once a day)."""
        async with self.tx():
            ts = now_iso()
            await self.db.execute("INSERT OR IGNORE INTO visits(day, user_id, source) VALUES (?,?,?)",
                                  (today_local().isoformat(), user_id, source))
            await self.db.execute(
                """INSERT INTO users(id, username, tg_name, created_at, updated_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET username=excluded.username, tg_name=excluded.tg_name,
                   updated_at=excluded.updated_at""",
                (user_id, username, tg_name, ts, ts),
            )
            return await self._one("SELECT * FROM users WHERE id=?", (user_id,))

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM users WHERE id=?", (user_id,))

    async def set_lang(self, user_id: int, lang: str) -> None:
        async with self.tx():
            await self.db.execute("UPDATE users SET lang=?, updated_at=? WHERE id=?", (lang, now_iso(), user_id))

    async def save_contact(self, user_id: int, name: str, phone: str) -> None:
        async with self.tx():
            await self.db.execute(
                "UPDATE users SET contact_name=?, contact_phone=?, updated_at=? WHERE id=?",
                (name, phone, now_iso(), user_id),
            )

    # --------------------------------------------------------------- listings
    async def get_listing(self, listing_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM listings WHERE id=?", (listing_id,))

    @staticmethod
    def is_expired(listing: dict[str, Any], today: date | None = None) -> bool:
        return listing["end_date"] < (today or today_local()).isoformat()

    def is_available(self, listing: dict[str, Any] | None, today: date | None = None) -> bool:
        """Visible in the catalog and open for purchase requests."""
        return (bool(listing) and listing["status"] == LS.APPROVED and not self.is_expired(listing, today)
                and (self.promo_enabled or listing["kind"] != LK.PROMO))

    def percent_for(self, kind: str | None) -> int:  # still stored on a listing; nothing charges it now
        """Today's rate: 15% for a membership, 30% for a promo code (both from the settings)."""
        return self.promo_commission_percent if kind == LK.PROMO else self.commission_percent

    @staticmethod
    def _check_draft(draft: dict[str, Any]) -> None:
        # The name and the phone are shown to every buyer: without them nobody can reach the seller.
        required = ("city", "end_date", "price", "seller_name", "seller_phone")
        if any(draft.get(k) in (None, "") for k in required):
            raise RepoError("incomplete")
        kind = draft.get("kind") or LK.MEMBERSHIP
        if kind not in LK.ALL:
            raise RepoError("incomplete")
        if kind == LK.PROMO:
            if not draft.get("promo_code") or not draft.get("tariff"):  # tariff: what the promo code gives
                raise RepoError("incomplete")
            return  # a promo code has no start date and no installments: they are not stored
        if draft.get("start_date") and draft["start_date"] >= draft["end_date"]:
            raise RepoError("bad_start")
        if draft.get("club", FIT) not in CLUBS:
            raise RepoError("incomplete")
        if not is_fit(draft):
            return  # other clubs: no 1Fit tariff and no 1Fit installments
        if draft.get("tariff_days") not in TARIFF_PLANS:  # 6, 12 or 24 months: picked, not typed
            raise RepoError("incomplete")
        # Fully paid or installment must be stated: «9 months, 500 000» meant 500 000 every month.
        paid = draft.get("paid_in_full")
        if paid not in (True, False, 0, 1):
            raise RepoError("incomplete")
        if not paid and not (draft.get("monthly_payment") and draft.get("payments_left")):
            raise RepoError("incomplete")

    @staticmethod
    def _bonus(draft: dict[str, Any]) -> int:
        return int(draft.get("bonus_days") or 0) if draft.get("kind") != LK.PROMO and is_fit(draft) else 0

    @staticmethod
    def _club(draft: dict[str, Any]) -> str:
        return FIT if draft.get("kind") == LK.PROMO else (draft.get("club") or FIT)

    @staticmethod
    def _end(draft: dict[str, Any]) -> str:
        """A draft has the end date from the 1Fit card; the listing keeps the real one, the bonus days after it."""
        return with_bonus(draft["end_date"], Repo._bonus(draft))

    @staticmethod
    def _tariff(draft: dict[str, Any]) -> str:
        """Only a promo code says what it gives; a membership shows the months left instead of a tariff name."""
        return draft.get("tariff") or "" if draft.get("kind") == LK.PROMO else ""

    @staticmethod
    def _kind_fields(draft: dict[str, Any]) -> tuple[Any, ...]:
        """kind, promo_code, tariff_days, freeze_days, start_date, paid_in_full, monthly_payment, payments_left."""
        if draft.get("kind") == LK.PROMO:
            return LK.PROMO, draft["promo_code"], None, None, None, None, None, None
        if not is_fit(draft):  # another club: paid for, no 1Fit tariff
            return LK.MEMBERSHIP, None, None, draft.get("freeze_days"), draft.get("start_date"), 1, None, None
        return (LK.MEMBERSHIP, None, draft["tariff_days"], draft.get("freeze_days"), draft.get("start_date"),
                *Repo._payment(draft))

    @staticmethod
    def _shots(draft: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        """screenshot_file_id, screenshots (JSON list, at most 3), ocr (JSON) from a draft."""
        ids = [i for i in (draft.get("screenshot_file_ids") or []) if i][:3]
        if not ids and draft.get("screenshot_file_id"):
            ids = [draft["screenshot_file_id"]]
        read = draft.get("ocr") or None
        return (ids[0] if ids else None, json.dumps(ids) if ids else None,
                json.dumps(read, ensure_ascii=False) if read else None)

    @staticmethod
    def screenshot_ids(listing: dict[str, Any]) -> list[str]:
        """All screenshots of a listing; old ones have only screenshot_file_id."""
        if listing.get("screenshots"):
            try:
                return [i for i in json.loads(listing["screenshots"]) if i][:3]
            except (TypeError, ValueError):
                pass
        return [listing["screenshot_file_id"]] if listing.get("screenshot_file_id") else []

    @staticmethod
    def ocr_of(listing: dict[str, Any]) -> dict[str, Any]:
        try:
            return json.loads(listing.get("ocr") or "{}") or {}
        except (TypeError, ValueError):
            return {}

    async def save_screenshot_read(self, file_id: str, user_id: int, data: dict[str, Any]) -> None:
        async with self.tx():
            await self.db.execute(
                "INSERT OR REPLACE INTO screenshot_reads(file_id, user_id, data, created_at) VALUES (?,?,?,?)",
                (file_id, user_id, json.dumps(data, ensure_ascii=False), now_iso()),
            )

    async def screenshot_reads(self, file_ids: list[str], user_id: int) -> list[dict[str, Any]]:
        """What was read from these screenshots, in their order; only the uploader's own."""
        out = []
        for file_id in file_ids:
            row = await self._one("SELECT data FROM screenshot_reads WHERE file_id=? AND user_id=?", (file_id, user_id))
            out.append(json.loads(row["data"]) if row else {})
        return out

    @staticmethod
    def _auto_fields(draft: dict[str, Any]) -> tuple[int, str | None]:
        """price_auto, payments_as_of: whether the price follows the days, and when payments_left was stated."""
        membership = draft.get("kind") != LK.PROMO
        auto = int(bool(draft.get("price_auto")) and membership and is_fit(draft))  # the advice is 1Fit's only
        installment = membership and draft.get("paid_in_full") in (False, 0) and draft.get("paid_in_full") is not None
        return auto, (today_local().isoformat() if installment and draft.get("payments_left") else None)

    @staticmethod
    def _payment(draft: dict[str, Any]) -> tuple[int, int | None, int | None]:
        if draft["paid_in_full"]:
            return 1, None, None
        return 0, draft["monthly_payment"], draft["payments_left"]

    async def create_listing(self, seller_id: int, lang: str, draft: dict[str, Any], fee: int = 0) -> int:
        """``fee``: what the seller pays to place it; until then it waits as unpaid, unseen by the admins."""
        self._check_draft(draft)
        async with self.tx():
            ts = now_iso()
            cur = await self.db.execute(
                """INSERT INTO listings(seller_id, lang, city, tariff, end_date, kind, promo_code, tariff_days, freeze_days,
                       start_date,
                       paid_in_full, monthly_payment, payments_left, price, price_negotiable,
                       description, screenshot_file_id, seller_name, seller_phone, commission_accepted_price,
                       commission_percent, price_auto, payments_as_of, screenshots, ocr, status, created_at, updated_at,
                       fee_amount, bonus_days, club)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    seller_id, lang, draft["city"], self._tariff(draft), self._end(draft), *self._kind_fields(draft),
                    draft["price"], int(bool(draft.get("price_negotiable"))),
                    draft.get("description") or "", self._shots(draft)[0],
                    draft["seller_name"], draft.get("seller_phone") or "", draft["price"],
                    self.percent_for(draft.get("kind")), *self._auto_fields(draft), *self._shots(draft)[1:],
                    LS.UNPAID if fee > 0 else LS.PENDING, ts, ts, fee if fee > 0 else None, self._bonus(draft),
                    self._club(draft),
                ),
            )
            listing_id = cur.lastrowid
            await self._log(listing_id, seller_id, "created", price=draft["price"])
            return listing_id

    async def update_listing_by_seller(self, listing_id: int, seller_id: int, lang: str, draft: dict[str, Any]) -> None:
        self._check_draft(draft)
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            if listing["status"] not in LS.SELLER_EDITABLE:
                raise RepoError("bad_status")
            # Paid once, edited for free: only a listing still waiting for its fee stays unpaid.
            status = LS.UNPAID if self.fee_due(listing) else LS.PENDING
            await self.db.execute(
                """UPDATE listings SET lang=?, city=?, tariff=?, end_date=?, kind=?, promo_code=?, tariff_days=?, freeze_days=?, start_date=?,
                       paid_in_full=?, monthly_payment=?, payments_left=?, price=?, price_negotiable=?, description=?,
                       screenshot_file_id=?, seller_name=?, seller_phone=?, commission_accepted_price=?, commission_percent=?,
                       price_auto=?, payments_as_of=?, screenshots=?, ocr=?, status=?, reject_reason=NULL, published_at=NULL, sale_reported_at=NULL, updated_at=?,
                       bonus_days=?, club=?
                   WHERE id=?""",
                (
                    lang, draft["city"], self._tariff(draft), self._end(draft), *self._kind_fields(draft), draft["price"],
                    int(bool(draft.get("price_negotiable"))),
                    draft.get("description") or "", self._shots(draft)[0],
                    draft["seller_name"], draft.get("seller_phone") or "", draft["price"],
                    self.percent_for(draft.get("kind")), *self._auto_fields(draft), *self._shots(draft)[1:],
                    status, now_iso(), self._bonus(draft), self._club(draft), listing_id,
                ),
            )
            await self._log(listing_id, seller_id, "edited", price=draft["price"])

    async def seller_listings(self, seller_id: int, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        total = await self._scalar("SELECT COUNT(*) FROM listings WHERE seller_id=?", (seller_id,))
        rows = await self._all(
            "SELECT * FROM listings WHERE seller_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (seller_id, limit, offset)
        )
        return rows, total

    async def withdraw_by_seller(self, listing_id: int, seller_id: int) -> None:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            if listing["status"] not in LS.SELLER_WITHDRAWABLE:
                raise RepoError("bad_status")
            await self.db.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?", (LS.WITHDRAWN, now_iso(), listing_id)
            )
            await self._log(listing_id, seller_id, "withdrawn")

    async def mark_sold(self, listing_id: int, seller_id: int) -> None:
        """The seller sold it to a buyer who called: it leaves the catalog for good."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            if listing["status"] not in LS.SELLER_SELLABLE:
                raise RepoError("bad_status")
            ts = now_iso()
            await self.db.execute(
                "UPDATE listings SET status=?, sale_reported_at=?, updated_at=? WHERE id=?",
                (LS.SOLD, ts, ts, listing_id),
            )
            await self._log(listing_id, seller_id, "sold")

    # ------------------------------------------------------------- listing fee
    @staticmethod
    def fee_due(listing: dict[str, Any]) -> bool:
        """A fee was set when it was created and has not been paid (withdrawing it does not waive it)."""
        return bool(listing.get("fee_amount")) and not listing.get("fee_paid_at")

    async def fee_problem(self, listing_id: int, user_id: int, amount: int | None = None) -> str | None:
        """Why this listing cannot be paid for now (None: it can). Checked before Telegram takes the money."""
        listing = await self.get_listing(listing_id)
        if not listing or listing["seller_id"] != user_id:
            return "not_found"
        if listing["status"] != LS.UNPAID or not self.fee_due(listing):
            return "already"
        if amount is not None and amount != listing["fee_amount"]:
            return "stale"  # the fee changed since the invoice was sent
        return None

    async def pay_fee(self, listing_id: int, user_id: int, amount: int, currency: str, charge_id: str,
                      provider_charge_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Telegram took the money: the listing goes to moderation. Returns (listing, first time).
        The same charge reported twice is recorded once; a payment is never lost, even for a listing
        that was withdrawn meanwhile (the admin sees it and can refund or publish)."""
        async with self.tx():
            if await self._one("SELECT id FROM fee_payments WHERE charge_id=?", (charge_id,)):
                return await self.get_listing(listing_id), False
            return await self._record_fee(listing_id, user_id, amount, currency, charge_id, provider_charge_id)

    async def pay_fee_manually(self, listing_id: int, admin_id: int) -> tuple[dict[str, Any], int]:
        """An admin confirms the seller paid the operator by hand: the listing goes to moderation.
        Returns (listing, amount)."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] != LS.UNPAID or not self.fee_due(listing):
                raise RepoError("already")
            amount = listing["fee_amount"]
            listing, _ = await self._record_fee(listing_id, listing["seller_id"], amount, "UZS",
                                                f"manual:{listing_id}:{admin_id}:{now_iso()}", None)
            await self._log(listing_id, admin_id, "fee_manual", amount=amount)
        return listing, amount

    async def _record_fee(self, listing_id: int, user_id: int, amount: int, currency: str, charge_id: str,
                          provider_charge_id: str | None) -> tuple[dict[str, Any], bool]:
        """Inside a transaction: the payment row, and the listing moves from unpaid to moderation."""
        listing = await self.get_listing(listing_id)
        if not listing:
            raise RepoError("not_found")
        ts = now_iso()
        await self.db.execute(
            """INSERT INTO fee_payments(listing_id, user_id, amount, currency, charge_id, provider_charge_id,
                   paid_at) VALUES (?,?,?,?,?,?,?)""",
            (listing_id, user_id, amount, currency, charge_id, provider_charge_id, ts),
        )
        first = not listing["fee_paid_at"]
        if first:
            await self.db.execute(
                "UPDATE listings SET fee_paid_at=?, fee_charge_id=?, updated_at=? WHERE id=?",
                (ts, charge_id, ts, listing_id),
            )
            await self.db.execute(
                "UPDATE listings SET status=? WHERE id=? AND status=?", (LS.PENDING, listing_id, LS.UNPAID)
            )
        await self._log(listing_id, user_id, "fee_paid", amount=amount, charge_id=charge_id, first=first)
        return await self.get_listing(listing_id), first

    async def _refund_fee(self, listing_id: int, charge_id: str) -> dict[str, Any] | None:
        """Inside a transaction: the payment was returned. If it was the one that paid the listing,
        the listing owes the fee again and leaves moderation and the catalog until it is paid."""
        ts = now_iso()
        await self.db.execute("UPDATE fee_payments SET refunded_at=? WHERE charge_id=? AND refunded_at IS NULL",
                              (ts, charge_id))
        listing = await self.get_listing(listing_id)
        if listing and listing["fee_charge_id"] == charge_id:
            await self.db.execute("UPDATE listings SET fee_paid_at=NULL, fee_charge_id=NULL, updated_at=? WHERE id=?",
                                  (ts, listing_id))
            await self.db.execute("UPDATE listings SET status=?, published_at=NULL WHERE id=? AND status IN (?,?)",
                                  (LS.UNPAID, listing_id, LS.PENDING, LS.APPROVED))
        await self._log(listing_id, None, "fee_refunded", charge_id=charge_id)
        return await self.get_listing(listing_id)

    # ---------------------------------------------------------- buyers reaching sellers
    CONTACT_ACTIONS = ("view", "call", "telegram", "copy")
    CONTACT_REPEAT_SECONDS = 600  # the same tap again within 10 minutes is not a new contact

    async def log_contact(self, listing_id: int, user_id: int, action: str) -> bool:
        """A buyer saw or used the seller's contacts. The seller's own taps are not counted."""
        if action not in self.CONTACT_ACTIONS:
            return False
        listing = await self.get_listing(listing_id)
        if not listing or listing["seller_id"] == user_id:
            return False
        since = (datetime.now(timezone.utc) - timedelta(seconds=self.CONTACT_REPEAT_SECONDS)).isoformat(timespec="seconds")
        async with self.tx():
            if await self._scalar("SELECT 1 FROM contact_events WHERE listing_id=? AND user_id=? AND action=?"
                                  " AND created_at>=?", (listing_id, user_id, action, since)):
                return False
            await self.db.execute("INSERT INTO contact_events(listing_id, user_id, action, created_at) VALUES (?,?,?,?)",
                                  (listing_id, user_id, action, now_iso()))
        return True

    async def contact_buyers(self, listing_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Who reached this seller, most recent first: one row per buyer with how and when."""
        return await self._all(
            """SELECT e.user_id, u.username, u.tg_name, u.contact_name, u.contact_phone,
                      SUM(e.action='view') AS views, SUM(e.action='call') AS calls,
                      SUM(e.action='telegram') AS telegram, SUM(e.action='copy') AS copies,
                      MIN(e.created_at) AS first_at, MAX(e.created_at) AS last_at
               FROM contact_events e LEFT JOIN users u ON u.id = e.user_id
               WHERE e.listing_id=? GROUP BY e.user_id ORDER BY last_at DESC LIMIT ?""", (listing_id, limit))

    async def contact_counts(self, listing_id: int) -> dict[str, int]:
        """buyers: everyone who opened the contacts; reached: those who tapped the phone, Telegram or «Call»."""
        row = await self._one(
            """SELECT COUNT(DISTINCT user_id) AS buyers,
                      COUNT(DISTINCT CASE WHEN action != 'view' THEN user_id END) AS reached
               FROM contact_events WHERE listing_id=?""", (listing_id,))
        return {"buyers": row["buyers"] or 0, "reached": row["reached"] or 0}

    # ---------------------------------------------------------- Payme transactions
    # Callers hold ``tx()``: Payme's calls for one listing must not interleave.
    async def payme_get(self, payme_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM payme_transactions WHERE payme_id=?", (payme_id,))

    async def payme_active(self, listing_id: int) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM payme_transactions WHERE listing_id=? AND state=1", (listing_id,))

    async def payme_insert(self, payme_id: str, listing_id: int, amount: int, payme_time: int,
                           create_time: int) -> dict[str, Any]:
        await self.db.execute(
            """INSERT INTO payme_transactions(payme_id, listing_id, amount, payme_time, create_time, state)
               VALUES (?,?,?,?,?,1)""", (payme_id, listing_id, amount, payme_time, create_time))
        return await self.payme_get(payme_id)

    async def payme_update(self, payme_id: str, **fields: Any) -> dict[str, Any]:
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE payme_transactions SET {sets} WHERE payme_id=?", (*fields.values(), payme_id))
        return await self.payme_get(payme_id)

    async def payme_statement(self, start: int, end: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM payme_transactions WHERE payme_time BETWEEN ? AND ? ORDER BY payme_time", (start, end))

    # ---------------------------------------------------------------- catalog
    SOLD_SHOWN_DAYS = 30  # a sold listing stays in the catalog this long, marked «Sold», without contacts

    @classmethod
    def _sold_since(cls) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=cls.SOLD_SHOWN_DAYS)).isoformat(timespec="seconds")

    def is_sold_shown(self, listing: dict[str, Any] | None) -> bool:
        """Sold lately: buyers still see it in the catalog with the «Sold» badge (2FIT works), but cannot reach the seller."""
        return (bool(listing) and listing["status"] == LS.SOLD and (listing["sale_reported_at"] or "") >= self._sold_since()
                and (self.promo_enabled or listing["kind"] != LK.PROMO))

    async def catalog(self, city: str | None, limit: int, offset: int,
                      today: date | None = None, club: str | None = None) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {"st": LS.APPROVED, "today": (today or today_local()).isoformat(),
                                  "sold": LS.SOLD, "since": self._sold_since()}
        where = ("((status=:st AND end_date>=:today) OR (status=:sold AND sale_reported_at>=:since))"
                 + self._kind_filter())
        if city:
            where += " AND city=:city"
            params["city"] = city
        if club:
            where += " AND club=:club"
            params["club"] = club
        total = await self._scalar(f"SELECT COUNT(*) FROM listings WHERE {where}", params)
        rows = await self._all(
            # cheapest first, by the price buyers see: installments cost monthly × payments left
            # the sold ones after everything on sale
            f"SELECT * FROM listings WHERE {where} ORDER BY status=:sold, {SHOWN_PRICE} ASC, published_at DESC, id DESC"
            " LIMIT :lim OFFSET :off",
            {**params, "lim": limit, "off": offset},
        )
        return rows, total

    def _kind_filter(self) -> str:
        return "" if self.promo_enabled else f" AND kind != '{LK.PROMO}'"

    async def catalog_clubs(self, today: date | None = None) -> list[tuple[str, int]]:
        """Clubs with memberships on sale now and how many, in the order of ``CLUBS``."""
        rows = await self._all(
            "SELECT club, COUNT(*) AS n FROM listings WHERE status=? AND end_date>=?" + self._kind_filter()
            + " GROUP BY club", (LS.APPROVED, (today or today_local()).isoformat()))
        counts = {r["club"]: r["n"] for r in rows}
        return [(c, counts[c]) for c in CLUBS if counts.get(c)]

    async def catalog_cities(self, today: date | None = None) -> list[tuple[str, int]]:
        rows = await self._all(
'SELECT city, COUNT(*) AS n FROM listings WHERE status=? AND end_date>=?' + self._kind_filter()
            + " GROUP BY city ORDER BY n DESC, city",
            (LS.APPROVED, (today or today_local()).isoformat()),
        )
        return [(r["city"], r["n"]) for r in rows]

    # ------------------------------------------------------------------ admin
    async def listings_by_status(self, status: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        if status:
            total = await self._scalar("SELECT COUNT(*) FROM listings WHERE status=?", (status,))
            rows = await self._all(
                "SELECT * FROM listings WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?", (status, limit, offset)
            )
        else:
            total = await self._scalar("SELECT COUNT(*) FROM listings")
            rows = await self._all("SELECT * FROM listings ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return rows, total

    async def live_listings_by_seller(self, today: date | None = None) -> dict[int, list[int]]:
        """Seller id -> ids of their memberships that buyers see or will see soon (published or in moderation)."""
        rows = await self._all(
            "SELECT id, seller_id FROM listings WHERE status IN (?,?) AND kind=? AND end_date>=? ORDER BY seller_id, id",
            (LS.APPROVED, LS.PENDING, LK.MEMBERSHIP, (today or today_local()).isoformat()),
        )
        out: dict[int, list[int]] = {}
        for r in rows:
            out.setdefault(r["seller_id"], []).append(r["id"])
        return out

    async def count_status(self, status: str) -> int:
        return await self._scalar("SELECT COUNT(*) FROM listings WHERE status=?", (status,))

    async def approve(self, listing_id: int, admin_id: int, clear_description: bool = False) -> dict[str, Any]:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] != LS.PENDING:
                raise RepoError("bad_status")
            ts = now_iso()
            await self.db.execute(
                f"""UPDATE listings SET status=?, reject_reason=NULL, published_at=?, updated_at=?
                    {", description=''" if clear_description else ""} WHERE id=?""",
                (LS.APPROVED, ts, ts, listing_id),
            )
            await self._log(listing_id, admin_id, "approved", cleared_description=clear_description)
            return await self.get_listing(listing_id)

    async def reject(self, listing_id: int, admin_id: int, reason: str) -> dict[str, Any]:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] != LS.PENDING:
                raise RepoError("bad_status")
            await self.db.execute(
                "UPDATE listings SET status=?, reject_reason=?, updated_at=? WHERE id=?",
                (LS.REJECTED, reason, now_iso(), listing_id),
            )
            await self._log(listing_id, admin_id, "rejected", reason=reason)
            return await self.get_listing(listing_id)

    async def remove(self, listing_id: int, admin_id: int) -> tuple[dict[str, Any], list[int]]:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] not in LS.ADMIN_REMOVABLE:
                raise RepoError("bad_status")
            await self.db.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?", (LS.REMOVED, now_iso(), listing_id)
            )
            await self._log(listing_id, admin_id, "removed")
            return await self.get_listing(listing_id), []

    async def stats(self, today: date | None = None) -> dict[str, int]:
        t = (today or today_local()).isoformat()
        fees = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM fee_payments WHERE refunded_at IS NULL")
        return {
            "active": await self._scalar(
                "SELECT COUNT(*) FROM listings WHERE status=? AND end_date>=?", (LS.APPROVED, t)
            ),
            "pending": await self.count_status(LS.PENDING),
            "unpaid": await self.count_status(LS.UNPAID),
            "sold": await self.count_status(LS.SOLD),
            "payments": fees["n"],
            "fees": fees["total"],
        }

    VISIT_DAYS = 7  # the day-by-day lines in the statistics

    async def visit_stats(self, exclude: frozenset[int] = frozenset(), today: date | None = None) -> dict[str, Any]:
        """How many people came: unique per period, through the bot and the Mini App, and how many are new.
        ``exclude``: the admins, who come every day and are not the audience."""
        today = today or today_local()
        day = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
        skip = f" AND user_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
        skip_users = skip.replace("user_id", "id")
        ex = tuple(exclude)

        async def came(since: str, until: str | None = None, source: str | None = None) -> int:
            where = "day>=?" + (" AND day<=?" if until else "") + (" AND source=?" if source else "")
            args = (since,) + ((until,) if until else ()) + ((source,) if source else ())
            return await self._scalar(f"SELECT COUNT(DISTINCT user_id) FROM visits WHERE {where}{skip}", args + ex)

        # a new user: their first day in Tashkent time (created_at is UTC)
        first_day = "date(created_at, '+5 hours')"

        async def new(since: str, until: str | None = None) -> int:
            where = f"{first_day}>=?" + (f" AND {first_day}<=?" if until else "")
            args = (since,) + ((until,) if until else ())
            return await self._scalar(f"SELECT COUNT(*) FROM users WHERE {where}{skip_users}", args + ex)

        days = []
        for n in range(self.VISIT_DAYS):
            d = day(n)
            days.append({"day": d, "came": await came(d, d), "new": await new(d, d)})
        since_row = await self._one("SELECT MIN(day) AS d FROM visits")
        return {
            "users": await self._scalar(f"SELECT COUNT(*) FROM users WHERE 1=1{skip_users}", ex),
            "new_today": await new(day(0)), "new_7": await new(day(6)), "new_30": await new(day(29)),
            "today": await came(day(0)), "today_bot": await came(day(0), source="bot"),
            "today_app": await came(day(0), source="app"),
            "yesterday": await came(day(1), day(1)),
            "d7": await came(day(6)), "d7_app": await came(day(6), source="app"),
            "d30": await came(day(29)),
            "days": days,
            "since": since_row["d"] if since_row else None,  # counting started that day
        }

    async def admins_langs(self, admin_ids: frozenset[int]) -> dict[int, str]:
        if not admin_ids:
            return {}
        marks = ",".join("?" * len(admin_ids))
        rows = await self._all(f"SELECT id, lang FROM users WHERE id IN ({marks})", tuple(admin_ids))
        found = {r["id"]: r["lang"] or "ru" for r in rows}
        return {a: found.get(a, "ru") for a in admin_ids}

    # -------------------------------------------------------------------- fsm
    async def fsm_get(self, key: str) -> tuple[str | None, dict[str, Any]]:
        row = await self._one("SELECT state, data FROM fsm WHERE key=?", (key,))
        if not row:
            return None, {}
        return row["state"], json.loads(row["data"] or "{}")

    async def fsm_set_state(self, key: str, state: str | None) -> None:
        async with self.tx():
            await self.db.execute(
                "INSERT INTO fsm(key, state, data) VALUES (?,?,'{}') ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                (key, state),
            )

    async def fsm_set_data(self, key: str, data: dict[str, Any]) -> None:
        async with self.tx():
            await self.db.execute(
                "INSERT INTO fsm(key, state, data) VALUES (?,NULL,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (key, json.dumps(data, ensure_ascii=False)),
            )

    # -------------------------------------------------------------------- settings
    async def get_setting(self, key: str) -> Any:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    async def set_setting(self, key: str, value: Any) -> None:
        async with self.tx():
            await self.db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )

    async def reprice(self, today: date | None = None) -> int:
        """Every day: one installment fewer for each month the seller has paid since listing, and the automatic
        price lowered to what the days still left are worth. Listings in a deal (reserved) keep their price.
        Returns how many prices changed."""
        today = today or today_local()
        rows = await self._all(
            "SELECT * FROM listings WHERE status IN (?,?) AND kind=? AND end_date>=?",
            (LS.PENDING, LS.APPROVED, LK.MEMBERSHIP, today.isoformat()),
        )
        changed = 0
        for listing in rows:
            async with self.tx():
                listing = await self.get_listing(listing["id"])  # fresh: a deal may have started meanwhile
                if listing["status"] not in (LS.PENDING, LS.APPROVED):
                    continue
                left, as_of = listing["payments_left"], listing["payments_as_of"]
                if listing["paid_in_full"] == 0 and left and as_of:
                    paid = int((today - date.fromisoformat(as_of)).days // DAYS_PER_MONTH)
                    if paid > 0:
                        # the price was «monthly × payments left»: it follows the payments down
                        derived = listing["price"] == listing["monthly_payment"] * listing["payments_left"]
                        left = max(0, left - paid)
                        as_of = (date.fromisoformat(as_of) + timedelta(days=round(paid * DAYS_PER_MONTH))).isoformat()
                        if left:
                            await self.db.execute("UPDATE listings SET payments_left=?, payments_as_of=? WHERE id=?",
                                                  (left, as_of, listing["id"]))
                            if derived:
                                await self.db.execute("UPDATE listings SET price=? WHERE id=?",
                                                      (listing["monthly_payment"] * left, listing["id"]))
                        else:  # all paid off: from now on it is a fully paid membership
                            await self.db.execute(
                                "UPDATE listings SET paid_in_full=1, monthly_payment=NULL, payments_left=NULL, "
                                "payments_as_of=NULL WHERE id=?", (listing["id"],))
                        listing = await self.get_listing(listing["id"])
                if not (listing["price_auto"] and listing["tariff_days"]):
                    continue
                installment = listing["paid_in_full"] == 0
                a = advise(listing["end_date"], today, self.fit_prices,
                           listing["monthly_payment"] if installment else None,
                           listing["payments_left"] if installment else None, plan_days=listing["tariff_days"])
                if a and a.recommended != listing["price"]:
                    await self.db.execute(
                        "UPDATE listings SET price=?, commission_accepted_price=? WHERE id=?",
                        (a.recommended, a.recommended, listing["id"]))
                    await self._log(listing["id"], None, "repriced", old=listing["price"], new=a.recommended)
                    changed += 1
        return changed

    async def set_fit_prices(self, prices: dict[int, int], admin_id: int | None = None) -> None:
        clean = normalize_prices(prices)
        async with self.tx():
            await self.db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES ('fit_prices',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (json.dumps({str(k): v for k, v in clean.items()}), now_iso()),
            )
            await self._log(None, admin_id, "fit_prices", prices={str(k): v for k, v in clean.items()})
        self.fit_prices = clean
        await self.reprice()  # automatic prices follow 1Fit's

    async def log_price_check(self, status: str, prices: dict[int, int] | None = None,
                              full: dict[int, int] | None = None, error: str | None = None) -> None:
        dump = lambda d: json.dumps({str(k): v for k, v in d.items()}) if d else None  # noqa: E731
        async with self.tx():
            await self.db.execute(
                "INSERT INTO fit_price_checks(checked_at, status, prices, full, error) VALUES (?,?,?,?,?)",
                (now_iso(), status, dump(prices), dump(full), error),
            )

    async def last_price_checks(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM fit_price_checks ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            for k in ("prices", "full"):
                r[k] = {int(d): v for d, v in json.loads(r[k]).items()} if r[k] else None
        return rows
