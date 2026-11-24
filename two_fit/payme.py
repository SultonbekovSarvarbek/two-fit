"""Payme Merchant API: the listing fee paid on Payme's own checkout page.

How a payment goes:
1. The seller opens ``checkout_url`` (Payme's page) — the bot gives it as a button, the Mini App opens it.
2. Payme calls our endpoint (``POST /payme``, JSON-RPC, Basic auth ``Paycom:<key>``):
   CheckPerformTransaction (may this listing be paid? also returns the fiscal receipt items),
   CreateTransaction (Payme holds the money), PerformTransaction (paid: the listing goes to moderation),
   CancelTransaction (before or after perform), CheckTransaction and GetStatement (reconciliation).
3. Amounts are in tiyin, times in Unix milliseconds. The account names the listing: ``ac.<account>=<id>``.

Docs: https://developer.help.paycom.uz/metody-merchant-api/
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .config import Config
from .repo import LS, Repo

log = logging.getLogger(__name__)

SUBUNITS = 100            # 1 сум = 100 tiyin
TIMEOUT_MS = 12 * 3600 * 1000  # a created transaction not performed within 12 hours is cancelled
CHECKOUT = "https://checkout.paycom.uz"
CHECKOUT_TEST = "https://checkout.test.paycom.uz"
KEY_SETTING = "payme_key"  # a key Payme set with ChangePassword overrides the one in .env…

# Transaction states and cancel reasons (as Payme numbers them)
CREATED, PERFORMED, CANCELLED, CANCELLED_AFTER_PERFORM = 1, 2, -1, -2
REASON_TIMEOUT = 4

# Error codes
E_PARSE, E_RPC, E_METHOD, E_POST, E_AUTH, E_SYSTEM = -32700, -32600, -32601, -32300, -32504, -32400
E_AMOUNT, E_NO_TX, E_CANT_CANCEL, E_CANT_PERFORM = -31001, -31003, -31007, -31008
# Account errors (-31050…-31099, «data» names the account field): the sandbox expects them for an order
# that does not exist, is already paid or withdrawn («Заблокирован»), or is held by another payment («В процессе»).
E_NO_ORDER, E_NOT_PAYABLE, E_BUSY = -31050, -31051, -31052

MESSAGES = {
    E_PARSE: ("Ошибка разбора JSON", "JSON таҳлилида хато", "Parse error"),
    E_RPC: ("Неверный запрос", "Нотўғри сўров", "Invalid request"),
    E_METHOD: ("Метод не найден", "Метод топилмади", "Method not found"),
    E_POST: ("Требуется POST", "POST талаб қилинади", "POST required"),
    E_AUTH: ("Недостаточно привилегий", "Ҳуқуқ етарли эмас", "Insufficient privileges"),
    E_SYSTEM: ("Системная ошибка", "Тизим хатоси", "System error"),
    E_AMOUNT: ("Неверная сумма", "Нотўғри сумма", "Wrong amount"),
    E_NO_TX: ("Транзакция не найдена", "Транзакция топилмади", "Transaction not found"),
    E_CANT_CANCEL: ("Нельзя отменить транзакцию", "Транзакцияни бекор қилиб бўлмайди", "Cannot cancel"),
    E_CANT_PERFORM: ("Невозможно выполнить операцию", "Амални бажариб бўлмайди", "Cannot perform"),
    E_NO_ORDER: ("Объявление не найдено", "Эълон топилмади", "Listing not found"),
    E_NOT_PAYABLE: ("Объявление уже оплачено или снято", "Эълон аллақачон тўланган ёки олинган",
                    "Listing is already paid or withdrawn"),
    E_BUSY: ("Объявление уже оплачивается", "Эълон ҳозир тўланмоқда", "Listing is being paid"),
}


class PaymeError(Exception):
    def __init__(self, code: int, data: str | None = None):
        super().__init__(code)
        self.code, self.data = code, data

    def body(self) -> dict[str, Any]:
        ru, uz, en = MESSAGES[self.code]
        error: dict[str, Any] = {"code": self.code, "message": {"ru": ru, "uz": uz, "en": en}}
        if self.data:
            error["data"] = self.data
        return error


def now_ms() -> int:
    return int(time.time() * 1000)


def checkout_url(cfg: Config, listing_id: int, amount: int, lang: str = "ru", return_url: str | None = None) -> str:
    """Payme's checkout page for this listing's fee (``amount`` in сум)."""
    params = [f"m={cfg.payme_merchant_id}", f"ac.{cfg.payme_account}={listing_id}", f"a={amount * SUBUNITS}",
              f"l={lang if lang in ('ru', 'uz', 'en') else 'ru'}"]
    if return_url:
        params.append(f"c={return_url}")
    token = base64.b64encode(";".join(params).encode()).decode()
    return f"{CHECKOUT_TEST if cfg.payme_test else CHECKOUT}/{token}"


Paid = Callable[[dict[str, Any], bool, int], Awaitable[None]]      # listing, first time, amount in сум
Refunded = Callable[[dict[str, Any], int], Awaitable[None]]         # listing, amount in сум


class Payme:
    def __init__(self, repo: Repo, cfg: Config, receipt_title: Callable[[int], str],
                 on_paid: Paid | None = None, on_refunded: Refunded | None = None):
        self.repo, self.cfg, self.receipt_title = repo, cfg, receipt_title
        self.on_paid, self.on_refunded = on_paid, on_refunded

    # ------------------------------------------------------------------ entry
    async def handle(self, raw: bytes, authorization: str | None) -> dict[str, Any]:
        """One JSON-RPC call; always an answer for Payme (HTTP 200), errors included."""
        request_id = None
        try:
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise PaymeError(E_PARSE) from None
            if not isinstance(body, dict):
                raise PaymeError(E_RPC)
            request_id = body.get("id")
            if not await self._authorized(authorization):
                raise PaymeError(E_AUTH)
            method, params = body.get("method"), body.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise PaymeError(E_RPC)
            handler = self.METHODS.get(method)
            if handler is None:
                raise PaymeError(E_METHOD, method)
            result, after = await handler(self, params)
            if after is not None:  # notifications run after the database transaction is committed
                try:
                    await after()
                except Exception:  # noqa: BLE001 — the payment is recorded; a failed message must not undo it
                    log.exception("payme: notification failed")
            return {"id": request_id, "result": result}
        except PaymeError as e:
            return {"id": request_id, "error": e.body()}
        except Exception:  # noqa: BLE001
            log.exception("payme: request failed")
            return {"id": request_id, "error": PaymeError(E_SYSTEM).body()}

    def _key_setting(self) -> str:
        """…but only that .env key: a new PAYME_KEY (the live key after the sandbox) starts clean."""
        return f"{KEY_SETTING}:{hashlib.sha256(self.cfg.payme_key.encode()).hexdigest()[:16]}"

    async def _key(self) -> str:
        return (await self.repo.get_setting(self._key_setting())) or self.cfg.payme_key

    async def _authorized(self, header: str | None) -> bool:
        scheme, _, value = (header or "").partition(" ")
        if scheme.lower() != "basic" or not value:
            return False
        try:
            login, _, password = base64.b64decode(value.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        key = await self._key()
        return bool(key) and login == "Paycom" and hmac.compare_digest(password.encode(), key.encode())

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _int(params: dict[str, Any], name: str) -> int:
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise PaymeError(E_RPC, name)
        return int(value)

    @staticmethod
    def _str(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value:
            raise PaymeError(E_RPC, name)
        return value

    async def _payable(self, params: dict[str, Any]) -> dict[str, Any]:
        """The listing named by the account, if its fee may be paid with this amount."""
        amount = self._int(params, "amount")
        account = params.get("account")
        field = self.cfg.payme_account
        raw = account.get(field) if isinstance(account, dict) else None
        try:
            listing_id = int(str(raw).strip())
        except ValueError:
            raise PaymeError(E_NO_ORDER, field) from None
        listing = await self.repo.get_listing(listing_id)
        if not listing or not listing["fee_amount"]:
            raise PaymeError(E_NO_ORDER, field)
        if amount != listing["fee_amount"] * SUBUNITS:
            raise PaymeError(E_AMOUNT, "amount")
        if listing["status"] != LS.UNPAID or not Repo.fee_due(listing):
            raise PaymeError(E_NOT_PAYABLE, field)  # already paid or withdrawn («Заблокирован»)
        return listing

    async def _not_busy(self, listing: dict[str, Any], payme_id: str | None = None) -> None:
        """Another payment of this listing is open («В процессе» in the sandbox): it cannot be paid twice.
        An open one past the 12 hours is cancelled on the way (caller holds tx())."""
        for other in await self.repo.payme_active(listing["id"]):
            if other["payme_id"] == payme_id:
                continue
            if not self._expired(other):
                raise PaymeError(E_BUSY, self.cfg.payme_account)
            await self._timeout(other)

    def _detail(self, listing: dict[str, Any]) -> dict[str, Any] | None:
        """The fiscal receipt: one item, the listing fee. Without an IKPU there is no receipt to send."""
        if not self.cfg.payme_ikpu:
            return None
        return {"receipt_type": 0, "items": [{
            "title": self.receipt_title(listing["id"]),
            "price": listing["fee_amount"] * SUBUNITS,
            "count": 1,
            "code": self.cfg.payme_ikpu,
            "package_code": self.cfg.payme_package_code,
            "vat_percent": self.cfg.payme_vat_percent,
        }]}

    @staticmethod
    def _expired(tx: dict[str, Any]) -> bool:
        return now_ms() - tx["create_time"] > TIMEOUT_MS

    async def _timeout(self, tx: dict[str, Any]) -> None:
        await self.repo.payme_update(tx["payme_id"], state=CANCELLED, cancel_time=now_ms(), reason=REASON_TIMEOUT)

    @staticmethod
    def _state(tx: dict[str, Any]) -> dict[str, Any]:
        return {"transaction": str(tx["id"]), "state": tx["state"]}

    # ------------------------------------------------------------------ methods
    async def check_perform(self, params: dict[str, Any]):
        async with self.repo.tx():
            listing = await self._payable(params)
            await self._not_busy(listing)
        result: dict[str, Any] = {"allow": True}
        detail = self._detail(listing)
        if detail:
            result["detail"] = detail
        return result, None

    async def create(self, params: dict[str, Any]):
        payme_id, payme_time = self._str(params, "id"), self._int(params, "time")
        async with self.repo.tx():
            tx = await self.repo.payme_get(payme_id)
            if tx is None:
                listing = await self._payable(params)
                await self._not_busy(listing)
                if now_ms() - payme_time > TIMEOUT_MS:
                    raise PaymeError(E_CANT_PERFORM)
                tx = await self.repo.payme_insert(payme_id, listing["id"], listing["fee_amount"] * SUBUNITS,
                                                  payme_time, now_ms())
            elif tx["state"] != CREATED:
                raise PaymeError(E_CANT_PERFORM)
            elif self._expired(tx):
                await self._timeout(tx)
                tx = None  # the cancel is kept: the error goes out after the commit
        if tx is None:
            raise PaymeError(E_CANT_PERFORM)
        return {"create_time": tx["create_time"], **self._state(tx)}, None

    async def perform(self, params: dict[str, Any]):
        payme_id = self._str(params, "id")
        after = None
        async with self.repo.tx():
            tx = await self.repo.payme_get(payme_id)
            if tx is None:
                raise PaymeError(E_NO_TX)
            if tx["state"] == CREATED and self._expired(tx):
                await self._timeout(tx)
                tx = None  # the cancel is kept: the error goes out after the commit
            elif tx["state"] == CREATED:
                listing = await self.repo.get_listing(tx["listing_id"])
                amount = tx["amount"] // SUBUNITS
                listing, first = await self.repo._record_fee(
                    tx["listing_id"], listing["seller_id"], amount, "UZS", f"payme:{payme_id}", payme_id)
                tx = await self.repo.payme_update(payme_id, state=PERFORMED, perform_time=now_ms())
                if self.on_paid:
                    after = lambda: self.on_paid(listing, first, amount)  # noqa: E731
            elif tx["state"] != PERFORMED:
                raise PaymeError(E_CANT_PERFORM)
        if tx is None:
            raise PaymeError(E_CANT_PERFORM)
        return {"perform_time": tx["perform_time"], **self._state(tx)}, after

    async def cancel(self, params: dict[str, Any]):
        payme_id, reason = self._str(params, "id"), self._int(params, "reason")
        after = None
        async with self.repo.tx():
            tx = await self.repo.payme_get(payme_id)
            if tx is None:
                raise PaymeError(E_NO_TX)
            if tx["state"] == CREATED:
                tx = await self.repo.payme_update(payme_id, state=CANCELLED, cancel_time=now_ms(), reason=reason)
            elif tx["state"] == PERFORMED:
                # The money goes back to the payer: the listing owes the fee again.
                listing = await self.repo._refund_fee(tx["listing_id"], f"payme:{payme_id}")
                tx = await self.repo.payme_update(payme_id, state=CANCELLED_AFTER_PERFORM, cancel_time=now_ms(),
                                                  reason=reason)
                if self.on_refunded and listing:
                    amount = tx["amount"] // SUBUNITS
                    after = lambda: self.on_refunded(listing, amount)  # noqa: E731
        return {"cancel_time": tx["cancel_time"], **self._state(tx)}, after

    async def check(self, params: dict[str, Any]):
        tx = await self.repo.payme_get(self._str(params, "id"))
        if tx is None:
            raise PaymeError(E_NO_TX)
        return {"create_time": tx["create_time"], "perform_time": tx["perform_time"],
                "cancel_time": tx["cancel_time"], **self._state(tx), "reason": tx["reason"]}, None

    async def statement(self, params: dict[str, Any]):
        rows = await self.repo.payme_statement(self._int(params, "from"), self._int(params, "to"))
        return {"transactions": [{
            "id": tx["payme_id"], "time": tx["payme_time"], "amount": tx["amount"],
            "account": {self.cfg.payme_account: str(tx["listing_id"])},
            "create_time": tx["create_time"], "perform_time": tx["perform_time"], "cancel_time": tx["cancel_time"],
            **self._state(tx), "reason": tx["reason"],
        } for tx in rows]}, None

    async def change_password(self, params: dict[str, Any]):
        await self.repo.set_setting(self._key_setting(), self._str(params, "password"))
        log.warning("payme: the cash desk key was changed by Payme (ChangePassword)")
        return {"success": True}, None

    METHODS = {
        "CheckPerformTransaction": check_perform,
        "CreateTransaction": create,
        "PerformTransaction": perform,
        "CancelTransaction": cancel,
        "CheckTransaction": check,
        "GetStatement": statement,
        "ChangePassword": change_password,
    }
