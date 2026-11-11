"""Business rules: commission, validation, statuses, no double sale / double commission, persistence."""
from datetime import date, timedelta

import pytest

from two_fit.money import calc_commission, fmt_money
from two_fit.repo import LS, RS, Repo, RepoError, today_local
from two_fit.validators import (
    ValidationError, contains_contacts, normalize_phone, parse_description, parse_end_date, parse_price,
)

FUTURE = (today_local() + timedelta(days=200)).isoformat()


def draft(price=2_000_000, **kw):
    d = {
        "city": "tashkent", "tariff": "Годовой", "end_date": FUTURE, "price": price,
        "description": "Осталось 7 месяцев", "screenshot_file_id": "file-1",
        "seller_name": "Алишер", "seller_phone": "+998901112233", "commission_accepted_price": price,
    }
    d.update(kw)
    return d


@pytest.fixture
async def repo(tmp_path):
    r = Repo(tmp_path / "t.db", 10)
    await r.connect()
    for uid in (1, 2, 3, 99):
        await r.touch_user(uid, f"user{uid}", f"User {uid}")
    yield r
    await r.close()


# ------------------------------------------------------------------ money
def test_commission_example_from_spec():
    c = calc_commission(2_000_000, 10)
    assert (c.commission, c.net) == (200_000, 1_800_000)


def test_commission_rounding_and_sum():
    for price in (10_001, 1_234_567, 99_999_999, 15):
        c = calc_commission(price, 10)
        assert c.commission + c.net == price
    assert calc_commission(15, 10).commission == 2  # 1.5 -> rounded half up


def test_fmt_money():
    assert fmt_money(2_000_000) == "2 000 000"


# ------------------------------------------------------------------ validators
def test_price_parsing():
    assert parse_price("2 000 000") == 2_000_000
    assert parse_price("1.500.000 сум") == 1_500_000
    for bad in ("abc", "5000", "999999999", "", "-100000"):
        with pytest.raises(ValidationError):
            parse_price(bad)


def test_date_parsing():
    today = date(2026, 10, 28)
    assert parse_end_date("31.05.2027", today) == date(2027, 5, 31)
    assert parse_end_date("1/2/27", today) == date(2027, 2, 1)
    with pytest.raises(ValidationError, match="bad_date_past"):
        parse_end_date("01.01.2026", today)
    with pytest.raises(ValidationError, match="bad_date_format"):
        parse_end_date("31.02.2027", today)
    with pytest.raises(ValidationError, match="bad_date_far"):
        parse_end_date("01.01.2035", today)


def test_phone():
    assert normalize_phone("+998 90 123-45-67") == "+998901234567"
    assert normalize_phone("90 123 45 67") == "+998901234567"
    with pytest.raises(ValidationError):
        normalize_phone("123")


@pytest.mark.parametrize("text", [
    "звоните +998 90 123 45 67", "пишите @alisher_fit", "t.me/alisher", "tel 901234567",
    "https://example.com", "mail me a@b.uz", "998901234567",
])
def test_contacts_detected(text):
    assert contains_contacts(text)


@pytest.mark.parametrize("text", [
    "Осталось 7 месяцев, цена 1 500 000", "Годовой безлимит до мая", "12 500 000 сум, торг", "Қолган 5 ой",
])
def test_no_false_positive(text):
    assert not contains_contacts(text)


def test_description_rejects_contacts():
    with pytest.raises(ValidationError, match="bad_contacts"):
        parse_description("звоните 90 123 45 67")


# ------------------------------------------------------------------ listings
async def test_commission_must_be_accepted_for_exact_price(repo):
    with pytest.raises(RepoError, match="commission_not_accepted"):
        await repo.create_listing(1, "ru", draft(commission_accepted_price=1_000_000))
    with pytest.raises(RepoError, match="commission_not_accepted"):
        await repo.create_listing(1, "ru", draft(commission_accepted_price=None))


