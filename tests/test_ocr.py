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


def test_installment_contract_is_not_the_membership_card():
    # listing #21: the seller sent the 1Fit installment contract, not only the card
    contract = """Контракт
    ПОКУПКА В 1Fit
    6 400 000 сум
    Заявка №690984 от 12.08.2026
    Детали покупки
    График платежей"""
    assert parse_card(contract, TODAY) == {
        "installment": True, "contract_total": 6_400_000, "contract_date": "2026-08-12"}


def test_payment_schedule_gives_the_payments_left_not_the_end_date():
    schedule = """График платежей
    01.09.2026 533 333 сум Оплачено
    01.10.2026 533 333 сум Оплачено
    01.11.2026 533 333 сум Оплачено
    01.12.2026 533 333 сум
    01.01.2027 533 333 сум
    01.08.2027 533 337 сум"""
    read = parse_card(schedule, TODAY)
    assert "end_date" not in read and "start_date" not in read  # was «до 01.08.2027 · начало 01.09.2026»
    assert read == {"installment": True, "payments_left": 3, "monthly_payment": 533_333}


def test_installment_screenshots_next_to_the_card():
    card = parse_card("for 365 days\n146 days left until April 18, 2027", TODAY)
    read = merge([parse_card("Контракт\nПОКУПКА В 1Fit 6 400 000 сум", TODAY), card])
    assert read["end_date"] == "2027-04-18" and read["installment"]
    assert mismatches(read, {"end_date": "2027-04-18", "paid_in_full": 1}) == ["installment"]
    assert mismatches(read, {"end_date": "2027-04-18", "paid_in_full": 0}) == []
    assert mismatches({"installment": True, "payments_left": 3},
                      {"paid_in_full": 0, "payments_left": 9}) == ["payments_left"]


def test_contract_dated_after_the_membership_began_is_flagged():
    # the real pair: the card says 365 days until 18.04.2027 (began about 18.04.2026),
    # the contract is from 12.08.2026 — four months later, so it is not this membership's
    card = parse_card("Personal membership\nfor 365 days\n146 days left until April 18, 2027", TODAY)
    contract = parse_card("Контракт\nПОКУПКА В 1Fit\n6 400 000 сум\nЗаявка №690984 от 12.08.2026", TODAY)
    read = merge([card, contract])
    assert mismatches(read, {"end_date": "2027-04-18", "tariff_days": 365, "paid_in_full": 0}) == ["contract_date"]
    bought_with_it = {**read, "contract_date": "2026-04-10"}
    assert mismatches(bought_with_it, {"end_date": "2027-04-18", "tariff_days": 365, "paid_in_full": 0}) == []


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


# ------------------------------------------------------------------ the real 1Fit card (app in English)
REAL_CARD_TWO_TILES = """17:00 LTE
<
Personal membership
for 365 days
52 days left until January 13, 2027
Your visit limit will reset on November 27 at 00:00.
More
Bonus days >
B 0 days
Share the promo code and earn more days
Attendance > Freezing >
100 /100 18
Perfect days left
"""

REAL_CARD_ONE_COLUMN = """Personal membership
for 365 days
52 days left
until January 13, 2027
Bonus days
0 days
Attendance
100 / 100
Perfect
Freezing
18
days left
"""


@pytest.mark.parametrize("text", [REAL_CARD_TWO_TILES, REAL_CARD_ONE_COLUMN])
def test_reads_the_real_english_card(text):
    """The screenshot a seller sent: «for 365 days», «until January 13, 2027», «Freezing 18 days left»."""
    assert parse_card(text, TODAY) == {"end_date": "2027-01-13", "tariff_days": 365, "freeze_days": 18}


def test_bonus_days_and_the_visit_limit_are_not_taken_for_anything():
    text = "Bonus days\n0 days\nYour visit limit will reset on November 27 at 00:00."
    assert parse_card(text, TODAY) == {}


def test_days_left_when_the_date_was_not_read():
    text = "for 365 days\n52 days left\nFreezing\n18\ndays left"
    assert parse_card(text, TODAY)["end_date"] == "2027-01-13"  # 22.11.2026 + 52 days
    assert parse_card("осталось 52 дня", TODAY)["end_date"] == "2027-01-13"


def test_reads_an_uzbek_latin_card():
    text = "Shaxsiy abonement\n365 kunga\n13-yanvar, 2027 gacha\nMuzlatish\n18 kun qoldi"
    assert parse_card(text, TODAY) == {"end_date": "2027-01-13", "tariff_days": 365, "freeze_days": 18}


# ------------------------------------------------------------------ the server read «100» instead of «18»
@pytest.mark.parametrize("row", ["100 | 100 18", "100 1100 18", "100 /100 18", "100 I 100 18"])
def test_freeze_is_the_last_number_before_days_left(row):
    text = f"Attendance > Freezing >\n{row}\nPerfect days left"
    assert parse_card(text, TODAY)["freeze_days"] == 18


def test_freeze_by_its_place_under_the_label():
    """Word boxes as on the screenshot (684 px wide): «100» and «/100» are in the Attendance tile on the left."""
    from two_fit.ocr import freeze_from_words

    def w(text, left, top, width, height=28):
        return {"text": text, "left": left, "top": top, "width": width, "height": height}
    words = [w("Attendance", 66, 873, 162), w(">", 285, 873, 12), w("Freezing", 377, 875, 120), w(">", 596, 875, 12),
             w("100", 66, 1040, 95, 44), w("/100", 170, 1052, 60), w("18", 378, 1043, 60, 44),
             w("Perfect", 66, 1094, 104), w("days", 377, 1096, 52), w("left", 435, 1096, 44)]
    assert freeze_from_words(words) == 18
    assert freeze_from_words([w("Attendance", 66, 873, 162), w("100", 66, 1040, 95)]) is None  # no label
