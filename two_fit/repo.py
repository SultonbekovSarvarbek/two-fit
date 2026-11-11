"""SQLite storage and all business rules of the marketplace.

Every state change goes through this module, so the rules (who may do what,
no double sale, no double commission) live in one place and are covered by tests.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .money import calc_commission

# Uzbekistan has no DST: UTC+5 all year round.
TZ = timezone(timedelta(hours=5))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_local() -> date:
    return datetime.now(TZ).date()


class LS:  # listing statuses
    PENDING = "pending"      # waiting for moderation
    APPROVED = "approved"    # published in the catalog
    REJECTED = "rejected"    # rejected by admin with a reason
    WITHDRAWN = "withdrawn"  # taken down by the seller
    REMOVED = "removed"      # taken down by an admin
    RESERVED = "reserved"    # admin picked a buyer, deal is being agreed
    SOLD = "sold"            # deal closed

    ALL = (PENDING, APPROVED, REJECTED, WITHDRAWN, REMOVED, RESERVED, SOLD)
    SELLER_EDITABLE = (PENDING, APPROVED, REJECTED, WITHDRAWN, REMOVED)
    SELLER_WITHDRAWABLE = (PENDING, APPROVED, REJECTED)
    ADMIN_REMOVABLE = (PENDING, APPROVED, REJECTED)


class RS:  # purchase request statuses
    NEW = "new"
    SELECTED = "selected"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CLOSED = "closed"

    ACTIVE = (NEW, SELECTED)


class CS:  # commission statuses
    PENDING = "pending"
    PAID = "paid"


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

CREATE TABLE IF NOT EXISTS listings (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id                 INTEGER NOT NULL REFERENCES users(id),
    lang                      TEXT NOT NULL,
    city                      TEXT NOT NULL,
    tariff                    TEXT NOT NULL,
    end_date                  TEXT NOT NULL,      -- YYYY-MM-DD
    price                     INTEGER NOT NULL CHECK (price > 0),
    description               TEXT NOT NULL DEFAULT '',
    screenshot_file_id        TEXT,
    seller_name               TEXT NOT NULL,
    seller_phone              TEXT NOT NULL,
    commission_accepted_price INTEGER NOT NULL,   -- price the seller explicitly accepted 10% for
    status                    TEXT NOT NULL,
    reject_reason             TEXT,
    reserved_request_id       INTEGER,
    final_price               INTEGER,
    final_price_confirmed     INTEGER NOT NULL DEFAULT 0,
    sale_reported_at          TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    published_at              TEXT
);
CREATE INDEX IF NOT EXISTS ix_listings_catalog ON listings(status, end_date, city, published_at);
CREATE INDEX IF NOT EXISTS ix_listings_seller ON listings(seller_id);

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
"""


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Repo:
    def __init__(self, db_path: str | Path, commission_percent: int = 10):
        self.db_path = str(db_path)
        self.commission_percent = commission_percent
        self._db: aiosqlite.Connection | None = None
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
    async def touch_user(self, user_id: int, username: str | None, tg_name: str | None) -> dict[str, Any]:
        async with self.tx():
            ts = now_iso()
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
        return bool(listing) and listing["status"] == LS.APPROVED and not self.is_expired(listing, today)

    @staticmethod
    def _check_draft(draft: dict[str, Any]) -> None:
        required = ("city", "tariff", "end_date", "price", "seller_name", "seller_phone")
        if any(draft.get(k) in (None, "") for k in required):
            raise RepoError("incomplete")
        if draft.get("commission_accepted_price") != draft["price"]:
            # The seller must have accepted the commission for exactly this price.
            raise RepoError("commission_not_accepted")

    async def create_listing(self, seller_id: int, lang: str, draft: dict[str, Any]) -> int:
        self._check_draft(draft)
        async with self.tx():
            ts = now_iso()
            cur = await self.db.execute(
                """INSERT INTO listings(seller_id, lang, city, tariff, end_date, price, description,
                       screenshot_file_id, seller_name, seller_phone, commission_accepted_price,
                       status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    seller_id, lang, draft["city"], draft["tariff"], draft["end_date"], draft["price"],
                    draft.get("description") or "", draft.get("screenshot_file_id"),
                    draft["seller_name"], draft["seller_phone"], draft["price"], LS.PENDING, ts, ts,
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
            await self.db.execute(
                """UPDATE listings SET lang=?, city=?, tariff=?, end_date=?, price=?, description=?,
                       screenshot_file_id=?, seller_name=?, seller_phone=?, commission_accepted_price=?,
                       status=?, reject_reason=NULL, published_at=NULL, sale_reported_at=NULL, updated_at=?
                   WHERE id=?""",
                (
                    lang, draft["city"], draft["tariff"], draft["end_date"], draft["price"],
                    draft.get("description") or "", draft.get("screenshot_file_id"),
                    draft["seller_name"], draft["seller_phone"], draft["price"], LS.PENDING, now_iso(), listing_id,
                ),
            )
            await self._log(listing_id, seller_id, "edited", price=draft["price"])

    async def seller_listings(self, seller_id: int, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        total = await self._scalar("SELECT COUNT(*) FROM listings WHERE seller_id=?", (seller_id,))
        rows = await self._all(
            "SELECT * FROM listings WHERE seller_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (seller_id, limit, offset)
        )
        return rows, total

    async def withdraw_by_seller(self, listing_id: int, seller_id: int) -> list[int]:
        """Returns buyer ids whose open requests were closed."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            if listing["status"] not in LS.SELLER_WITHDRAWABLE:
                raise RepoError("bad_status")
            await self.db.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?", (LS.WITHDRAWN, now_iso(), listing_id)
            )
            buyers = await self._close_open_requests(listing_id)
            await self._log(listing_id, seller_id, "withdrawn")
            return buyers

    async def report_sale(self, listing_id: int, seller_id: int) -> bool:
        """Seller says it was sold. Returns False if already reported. Admin still closes the deal."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            if listing["status"] not in (LS.APPROVED, LS.RESERVED):
                raise RepoError("bad_status")
            if listing["sale_reported_at"]:
                return False
            await self.db.execute(
                "UPDATE listings SET sale_reported_at=?, updated_at=? WHERE id=?", (now_iso(), now_iso(), listing_id)
            )
            await self._log(listing_id, seller_id, "sale_reported")
            return True

    # ---------------------------------------------------------------- catalog
    async def catalog(self, city: str | None, limit: int, offset: int,
                      today: date | None = None) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {"st": LS.APPROVED, "today": (today or today_local()).isoformat()}
        where = "status=:st AND end_date>=:today"
        if city:
            where += " AND city=:city"
            params["city"] = city
        total = await self._scalar(f"SELECT COUNT(*) FROM listings WHERE {where}", params)
        rows = await self._all(
            f"SELECT * FROM listings WHERE {where} ORDER BY published_at DESC, id DESC LIMIT :lim OFFSET :off",
            {**params, "lim": limit, "off": offset},
        )
        return rows, total

    async def catalog_cities(self, today: date | None = None) -> list[tuple[str, int]]:
        rows = await self._all(
            """SELECT city, COUNT(*) AS n FROM listings WHERE status=? AND end_date>=?
               GROUP BY city ORDER BY n DESC, city""",
            (LS.APPROVED, (today or today_local()).isoformat()),
        )
        return [(r["city"], r["n"]) for r in rows]

    # --------------------------------------------------------------- requests
    async def create_request(self, listing_id: int, buyer_id: int, name: str, phone: str,
                             today: date | None = None) -> int:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not self.is_available(listing, today):
                raise RepoError("not_available")
            if listing["seller_id"] == buyer_id:
                raise RepoError("own")
            if await self.active_request(listing_id, buyer_id):
                raise RepoError("duplicate")
            ts = now_iso()
            cur = await self.db.execute(
                """INSERT INTO purchase_requests(listing_id, buyer_id, buyer_name, buyer_phone, status,
                       created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
                (listing_id, buyer_id, name, phone, RS.NEW, ts, ts),
            )
            await self._log(listing_id, buyer_id, "request_created", request_id=cur.lastrowid)
            return cur.lastrowid

    async def active_request(self, listing_id: int, buyer_id: int) -> dict[str, Any] | None:
        return await self._one(
            "SELECT * FROM purchase_requests WHERE listing_id=? AND buyer_id=? AND status IN ('new','selected')",
            (listing_id, buyer_id),
        )

    async def get_request(self, request_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM purchase_requests WHERE id=?", (request_id,))

    async def listing_requests(self, listing_id: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM purchase_requests WHERE listing_id=? ORDER BY id DESC", (listing_id,)
        )

    async def active_requests(self, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        total = await self._scalar("SELECT COUNT(*) FROM purchase_requests WHERE status IN ('new','selected')")
        rows = await self._all(
            """SELECT * FROM purchase_requests WHERE status IN ('new','selected')
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return rows, total

    async def _close_open_requests(self, listing_id: int, exclude_id: int | None = None) -> list[int]:
        rows = await self._all(
            "SELECT id, buyer_id FROM purchase_requests WHERE listing_id=? AND status IN ('new','selected')",
            (listing_id,),
        )
        rows = [r for r in rows if r["id"] != exclude_id]
        for r in rows:
            await self.db.execute(
                "UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (RS.CLOSED, now_iso(), r["id"])
            )
        return [r["buyer_id"] for r in rows]

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
            buyers = await self._close_open_requests(listing_id)
            await self._log(listing_id, admin_id, "removed")
            return await self.get_listing(listing_id), buyers

    async def reserve(self, request_id: int, admin_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Admin picks a buyer: the listing leaves the catalog while the deal is agreed."""
        async with self.tx():
            req = await self.get_request(request_id)
            if not req:
                raise RepoError("not_found")
            if req["status"] != RS.NEW:
                raise RepoError("bad_status")
            listing = await self.get_listing(req["listing_id"])
            if listing["status"] != LS.APPROVED:
                raise RepoError("bad_status")
            ts = now_iso()
            cur = await self.db.execute(
                """UPDATE listings SET status=?, reserved_request_id=?, final_price=NULL,
                       final_price_confirmed=0, updated_at=? WHERE id=? AND status=?""",
                (LS.RESERVED, request_id, ts, listing["id"], LS.APPROVED),
            )
            if cur.rowcount != 1:
                raise RepoError("bad_status")
            await self.db.execute(
                "UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (RS.SELECTED, ts, request_id)
            )
            await self._log(listing["id"], admin_id, "reserved", request_id=request_id)
            return await self.get_listing(listing["id"]), await self.get_request(request_id)

    async def release_reserve(self, listing_id: int, admin_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] != LS.RESERVED:
                raise RepoError("bad_status")
            ts = now_iso()
            req = await self.get_request(listing["reserved_request_id"]) if listing["reserved_request_id"] else None
            if req and req["status"] == RS.SELECTED:
                await self.db.execute(
                    "UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (RS.CANCELLED, ts, req["id"])
                )
            await self.db.execute(
                """UPDATE listings SET status=?, reserved_request_id=NULL, final_price=NULL,
                       final_price_confirmed=0, sale_reported_at=NULL, updated_at=? WHERE id=?""",
                (LS.APPROVED, ts, listing_id),
            )
            await self._log(listing_id, admin_id, "reserve_released")
            return await self.get_listing(listing_id), req

    async def set_final_price(self, listing_id: int, admin_id: int, price: int) -> tuple[dict[str, Any], bool]:
        """Returns (listing, needs_seller_confirmation)."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if listing["status"] != LS.RESERVED:
                raise RepoError("bad_status")
            confirmed = 1 if price == listing["commission_accepted_price"] else 0
            await self.db.execute(
                "UPDATE listings SET final_price=?, final_price_confirmed=?, updated_at=? WHERE id=?",
                (price, confirmed, now_iso(), listing_id),
            )
            await self._log(listing_id, admin_id, "final_price_set", price=price, auto_confirmed=bool(confirmed))
            return await self.get_listing(listing_id), not confirmed

    async def seller_answer_price(self, listing_id: int, seller_id: int, price: int, accept: bool) -> dict[str, Any]:
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing or listing["seller_id"] != seller_id:
                raise RepoError("not_found")
            # Button is stale if the admin changed the price again or the deal moved on.
            if listing["status"] != LS.RESERVED or listing["final_price"] != price:
                raise RepoError("stale")
            if listing["final_price_confirmed"] and accept:
                raise RepoError("already")
            if accept:
                await self.db.execute(
                    """UPDATE listings SET final_price_confirmed=1, commission_accepted_price=?, updated_at=?
                       WHERE id=?""",
                    (price, now_iso(), listing_id),
                )
            else:
                await self.db.execute(
                    "UPDATE listings SET final_price=NULL, final_price_confirmed=0, updated_at=? WHERE id=?",
                    (now_iso(), listing_id),
                )
            await self._log(listing_id, seller_id, "price_accepted" if accept else "price_declined", price=price)
            return await self.get_listing(listing_id)

    async def close_deal(self, listing_id: int, admin_id: int) -> tuple[dict[str, Any], list[int]]:
        """Close the sale. Returns (deal, buyer ids of other requests that were closed)."""
        async with self.tx():
            listing = await self.get_listing(listing_id)
            if not listing:
                raise RepoError("not_found")
            if await self._one("SELECT id FROM deals WHERE listing_id=?", (listing_id,)):
                raise RepoError("already")
            if listing["status"] != LS.RESERVED:
                raise RepoError("bad_status")
            if not listing["final_price"] or not listing["final_price_confirmed"]:
                raise RepoError("price_not_confirmed")
            req = await self.get_request(listing["reserved_request_id"]) if listing["reserved_request_id"] else None
            c = calc_commission(listing["final_price"], self.commission_percent)
            ts = now_iso()
            cur = await self.db.execute(
                """INSERT INTO deals(listing_id, request_id, seller_id, seller_name, seller_phone, buyer_id,
                       buyer_name, buyer_phone, sale_price, commission_percent, commission_amount,
                       seller_amount, closed_at, closed_by, commission_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    listing_id, req["id"] if req else None, listing["seller_id"], listing["seller_name"],
                    listing["seller_phone"], req["buyer_id"] if req else None, req["buyer_name"] if req else None,
                    req["buyer_phone"] if req else None, c.price, c.percent, c.commission, c.net, ts, admin_id,
                    CS.PENDING,
                ),
            )
            deal_id = cur.lastrowid
            upd = await self.db.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=? AND status=?",
                (LS.SOLD, ts, listing_id, LS.RESERVED),
            )
            if upd.rowcount != 1:
                raise RepoError("already")
            if req:
                await self.db.execute(
                    "UPDATE purchase_requests SET status=?, updated_at=? WHERE id=?", (RS.COMPLETED, ts, req["id"])
                )
            others = await self._close_open_requests(listing_id, exclude_id=req["id"] if req else None)
            await self._log(listing_id, admin_id, "deal_closed", deal_id=deal_id, price=c.price,
                            commission=c.commission)
            return await self.get_deal(deal_id), others

    async def mark_commission_paid(self, deal_id: int, admin_id: int) -> dict[str, Any]:
        async with self.tx():
            deal = await self.get_deal(deal_id)
            if not deal:
                raise RepoError("not_found")
            cur = await self.db.execute(
                """UPDATE deals SET commission_status=?, commission_paid_at=?, commission_marked_by=?
                   WHERE id=? AND commission_status=?""",
                (CS.PAID, now_iso(), admin_id, deal_id, CS.PENDING),
            )
            if cur.rowcount != 1:
                raise RepoError("already")
            await self._log(deal["listing_id"], admin_id, "commission_paid", deal_id=deal_id,
                            amount=deal["commission_amount"])
            return await self.get_deal(deal_id)

    async def get_deal(self, deal_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM deals WHERE id=?", (deal_id,))

    async def deal_for_listing(self, listing_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM deals WHERE listing_id=?", (listing_id,))

    async def deals(self, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        total = await self._scalar("SELECT COUNT(*) FROM deals")
        rows = await self._all("SELECT * FROM deals ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return rows, total

    async def stats(self, today: date | None = None) -> dict[str, int]:
        t = (today or today_local()).isoformat()
        agg = await self._one(
            """SELECT COUNT(*) AS deals, COALESCE(SUM(sale_price),0) AS volume,
                      COALESCE(SUM(commission_amount),0) AS accrued,
                      COALESCE(SUM(CASE WHEN commission_status='paid' THEN commission_amount END),0) AS paid
               FROM deals"""
        )
        return {
            "active": await self._scalar(
                "SELECT COUNT(*) FROM listings WHERE status=? AND end_date>=?", (LS.APPROVED, t)
            ),
            "pending": await self.count_status(LS.PENDING),
            "reserved": await self.count_status(LS.RESERVED),
            "requests": await self._scalar("SELECT COUNT(*) FROM purchase_requests"),
            "active_requests": await self._scalar(
                "SELECT COUNT(*) FROM purchase_requests WHERE status IN ('new','selected')"
            ),
            "deals": agg["deals"],
            "volume": agg["volume"],
            "accrued": agg["accrued"],
            "paid": agg["paid"],
            "due": agg["accrued"] - agg["paid"],
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
