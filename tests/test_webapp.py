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

from .harness import Harness, buttons_of, text_of, texts

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
            "accepted_price": 2_000_000, "screenshot": None, "paid_in_full": True,
            "kind": "membership", "tariff_days": 365}
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
async def test_catalog_is_sorted_cheapest_first(h, api):
    mid = await publish(h, api, price="2 000 000", accepted_price=2_000_000)
    high = await publish(h, api, price="3 500 000", accepted_price=3_500_000)
    low = await publish(h, api, price="1 500 000", accepted_price=1_500_000)
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert [x["id"] for x in cat["items"]] == [low, mid, high]


async def test_negotiable_price_is_saved_and_shown(h, api):
    fixed = await publish(h, api)
    negotiable = await publish(h, api, price_negotiable=True)
    assert (await h.repo.get_listing(fixed))["price_negotiable"] == 0
    assert (await h.repo.get_listing(negotiable))["price_negotiable"] == 1
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert {x["id"]: x["price_negotiable"] for x in cat["items"]} == {fixed: False, negotiable: True}
    # Only a real JSON true counts; the flag can be switched off on edit
    status, _ = await api.call("PUT", f"/api/listings/{negotiable}", SELLER, draft(price_negotiable="yes"))
    assert status == 200 and (await h.repo.get_listing(negotiable))["price_negotiable"] == 0


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
    ({"end_date": "2020-01-01"}, "bad_date_past", "end_date"),
    ({"end_date": "31.05.2027"}, "bad_date_format", "end_date"),
    ({"start_date": "2999-01-01"}, "bad_start_future", "start_date"),
    ({"start_date": "01.01.2026"}, "bad_date_format", "start_date"),
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
    await publish(h, api, uid=STRANGER, city="samarkand", price="1500000", accepted_price=1_500_000)
    status, cat = await api.call("GET", "/api/catalog", BUYER)
    assert status == 200 and cat["total"] == 2
    assert {c["code"]: c["count"] for c in cat["cities"]} == {"tashkent": 1, "samarkand": 1}
    for item in cat["items"]:
        assert set(item) == {"id", "city", "tariff", "end_date", "start_date", "usage", "price", "price_negotiable",
                             "description", "price_total", "kind", "left",
                             "month_cost", "tariff_days", "freeze_days"}
        # No start date, but the 12-month tariff gives it: «used 5 of 12 months» still shows.
        assert item["start_date"] is None and item["tariff_days"] == 365 and item["usage"]["total"] == 12
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


async def test_start_date_shows_usage(h, api):
    from datetime import timedelta

    from two_fit.repo import today_local
    start = (today_local() - timedelta(days=165)).isoformat()
    end = (today_local() + timedelta(days=200)).isoformat()
    lid = await publish(h, api, start_date=start, end_date=end)
    status, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert status == 200
    assert card["listing"]["start_date"] == start
    assert card["listing"]["usage"] == {"used": 5, "total": 12, "unit": "m", "pct": 45}
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert boot["limits"]["start_max"] == today_local().isoformat()
    assert {"app.usage", "app.usage_left", "app.unit.m", "app.unit.d", "app.f.start"} <= set(boot["texts"])


