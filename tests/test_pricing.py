"""Price advice for sellers and the 1Fit price watcher."""
from datetime import date, datetime, timedelta

import pytest

from two_fit.price_watch import PENDING_KEY, PriceError, PriceWatcher, next_run, parse_prices, parse_times
from two_fit.pricing import DEFAULT_FIT_PRICES, advise
from two_fit.repo import TZ, Repo, today_local

from .harness import Harness, alerts, texts
from .test_webapp import Api, api, draft  # noqa: F401 — fixtures shared with the Mini App tests

SELLER, ADMIN = 1, 900
TODAY = date(2026, 11, 21)

# What 1Fit's API returns for Tashkent (city_id=1).
FIT_ANSWER = {"result": [
    {"id": 92, "name": "3 месяца", "days": 90, "monthly_price": 1630000, "price": 4890000, "price_with_discount": None},
    {"id": 95, "name": "6 месяцев", "days": 180, "monthly_price": 981667, "price": 5890000, "price_with_discount": None},
    {"id": 91, "name": "12 месяцев", "days": 365, "monthly_price": 657500, "price": 8990000,
     "price_with_discount": 7890000},
    {"id": 93, "name": "24 месяца", "days": 730, "monthly_price": 1015833, "price": 13090000,
     "price_with_discount": 12190000},
]}