async def test_listing_visible_only_after_approval(repo):
    lid = await repo.create_listing(1, "ru", draft())
    assert (await repo.catalog(None, 10, 0))[1] == 0
    await repo.approve(lid, 99)
    rows, total = await repo.catalog(None, 10, 0)
    assert total == 1 and rows[0]["id"] == lid


async def test_catalog_hides_expired_reserved_sold_withdrawn(repo):
    ids = [await repo.create_listing(1, "ru", draft()) for _ in range(4)]
    for i in ids:
        await repo.approve(i, 99)
    await repo.withdraw_by_seller(ids[0], 1)
    r = await repo.create_request(ids[1], 2, "Buyer", "+998900000000")
    await repo.reserve(r, 99)
    later = today_local() + timedelta(days=400)  # past every end_date
    assert (await repo.catalog(None, 10, 0))[1] == 2
    assert (await repo.catalog(None, 10, 0, today=later))[1] == 0
    assert not repo.is_available(await repo.get_listing(ids[2]), today=later)


async def test_catalog_newest_first_and_city_filter(repo):
    a = await repo.create_listing(1, "ru", draft(city="tashkent"))
    b = await repo.create_listing(1, "ru", draft(city="samarkand"))
    await repo.approve(a, 99)
    await repo.approve(b, 99)
    rows, _ = await repo.catalog(None, 10, 0)
    assert [r["id"] for r in rows] == [b, a]
    rows, total = await repo.catalog("samarkand", 10, 0)
    assert total == 1 and rows[0]["id"] == b
    assert dict(await repo.catalog_cities()) == {"tashkent": 1, "samarkand": 1}


async def test_edit_goes_back_to_moderation(repo):
    lid = await repo.create_listing(1, "ru", draft())
    await repo.approve(lid, 99)
    await repo.update_listing_by_seller(lid, 1, "ru", draft(price=1_500_000))
    listing = await repo.get_listing(lid)
    assert listing["status"] == LS.PENDING and listing["commission_accepted_price"] == 1_500_000
    with pytest.raises(RepoError, match="not_found"):
        await repo.update_listing_by_seller(lid, 2, "ru", draft())  # someone else's listing


async def test_requests_rules(repo):
    lid = await repo.create_listing(1, "ru", draft())
    with pytest.raises(RepoError, match="not_available"):
        await repo.create_request(lid, 2, "B", "+998900000000")  # not approved yet
    await repo.approve(lid, 99)
    with pytest.raises(RepoError, match="own"):
        await repo.create_request(lid, 1, "S", "+998900000000")
    await repo.create_request(lid, 2, "B", "+998900000000")
    with pytest.raises(RepoError, match="duplicate"):
        await repo.create_request(lid, 2, "B", "+998900000000")
    listing = await repo.get_listing(lid)
    assert listing["status"] == LS.APPROVED  # a request does not reserve automatically


async def full_deal(repo, final_price=None):
    lid = await repo.create_listing(1, "ru", draft())
    await repo.approve(lid, 99)
    r2 = await repo.create_request(lid, 2, "Buyer Two", "+998900000002")
    r3 = await repo.create_request(lid, 3, "Buyer Three", "+998900000003")
    listing, _ = await repo.reserve(r2, 99)
    assert listing["status"] == LS.RESERVED
    return lid, r2, r3


async def test_close_requires_confirmed_final_price(repo):
    lid, r2, r3 = await full_deal(repo)
    with pytest.raises(RepoError, match="price_not_confirmed"):
        await repo.close_deal(lid, 99)
    # Same price as accepted -> no extra confirmation needed.
    _, needs = await repo.set_final_price(lid, 99, 2_000_000)
    assert needs is False
    deal, others = await repo.close_deal(lid, 99)
    assert (deal["sale_price"], deal["commission_amount"], deal["seller_amount"]) == (2_000_000, 200_000, 1_800_000)
    assert deal["commission_status"] == "pending" and others == [3]
    assert (await repo.get_listing(lid))["status"] == LS.SOLD
    assert (await repo.get_request(r2))["status"] == RS.COMPLETED
    assert (await repo.get_request(r3))["status"] == RS.CLOSED