async def test_installment_is_asked_and_shown(h, api):
    status, data = await api.call("POST", "/api/listings", SELLER, draft(paid_in_full=None))
    assert status == 400 and data["field"] == "payment"
    status, data = await api.call("POST", "/api/listings", SELLER, draft(paid_in_full=False, payments_left=9))
    assert status == 400 and data["field"] == "monthly_payment"
    status, data = await api.call("POST", "/api/listings", SELLER,
                                  draft(paid_in_full=False, monthly_payment="500 000", payments_left=61))
    assert status == 400 and data["field"] == "payments_left"

    full = await publish(h, api)
    inst = await publish(h, api, paid_in_full=False, monthly_payment=500_000, payments_left="9")
    # a buyer sees only the whole sum: 2 000 000 now + 9 × 500 000 to 1Fit, never the installments themselves
    _, card = await api.call("GET", f"/api/listings/{inst}", BUYER)
    l = card["listing"]
    assert l["price_total"] == 6_500_000 and not {"paid_in_full", "monthly_payment", "payments_left"} & set(l)
    _, card = await api.call("GET", f"/api/listings/{full}", BUYER)
    assert card["listing"]["price_total"] == card["listing"]["price"]
    # the seller still sees and edits their own answers
    _, mine = await api.call("GET", f"/api/listings/{inst}", SELLER)
    l = mine["listing"]
    assert (l["paid_in_full"], l["monthly_payment"], l["payments_left"]) == (False, 500_000, 9)

    # Switching to «fully paid» drops the installment numbers.
    status, _ = await api.call("PUT", f"/api/listings/{inst}", SELLER,
                               draft(paid_in_full=True, monthly_payment=500_000, payments_left=9))
    assert status == 200
    stored = await h.repo.get_listing(inst)
    assert (stored["paid_in_full"], stored["monthly_payment"], stored["payments_left"]) == (1, None, None)
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert {"app.pay.full", "app.pay.installment", "app.pay.installment_note", "app.err.payment"} <= set(boot["texts"])


async def test_promo_code_listing(h, api):
    status, data = await api.call("POST", "/api/listings", SELLER, draft(kind="gift"))
    assert status == 400 and data["field"] == "kind"
    promo = dict(kind="promo", promo_code="FIT-2026", tariff="Безлимит на 1 месяц", price="100 000",
                 accepted_price=100_000, paid_in_full=None)
    status, data = await api.call("POST", "/api/listings", SELLER, draft(**{**promo, "promo_code": "no spaces"}))
    assert status == 400 and data["field"] == "promo_code"
    lid = await publish(h, api, **promo)
    stored = await h.repo.get_listing(lid)
    assert (stored["kind"], stored["promo_code"], stored["paid_in_full"]) == ("promo", "FIT-2026", None)

    # Buyers see that it is a promo code, never the code itself.
    _, cat = await api.call("GET", "/api/catalog", BUYER)
    assert cat["items"][0]["kind"] == "promo" and "FIT-2026" not in str(cat)
    _, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert "FIT-2026" not in str(card)
    # The seller sees the code and a 30% commission.
    _, mine = await api.call("GET", "/api/my", SELLER)
    own = mine["items"][0]
    assert own["promo_code"] == "FIT-2026" and (own["percent"], own["commission"], own["net"]) == (30, 30_000, 70_000)
    # What is sold cannot be changed by editing.
    status, _ = await api.call("PUT", f"/api/listings/{lid}", SELLER, draft(**{**promo, "kind": "membership"}))
    assert status == 200 and (await h.repo.get_listing(lid))["kind"] == "promo"
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert boot["vars"]["promo_percent"] == 30 and "app.kind.promo" in boot["texts"]
    for key in ("any", "membership", "promo"):  # the sell page title and intro follow the choice
        assert f"app.sell.title_{key}" in boot["texts"] and f"app.sell.intro_{key}" in boot["texts"]


async def test_membership_shows_months_left_not_a_tariff(h, api):
    lid = await publish(h, api, tariff="Безлимит 12 месяцев")
    _, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert card["listing"]["tariff"] == "" and card["listing"]["left"]["unit"] == "m"
    status, data = await api.call("POST", "/api/listings", SELLER,
                                  draft(kind="promo", promo_code="FIT-1", tariff="x", paid_in_full=None))
    assert status == 400 and data["field"] == "tariff"
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert {"app.left", "app.left_title", "app.f.time_left"} <= set(boot["texts"])


