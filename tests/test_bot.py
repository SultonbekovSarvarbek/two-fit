"""End-to-end scenarios through the real handlers (fake Telegram, no token)."""
from datetime import timedelta

import pytest

from two_fit.repo import LS, today_local

from .harness import Harness, alerts, buttons_of, texts

SELLER, BUYER, STRANGER, ADMIN = 1, 2, 3, 900
END = (today_local() + timedelta(days=200)).strftime("%d.%m.%Y")
START = (today_local() - timedelta(days=165)).strftime("%d.%m.%Y")  # 365 days in all: 5 of 12 months used


@pytest.fixture
async def h(tmp_path):
    harness = await Harness(tmp_path / "bot.db").start()
    yield harness
    await harness.stop()


async def choose_lang(h, uid, lang):
    first = await h.send(uid, "/start")
    assert "Выберите язык" in texts(first) and "Тилни танланг" in texts(first)
    return await h.click(uid, f"lang:{lang}")


async def create_listing(h, uid=SELLER, price="2 000 000"):
    """Seller (in Uzbek) walks the whole wizard. Returns the calls of the final «send»."""
    await h.send(uid, "➕ Абонемент сотиш")
    await h.click(uid, "sl:kind:membership")
    await h.click(uid, "sl:city:tashkent")
    await h.click(uid, "sl:plan:365")
    await h.send(uid, END)
    await h.send(uid, START)
    await h.click(uid, "sl:pay:full")
    await h.click(uid, "sl:freeze:no")
    calc = await h.send(uid, price)
    assert "Нарх: <b>2\xa0000\xa0000 сўм" in texts(calc) and "комиссия" not in texts(calc).lower()
    await h.send(uid, "Қолган 7 ой, зал яқин")
    await h.send(uid, photo=True)
    await h.click(uid, "sl:shotsdone:")
    await h.send(uid, "Алишер")
    preview = await h.send(uid, contact="+998901112233")
    pt = texts(preview)
    assert "комиссия" not in pt.lower()
    # the contacts are public now: the seller sees them as every buyer will
    assert "Сотувчи: <b>Алишер</b>" in pt and "+998901112233" in pt and "@user1" in pt
    assert "барча харидорлар кўради" in pt
    assert "келишилади" not in pt
    assert "Ишлатилди" not in pt and "▰" not in pt  # no «used … of 12 months» bar
    end = today_local() + timedelta(days=200)
    month = "январь феврал март апрел май июн июл август сентябр октябр ноябр декабр".split()[end.month - 1]
    assert f"Амал қилиш муддати: {end.day} {month} {end.year} гача" in pt  # words, not 07.05.2027
    toggled = texts(await h.click(uid, "sl:negotiable:"))
    assert "сўм</b> · <i>келишилади</i>" in toggled
    await h.click(uid, "sl:negotiable:")  # back to a fixed price
    return await h.click(uid, "sl:send:")


