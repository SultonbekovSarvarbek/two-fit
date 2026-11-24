"""Payme Merchant API: what Payme calls on POST /payme, and the seller's way to Payme's checkout page."""
import base64
from dataclasses import replace
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from two_fit import payme
from two_fit.i18n import I18n
from two_fit.repo import LS, today_local
from two_fit.webapp.server import create_webapp

from .harness import Harness, buttons_of
from .test_webapp import Api, draft

SELLER, ADMIN = 1, 900
KEY = "test-key"
FEE = 30_000
TIYIN = FEE * 100


def basic(password: str = KEY, login: str = "Paycom") -> str:
    return "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()


class Rpc:
    def __init__(self, client: TestClient):
        self.client, self.n = client, 0

    async def __call__(self, method: str, auth: str | None = None, **params):
        self.n += 1
        headers = {"Authorization": auth or basic()}
        res = await self.client.post("/payme", json={"method": method, "params": params, "id": self.n}, headers=headers)
        assert res.status == 200
        body = await res.json()
        assert body["id"] == self.n
        return body

    async def ok(self, method: str, **params):
        body = await self(method, **params)
        assert "error" not in body, body
        return body["result"]

    async def err(self, method: str, **params) -> int:
        body = await self(method, **params)
        assert "result" not in body, body
        return body["error"]["code"]


@pytest.fixture
async def env(tmp_path):
    h = Harness(tmp_path / "payme.db", fee=FEE)
    h.cfg = replace(h.cfg, payme_merchant_id="m-123", payme_key=KEY, payme_ikpu="10305008002000000",
                    payme_package_code="1545643", payme_vat_percent=12)
    await h.start()
    await h.repo.touch_user(ADMIN, "boss", "Boss")
    app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
    async with TestClient(TestServer(app)) as client:
        yield h, Api(client, h.cfg.bot_token), Rpc(client)
    await h.stop()


async def new_listing(api) -> int:
    status, made = await api.call("POST", "/api/listings", SELLER, draft())
    assert status == 201
    return made["id"]


def account(lid) -> dict:
    return {"order_id": str(lid)}


async def test_needs_payme_credentials(env):
    h, api, rpc = env
    lid = await new_listing(api)
    for auth in ("", "Basic xxx", basic("wrong"), basic(KEY, login="Other"), "Bearer " + KEY):
        body = await rpc("CheckPerformTransaction", auth=auth or "None", amount=TIYIN, account=account(lid))
        assert body["error"]["code"] == payme.E_AUTH
    assert await rpc.err("NoSuchMethod") == payme.E_METHOD


async def test_full_payment(env):
    h, api, rpc = env
    lid = await new_listing(api)
    assert (await h.repo.get_listing(lid))["status"] == LS.UNPAID

    assert await rpc.err("CheckPerformTransaction", amount=TIYIN - 100, account=account(lid)) == payme.E_AMOUNT
    assert await rpc.err("CheckPerformTransaction", amount=TIYIN, account=account(9999)) == payme.E_NO_ORDER
    assert await rpc.err("CheckPerformTransaction", amount=TIYIN, account={}) == payme.E_NO_ORDER
    check = await rpc.ok("CheckPerformTransaction", amount=TIYIN, account=account(lid))
    assert check["allow"] is True
    item, = check["detail"]["items"]
    assert check["detail"]["receipt_type"] == 0
    assert item == {"title": f"Размещение объявления №{lid}", "price": TIYIN, "count": 1,
                    "code": "10305008002000000", "package_code": "1545643", "vat_percent": 12}

    t = payme.now_ms()
    created = await rpc.ok("CreateTransaction", id="tx-1", time=t, amount=TIYIN, account=account(lid))
    assert created["state"] == 1 and created["transaction"]
    again = await rpc.ok("CreateTransaction", id="tx-1", time=t, amount=TIYIN, account=account(lid))
    assert again == created  # Payme may repeat a call: the same answer
    # a second payment of the same listing while the first one is open
    assert await rpc.err("CreateTransaction", id="tx-2", time=t, amount=TIYIN, account=account(lid)) == payme.E_BUSY

    done = await rpc.ok("PerformTransaction", id="tx-1")
    assert done["state"] == 2 and done["perform_time"] > 0
    listing = await h.repo.get_listing(lid)
    assert listing["status"] == LS.PENDING and listing["fee_paid_at"]
    assert "Оплата 30\xa0000 сум получена" in h.texts_to(SELLER)
    assert f"№{lid}" in h.texts_to(ADMIN) and "30\xa0000" in h.texts_to(ADMIN)
    sent = len(h.session.calls)
    assert await rpc.ok("PerformTransaction", id="tx-1") == done  # repeated: nothing new happens
    assert len(h.session.calls) == sent

    info = await rpc.ok("CheckTransaction", id="tx-1")
    assert (info["state"], info["perform_time"], info["cancel_time"], info["reason"]) == (2, done["perform_time"], 0, None)
    assert await rpc.err("CheckTransaction", id="nope") == payme.E_NO_TX
    assert await rpc.err("PerformTransaction", id="nope") == payme.E_NO_TX

    statement = await rpc.ok("GetStatement", **{"from": t - 1000, "to": t + 1000})
    tx, = statement["transactions"]
    assert (tx["id"], tx["amount"], tx["account"], tx["state"]) == ("tx-1", TIYIN, account(lid), 2)
    assert (await rpc.ok("GetStatement", **{"from": t + 1, "to": t + 1000}))["transactions"] == []

    # paid: Payme may not take it again
    assert await rpc.err("CheckPerformTransaction", amount=TIYIN, account=account(lid)) == payme.E_NOT_PAYABLE
    assert (await h.repo.stats())["payments"] == 1


