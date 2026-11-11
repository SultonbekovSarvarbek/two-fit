"""Mini App API through a real aiohttp server: signed initData, same rules as in the bot."""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from two_fit.i18n import I18n
from two_fit.repo import LS, today_local
from two_fit.webapp.server import create_webapp

from .harness import Harness, text_of

SELLER, BUYER, STRANGER, ADMIN = 1, 2, 3, 900
END = (today_local() + timedelta(days=200)).isoformat()


def init_data(token: str, uid: int, *, age: int = 0, lang: str = "ru") -> str:
    """What Telegram hands to the page: fields signed with a key derived from the bot token."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAF",
        "user": json.dumps({"id": uid, "first_name": f"Name{uid}", "username": f"user{uid}", "language_code": lang}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Api:
    def __init__(self, client: TestClient, token: str):
        self.client, self.token = client, token

    async def call(self, method, path, uid, body=None, **kw):
        headers = {"Authorization": "tma " + init_data(self.token, uid, **kw)}
        res = await self.client.request(method, path, json=body, headers=headers)
        return res.status, await res.json()


@pytest.fixture
async def h(tmp_path):
    harness = await Harness(tmp_path / "bot.db").start()
    yield harness
    await harness.stop()


@pytest.fixture
async def api(h):
    app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
    async with TestClient(TestServer(app)) as client:
        yield Api(client, h.cfg.bot_token)


def draft(**over):
    body = {"city": "tashkent", "tariff": "Безлимит 12 месяцев", "end_date": END, "price": "2 000 000",
            "description": "Осталось 7 месяцев", "name": "Алишер", "phone": "+998 90 111 22 33",
            "accepted_price": 2_000_000, "screenshot": None}
    body.update(over)
    return body


async def publish(h, api, uid=SELLER, **over) -> int:
    status, data = await api.call("POST", "/api/listings", uid, draft(**over))
    assert status == 201, data
    await h.repo.approve(data["id"], ADMIN)
    return data["id"]


# ------------------------------------------------------------------ access
async def test_page_is_served_without_auth(api):
    res = await api.client.get("/")
    assert res.status == 200 and "telegram-web-app.js" in await res.text()


async def test_api_needs_valid_telegram_signature(api):
    for headers in ({}, {"Authorization": "tma user=%7B%22id%22%3A1%7D&hash=00"},
                    {"Authorization": "tma " + init_data("42:OTHER", SELLER)},
                    {"Authorization": "Bearer " + init_data(api.token, SELLER)}):
        res = await api.client.get("/api/bootstrap", headers=headers)
        assert res.status == 401
    status, _ = await api.call("GET", "/api/bootstrap", SELLER, age=2 * 24 * 3600)
    assert status == 401  # an old initData string cannot be replayed forever
    status, _ = await api.call("GET", "/api/bootstrap", SELLER)
    assert status == 200


async def test_tampered_user_is_rejected(api):
    forged = init_data(api.token, SELLER).replace("Name1", "Name9")
    res = await api.client.get("/api/my", headers={"Authorization": "tma " + forged})
    assert res.status == 401


async def test_bootstrap_language_and_switch(h, api):
    status, data = await api.call("GET", "/api/bootstrap", SELLER, lang="uz")
    assert status == 200 and data["lang"] == "uz" and data["texts"]["app.tab.sell"] == "Сотиш"
    assert (await h.repo.get_user(SELLER))["lang"] is None  # a guess is not saved: the bot still asks
    # Only page texts leave the server — no admin texts
    assert not [k for k in data["texts"] if k.startswith(("admin.", "notify."))]
    status, data = await api.call("POST", "/api/lang", SELLER, {"lang": "ru"}, lang="uz")
    assert data["lang"] == "ru" and (await h.repo.get_user(SELLER))["lang"] == "ru"
    status, _ = await api.call("POST", "/api/lang", SELLER, {"lang": "en"})
    assert status == 400


# ------------------------------------------------------------------ selling
async def test_create_listing_goes_to_moderation_and_notifies_admin(h, api):
    await h.repo.touch_user(ADMIN, "boss", "Boss")
    status, data = await api.call("POST", "/api/listings", SELLER, draft())
    assert status == 201
    listing = await h.repo.get_listing(data["id"])
    assert listing["status"] == LS.PENDING and listing["price"] == 2_000_000
    assert listing["seller_phone"] == "+998901112233" and listing["commission_accepted_price"] == 2_000_000
    assert f"№{data['id']}" in h.texts_to(ADMIN)
    assert h.button(ADMIN, "Открыть").startswith(f"ad:open:{data['id']}:")
    # Not in the catalog until approved
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert cat["total"] == 0
    # Contacts are remembered for the next form
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert boot["user"] == {"name": "Алишер", "phone": "+998901112233"}


@pytest.mark.parametrize("over, code, field", [
    ({"accepted_price": None}, "commission_not_accepted", "accept"),
    ({"accepted_price": 1_500_000}, "commission_not_accepted", "accept"),  # consent was for another price
    ({"city": "moscow"}, "bad_city", "city"),
    ({"tariff": "x"}, "bad_tariff", "tariff"),
    ({"end_date": "2020-01-01"}, "bad_date_past", "end_date"),
    ({"end_date": "31.05.2027"}, "bad_date_format", "end_date"),
    ({"price": "100", "accepted_price": 100}, "bad_price", "price"),
    ({"price": 2000000.5}, "bad_price", "price"),
    ({"description": "пишите @alisher_fit"}, "bad_contacts", "description"),
    ({"description": "x" * 501}, "bad_desc_long", "description"),
    ({"name": ""}, "bad_name", "name"),
    ({"phone": "12"}, "bad_phone", "phone"),
    ({"screenshot": {"file_id": "someone-elses", "sig": "0" * 32}}, "bad_photo", "screenshot"),
])
async def test_create_listing_validation(h, api, over, code, field):
    status, data = await api.call("POST", "/api/listings", SELLER, draft(**over))
    assert (status, data["error"], data["field"]) == (400, code, field)
    assert data["message"] and "{" not in data["message"]
    assert (await h.repo.seller_listings(SELLER, 10, 0))[1] == 0


async def test_screenshot_upload_and_use(h, api):
    form = FormData()
    form.add_field("file", b"\x89PNG fake", filename="shot.png", content_type="image/png")
    headers = {"Authorization": "tma " + init_data(api.token, SELLER)}
    res = await api.client.post("/api/screenshot", data=form, headers=headers)
    assert res.status == 200
    shot = await res.json()
    assert "только администратор" in h.texts_to(SELLER)  # the picture went to the seller's own chat
    # The reference is bound to the uploader
    status, data = await api.call("POST", "/api/listings", STRANGER, draft(screenshot=shot))
    assert (status, data["error"]) == (400, "bad_photo")
    status, data = await api.call("POST", "/api/listings", SELLER, draft(screenshot=shot))
    assert status == 201
    assert (await h.repo.get_listing(data["id"]))["screenshot_file_id"] == shot["file_id"]

    form = FormData()
    form.add_field("file", b"not a picture", filename="a.txt", content_type="text/plain")
    res = await api.client.post("/api/screenshot", data=form, headers=headers)
    assert res.status == 400


# ------------------------------------------------------------------ catalog and buying
async def test_catalog_shows_only_public_fields(h, api):
    lid = await publish(h, api)
    await publish(h, api, uid=STRANGER, city="samarkand", price="900000", accepted_price=900_000)
    status, cat = await api.call("GET", "/api/catalog", BUYER)
    assert status == 200 and cat["total"] == 2
    assert {c["code"]: c["count"] for c in cat["cities"]} == {"tashkent": 1, "samarkand": 1}
    for item in cat["items"]:
        assert set(item) == {"id", "city", "tariff", "end_date", "price", "description"}
    _, only = await api.call("GET", "/api/catalog?city=samarkand", BUYER)
    assert [i["city"] for i in only["items"]] == ["samarkand"]

    status, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert status == 200 and card["own"] is False and card["requested"] is False
    raw = json.dumps(card, ensure_ascii=False)
    assert "Алишер" not in raw and "99890" not in raw and "seller" not in raw


async def test_buy_request_rules(h, api):
    await h.repo.touch_user(ADMIN, "boss", "Boss")
    lid = await publish(h, api)
    status, data = await api.call("POST", f"/api/listings/{lid}/request", BUYER, {"name": "Б", "phone": "901234567"})
    assert (status, data["error"], data["field"]) == (400, "bad_name", "name")

    status, data = await api.call("POST", f"/api/listings/{lid}/request", BUYER, {"name": "Борис", "phone": "901234567"})
    assert status == 200
    req = await h.repo.get_request(data["request_id"])
    assert (req["buyer_id"], req["buyer_phone"], req["status"]) == (BUYER, "+998901234567", "new")
    assert "Борис" in h.texts_to(ADMIN) and h.button(ADMIN, "Открыть").startswith(f"ad:reqs:{lid}:")
    # A request does not reserve the listing
    assert (await h.repo.get_listing(lid))["status"] == LS.APPROVED

    _, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert card["requested"] is True
    status, data = await api.call("POST", f"/api/listings/{lid}/request", BUYER, {"name": "Борис", "phone": "901234567"})
    assert (status, data["error"]) == (409, "duplicate")
    status, data = await api.call("POST", f"/api/listings/{lid}/request", SELLER, {"name": "Алишер", "phone": "901112233"})
    assert (status, data["error"]) == (409, "own")
    status, data = await api.call("POST", "/api/listings/999/request", BUYER, {"name": "Борис", "phone": "901234567"})
    assert (status, data["error"]) == (409, "not_available")


# ------------------------------------------------------------------ my listings
async def test_my_listings_are_private_to_the_seller(h, api):
    status, data = await api.call("POST", "/api/listings", SELLER, draft())
    lid = data["id"]
    _, mine = await api.call("GET", "/api/my", SELLER)
    assert [(x["id"], x["status"], x["commission"], x["net"]) for x in mine["items"]] == [(lid, "pending", 200_000, 1_800_000)]
    assert mine["items"][0]["seller_phone"] == "+998901112233"
    _, other = await api.call("GET", "/api/my", STRANGER)
    assert other["items"] == []
    # A pending listing is visible to its seller only
    status, card = await api.call("GET", f"/api/listings/{lid}", SELLER)
    assert status == 200 and card["own"] is True and card["listing"]["can_edit"]
    status, _ = await api.call("GET", f"/api/listings/{lid}", STRANGER)
    assert status == 404
    for method, path in (("PUT", f"/api/listings/{lid}"), ("POST", f"/api/listings/{lid}/withdraw"),
                         ("POST", f"/api/listings/{lid}/report")):
        status, data = await api.call(method, path, STRANGER, draft())
        assert (status, data["error"]) == (404, "not_found")
    assert (await h.repo.get_listing(lid))["status"] == LS.PENDING


async def test_edit_sends_back_to_moderation(h, api):
    lid = await publish(h, api)
    await h.repo.touch_user(ADMIN, "boss", "Boss")
    status, data = await api.call("PUT", f"/api/listings/{lid}", SELLER, draft(price="1 500 000"))
    assert (status, data["error"]) == (400, "commission_not_accepted")  # new price needs a new consent
    status, data = await api.call("PUT", f"/api/listings/{lid}", SELLER, draft(price="1 500 000", accepted_price=1_500_000))
    assert status == 200
    listing = await h.repo.get_listing(lid)
    assert (listing["status"], listing["price"], listing["published_at"]) == (LS.PENDING, 1_500_000, None)
    assert "повторной модерации" in h.texts_to(ADMIN)
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert cat["total"] == 0


async def test_withdraw_and_report(h, api):
    await h.repo.touch_user(ADMIN, "boss", "Boss")
    lid = await publish(h, api)
    await api.call("POST", f"/api/listings/{lid}/request", BUYER, {"name": "Борис", "phone": "901234567"})

    status, data = await api.call("POST", f"/api/listings/{lid}/report", SELLER)
    assert status == 200 and data["listing"]["sale_reported"] and not data["listing"]["can_report"]
    admin_before = len(h.to(ADMIN))
    status, _ = await api.call("POST", f"/api/listings/{lid}/report", SELLER)
    assert status == 200 and len(h.to(ADMIN)) == admin_before  # the admin is told once

    status, data = await api.call("POST", f"/api/listings/{lid}/withdraw", SELLER)
    assert status == 200 and data["listing"]["status"] == "withdrawn" and not data["listing"]["can_withdraw"]
    assert f"№{lid}" in text_of(h.to(BUYER)[-1])  # the buyer learns the listing is gone
    status, data = await api.call("POST", f"/api/listings/{lid}/withdraw", SELLER)
    assert (status, data["error"]) == (409, "bad_status")
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert cat["total"] == 0


# ------------------------------------------------------------------ bot side
def _markups(calls):
    return [c.reply_markup for c in calls if getattr(c, "reply_markup", None) is not None]


async def test_app_command_only_when_configured(h):
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    calls = await h.send(SELLER, "/app")
    assert not [m for m in _markups(calls) if hasattr(m, "inline_keyboard")]
    object.__setattr__(h.cfg, "webapp_url", "https://app.example.com/")
    calls = await h.send(SELLER, "/app")
    button = calls[-1].reply_markup.inline_keyboard[0][0]
    assert button.web_app.url == "https://app.example.com/"


async def test_chat_keeps_only_the_open_app_button(h):
    object.__setattr__(h.cfg, "webapp_url", "https://app.example.com/")
    await h.send(SELLER, "/start")
    calls = await h.click(SELLER, "lang:ru")
    calls += await h.send(SELLER, "/start")
    calls += await h.send(SELLER, "что-то непонятное")
    calls += await h.send(SELLER, "/menu")
    markups = _markups(calls)
    # The old reply keyboard is removed and never sent again
    assert any(type(m).__name__ == "ReplyKeyboardRemove" for m in markups)
    assert not [m for m in markups if hasattr(m, "keyboard")]
    buttons = [b for m in markups if hasattr(m, "inline_keyboard") for row in m.inline_keyboard for b in row]
    assert len(buttons) == 4 and all(b.web_app and b.web_app.url == "https://app.example.com/" for b in buttons)
    assert "Выберите действие в меню" not in h.texts_to(SELLER)
