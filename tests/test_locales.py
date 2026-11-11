"""Both languages: same keys, same placeholders, proper Uzbek Cyrillic, every key used in code exists."""
import re
import string
from pathlib import Path

from two_fit.i18n import CITIES, I18n

i18n = I18n()
RU, UZ = i18n.texts["ru"], i18n.texts["uz"]
SRC = Path(__file__).resolve().parent.parent / "two_fit"


def fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def test_same_keys():
    assert set(RU) == set(UZ)


def test_same_placeholders():
    bad = {k for k in RU if fields(RU[k]) != fields(UZ[k])}
    assert not bad, bad


def test_uzbek_is_cyrillic_with_proper_letters():
    # lang.* keys are intentionally bilingual (the very first screen)
    text = " ".join(v for k, v in UZ.items() if not k.startswith("lang."))
    for letter in "ўқғҳ":
        assert letter in text
    # Russian-only letters must not leak into Uzbek texts
    assert not re.search(r"[ыщ]", text)
    # Latin Uzbek spellings (o', g') must not appear
    assert not re.search(r"\b[a-z]+['ʻ’][a-z]", re.sub(r"<[^>]+>", "", text))


def test_menu_labels_from_spec():
    assert RU["menu.catalog"].endswith("Все абонементы")
    assert UZ["menu.catalog"].endswith("Барча абонементлар")
    assert UZ["menu.sell"].endswith("Абонемент сотиш")
    assert UZ["menu.my"].endswith("Менинг эълонларим")
    assert UZ["menu.support"].endswith("Ёрдам")
    assert UZ["menu.how"].endswith("Бу қандай ишлайди")
    assert UZ["menu.lang"].endswith("Тилни ўзгартириш")
    assert UZ["common.operator_btn"].endswith("Оператор билан боғланиш")
    assert "Укажите оператору номер объявления" in RU["catalog.op_hint"]
    assert "Операторга эълон рақамини айтинг" in UZ["catalog.op_hint"]


def test_all_templates_format():
    for lang in ("ru", "uz"):
        for key, tpl in i18n.texts[lang].items():
            tpl.format(**{f: "x" for f in fields(tpl)})


def test_every_key_used_in_code_exists():
    used = set()
    for path in SRC.rglob("*.py"):
        used |= set(re.findall(r"""\.t\(\s*(?:[a-z]+,\s*)?["']([a-z_]+\.[a-z_.!]+)["']""", path.read_text()))
    missing = {k for k in used if k not in RU}
    assert not missing, missing
    # dynamic keys
    for prefix, items in {
        "status.": ["pending", "approved", "rejected", "withdrawn", "removed", "reserved", "sold", "expired"],
        "rstatus.": ["new", "selected", "completed", "cancelled", "closed"],
        "cstatus.": ["pending", "paid"],
        "sell.field.": ["city", "tariff", "date", "price", "desc", "photo", "contacts"],
        "admin.err.": ["not_found", "bad_status", "already", "price_not_confirmed", "stale"],
        "err.": ["bad_tariff", "bad_date_format", "bad_date_past", "bad_date_far", "bad_price", "bad_desc_long",
                 "bad_contacts", "bad_photo", "bad_name", "bad_phone"],
        "city.": list(CITIES),
    }.items():
        for item in items:
            assert prefix + item in RU, prefix + item


def test_no_promises_we_must_not_make():
    for texts in (RU, UZ):
        how = texts["how.text"]
        assert "1Fit" in how
    assert "не партнёр 1Fit" in RU["how.text"] and "не гарантируем" in RU["how.text"]
    assert "ҳамкори эмас" in UZ["how.text"] and "кафолатламаймиз" in UZ["how.text"]