async def test_cancel_before_perform(env):
    h, api, rpc = env
    lid = await new_listing(api)
    t = payme.now_ms()
    await rpc.ok("CreateTransaction", id="tx-1", time=t, amount=TIYIN, account=account(lid))
    cancelled = await rpc.ok("CancelTransaction", id="tx-1", reason=3)
    assert cancelled["state"] == -1 and cancelled["cancel_time"] > 0
    assert await rpc.ok("CancelTransaction", id="tx-1", reason=3) == cancelled
    assert await rpc.err("PerformTransaction", id="tx-1") == payme.E_CANT_PERFORM
    assert await rpc.err("CreateTransaction", id="tx-1", time=t, amount=TIYIN, account=account(lid)) == payme.E_CANT_PERFORM
    assert (await h.repo.get_listing(lid))["status"] == LS.UNPAID
    # the listing is free to be paid with another transaction
    await rpc.ok("CreateTransaction", id="tx-2", time=t, amount=TIYIN, account=account(lid))
    assert (await rpc.ok("CheckTransaction", id="tx-1"))["reason"] == 3


async def test_cancel_after_perform_returns_the_fee(env):
    h, api, rpc = env
    lid = await new_listing(api)
    await rpc.ok("CreateTransaction", id="tx-1", time=payme.now_ms(), amount=TIYIN, account=account(lid))
    await rpc.ok("PerformTransaction", id="tx-1")
    await h.repo.approve(lid, ADMIN)
    cancelled = await rpc.ok("CancelTransaction", id="tx-1", reason=5)
    assert cancelled["state"] == -2
    listing = await h.repo.get_listing(lid)
    assert (listing["status"], listing["fee_paid_at"]) == (LS.UNPAID, None)  # out of the catalog until paid again
    assert (await h.repo.stats())["payments"] == 0
    assert "отменена и возвращена" in h.texts_to(SELLER)
    assert "Payme отменил оплату" in h.texts_to(ADMIN)
    # paid again with a new transaction
    await rpc.ok("CreateTransaction", id="tx-2", time=payme.now_ms(), amount=TIYIN, account=account(lid))
    await rpc.ok("PerformTransaction", id="tx-2")
    assert (await h.repo.get_listing(lid))["status"] == LS.PENDING


async def test_unperformed_transaction_expires_after_12_hours(env):
    h, api, rpc = env
    lid = await new_listing(api)
    await rpc.ok("CreateTransaction", id="tx-1", time=payme.now_ms(), amount=TIYIN, account=account(lid))
    old = payme.now_ms() - payme.TIMEOUT_MS - 1000
    await h.repo.db.execute("UPDATE payme_transactions SET create_time=? WHERE payme_id='tx-1'", (old,))
    await h.repo.db.commit()
    assert await rpc.err("PerformTransaction", id="tx-1") == payme.E_CANT_PERFORM
    info = await rpc.ok("CheckTransaction", id="tx-1")
    assert (info["state"], info["reason"]) == (-1, payme.REASON_TIMEOUT)
    assert (await h.repo.get_listing(lid))["status"] == LS.UNPAID


async def test_change_password(env):
    h, api, rpc = env
    assert await rpc.ok("ChangePassword", password="new-key") == {"success": True}
    assert (await rpc("CheckTransaction", id="x"))["error"]["code"] == payme.E_AUTH  # the old key no longer works
    body = await rpc("CheckTransaction", auth=basic("new-key"), id="x")
    assert body["error"]["code"] == payme.E_NO_TX


def decode(url: str) -> str:
    return base64.b64decode(url.rsplit("/", 1)[1]).decode()


async def test_seller_gets_payme_checkout(env):
    h, api, rpc = env
    status, made = await api.call("POST", "/api/listings", SELLER, draft())
    lid = made["id"]
    url = made["invoice_url"]
    assert url.startswith(payme.CHECKOUT + "/")
    assert decode(url) == f"m=m-123;ac.order_id={lid};a={TIYIN};l=ru"
    _, mine = await api.call("GET", "/api/my", SELLER)
    assert mine["items"][0]["can_pay"] is True
    # the bot's «Pay» sends a button to Payme instead of a Telegram invoice
    from two_fit.bot.context import Ctx
    from two_fit.bot.payments import send_fee_invoice
    ctx = Ctx(h.repo, I18n(), h.cfg, {"id": SELLER, "lang": "uz"})
    await send_fee_invoice(h.bot, ctx, SELLER, await h.repo.get_listing(lid))
    button, = buttons_of(h.session.calls[-1])
    assert "Payme" in button.text and decode(button.url).endswith(";l=uz")


def test_checkout_url_in_the_sandbox():
    from two_fit.config import Config
    cfg = Config(bot_token="", admin_ids=frozenset(), support_username="x", db_path=None, commission_percent=0,
                 page_size=5, welcome_image=None, logo=None, payme_merchant_id="m", payme_key="k", payme_test=True,
                 payme_account="listing")
    url = payme.checkout_url(cfg, 7, 30_000, "en", "https://t.me/two_fit_bot")
    assert url.startswith(payme.CHECKOUT_TEST + "/")
    assert decode(url) == "m=m;ac.listing=7;a=3000000;l=en;c=https://t.me/two_fit_bot"


async def test_no_payme_endpoint_without_credentials(tmp_path):
    h = await Harness(tmp_path / "off.db").start()
    try:
        app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
        async with TestClient(TestServer(app)) as client:
            res = await client.post("/payme", json={"method": "CheckTransaction", "params": {}, "id": 1})
            assert res.status in (404, 405)
    finally:
        await h.stop()