async def test_sell_form_needs_no_contacts(h, api):
    body = draft()
    del body["name"], body["phone"]
    status, data = await api.call("POST", "/api/listings", SELLER, body)
    assert status == 201, data
    listing = await h.repo.get_listing(data["id"])
    assert listing["seller_name"] == f"Name{SELLER}" and listing["seller_phone"] == ""  # from Telegram
    assert (await h.repo.get_user(SELLER))["contact_phone"] is None  # nothing empty saved as a contact
    # The admin card shows no phone, but the seller's Telegram to reach them.
    from two_fit.bot.context import Ctx
    from two_fit.bot.handlers.admin import render_card
    text, _ = await render_card(Ctx(h.repo, I18n(), h.cfg, {"id": ADMIN, "lang": "ru"}), listing)
    assert "📞 —" in text and f"@user{SELLER}" in text
    status, _ = await api.call("PUT", f"/api/listings/{data['id']}", SELLER, body)
    assert status == 200 and (await h.repo.get_listing(data["id"]))["seller_name"] == f"Name{SELLER}"


async def test_editing_after_a_rate_change_asks_to_accept_again(h, api):
    lid = await publish(h, api)  # accepted at the harness rate, 10%
    await h.repo.db.execute("UPDATE listings SET commission_percent=5 WHERE id=?", (lid,))  # as if accepted earlier at 5%
    _, mine = await api.call("GET", "/api/my", SELLER)
    own = mine["items"][0]
    assert own["percent"] == 5 and own["rate_changed"] is True
    await api.call("PUT", f"/api/listings/{lid}", SELLER, draft())
    assert (await h.repo.get_listing(lid))["commission_percent"] == 10  # saving accepts today's rate


async def test_only_1fit_cities_are_offered(h, api):
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert boot["cities"] == ["tashkent", "samarkand", "bukhara", "andijan", "namangan", "fergana", "navoi"]
    status, data = await api.call("POST", "/api/listings", SELLER, draft(city="nukus"))
    assert status == 400 and data["field"] == "city"
    # A listing made in Nukus before the change still shows and filters.
    lid = await h.repo.create_listing(SELLER, "ru", {**repo_draft(), "city": "nukus"})
    await h.repo.approve(lid, ADMIN)
    status, cat = await api.call("GET", "/api/catalog?city=nukus", BUYER)
    assert status == 200 and [i["id"] for i in cat["items"]] == [lid]


def repo_draft():
    return {"tariff": "", "end_date": END, "price": 2_000_000, "commission_accepted_price": 2_000_000,
            "seller_name": "A", "seller_phone": "", "paid_in_full": True, "kind": "membership", "tariff_days": 365}


async def test_promo_codes_hidden_by_default(tmp_path):
    """PROMO_ENABLED is off by default: no promo code to sell, none in the catalog, no «30%» in the texts."""
    from two_fit.config import Config
    assert Config.__dataclass_fields__["promo_enabled"].default is False

    h = await Harness(tmp_path / "hidden.db", promo=False).start()
    try:
        app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
        async with TestClient(TestServer(app)) as client:
            api = Api(client, h.cfg.bot_token)
            _, boot = await api.call("GET", "/api/bootstrap", SELLER)
            assert boot["vars"]["promo_enabled"] is False and boot["vars"]["fees"] == "10%"
            assert "промокод" not in T(boot, "app.how.6")
            promo = dict(kind="promo", promo_code="FIT-1", tariff="Безлимит на 1 месяц", price="100 000",
                         accepted_price=100_000, paid_in_full=None, tariff_days=None)
            status, err = await api.call("POST", "/api/listings", SELLER, draft(**promo))
            assert (status, err["field"]) == (400, "kind")
            # A promo code listed while selling them was on: hidden from the catalog and not for sale.
            old = await h.repo.create_listing(SELLER, "ru", {**repo_draft(), **promo, "city": "tashkent", "tariff": "1 месяц",
                                                             "price": 100_000, "commission_accepted_price": 100_000})
            await h.repo.approve(old, ADMIN)
            member = await publish(h, api)
            _, cat = await api.call("GET", "/api/catalog", BUYER)
            assert [i["id"] for i in cat["items"]] == [member]
            status, _ = await api.call("GET", f"/api/listings/{old}", BUYER)
            assert status == 404

        # The bot goes straight to the city: no «membership or promo code?» question.
        await h.send(SELLER, "/start")
        await h.click(SELLER, "lang:ru")
        first = texts(await h.send(SELLER, "➕ Продать абонемент"))
        assert "Шаг 1/9" in first and "Что вы продаёте" not in first and "промокод" not in first
        assert "промокод" not in texts(await h.send(SELLER, "ℹ️ Как это работает"))
    finally:
        await h.stop()