async def test_full_happy_path(h):
    # --- languages
    sel = await choose_lang(h, SELLER, "uz")
    assert "Тил: ўзбекча" in texts(sel) and "Асосий" not in texts(sel)
    assert "Барча абонементлар" in [b.text.split(" ", 1)[1] for c in sel for b in
                                     (getattr(getattr(c, "reply_markup", None), "keyboard", None) or [[]])[0]]
    await choose_lang(h, BUYER, "ru")
    await choose_lang(h, ADMIN, "ru")
    await choose_lang(h, STRANGER, "ru")

    # --- empty catalog
    empty = await h.send(BUYER, "🏋️ Все абонементы")
    assert "Пока нет доступных абонементов" in texts(empty)
    assert "https://t.me/s_sarvar" in h.url_buttons(BUYER)

    # --- sell
    sent = await create_listing(h)
    assert "№1 эълон текширувга юборилди" in texts(sent)
    # a new listing: the seller is asked to follow 2FIT on Instagram
    assert "Instagram саҳифамизга обуна бўлинг" in texts(sent)
    assert "https://www.instagram.com/2fit_uz/" in h.url_buttons(SELLER)
    assert "Новое объявление №1" in h.texts_to(ADMIN)
    assert (await h.repo.get_listing(1))["status"] == LS.PENDING
    # not in catalog until approved
    assert "Пока нет доступных" in texts(await h.send(BUYER, "🏋️ Все абонементы"))

    # --- closed admin
    assert "Админка" not in texts(await h.send(STRANGER, "/admin"))
    assert alerts(await h.click(STRANGER, "ad:open:1:0:")) == ["Эта кнопка устарела. Откройте раздел заново."]
    assert "+998901112233" not in h.texts_to(STRANGER)
    home = await h.send(ADMIN, "/admin")
    assert "Админка Two Fit" in texts(home)
    card = await h.click(ADMIN, "ad:open:1:0:")
    assert "+998901112233" in texts(card) and "Алишер" in texts(card) and "@user1" in texts(card)
    shot = await h.click(ADMIN, "ad:shot:1:0:")
    assert any(type(c).__name__ == "SendPhoto" and c.photo == "shot-1" for c in shot)
    await h.click(ADMIN, "ad:appr:1:0:")
    assert "№1 эълон маъқулланди" in h.texts_to(SELLER)

    # --- catalog & card (buyer, Russian)
    cat = await h.send(BUYER, "🏋️ Все абонементы")
    assert "Найдено: 1" in texts(cat)
    card = await h.click(BUYER, h.button(BUYER, "№1"))
    ct = texts(card)
    assert "Объявление №1" in ct and "Ташкент" in ct and "Қолган 7 ой" in ct
    assert "⏳ Осталось: <b>7 мес.</b>" in ct and "Тариф" not in ct  # months left, not a misleading tariff name
    # --- the seller's contacts, straight on the card: a regular marketplace
    assert "Продавец: <b>Алишер</b>" in ct and "+998901112233" in ct and "@user1" in ct
    assert "https://t.me/user1" in h.url_buttons(BUYER)
    assert "Не переводите деньги" in ct and "buy:" not in str([getattr(c, "reply_markup", None) for c in card])

    # --- city filter
    await h.click(BUYER, "cty:")
    assert "Найдено: 1" in texts(await h.click(BUYER, "cat:0:tashkent"))
    assert "В городе Бухара пока нет" in texts(await h.click(BUYER, "cat:0:bukhara"))

    # --- the seller sold it: gone from the catalog
    await h.click(SELLER, "my:sold:1:0")
    sold = await h.click(SELLER, "my:sold!:1:0")
    assert "каталогдан олинди" in texts(sold)
    assert (await h.repo.get_listing(1))["status"] == LS.SOLD
    assert "Пока нет" in texts(await h.send(BUYER, "🏋️ Все абонементы"))
    assert any("недоступно" in a for a in alerts(await h.click(BUYER, "card:1:0:")))
    stats = texts(await h.click(ADMIN, "ad:stats:0:0:"))
    assert "Продано: 1" in stats and "Оплат размещения: 0" in stats