def end_in(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


@pytest.fixture
async def h(tmp_path):
    harness = await Harness(tmp_path / "bot.db").start()
    yield harness
    await harness.stop()


# ------------------------------------------------------------------ the formula
@pytest.mark.parametrize("days, base, recommended, max_price", [
    (600, 730, 8_510_000, 8_510_000),   # 24-month membership, most of it left
    (300, 365, 5_510_000, 5_510_000),   # the buyer would otherwise buy 12 months
    (170, 180, 4_720_000, 4_720_000),   # 5 months 20 days: compared with 6 months
    (135, 180, 3_750_000, 3_750_000),   # 4 months 15 days
    (60, 90, 2_770_000, 2_770_000),     # under 3 months: compared with the 3-month tariff
])
def test_advice_follows_the_cheapest_1fit_tariff(days, base, recommended, max_price):
    a = advise(end_in(days), TODAY)
    assert (a.base_days, a.recommended, a.max_price) == (base, recommended, max_price)
    assert a.saving(a.recommended) == 15  # the advised price is about 15% cheaper than 1Fit per month
    assert not a.too_high(max_price) and a.too_high(max_price + 1)


def test_month_cost_is_what_1fit_charges_per_month():
    a = advise(end_in(300), TODAY)
    assert abs(a.fit_month - 657_500) < 1_000  # 7 890 000 / 12
    assert advise(end_in(600), TODAY).fit_month < 510_000  # 24 months: the real 507 917, not 1Fit's 1 015 833


def test_installments_the_buyer_takes_over_come_off_the_price():
    full = advise(end_in(200), TODAY)
    inst = advise(end_in(200), TODAY, monthly_payment=300_000, payments_left=3)
    assert inst.installments == 900_000
    assert inst.recommended == full.recommended - 900_000
    assert inst.month_cost(inst.recommended) == full.month_cost(full.recommended)  # same total for the buyer


def test_installments_as_dear_as_1fit_leave_no_limit():
    a = advise(end_in(200), TODAY, monthly_payment=500_000, payments_left=9)  # 4.5M left to pay vs ~4.3M value
    assert a.max_price == 0 and not a.too_high(3_000_000)  # no cap that would leave nothing to ask
    assert a.recommended == 10_000


def test_ended_membership_gets_no_advice():
    assert advise(end_in(0), TODAY) is None


def test_advice_uses_the_prices_it_is_given():
    dearer = {**DEFAULT_FIT_PRICES, 365: 8_990_000}
    assert advise(end_in(300), TODAY, dearer).recommended > advise(end_in(300), TODAY).recommended


# ------------------------------------------------------------------ the bot
async def walk_to_price(h, days=200):
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    await h.send(SELLER, (today_local() + timedelta(days=days)).strftime("%d.%m.%Y"))
    await h.click(SELLER, "sl:pay:full")
    return await h.click(SELLER, "sl:freeze:no")


async def test_bot_shows_and_applies_the_recommended_price(h):
    a = advise((today_local() + timedelta(days=200)).isoformat(), today_local())
    ask = texts(await walk_to_price(h))
    assert "Рекомендуемая цена" in ask and "Покупатель заплатит за" in ask and "1Fit" in ask
    cb = h.button(SELLER, "Поставить")
    assert cb == f"sl:advice:{a.recommended}"
    calc = texts(await h.click(SELLER, cb))
    assert "Цена: <b>" in calc
    data = await h.dp.storage.get_data(key=_key(h))
    assert data["draft"]["price"] == a.recommended


async def test_bot_refuses_a_price_dearer_than_1fit(h):
    await walk_to_price(h)
    refused = texts(await h.send(SELLER, "6 000 000"))
    assert "не купят" in refused
    data = await h.dp.storage.get_data(key=_key(h))
    assert "price" not in data["draft"]
    low = texts(await h.send(SELLER, "500 000"))  # 500 000 for 200 days: most likely the monthly price
    assert "Проверьте цену" in low and "один месяц" in low
    ok = texts(await h.click(SELLER, "sl:lowok:500000"))  # the seller confirms: allowed, with a warning
    assert "намного дешевле" in ok


async def test_card_shows_the_whole_price_without_the_1fit_comparison(h):
    await walk_to_price(h)
    await h.send(SELLER, "2 000 000")
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    preview = texts(await h.send(SELLER, contact="+998901112233"))
    assert "💰 Цена: <b>2\xa0000\xa0000 сум</b>" in preview  # fully paid: the whole sum is the price
    assert "В 1Fit те же" not in preview and "сэкономьте" not in preview  # no comparison with 1Fit on the card
    assert "в месяц" not in preview


def _key(h):
    from aiogram.fsm.storage.base import StorageKey
    return StorageKey(bot_id=h.bot.id, chat_id=SELLER, user_id=SELLER)


# ------------------------------------------------------------------ the Mini App
async def test_api_advice_and_limit(h, api):  # noqa: F811
    end = (today_local() + timedelta(days=200)).isoformat()
    status, data = await api.call("POST", "/api/advice", SELLER, {"end_date": end, "paid_in_full": True})
    a = advise(end, today_local())
    assert status == 200 and data["advice"]["recommended"] == a.recommended and data["advice"]["max"] == a.max_price
    _, half = await api.call("POST", "/api/advice", SELLER, {"end_date": end, "paid_in_full": False})
    assert half["advice"] is None  # installments not typed yet
    _, none = await api.call("POST", "/api/advice", SELLER, {"end_date": "", "paid_in_full": True})
    assert none["advice"] is None

    status, err = await api.call("POST", "/api/listings", SELLER, draft(price="6 000 000", accepted_price=6_000_000))
    assert (status, err["error"], err["field"]) == (400, "price_too_high", "price")

    status, made = await api.call("POST", "/api/listings", SELLER, draft())
    assert status == 201
    await h.repo.approve(made["id"], ADMIN)
    _, cat = await api.call("GET", "/api/catalog", 2)
    cost = cat["items"][0]["month_cost"]
    assert cost["saving"] > 0 and cost["total"] < cost["fit_total"]


# ------------------------------------------------------------------ the price watcher
def test_parse_1fit_answer():
    actual, full = parse_prices(FIT_ANSWER)
    assert actual == DEFAULT_FIT_PRICES  # the discounted price when there is one
    assert full[365] == 8_990_000 and full[730] == 13_090_000


@pytest.mark.parametrize("payload", [
    {}, {"result": []}, {"result": "x"},
    {"result": [{"days": 365, "price": 7890000}]},                           # tariffs missing
    {"result": [dict(x, price=489, price_with_discount=None) for x in FIT_ANSWER["result"]]},  # tenge-like misread
])
def test_parse_rejects_odd_answers(payload):
    with pytest.raises(PriceError):
        parse_prices(payload)


def test_schedule():
    times = parse_times("08:00, 11:00,14:00,17:00,20:00")
    assert len(times) == 5
    at = next_run(datetime(2026, 11, 21, 9, 30, tzinfo=TZ), times)
    assert (at.hour, at.minute, at.day) == (11, 0, 21)
    assert next_run(datetime(2026, 11, 21, 20, 0, tzinfo=TZ), times).day == 22  # after the last one: tomorrow 08:00


def watcher(h, answers):
    it = iter(answers)

    async def fetch():
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return value
    w = PriceWatcher(h.repo, h.dp["price_watcher"].i18n, h.cfg, h.dp["notifier"], fetch=fetch)
    h.dp["price_watcher"] = w
    return w


async def test_watcher_same_prices_stay_quiet(h):
    w = watcher(h, [FIT_ANSWER])
    assert (await w.check())["status"] == "ok"
    assert not h.to(ADMIN)
    assert (await h.repo.last_price_checks(1))[0]["status"] == "ok"


async def test_watcher_announces_new_prices_once_and_admin_applies_them(h):
    dearer = {"result": [dict(x, price_with_discount=8_490_000) if x["days"] == 365 else x
                         for x in FIT_ANSWER["result"]]}
    w = watcher(h, [dearer, dearer])
    assert (await w.check())["status"] == "changed"
    msg = h.texts_to(ADMIN)
    assert "1Fit изменил цены" in msg and "7\xa0890\xa0000 → <b>8\xa0490\xa0000</b>" in msg and "+7,6%" in msg
    await w.check()
    assert h.texts_to(ADMIN).count("1Fit изменил цены") == 1  # not five times a day
    assert h.repo.fit_prices[365] == 7_890_000  # nothing changes on its own

    await h.send(ADMIN, "/start")
    await h.click(ADMIN, "lang:ru")
    apply_cb = h.button(ADMIN, "Обновить цены")
    done = await h.click(ADMIN, apply_cb)
    assert any("Цены обновлены" in a for a in alerts(done))
    assert h.repo.fit_prices[365] == 8_490_000 and await h.repo.get_setting(PENDING_KEY) is None
    assert any("Эти цены уже неактуальны" in a for a in alerts(await h.click(ADMIN, apply_cb)))  # no double apply

    # The new prices survive a restart
    again = Repo(h.cfg.db_path)
    await again.connect()
    assert again.fit_prices[365] == 8_490_000
    await again.close()


async def test_watcher_reports_errors_after_two_failures_once_a_day(h):
    w = watcher(h, [PriceError("HTTP 500"), PriceError("HTTP 500"), PriceError("HTTP 500")])
    await w.check()
    assert not h.to(ADMIN)
    await w.check()
    await w.check()
    assert h.texts_to(ADMIN).count("не получается прочитать цены 1Fit") == 1
    assert h.repo.fit_prices == DEFAULT_FIT_PRICES


async def test_admin_prices_screen_and_check_now(h):
    w = watcher(h, [FIT_ANSWER])
    await h.send(ADMIN, "/start")
    await h.click(ADMIN, "lang:ru")
    screen = texts(await h.send(ADMIN, "/prices"))
    assert "Цены 1Fit" in screen and "12 мес.: 7\xa0890\xa0000 сум" in screen
    checked = await h.click(ADMIN, h.button(ADMIN, "Проверить сейчас"))
    assert "совпадают" in texts(checked)
    assert "Цены 1Fit" not in texts(await h.send(SELLER, "/prices"))  # admins only
    assert w.fails == 0


# ------------------------------------------------------------------ tariff picked from a list, suspicious prices
def test_monthly_price_for_the_whole_term_is_suspicious():
    a = advise(end_in(270), TODAY, plan_days=365)  # 9 months left of a 12-month tariff
    assert a.base_days == 365
    assert a.suspicious(485_000)            # the screenshot: «485 000 for 9 months» is one month's price
    assert not a.suspicious(a.recommended)  # a real price for 9 months is not
    assert not a.suspicious(2_000_000)      # a big discount, but plausible for the whole term


def test_usage_comes_from_the_tariff_without_a_start_date():
    from two_fit.usage import usage
    u = usage(None, end_in(270), TODAY, 365)
    assert (u["used"], u["total"], u["unit"]) == (3, 12, "m")
    assert usage(None, end_in(270), TODAY) is None  # no tariff, no start date: nothing to show


def test_end_date_must_fit_the_tariff():
    from two_fit.validators import ValidationError, check_plan_end
    check_plan_end(TODAY + timedelta(days=365 + 90), TODAY, 365)  # a 12-month tariff plus freezes
    with pytest.raises(ValidationError):
        check_plan_end(TODAY + timedelta(days=365 + 91), TODAY, 365)
    with pytest.raises(ValidationError):
        check_plan_end(TODAY + timedelta(days=300), TODAY, 180)  # 10 months left on a 6-month tariff


async def test_api_needs_a_tariff_and_asks_about_a_suspicious_price(h, api):  # noqa: F811
    status, err = await api.call("POST", "/api/listings", SELLER, draft(tariff_days=None))
    assert (status, err["field"]) == (400, "tariff_days")
    status, err = await api.call("POST", "/api/listings", SELLER, draft(tariff_days=180, end_date=(
        today_local() + timedelta(days=400)).isoformat()))
    assert (status, err["error"], err["field"]) == (400, "bad_end_for_plan", "end_date")

    low = dict(price="485 000", accepted_price=485_000)
    status, err = await api.call("POST", "/api/listings", SELLER, draft(**low))
    assert (status, err["error"], err["field"]) == (400, "price_suspicious", "price") and "один месяц" in err["message"]
    status, made = await api.call("POST", "/api/listings", SELLER, draft(**low, low_price_ok=True))
    assert status == 201
    await h.repo.approve(made["id"], ADMIN)
    _, cat = await api.call("GET", "/api/catalog", SELLER)
    assert cat["items"][0]["month_cost"] == {"suspicious": True}  # flagged, not a «−92%» bargain


async def test_bot_card_has_no_low_price_warning(h):
    await walk_to_price(h)
    await h.send(SELLER, "300 000")
    await h.click(SELLER, "sl:lowok:300000")
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    preview = texts(await h.send(SELLER, contact="+998901112233"))
    assert "Очень низкая цена" not in preview and "сэкономьте" not in preview  # buyers get no such warning
    assert "Использовано" not in preview  # no «used … of 12 months» bar on the card


# ------------------------------------------------------------------ AUTO_PRICE: the price is the recommended one
def test_auto_price_is_on_by_default(monkeypatch):
    from two_fit.config import load_config
    monkeypatch.delenv("AUTO_PRICE", raising=False)
    monkeypatch.setattr("two_fit.config.load_dotenv", lambda *a, **k: None)
    assert load_config().auto_price is True
    monkeypatch.setenv("AUTO_PRICE", "0")
    assert load_config().auto_price is False


async def test_auto_price_in_the_mini_app(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from two_fit.i18n import I18n
    from two_fit.webapp.server import create_webapp

    from .test_webapp import END

    h = await Harness(tmp_path / "auto.db", auto_price=True).start()
    try:
        async with TestClient(TestServer(create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"]))) as client:
            a = advise(END, today_local(), plan_days=365)
            api = Api(client, h.cfg.bot_token)
            _, boot = await api.call("GET", "/api/bootstrap", SELLER)
            assert boot["vars"]["auto_price"] is True
            # Whatever price the app sends, the listing gets the recommended one.
            status, made = await api.call("POST", "/api/listings", SELLER, draft(price="999 999 999"))
            assert status == 201, made
            assert (await h.repo.get_listing(made["id"]))["price"] == a.recommended
    finally:
        await h.stop()


async def test_auto_price_in_the_bot(tmp_path):
    h = await Harness(tmp_path / "auto-bot.db", auto_price=True).start()
    try:
        end = today_local() + timedelta(days=200)
        a = advise(end.isoformat(), today_local(), plan_days=365)
        await h.send(SELLER, "/start")
        await h.click(SELLER, "lang:ru")
        await h.send(SELLER, "➕ Продать абонемент")
        await h.click(SELLER, "sl:kind:membership")
        await h.click(SELLER, "sl:city:tashkent")
        await h.click(SELLER, "sl:plan:365")
        await h.send(SELLER, end.strftime("%d.%m.%Y"))
        await h.click(SELLER, "sl:pay:full")
        nxt = texts(await h.click(SELLER, "sl:freeze:no"))
        assert "Шаг 6/8" in nxt and "описание" in nxt  # no price question: straight to the description
        await h.click(SELLER, "sl:skip:")
        await h.click(SELLER, "sl:skip:")
        await h.send(SELLER, "Алишер")
        preview = texts(await h.send(SELLER, contact="+998901112233"))
        assert "рассчитана автоматически" in preview and f"{a.recommended:,}".replace(",", "\xa0") in preview
        await h.click(SELLER, "sl:accept:")
        await h.click(SELLER, "sl:send:")
        listing = (await h.repo.seller_listings(SELLER, 10, 0))[0][0]
        assert listing["price"] == listing["commission_accepted_price"] == a.recommended
    finally:
        await h.stop()


def test_saving_in_sums_and_round_numbers():
    from two_fit.money import round_k
    a = advise(end_in(300), TODAY, plan_days=365)
    assert a.saved(a.recommended) == round(a.fit_value - a.recommended)
    assert a.saved(10**9) == 0  # never a negative saving
    assert (round_k(465_287), round_k(657_950), round_k(499)) == (465_000, 658_000, 0)


async def test_catalog_explains_the_saving(h, api):  # noqa: F811
    lid = (await api.call("POST", "/api/listings", SELLER, draft()))[1]["id"]
    await h.repo.approve(lid, ADMIN)
    _, cat = await api.call("GET", "/api/catalog", SELLER)
    m = cat["items"][0]["month_cost"]
    assert set(m) == {"total", "fit_total", "saving", "saved"}
    assert m["total"] % 1000 == 0 and m["fit_total"] % 1000 == 0 and m["saved"] > 0
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert not any(k.startswith("app.mc.") or k == "app.badge.saving" for k in boot["texts"])



def test_start_and_end_must_fit_the_tariff():
    """The screenshot: a 12-month tariff from 01.10.2026 to 31.12.2027 is 15 months — a mistake."""
    from two_fit.validators import ValidationError, check_plan_span
    with pytest.raises(ValidationError) as e:
        check_plan_span(date(2026, 10, 1), date(2027, 12, 31), 365)
    assert e.value.code == "bad_dates_for_plan"
    check_plan_span(date(2026, 10, 1), date(2027, 10, 1), 365)   # exactly 12 months
    check_plan_span(date(2026, 10, 1), date(2027, 12, 20), 365)  # 12 months + 80 days of freezes
    check_plan_span(date(2026, 10, 10), date(2027, 10, 1), 365)  # activated a few days after buying
    with pytest.raises(ValidationError):
        check_plan_span(date(2026, 10, 1), date(2027, 4, 1), 365)  # 6 months on a 12-month tariff
    check_plan_span(None, date(2027, 12, 31), 365)  # no start date: nothing to compare


async def test_api_refuses_dates_that_do_not_fit_the_tariff(h, api):  # noqa: F811
    end = (today_local() + timedelta(days=200)).isoformat()
    wrong = (today_local() - timedelta(days=260)).isoformat()  # 460 days between the dates, 12-month tariff
    status, err = await api.call("POST", "/api/listings", SELLER, draft(end_date=end, start_date=wrong))
    assert (status, err["error"], err["field"]) == (400, "bad_dates_for_plan", "start_date")
    right = (today_local() - timedelta(days=165)).isoformat()
    status, _ = await api.call("POST", "/api/listings", SELLER, draft(end_date=end, start_date=right))
    assert status == 201


async def test_bot_does_not_ask_the_start_date(h):
    # «used N of 12» comes from the end date minus the tariff: the start date is no longer asked
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    nxt = texts(await h.send(SELLER, (today_local() + timedelta(days=200)).strftime("%d.%m.%Y")))
    assert "Как оплачен абонемент" in nxt and "Дата начала" not in nxt


def test_payments_left_are_guessed_from_the_end_date():
    from two_fit.usage import payments_guess
    assert payments_guess(end_in(200), TODAY) == 7   # 200 days: about 7 monthly payments
    assert payments_guess(end_in(10), TODAY) == 1    # never 0
    assert payments_guess(end_in(5000), TODAY) == 60  # never above the limit


async def test_bot_offers_the_number_of_payments(h):
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    await h.send(SELLER, (today_local() + timedelta(days=200)).strftime("%d.%m.%Y"))
    await h.click(SELLER, "sl:pay:installment")
    ask = texts(await h.send(SELLER, "500 000"))
    assert "По дате окончания — <b>7</b>" in ask and h.button(SELLER, "Да, 7") == "sl:leftok:7"
    nxt = texts(await h.click(SELLER, "sl:leftok:7"))
    assert "заморозки" in nxt  # one tap, straight to the next question
    data = await h.dp.storage.get_data(key=_key(h))
    assert data["draft"]["payments_left"] == 7


# ------------------------------------------------------------------ the price follows the days left
async def _auto_listing(repo, today_end_days=300, **over):
    end = (today_local() + timedelta(days=today_end_days)).isoformat()
    a = advise(end, today_local(), repo.fit_prices, plan_days=365)
    d = {"city": "tashkent", "kind": "membership", "tariff_days": 365, "end_date": end, "paid_in_full": True,
         "price": a.recommended, "seller_name": "A", "seller_phone": "+998901112233",
         "price_auto": True, **over}
    lid = await repo.create_listing(SELLER, "ru", d)
    return lid, end


async def test_reprice_lowers_the_automatic_price_as_days_pass(h):
    await h.repo.touch_user(SELLER, "s", "S")
    lid, end = await _auto_listing(h.repo)
    first = (await h.repo.get_listing(lid))["price"]
    assert await h.repo.reprice(today_local()) == 0  # same day: nothing to change
    month_later = today_local() + timedelta(days=30)
    assert await h.repo.reprice(month_later) == 1
    listing = await h.repo.get_listing(lid)
    expected = advise(end, month_later, h.repo.fit_prices, plan_days=365).recommended
    assert listing["price"] == expected < first


async def test_reprice_leaves_typed_prices_and_sold_alone(h):
    await h.repo.touch_user(SELLER, "s", "S")
    typed, _ = await _auto_listing(h.repo, price_auto=False)
    sold, _ = await _auto_listing(h.repo)
    await h.repo.approve(sold, ADMIN)
    await h.repo.mark_sold(sold, SELLER)
    before = {i: (await h.repo.get_listing(i))["price"] for i in (typed, sold)}
    await h.repo.reprice(today_local() + timedelta(days=60))
    assert {i: (await h.repo.get_listing(i))["price"] for i in (typed, sold)} == before


async def test_reprice_counts_down_the_installments(h):
    await h.repo.touch_user(SELLER, "s", "S")
    lid, _ = await _auto_listing(h.repo, paid_in_full=False, monthly_payment=500_000, payments_left=3)
    assert (await h.repo.get_listing(lid))["payments_as_of"] == today_local().isoformat()
    await h.repo.reprice(today_local() + timedelta(days=31))  # the seller paid one more month
    assert (await h.repo.get_listing(lid))["payments_left"] == 2
    await h.repo.reprice(today_local() + timedelta(days=100))  # all paid off
    listing = await h.repo.get_listing(lid)
    assert (listing["paid_in_full"], listing["payments_left"], listing["monthly_payment"]) == (1, None, None)


async def test_listings_get_the_automatic_flag(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from two_fit.i18n import I18n
    from two_fit.webapp.server import create_webapp

    from .test_webapp import END
    h = await Harness(tmp_path / "flag.db", auto_price=True).start()
    try:
        async with TestClient(TestServer(create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"]))) as client:
            a = advise(END, today_local(), plan_days=365)
            _, made = await Api(client, h.cfg.bot_token).call("POST", "/api/listings", SELLER, draft())
            assert (await h.repo.get_listing(made["id"]))["price_auto"] == 1
    finally:
        await h.stop()


async def test_how_it_works_explains_the_automatic_price(tmp_path):
    for auto in (True, False):
        h = await Harness(tmp_path / f"how-{auto}.db", auto_price=auto).start()
        try:
            await h.send(SELLER, "/start")
            await h.click(SELLER, "lang:ru")
            how = texts(await h.send(SELLER, "ℹ️ Как это работает"))
            assert ("Как считается цена" in how) is auto
            if auto:
                assert "текущие цены 1Fit" in how and "на 15% дешевле" in how and "комисси" not in how.lower()
                assert how.index("Как считается цена") < how.index("не партнёр 1Fit")  # right after the steps
        finally:
            await h.stop()


async def test_buyers_are_told_to_pay_after_the_transfer(h, api):  # noqa: F811
    """Two Fit is not in the deal: buyers are told to pay the seller only once 1Fit moved the membership."""
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    how = texts(await h.send(SELLER, "ℹ️ Как это работает"))
    assert "только после того, как абонемент переоформлен" in how and "не участвуем в сделке" in how
    _, boot = await api.call("GET", "/api/bootstrap", SELLER)
    assert "переоформлен" in boot["texts"]["app.how.pay"] and "переоформлен" in boot["texts"]["app.seller.safety"]


def test_seller_tariff_does_not_lower_the_base():
    # listing #20: 147 days left of a 12-month membership. The buyer would buy 1Fit's 6 months (5 890 000),
    # not a year: priced from the year it got 3 178 000 of value and, after the installments, 10 000.
    a = advise(end_in(147), TODAY, plan_days=365)
    assert a.base_days == 180 and a.fit_value == round(147 / 180 * 5_890_000)
    with_installments = advise(end_in(147), TODAY, None, 532_500, 5, plan_days=365)
    assert with_installments.recommended == 1_420_000
