"""Reading a 1Fit membership card: the text parser (Tesseract itself is optional and not needed here)."""
from datetime import date

from two_fit.ocr import merge, mismatches, parse_card

TODAY = date(2026, 11, 22)


def test_reads_a_russian_card():
    text = """Абонемент
    Безлимит 12 месяцев
    Активирован 01.10.2026
    Действует до
    12 мая 2027
    Заморозка
    осталось 20 дней"""
    assert parse_card(text, TODAY) == {
        "end_date": "2027-05-12", "start_date": "2026-10-01", "tariff_days": 365, "freeze_days": 20}


def test_reads_an_uzbek_card_and_numeric_dates():
    text = "Обуна 6 ой\n31.03.2027 гача амал қилади\nМузлатиш: 14 кун"
    assert parse_card(text, TODAY) == {"end_date": "2027-03-31", "tariff_days": 180, "freeze_days": 14}


def test_reads_days_instead_of_months_and_ignores_noise():
    assert parse_card("Тариф 730 дней\nдо 1.2.28", TODAY) == {"tariff_days": 730, "end_date": "2028-02-01"}
    assert parse_card("", TODAY) == {} and parse_card(None, TODAY) == {}
    assert parse_card("32.13.2027 и 99 мес", TODAY) == {}  # not a date, not a tariff


def test_several_screenshots_and_mismatches():
    both = merge([{"end_date": "2027-05-12"}, {"end_date": "2027-06-01", "freeze_days": 20}])
    assert both == {"end_date": "2027-05-12", "freeze_days": 20}  # the first value found wins
    listing = {"end_date": "2027-05-14", "tariff_days": 365, "freeze_days": 10}
    assert mismatches({**both, "tariff_days": 365}, listing) == ["freeze_days"]  # 2 days off is fine
    assert mismatches({"end_date": "2027-12-31"}, listing) == ["end_date"]


# ------------------------------------------------------------------ the bot and the Mini App with a fake Tesseract
from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from aiohttp import FormData  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from two_fit.i18n import I18n  # noqa: E402
from two_fit.repo import today_local  # noqa: E402
from two_fit.webapp.server import create_webapp  # noqa: E402

from .harness import Harness, texts  # noqa: E402
from .test_webapp import Api, draft, init_data  # noqa: E402

SELLER, ADMIN = 1, 900


@pytest.fixture
async def h(tmp_path):
    harness = await Harness(tmp_path / "ocr.db").start()
    yield harness
    await harness.stop()


async def test_bot_offers_the_values_read_off_the_screenshots(h, monkeypatch):
    end = today_local() + timedelta(days=200)
    on_card = (end + timedelta(days=30)).isoformat()

    async def fake_read(bot, file_ids):
        assert len(file_ids) == 2
        return {"end_date": on_card, "tariff_days": 365, "freeze_days": 20}

    monkeypatch.setattr("two_fit.bot.handlers.sell.read_screens", fake_read)
    await h.send(SELLER, "/start")
    await h.click(SELLER, "lang:ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    await h.send(SELLER, end.strftime("%d.%m.%Y"))
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:pay:full")
    await h.click(SELLER, "sl:freeze:no")
    await h.send(SELLER, "2 000 000")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, photo=True)
    await h.send(SELLER, photo=True)
    diff = texts(await h.click(SELLER, "sl:shotsdone:"))
    assert "не то же, что вы указали" in diff and "действует до" in diff and "заморозка" in diff
    assert "Готово — данные со скриншота" in texts(await h.click(SELLER, "sl:screentake:"))
    await h.send(SELLER, "Алишер")
    await h.send(SELLER, contact="+998901112233")
    await h.click(SELLER, "sl:accept:")
    await h.click(SELLER, "sl:send:")
    listing = (await h.repo.seller_listings(SELLER, 10, 0))[0][0]
    assert (listing["end_date"], listing["freeze_days"]) == (on_card, 20)
    assert len(h.repo.screenshot_ids(listing)) == 2 and h.repo.ocr_of(listing)["tariff_days"] == 365

    from two_fit.bot.context import Ctx
    from two_fit.bot.handlers.admin import screen_check
    lines = screen_check(Ctx(h.repo, I18n(), h.cfg, {"id": ADMIN, "lang": "ru"}), listing)
    assert lines[0].startswith("🔎 Со скриншота:") and len(lines) == 1  # taken from the card: nothing differs


async def test_mini_app_reads_screenshots_and_keeps_up_to_three(h, monkeypatch):
    on_card = {"end_date": (today_local() + timedelta(days=210)).isoformat(), "tariff_days": 365, "freeze_days": 20}
    monkeypatch.setattr("two_fit.ocr.read_card", lambda image, today: dict(on_card))
    monkeypatch.setattr("two_fit.ocr.available", lambda: True)  # as if Tesseract were installed
    app = create_webapp(h.repo, I18n(), h.cfg, h.bot, h.dp["notifier"])
    async with TestClient(TestServer(app)) as client:
        api = Api(client, h.cfg.bot_token)
        headers = {"Authorization": "tma " + init_data(h.cfg.bot_token, SELLER)}

        async def upload():
            form = FormData()
            form.add_field("file", b"\x89PNG fake", filename="s.png", content_type="image/png")
            res = await client.post("/api/screenshot", data=form, headers=headers)
            return await res.json()

        shots = [await upload() for _ in range(4)]
        assert shots[0]["read"] == on_card  # the form fills itself with it
        refs = [{"file_id": s["file_id"], "sig": s["sig"]} for s in shots]
        status, err = await api.call("POST", "/api/listings", SELLER, draft(screenshots=refs))
        assert (status, err["error"]) == (400, "bad_photo")  # four is one too many
        status, made = await api.call("POST", "/api/listings", SELLER, draft(screenshots=refs[:3]))
        assert status == 201
        listing = await h.repo.get_listing(made["id"])
        assert len(h.repo.screenshot_ids(listing)) == 3 and h.repo.ocr_of(listing) == on_card
        # Editing with "keep" keeps all three and what was read off them.
        status, _ = await api.call("PUT", f"/api/listings/{made['id']}", SELLER, draft(screenshots=["keep"]))
        listing = await h.repo.get_listing(made["id"])
        assert status == 200 and len(h.repo.screenshot_ids(listing)) == 3 and h.repo.ocr_of(listing) == on_card