async def test_listing_fee_is_paid_before_moderation(tmp_path):
    h = await Harness(tmp_path / "fee.db", fee=30_000, payment_token="CLICK:TEST").start()
    try:
        await choose_lang(h, SELLER, "uz")
        await choose_lang(h, ADMIN, "ru")
        assert "Эълон жойлаш — 30\xa0000 сўм, бир марта" in texts(await h.send(SELLER, "/start"))
        assert "30\xa0000 сўм" in texts(await h.send(SELLER, "➕ Абонемент сотиш"))
        await h.send(SELLER, "/start")
        sent = await create_listing(h)
        invoices = [c for c in sent if type(c).__name__ == "SendInvoice"]
        assert len(invoices) == 1
        inv = invoices[0]
        assert (inv.currency, inv.prices[0].amount, inv.payload, inv.provider_token) == \
            ("UZS", 3_000_000, "fee:1", "CLICK:TEST")
        assert "тўлаш қолди" in texts(sent) and "Instagram" in texts(sent)
        assert type(sent[-1]).__name__ == "SendInvoice"  # the invoice stays at the bottom of the chat
        assert (await h.repo.get_listing(1))["status"] == LS.UNPAID
        assert "Новое объявление" not in h.texts_to(ADMIN)  # not before the payment

        # Telegram asks first: a wrong sum, someone else's listing, a strange payload are refused
        def answer(calls):
            return next(c for c in calls if type(c).__name__ == "AnswerPreCheckoutQuery")
        assert answer(await h.pre_checkout(SELLER, "fee:1", 1_000_000)).ok is False
        assert answer(await h.pre_checkout(STRANGER, "fee:1", 3_000_000)).ok is False
        assert answer(await h.pre_checkout(SELLER, "nonsense", 3_000_000)).ok is False
        assert answer(await h.pre_checkout(SELLER, "fee:1", 3_000_000)).ok is True

        paid = await h.pay(SELLER, "fee:1", 3_000_000)
        assert "30\xa0000 сўм тўлов қабул қилинди" in texts(paid)
        listing = await h.repo.get_listing(1)
        assert listing["status"] == LS.PENDING and listing["fee_paid_at"]
        assert "Новое объявление №1" in h.texts_to(ADMIN)
        assert "Оплачено размещение: 30\xa0000" in h.texts_to(ADMIN)
        assert "Размещение оплачено: 30\xa0000" in texts(await h.click(ADMIN, "ad:open:1:0:"))
        # the same payment reported twice changes nothing
        assert texts(await h.pay(SELLER, "fee:1", 3_000_000)) == ""
        assert answer(await h.pre_checkout(SELLER, "fee:1", 3_000_000)).ok is False  # already paid
        stats = texts(await h.click(ADMIN, "ad:stats:0:0:"))
        assert "Оплат размещения: 1" in stats and "Получено: 30\xa0000" in stats

        # editing after the payment is free: straight back to moderation
        await h.click(ADMIN, "ad:appr:1:0:")
        await h.click(SELLER, "my:edit:1:0")
        await h.click(SELLER, "sl:edit:")
        await h.click(SELLER, "sl:field:desc")
        again = await h.send(SELLER, "Янги тавсиф")
        assert "тўлаб юбориш" not in str([c.reply_markup for c in again])
        resent = await h.click(SELLER, "sl:send:")
        assert not [c for c in resent if type(c).__name__ == "SendInvoice"]
        assert "Instagram" not in texts(resent)  # asked once, not on every edit
        assert (await h.repo.get_listing(1))["status"] == LS.PENDING
    finally:
        await h.stop()


async def test_unpaid_listing_can_be_paid_later(tmp_path):
    h = await Harness(tmp_path / "later.db", fee=30_000, payment_token="CLICK:TEST").start()
    try:
        await choose_lang(h, SELLER, "uz")
        await create_listing(h)
        card = texts(await h.click(SELLER, "my:open:1:0"))
        assert "Эълон жойлаш тўланмаган: 30\xa0000 сўм" in card
        again = await h.click(SELLER, "my:pay:1:0")
        assert [c for c in again if type(c).__name__ == "SendInvoice"]
        await choose_lang(h, STRANGER, "ru")
        assert alerts(await h.click(STRANGER, "my:pay:1:0")) == ["Объявление не найдено."]
        # withdrawn while unpaid: it never reached the admins and still owes the fee
        await h.click(SELLER, "my:wd!:1:0")
        assert (await h.repo.get_listing(1))["status"] == LS.WITHDRAWN
        assert alerts(await h.click(SELLER, "my:pay:1:0"))
    finally:
        await h.stop()