async def test_changed_price_needs_seller_confirmation(repo):
    lid, *_ = await full_deal(repo)
    _, needs = await repo.set_final_price(lid, 99, 1_700_000)
    assert needs is True
    with pytest.raises(RepoError, match="price_not_confirmed"):
        await repo.close_deal(lid, 99)
    with pytest.raises(RepoError, match="not_found"):
        await repo.seller_answer_price(lid, 2, 1_700_000, True)  # only the seller may confirm
    with pytest.raises(RepoError, match="stale"):
        await repo.seller_answer_price(lid, 1, 1_600_000, True)  # old button with another price
    await repo.seller_answer_price(lid, 1, 1_700_000, True)
    deal, _ = await repo.close_deal(lid, 99)
    assert (deal["sale_price"], deal["commission_amount"], deal["seller_amount"]) == (1_700_000, 170_000, 1_530_000)


async def test_seller_declines_price(repo):
    lid, *_ = await full_deal(repo)
    await repo.set_final_price(lid, 99, 1_000_000)
    listing = await repo.seller_answer_price(lid, 1, 1_000_000, False)
    assert listing["final_price"] is None
    with pytest.raises(RepoError, match="price_not_confirmed"):
        await repo.close_deal(lid, 99)


async def test_no_double_sale_and_no_double_commission(repo):
    lid, *_ = await full_deal(repo)
    await repo.set_final_price(lid, 99, 2_000_000)
    deal, _ = await repo.close_deal(lid, 99)
    with pytest.raises(RepoError, match="already"):
        await repo.close_deal(lid, 99)
    with pytest.raises(RepoError):
        await repo.reserve(await repo.create_request(lid, 3, "x", "+998900000009"), 99)
    paid = await repo.mark_commission_paid(deal["id"], 99)
    assert paid["commission_status"] == "paid" and paid["commission_paid_at"]
    with pytest.raises(RepoError, match="already"):
        await repo.mark_commission_paid(deal["id"], 99)
    s = await repo.stats()
    assert (s["deals"], s["volume"], s["accrued"], s["paid"], s["due"]) == (1, 2_000_000, 200_000, 200_000, 0)


async def test_concurrent_close_only_once(repo):
    import asyncio
    lid, *_ = await full_deal(repo)
    await repo.set_final_price(lid, 99, 2_000_000)
    results = await asyncio.gather(*(repo.close_deal(lid, 99) for _ in range(5)), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert (await repo.deals(10, 0))[1] == 1


async def test_release_reserve_returns_to_catalog(repo):
    lid, r2, _ = await full_deal(repo)
    listing, req = await repo.release_reserve(lid, 99)
    assert listing["status"] == LS.APPROVED and req["id"] == r2
    assert (await repo.get_request(r2))["status"] == RS.CANCELLED
    assert (await repo.catalog(None, 10, 0))[1] == 1
    s = await repo.stats()
    assert s["deals"] == 0 and s["accrued"] == 0  # failed sale -> no commission


async def test_reject_with_reason(repo):
    lid = await repo.create_listing(1, "ru", draft())
    listing = await repo.reject(lid, 99, "Контакты в описании")
    assert listing["status"] == LS.REJECTED and listing["reject_reason"] == "Контакты в описании"
    with pytest.raises(RepoError, match="bad_status"):
        await repo.approve(lid, 99)


async def test_data_survives_restart(tmp_path):
    path = tmp_path / "persist.db"
    r = Repo(path)
    await r.connect()
    await r.touch_user(1, "u", "U")
    await r.set_lang(1, "uz")
    lid = await r.create_listing(1, "uz", draft())
    await r.fsm_set_state("k", "SellForm:price")
    await r.fsm_set_data("k", {"draft": {"city": "tashkent"}})
    await r.close()

    r2 = Repo(path)
    await r2.connect()
    assert (await r2.get_user(1))["lang"] == "uz"
    assert (await r2.get_listing(lid))["status"] == LS.PENDING
    assert await r2.fsm_get("k") == ("SellForm:price", {"draft": {"city": "tashkent"}})
    await r2.close()
