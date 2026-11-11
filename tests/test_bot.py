"""End-to-end scenarios through the real handlers (fake Telegram, no token)."""
from datetime import timedelta

import pytest

from two_fit.repo import LS, today_local

from .harness import Harness, alerts, texts

SELLER, BUYER, STRANGER, ADMIN = 1, 2, 3, 900
END = (today_local() + timedelta(days=200)).strftime("%d.%m.%Y")


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
    await h.click(uid, "sl:city:tashkent")
    await h.send(uid, "Йиллик безлимит")
    await h.send(uid, END)
    calc = await h.send(uid, price)
    assert "200 000" in texts(calc) and "1 800 000" in texts(calc)
    await h.send(uid, "Қолган 7 ой, зал яқин")
    await h.send(uid, photo=True)
    await h.send(uid, "Алишер")
    preview = await h.send(uid, contact="+998901112233")
    pt = texts(preview)
    assert "Two Fit комиссияси (10%): 200 000" in pt and "Сизга қолади: <b>1 800 000" in pt
    assert "Фақат администратор кўради" in pt and "+998901112233" in pt
    # Send without accepting the commission is refused
    refused = await h.click(uid, "sl:send:")
    assert any("комиссияга розилигингизни" in a for a in alerts(refused))
    await h.click(uid, "sl:accept:")
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
    assert "Объявление №1" in ct and "Ташкент" in ct and "Йиллик безлимит" in ct and "Қолган 7 ой" in ct
    assert "Укажите оператору номер объявления" in ct
    assert "https://t.me/s_sarvar" in h.url_buttons(BUYER)

    # --- city filter
    await h.click(BUYER, "cty:")
    assert "Найдено: 1" in texts(await h.click(BUYER, "cat:0:tashkent"))
    assert "В городе Бухара пока нет" in texts(await h.click(BUYER, "cat:0:bukhara"))

    # --- buy
    await h.click(BUYER, "buy:1")
    await h.send(BUYER, "Бобур")
    done = await h.send(BUYER, "+998 93 000 00 02")
    assert "Заявка на объявление №1 принята" in texts(done)
    assert "Новая заявка #1 на объявление №1" in h.texts_to(ADMIN) and "+998930000002" in h.texts_to(ADMIN)
    assert any("уже оставили заявку" in a for a in alerts(await h.click(BUYER, "buy:1")))
    assert any("сизнинг эълонингиз" in a for a in alerts(await h.click(SELLER, "buy:1")))
    assert (await h.repo.get_listing(1))["status"] == LS.APPROVED  # no automatic reserve

    # --- reserve, changed price, seller confirms
    await h.click(ADMIN, "ad:reqs:1:0:")
    await h.click(ADMIN, "ad:sel:1:0:")
    assert (await h.repo.get_listing(1))["status"] == LS.RESERVED
    assert "харидор топилди" in h.texts_to(SELLER)
    assert "зарезервирован" in h.texts_to(BUYER)
    assert "Пока нет" in texts(await h.send(STRANGER, "🏋️ Все абонементы"))  # reserved = hidden
    await h.click(ADMIN, "ad:fp:1:0:")
    await h.send(ADMIN, "1 800 000")
    assert "Якуний нарх" not in h.texts_to(ADMIN)  # admin speaks Russian
    seller_text = h.texts_to(SELLER)
    assert "якуний нарх ўзгарди" in seller_text and "180 000" in seller_text and "1 620 000" in seller_text
    refused = await h.click(ADMIN, "ad:close:1:0:")
    assert any("подтверждения продавца" in a for a in alerts(refused))
    await h.click(SELLER, h.button(SELLER, "Тасдиқлайман"))
    assert "Продавец подтвердил итоговую цену" in h.texts_to(ADMIN)

    # --- close deal once, commission once
    await h.click(ADMIN, "ad:close:1:0:")
    closed = await h.click(ADMIN, "ad:close!:1:0:")
    assert "Сделка #1" in texts(closed) and "⏳ ожидается" in texts(closed)
    assert "битим ёпилди" in h.texts_to(SELLER) and "Сделка по объявлению №1 закрыта" in h.texts_to(BUYER)
    assert any("уже сделано" in a for a in alerts(await h.click(ADMIN, "ad:close!:1:0:")))
    await h.click(ADMIN, "ad:paid:1:0:")
    await h.click(ADMIN, "ad:paid!:1:0:")
    assert any("уже сделано" in a for a in alerts(await h.click(ADMIN, "ad:paid!:1:0:")))
    stats = texts(await h.click(ADMIN, "ad:stats:0:0:"))
    assert "Закрытых сделок: 1" in stats and "Получено комиссии: 180 000" in stats
    assert "Ожидает оплаты: 0" in stats and "Общая стоимость сделок: 1 800 000" in stats

    # sold listing is gone from the catalog, history stays
    assert "Пока нет" in texts(await h.send(BUYER, "🏋️ Все абонементы"))
    assert (await h.repo.deal_for_listing(1))["commission_status"] == "paid"
    # old card button after the sale
    stale = await h.click(BUYER, "card:1:0:")
    assert any("недоступно" in a for a in alerts(stale))

    # --- privacy: neither side ever saw the other's data
    buyer_seen = h.texts_to(BUYER)
    for secret in ("+998901112233", "Алишер", "@user1", "shot-1"):
        assert secret not in buyer_seen
    seller_seen = h.texts_to(SELLER)
    for secret in ("+998930000002", "Бобур", "@user2"):
        assert secret not in seller_seen