async def test_wizard_navigation_and_validation(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:samarkand")
    assert "Шаг 1/9" in texts(await h.click(SELLER, "sl:back:"))  # back to city
    await h.click(SELLER, "sl:city:samarkand")
    await h.click(SELLER, "sl:plan:365")
    assert "прошла" in texts(await h.send(SELLER, "01.01.2020"))
    assert "Как оплачен абонемент" in texts(await h.send(SELLER, END))  # no start date question any more
    await h.click(SELLER, "sl:pay:full")
    await h.click(SELLER, "sl:freeze:no")
    assert "цифрами" in texts(await h.send(SELLER, "дорого"))
    await h.send(SELLER, "3000000")
    assert "контакты" in texts(await h.send(SELLER, "пишите @alisher_fit"))
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    assert "Не удалось распознать номер" in texts(await h.send(SELLER, "12"))
    await h.send(SELLER, "+998901112233")
    # change the price from the preview, then send: nothing to accept, no fee while payments are off
    await h.click(SELLER, "sl:edit:")
    await h.click(SELLER, "sl:field:price")
    changed = await h.send(SELLER, "2 500 000")
    assert "Цена: <b>2\xa0500\xa0000 сум" in texts(changed)
    assert "📤 Отправить" in str([c.reply_markup for c in changed])
    await h.click(SELLER, "sl:send:")
    listing = await h.repo.get_listing(1)
    assert listing["price"] == 2_500_000 and listing["status"] == LS.PENDING
    assert listing["description"] == "" and listing["screenshot_file_id"] is None
    # main-menu buttons work from anywhere and cancel the wizard
    await h.send(SELLER, "➕ Продать абонемент")
    how = await h.send(SELLER, "ℹ️ Как это работает")
    assert "не партнёр 1Fit" in texts(how)
    assert "Выберите действие" in texts(await h.send(SELLER, "Годовой"))  # wizard was cancelled


async def test_my_listings_edit_withdraw(h):
    await choose_lang(h, SELLER, "uz")
    await choose_lang(h, ADMIN, "ru")
    await create_listing(h)
    await h.click(ADMIN, "ad:rej:1:0:")
    await h.send(ADMIN, "Уберите контакты из описания")
    assert "рад этилди" in h.texts_to(SELLER) and "Уберите контакты" in h.texts_to(SELLER)

    my = await h.send(SELLER, "📂 Менинг эълонларим")
    assert "Рад этилган" in str([c.reply_markup for c in my])
    await h.click(SELLER, "my:open:1:0")
    await h.click(SELLER, "my:edit:1:0")
    await h.click(SELLER, "sl:edit:")
    await h.click(SELLER, "sl:field:desc")
    await h.send(SELLER, "Тоза тавсиф")
    await h.click(SELLER, "sl:send:")
    assert "қайта текширувга" in h.texts_to(SELLER)
    assert (await h.repo.get_listing(1))["status"] == LS.PENDING
    assert "изменено и ждёт повторной модерации" in h.texts_to(ADMIN)

    await h.click(ADMIN, "ad:appr:1:0:")

    # someone else cannot touch my listing
    await choose_lang(h, STRANGER, "ru")
    assert alerts(await h.click(STRANGER, "my:wd!:1:0")) == ["Эта кнопка устарела. Откройте раздел заново."]

    await h.click(SELLER, "my:wd:1:0")
    await h.click(SELLER, "my:wd!:1:0")
    assert (await h.repo.get_listing(1))["status"] == LS.WITHDRAWN
    assert "Продавец снял объявление №1" in h.texts_to(ADMIN)


async def test_support_and_language_switch(h):
    await choose_lang(h, BUYER, "ru")
    sup = await h.send(BUYER, "💬 Поддержка")
    assert "@s_sarvar" in texts(sup) and "https://t.me/s_sarvar" in h.url_buttons(BUYER)
    await h.send(BUYER, "🌐 Сменить язык")
    switched = await h.click(BUYER, "lang:uz")
    assert "Тил: ўзбекча" in texts(switched)
    assert "ёрдам хизмати" in texts(await h.send(BUYER, "💬 Ёрдам"))


async def test_restart_keeps_language_data_and_wizard(tmp_path):
    db = tmp_path / "restart.db"
    h1 = await Harness(db).start()
    await choose_lang(h1, SELLER, "uz")
    await h1.send(SELLER, "➕ Абонемент сотиш")
    await h1.click(SELLER, "sl:kind:membership")
    await h1.click(SELLER, "sl:city:tashkent")
    await h1.click(SELLER, "sl:plan:365")
    await h1.stop()

    h2 = await Harness(db).start()  # "restart"
    nxt = await h2.send(SELLER, END)  # wizard continues at the payment step, in Uzbek
    assert "4/9-қадам" in texts(nxt)
    welcome = await h2.send(SELLER, "/start")
    assert "Тилни танланг" not in texts(welcome) and "Эълон жойлаш — бепул" in texts(welcome)
    await h2.stop()


async def test_bot_ignores_group_chats(h):
    upd = {"message": {"message_id": 5, "date": 0, "chat": {"id": -100, "type": "supergroup"},
                       "from": h._user(SELLER), "text": "/start"}}
    assert await h._feed(upd) == []


async def test_installment_wizard_and_card(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    assert "Шаг 4/9" in texts(await h.send(SELLER, END))
    assert "Выберите действие" in texts(await h.send(SELLER, "500000"))  # a button is expected here
    assert "в месяц" in texts(await h.click(SELLER, "sl:pay:installment"))
    assert "цифрами" in texts(await h.send(SELLER, "много"))
    assert "платежей" in texts(await h.send(SELLER, "500 000"))
    assert "от 1 до 60" in texts(await h.send(SELLER, "100"))
    assert "Шаг 5/9" in texts(await h.send(SELLER, "9")) # then the freeze question
    ask_price = texts(await h.click(SELLER, "sl:freeze:no"))
    assert "заплатит вам сейчас" in ask_price and "500\xa0000 сум × 9" in ask_price
    # Back from the price goes to the freeze question; «yes» then asks how many days
    assert "заморозки" in texts(await h.click(SELLER, "sl:back:"))
    assert "Сколько дней" in texts(await h.click(SELLER, "sl:freeze:yes"))
    assert "от 1 до 365" in texts(await h.send(SELLER, "0"))
    await h.send(SELLER, "20")
    await h.send(SELLER, "300 000")
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    preview = texts(await h.send(SELLER, contact="+998901112233"))
    # the price shown is the whole sum: 300 000 now + 9 × 500 000 to 1Fit
    assert "💰 Цена: <b>4\xa0800\xa0000 сум</b>" in preview
    assert "Рассрочка" not in preview  # buyers never see the installments, only the whole sum
    assert "❄️ Заморозка: осталось 20 дн." in preview
    await h.click(SELLER, "sl:send:")
    listing = (await h.repo.seller_listings(SELLER, 10, 0))[0][0]
    assert (listing["paid_in_full"], listing["monthly_payment"], listing["payments_left"]) == (0, 500_000, 9)
    assert listing["freeze_days"] == 20
    assert listing["price"] == 300_000


async def test_only_12_and_24_month_tariffs_can_be_sold(h):
    """1Fit re-registers only 12- and 24-month memberships: no «6 months» button, an old button is refused."""
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    ask = await h.click(SELLER, "sl:city:tashkent")
    plans = [b.callback_data for c in ask for b in buttons_of(c) if (b.callback_data or "").startswith("sl:plan:")]
    assert plans == ["sl:plan:365", "sl:plan:730"]
    assert "12 или 24 месяца" in texts(ask)
    await h.click(SELLER, "sl:plan:180")
    assert (await h.dp.storage.get_data(key=_key(h)))["draft"].get("tariff_days") is None


async def test_fully_paid_skips_installment_questions(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    await h.send(SELLER, END)
    assert "Шаг 5/9" in texts(await h.click(SELLER, "sl:pay:full"))
    ask_price = texts(await h.click(SELLER, "sl:freeze:no"))
    assert "Шаг 6/9" in ask_price and "больше он ничего не доплачивает" in ask_price
    assert "Шаг 5/9" in texts(await h.click(SELLER, "sl:back:"))  # back to the freeze question
    assert "Шаг 4/9" in texts(await h.click(SELLER, "sl:back:"))  # then the payment, skipping installments


async def test_promo_code_wizard(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:promo")
    await h.click(SELLER, "sl:city:tashkent")
    await h.send(SELLER, "Безлимит на 1 месяц")
    ask_code = texts(await h.send(SELLER, END))
    assert "Шаг 4/8" in ask_code and "Сам промокод" in ask_code  # no start date, no payment question
    assert "одно слово" in texts(await h.send(SELLER, "два слова"))
    ask_price = texts(await h.send(SELLER, "FIT-2026"))
    assert "Цена промокода" in ask_price and "%" not in ask_price
    calc = texts(await h.send(SELLER, "100 000"))
    assert "Цена: <b>100\xa0000 сум" in calc
    assert "Шаг 5/8" in texts(await h.click(SELLER, "sl:back:"))  # from the description back to the price
    await h.send(SELLER, "100 000")
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    preview = texts(await h.send(SELLER, contact="+998901112233"))
    assert "🎁 промокод 1Fit" in preview and "Что даёт: Безлимит на 1 месяц" in preview
    assert "Промокод: <code>FIT-2026</code>" in preview  # in the private block
    await h.click(SELLER, "sl:send:")
    listing = (await h.repo.seller_listings(SELLER, 10, 0))[0][0]
    assert (listing["kind"], listing["promo_code"], listing["price"]) == ("promo", "FIT-2026", 100_000)


async def test_preview_says_what_the_seller_gives(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:kind:membership")
    await h.click(SELLER, "sl:city:tashkent")
    await h.click(SELLER, "sl:plan:365")
    await h.send(SELLER, END)
    await h.click(SELLER, "sl:pay:full")
    await h.click(SELLER, "sl:freeze:no")
    await h.send(SELLER, "2 000 000")
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    preview = texts(await h.send(SELLER, contact="+998901112233"))
    assert "📦 Вы отдаёте покупателю <b>7 мес.</b> — до" in preview and "Всё верно?" in preview


def test_stored_installment_is_not_a_suspicious_price():
    # listing #21: 10 000 now + 9 × 533 333 to 1Fit was flagged «possibly the monthly price», because the
    # stored listing says paid_in_full=0 and only False counted as an installment
    from types import SimpleNamespace

    from two_fit.bot.ui import price_advice
    ctx = SimpleNamespace(repo=SimpleNamespace(fit_prices=None))
    end = (today_local() + timedelta(days=293)).isoformat()
    stored = {"kind": "membership", "end_date": end, "tariff_days": 365, "price": 10_000,
              "paid_in_full": 0, "monthly_payment": 533_333, "payments_left": 9}
    a = price_advice(ctx, stored)
    assert a.installments == 4_799_997 and not a.suspicious(10_000)
    assert price_advice(ctx, {**stored, "paid_in_full": 1}).installments == 0


def _key(h):
    from aiogram.fsm.storage.base import StorageKey
    return StorageKey(bot_id=h.bot.id, chat_id=SELLER, user_id=SELLER)


async def test_admin_asks_every_seller_to_update_the_price(h):
    await choose_lang(h, SELLER, "uz")
    await choose_lang(h, ADMIN, "ru")
    await create_listing(h)                       # №1, in moderation
    await create_listing(h)                       # №2, published
    await h.repo.approve(2, ADMIN)
    await h.repo.touch_user(STRANGER, "user3", "Name3")
    from .test_core import draft as repo_draft
    gone = await h.repo.create_listing(STRANGER, "ru", repo_draft())
    await h.repo.withdraw_by_seller(gone, STRANGER)  # withdrawn: not asked

    assert "Админка" not in texts(await h.send(STRANGER, "/askprice"))  # admins only
    ask = texts(await h.send(ADMIN, "/askprice"))
    assert "1 продавцам (2 объявлений" in ask
    before = len(h.to(SELLER))
    done = await h.click(ADMIN, "ad:askp!:0:0:")
    assert "Отправлено: 1. Не доставлено: 0" in texts(done)
    got = h.to(SELLER)[before:]
    assert len(got) == 1 and "Бу 2FIT оператори" in texts(got) and "2FIT жамоаси" in texts(got)
    assert [b.callback_data for c in got for b in buttons_of(c)] == ["my:open:1:0", "my:open:2:0"]
    assert not h.to(STRANGER)[-1:] or "нархини" not in texts(h.to(STRANGER)[-1:])