def T(boot, key):
    import re
    return re.sub(r"\{(\w+)\}", lambda m: str(boot["vars"].get(m.group(1), "")), boot["texts"][key])


async def test_freeze_days_are_saved_and_shown(h, api):
    status, err = await api.call("POST", "/api/listings", SELLER, draft(freeze_days=400))
    assert (status, err["field"]) == (400, "freeze_days")
    lid = await publish(h, api, freeze_days=20)
    none = await publish(h, api)  # no freeze days: stored as 0
    _, card = await api.call("GET", f"/api/listings/{lid}", BUYER)
    assert card["listing"]["freeze_days"] == 20
    assert (await h.repo.get_listing(none))["freeze_days"] == 0


async def test_screenshot_is_required_and_seen_only_by_the_admin(tmp_path):
    h = await Harness(tmp_path / "shot.db", screenshot_required=True).start()
    try:
        app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
        async with TestClient(TestServer(app)) as client:
            api = Api(client, h.cfg.bot_token)
            _, boot = await api.call("GET", "/api/bootstrap", SELLER)
            assert boot["vars"]["shot_required"] is True
            assert "только администратор" in boot["texts"]["app.sell.shot_hint_required"]
            status, err = await api.call("POST", "/api/listings", SELLER, draft())
            assert (status, err["error"], err["field"]) == (400, "screenshot_required", "screenshot")
            form = FormData()
            form.add_field("file", b"\x89PNG fake", filename="shot.png", content_type="image/png")
            res = await client.post("/api/screenshot", data=form,
                                    headers={"Authorization": "tma " + init_data(h.cfg.bot_token, SELLER)})
            shot = await res.json()
            status, made = await api.call("POST", "/api/listings", SELLER, draft(screenshot=shot))
            assert status == 201
            await h.repo.approve(made["id"], ADMIN)
            _, card = await api.call("GET", f"/api/listings/{made['id']}", BUYER)
            assert "screenshot" not in str(card) and shot["file_id"] not in str(card)  # never shown to buyers
            # Editing keeps the attached screenshot.
            status, _ = await api.call("PUT", f"/api/listings/{made['id']}", SELLER, draft(screenshot="keep"))
            assert status == 200
    finally:
        await h.stop()


async def test_bot_requires_the_screenshot(tmp_path):
    from datetime import timedelta as td
    from two_fit.repo import today_local as tl
    h = await Harness(tmp_path / "shot-bot.db", screenshot_required=True).start()
    try:
        await h.send(SELLER, "/start")
        await h.click(SELLER, "lang:ru")
        await h.send(SELLER, "➕ Продать абонемент")
        await h.click(SELLER, "sl:kind:membership")
        await h.click(SELLER, "sl:city:tashkent")
        await h.click(SELLER, "sl:plan:365")
        await h.send(SELLER, (tl() + td(days=200)).strftime("%d.%m.%Y"))
        await h.click(SELLER, "sl:pay:full")
        await h.click(SELLER, "sl:freeze:no")
        await h.send(SELLER, "2 000 000")
        calls = await h.click(SELLER, "sl:skip:")  # no description: on to the screenshot
        ask = texts(calls)
        assert "обязательно" in ask and "только администратор" in ask
        assert not [b for c in calls for b in buttons_of(c) if "Пропустить" in b.text]  # no «Skip» here
        assert "без него объявление не отправить" in texts(await h.send(SELLER, "нет скрина"))
        assert "Скриншот добавлен (1/3)" in texts(await h.send(SELLER, photo=True))
        assert "Как вас зовут" in texts(await h.click(SELLER, "sl:shotsdone:"))
    finally:
        await h.stop()