async def test_wizard_navigation_and_validation(h):
    await choose_lang(h, SELLER, "ru")
    await h.send(SELLER, "➕ Продать абонемент")
    await h.click(SELLER, "sl:city:samarkand")
    assert "Шаг 1/7" in texts(await h.click(SELLER, "sl:back:"))  # back to city
    await h.click(SELLER, "sl:city:samarkand")
    assert "от 2 до 60" in texts(await h.send(SELLER, "x"))
    await h.send(SELLER, "Годовой")
    assert "прошла" in texts(await h.send(SELLER, "01.01.2020"))
    await h.send(SELLER, END)
    assert "цифрами" in texts(await h.send(SELLER, "дорого"))
    await h.send(SELLER, "3000000")
    assert "контакты" in texts(await h.send(SELLER, "пишите @alisher_fit"))
    await h.click(SELLER, "sl:skip:")
    await h.click(SELLER, "sl:skip:")
    await h.send(SELLER, "Алишер")
    assert "Не удалось распознать номер" in texts(await h.send(SELLER, "12"))
    await h.send(SELLER, "+998901112233")
    # change the price from the preview -> acceptance is reset and must be given again
    await h.click(SELLER, "sl:accept:")
    await h.click(SELLER, "sl:edit:")
    await h.click(SELLER, "sl:field:price")
    changed = await h.send(SELLER, "2 500 000")
    assert "Цена изменилась" in texts(changed) and "☐ Согласен" in str([c.reply_markup for c in changed])
    assert any("подтвердите согласие" in a for a in alerts(await h.click(SELLER, "sl:send:")))
    await h.click(SELLER, "sl:accept:")
    await h.click(SELLER, "sl:send:")
    listing = await h.repo.get_listing(1)
    assert listing["price"] == 2_500_000 and listing["commission_accepted_price"] == 2_500_000
    assert listing["description"] == "" and listing["screenshot_file_id"] is None
    # main-menu buttons work from anywhere and cancel the wizard
    await h.send(SELLER, "➕ Продать абонемент")
    how = await h.send(SELLER, "ℹ️ Как это работает")
    assert "не партнёр 1Fit" in texts(how)
    assert "Выберите действие" in texts(await h.send(SELLER, "Годовой"))  # wizard was cancelled


async def test_my_listings_edit_withdraw_report(h):
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
    await h.click(SELLER, "sl:send:")  # price unchanged -> acceptance kept
    assert "қайта текширувга" in h.texts_to(SELLER)
    assert (await h.repo.get_listing(1))["status"] == LS.PENDING
    assert "изменено и ждёт повторной модерации" in h.texts_to(ADMIN)

    await h.click(ADMIN, "ad:appr:1:0:")
    await h.click(SELLER, "my:rep:1:0")
    await h.click(SELLER, "my:rep!:1:0")
    assert "сообщил о продаже" in h.texts_to(ADMIN)
    assert (await h.repo.get_listing(1))["status"] == LS.APPROVED  # only admin closes the deal

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
    await h1.click(SELLER, "sl:city:tashkent")
    await h1.send(SELLER, "Йиллик")
    await h1.stop()

    h2 = await Harness(db).start()  # "restart"
    nxt = await h2.send(SELLER, END)  # wizard continues at the price step, in Uzbek
    assert "4/7-қадам" in texts(nxt)
    welcome = await h2.send(SELLER, "/start")
    assert "Тилни танланг" not in texts(welcome) and "Эълон жойлаш — бепул" in texts(welcome)
    await h2.stop()


async def test_bot_ignores_group_chats(h):
    upd = {"message": {"message_id": 5, "date": 0, "chat": {"id": -100, "type": "supergroup"},
                       "from": h._user(SELLER), "text": "/start"}}
    assert await h._feed(upd) == []
