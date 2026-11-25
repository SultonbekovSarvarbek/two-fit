"""Business rules: the listing fee, validation, statuses, one payment per listing, persistence."""
from datetime import date, timedelta

import pytest

from two_fit.money import calc_commission, fmt_money
from two_fit.repo import LS, Repo, RepoError, today_local
from two_fit.validators import (
    ValidationError, contains_contacts, normalize_phone, parse_description, parse_end_date, parse_monthly,
    parse_payments_left, parse_price, parse_start_date,
)
from two_fit.usage import remaining, usage

FUTURE = (today_local() + timedelta(days=200)).isoformat()


def draft(price=2_000_000, **kw):
    d = {
        "city": "tashkent", "tariff": "Годовой", "end_date": FUTURE, "price": price,
        "description": "Осталось 7 месяцев", "screenshot_file_id": "file-1",
        "seller_name": "Алишер", "seller_phone": "+998901112233", "commission_accepted_price": price,
        "paid_in_full": True, "tariff_days": 365,
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


def test_start_date_parsing():
    today, end = date(2026, 10, 28), date(2027, 3, 1)
    assert parse_start_date("01.03.2026", today, end) == date(2026, 3, 1)
    assert parse_start_date("28.10.2026", today, end) == today
    with pytest.raises(ValidationError, match="bad_start_future"):
        parse_start_date("29.10.2026", today, end)
    with pytest.raises(ValidationError, match="bad_start_order"):
        parse_start_date("01.10.2026", today, date(2026, 10, 1))
    with pytest.raises(ValidationError, match="bad_date_far"):
        parse_start_date("01.01.2019", today, end)


def test_usage():
    today = date(2026, 10, 28)
    # a 12-month membership activated 8 months ago
    assert usage("2026-02-28", "2027-02-28", today) == {"used": 8, "total": 12, "unit": "m", "pct": 66}
    assert usage("2026-10-28", "2027-10-28", today)["used"] == 0
    assert usage(None, "2027-02-28", today) is None
    assert usage("2027-03-01", "2027-02-28", today) is None  # inconsistent dates show nothing
    # short memberships are counted in days
    assert usage("2026-10-18", "2026-11-17", today) == {"used": 10, "total": 30, "unit": "d", "pct": 33}
    # used never exceeds the total, even past the end date
    u = usage("2025-01-01", "2026-01-01", today)
    assert u["used"] == u["total"] == 12 and u["pct"] == 100


def test_usage_left_matches_remaining():
    # listing #21: «Осталось 10 мес.» on top and «использовано 3 из 12 · осталось 9 мес.» below
    today = date(2026, 11, 22)
    u = usage("2026-09-01", "2027-09-11", today)
    assert u["total"] - u["used"] == remaining("2027-09-11", today)["n"] == 10


async def test_start_date_stored_and_checked(repo):
    start = (today_local() - timedelta(days=30)).isoformat()
    lid = await repo.create_listing(1, "ru", draft(start_date=start))
    assert (await repo.get_listing(lid))["start_date"] == start
    with pytest.raises(RepoError, match="bad_start"):
        await repo.create_listing(1, "ru", draft(start_date=FUTURE))


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
async def test_name_and_phone_are_required(repo):
    """Buyers call the seller: a listing without a phone is no use to anyone."""
    for missing in ("seller_name", "seller_phone"):
        with pytest.raises(RepoError, match="incomplete"):
            await repo.create_listing(1, "ru", draft(**{missing: ""}))


async def test_unpaid_listing_waits_for_the_fee(repo):
    lid = await repo.create_listing(1, "ru", draft(), fee=30_000)
    listing = await repo.get_listing(lid)
    assert (listing["status"], listing["fee_amount"], listing["fee_paid_at"]) == (LS.UNPAID, 30_000, None)
    assert await repo.count_status(LS.PENDING) == 0  # the admins do not see it yet
    with pytest.raises(RepoError, match="bad_status"):
        await repo.approve(lid, 99)
    assert await repo.fee_problem(lid, 2) == "not_found"  # someone else's listing
    assert await repo.fee_problem(lid, 1, 25_000) == "stale"  # an invoice for another sum
    assert await repo.fee_problem(lid, 1, 30_000) is None

    listing, first = await repo.pay_fee(lid, 1, 30_000, "UZS", "charge-1", "click-1")
    assert first and listing["status"] == LS.PENDING and listing["fee_paid_at"]
    assert await repo.fee_problem(lid, 1, 30_000) == "already"
    # Telegram may report the same payment twice: it is counted once
    _, again = await repo.pay_fee(lid, 1, 30_000, "UZS", "charge-1", "click-1")
    assert not again
    s = await repo.stats()
    assert (s["payments"], s["fees"], s["pending"], s["unpaid"]) == (1, 30_000, 1, 0)


async def test_paid_listing_is_edited_for_free(repo):
    lid = await repo.create_listing(1, "ru", draft(), fee=30_000)
    await repo.pay_fee(lid, 1, 30_000, "UZS", "charge-1")
    await repo.approve(lid, 99)
    await repo.update_listing_by_seller(lid, 1, "ru", draft(price=1_500_000))
    assert (await repo.get_listing(lid))["status"] == LS.PENDING  # back to moderation, not to the cashier


async def test_withdrawing_does_not_waive_the_fee(repo):
    lid = await repo.create_listing(1, "ru", draft(), fee=30_000)
    await repo.withdraw_by_seller(lid, 1)
    assert await repo.fee_problem(lid, 1) == "already"  # nothing to pay for while it is withdrawn
    await repo.update_listing_by_seller(lid, 1, "ru", draft())
    assert (await repo.get_listing(lid))["status"] == LS.UNPAID  # editing it back does not skip the fee


async def test_free_listing_goes_straight_to_moderation(repo):
    lid = await repo.create_listing(1, "ru", draft())  # payments are off
    listing = await repo.get_listing(lid)
    assert listing["status"] == LS.PENDING and not repo.fee_due(listing)


async def test_seller_marks_it_sold(repo):
    lid = await repo.create_listing(1, "ru", draft())
    with pytest.raises(RepoError, match="bad_status"):
        await repo.mark_sold(lid, 1)  # not published yet
    await repo.approve(lid, 99)
    with pytest.raises(RepoError, match="not_found"):
        await repo.mark_sold(lid, 2)
    await repo.mark_sold(lid, 1)
    assert (await repo.get_listing(lid))["status"] == LS.SOLD
    rows, total = await repo.catalog(None, 10, 0)  # a week in the list as «Sold»: proof that deals happen here
    assert total == 1 and rows[0]["status"] == LS.SOLD and repo.is_sold_shown(rows[0]) and not repo.is_available(rows[0])
    await repo.db.execute("UPDATE listings SET sale_reported_at='2020-01-01T00:00:00+00:00' WHERE id=?", (lid,))
    assert (await repo.catalog(None, 10, 0))[1] == 0
    with pytest.raises(RepoError, match="bad_status"):
        await repo.update_listing_by_seller(lid, 1, "ru", draft())  # sold is final


async def test_listing_visible_only_after_approval(repo):
    lid = await repo.create_listing(1, "ru", draft())
    assert (await repo.catalog(None, 10, 0))[1] == 0
    await repo.approve(lid, 99)
    rows, total = await repo.catalog(None, 10, 0)
    assert total == 1 and rows[0]["id"] == lid


async def test_catalog_hides_expired_unpaid_sold_withdrawn(repo):
    ids = [await repo.create_listing(1, "ru", draft()) for _ in range(4)]
    for i in ids:
        await repo.approve(i, 99)
    await repo.withdraw_by_seller(ids[0], 1)
    await repo.mark_sold(ids[1], 1)
    await repo.create_listing(1, "ru", draft(), fee=30_000)  # unpaid
    later = today_local() + timedelta(days=400)  # past every end_date
    rows, total = await repo.catalog(None, 10, 0)
    assert total == 3 and rows[-1]["id"] == ids[1]  # the sold one is shown last, for a week
    assert (await repo.catalog(None, 10, 0, today=later))[1] == 1  # expired ones go; the sold one stays its week
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


async def test_catalog_orders_installments_by_their_total(repo):
    # an installment is shown as 533 333 × 9 = 4 799 997: dearer than 2 000 000
    a = await repo.create_listing(1, "ru", draft(price=580_000, paid_in_full=False, monthly_payment=533_333,
                                                 payments_left=9, commission_accepted_price=580_000))
    b = await repo.create_listing(1, "ru", draft(price=2_000_000))
    await repo.approve(a, 99)
    await repo.approve(b, 99)
    rows, _ = await repo.catalog(None, 10, 0)
    assert [r["id"] for r in rows] == [b, a]


async def test_edit_goes_back_to_moderation(repo):
    lid = await repo.create_listing(1, "ru", draft())
    await repo.approve(lid, 99)
    await repo.update_listing_by_seller(lid, 1, "ru", draft(price=1_500_000))
    listing = await repo.get_listing(lid)
    assert listing["status"] == LS.PENDING and listing["commission_accepted_price"] == 1_500_000
    with pytest.raises(RepoError, match="not_found"):
        await repo.update_listing_by_seller(lid, 2, "ru", draft())  # someone else's listing


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


async def test_old_database_gets_new_columns(tmp_path):
    import aiosqlite

    from two_fit.repo import SCHEMA
    path = tmp_path / "old.db"
    old_schema = "\n".join(l for l in SCHEMA.splitlines() if not any(c in l for c in ("price_negotiable", "start_date", "paid_in_full", "monthly_payment", "payments_left",
                                 "tariff_days")))
    async with aiosqlite.connect(path) as db:  # a database from before the column existed
        await db.executescript(old_schema)
    r = Repo(path, 10)
    await r.connect()
    try:
        await r.touch_user(1, "u", "U")
        start = today_local().isoformat()
        lid = await r.create_listing(1, "ru", draft(price_negotiable=True, start_date=start))
        listing = await r.get_listing(lid)
        assert listing["price_negotiable"] == 1 and listing["start_date"] == start
        await r.close()
        await r.connect()  # running the migration twice is harmless
    finally:
        await r.close()


def test_installment_parsing():
    assert parse_monthly("500 000") == 500_000
    assert parse_payments_left("9") == 9 and parse_payments_left("9 месяцев") == 9
    for bad in ("0", "61", "", "девять", "1-2"):
        with pytest.raises(ValidationError) as e:
            parse_payments_left(bad)
        assert e.value.code == "bad_payments_left"
    with pytest.raises(ValidationError) as e:
        parse_monthly("100")
    assert e.value.code == "bad_monthly"


async def test_payment_kind_is_required(repo):
    with pytest.raises(RepoError):
        await repo.create_listing(1, "ru", draft(paid_in_full=None))
    with pytest.raises(RepoError):
        await repo.create_listing(1, "ru", draft(paid_in_full=False, monthly_payment=500_000))
    lid = await repo.create_listing(1, "ru", draft(paid_in_full=False, monthly_payment=500_000, payments_left=9))
    listing = await repo.get_listing(lid)
    assert (listing["paid_in_full"], listing["monthly_payment"], listing["payments_left"]) == (0, 500_000, 9)
    lid = await repo.create_listing(1, "ru", draft(monthly_payment=500_000, payments_left=9))  # fully paid
    listing = await repo.get_listing(lid)
    assert (listing["paid_in_full"], listing["monthly_payment"], listing["payments_left"]) == (1, None, None)


async def test_promo_code_listing(repo):
    with pytest.raises(RepoError):
        await repo.create_listing(1, "ru", draft(kind="promo", paid_in_full=None))  # the code is required
    lid = await repo.create_listing(1, "ru", draft(price=100_000, kind="promo", promo_code="FIT-2026",
                                                   paid_in_full=None, start_date=FUTURE))
    listing = await repo.get_listing(lid)
    assert (listing["kind"], listing["promo_code"], listing["start_date"]) == ("promo", "FIT-2026", None)


def test_promo_code_parsing():
    from two_fit.validators import parse_promo_code
    assert parse_promo_code("  FIT-2026 ") == "FIT-2026"
    for bad in ("", "ab", "two words", "x" * 41, "код!"):
        with pytest.raises(ValidationError):
            parse_promo_code(bad)


def test_remaining_replaces_the_tariff():
    from two_fit.usage import remaining
    today = date(2026, 10, 28)
    assert remaining("2027-05-28", today) == {"n": 7, "unit": "m"}
    assert remaining("2026-12-12", today) == {"n": 45, "unit": "d"}
    assert remaining("2026-10-01", today) == {"n": 0, "unit": "d"}  # already over: never negative


async def test_membership_keeps_no_tariff(repo):
    lid = await repo.create_listing(1, "ru", draft(tariff="Безлимит 12 месяцев"))
    assert (await repo.get_listing(lid))["tariff"] == ""  # «12 months» misled buyers about what is left
    with pytest.raises(RepoError):  # a promo code must say what it gives
        await repo.create_listing(1, "ru", draft(kind="promo", promo_code="FIT-1", tariff=""))


async def test_old_listings_get_the_rate_they_were_accepted_at(tmp_path):
    import aiosqlite

    from two_fit.repo import SCHEMA
    path = tmp_path / "old.db"
    old_schema = "\n".join(l for l in SCHEMA.splitlines() if "commission_percent        INTEGER," not in l)
    async with aiosqlite.connect(path) as db:
        await db.executescript(old_schema)
        ts = "2026-01-01T00:00:00+00:00"
        await db.execute("INSERT INTO users(id, created_at, updated_at) VALUES (1, ?, ?)", (ts, ts))
        for kind in ("membership", "promo"):
            await db.execute(
                """INSERT INTO listings(seller_id, lang, kind, city, tariff, end_date, price, seller_name, seller_phone,
                       commission_accepted_price, status, created_at, updated_at)
                   VALUES (1, 'ru', ?, 'tashkent', '', ?, 100000, 'A', '', 100000, 'approved', ?, ?)""",
                (kind, FUTURE, ts, ts))
        await db.commit()
    r = Repo(path, 15, 30)
    await r.connect()
    try:
        rows = await r._all("SELECT kind, commission_percent FROM listings ORDER BY id")
        assert [(x["kind"], x["commission_percent"]) for x in rows] == [("membership", 10), ("promo", 30)]
    finally:
        await r.close()


def test_default_membership_rate_is_15(monkeypatch):
    from two_fit.config import load_config
    monkeypatch.delenv("COMMISSION_PERCENT", raising=False)
    monkeypatch.setattr("two_fit.config.load_dotenv", lambda *a, **k: None)
    assert load_config().commission_percent == 15


async def test_old_installment_listings_get_the_total_price(tmp_path):
    """Listed before the rule: 5 800 000 typed + 11 × 557 000 to 1Fit becomes 6 127 000, once, on start."""
    path = tmp_path / "inst.db"
    r = Repo(path, 10)
    await r.connect()
    await r.touch_user(1, "u1", "U1")
    lid = await r.create_listing(1, "ru", draft(price=2_000_000))
    await r.db.execute("UPDATE listings SET paid_in_full=0, monthly_payment=557000, payments_left=11, price=5800000 "
                       "WHERE id=?", (lid,))
    await r.db.execute("DELETE FROM settings WHERE key='installment_price_v1'")  # as before the update
    await r.close()
    r = Repo(path, 10)
    await r.connect()
    try:
        assert (await r.get_listing(lid))["price"] == 6_127_000
    finally:
        await r.close()


async def test_catalog_is_ordered_by_the_price_buyers_see(repo):
    """An installment shows monthly × payments left: it is sorted by that, not by a price stored before."""
    inst = await repo.create_listing(1, "ru", draft(price=2_000_000))
    await repo.db.execute("UPDATE listings SET paid_in_full=0, monthly_payment=557000, payments_left=1, price=5800000 "
                          "WHERE id=?", (inst,))  # shown as 557 000
    full = await repo.create_listing(1, "ru", draft(price=750_000))
    dear = await repo.create_listing(1, "ru", draft(price=3_710_000))
    for i in (inst, full, dear):
        await repo.approve(i, 99)
    rows, _ = await repo.catalog(None, 10, 0)
    assert [r["id"] for r in rows] == [inst, full, dear]
