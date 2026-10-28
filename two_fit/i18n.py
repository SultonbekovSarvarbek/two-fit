"""Translations. All texts live in two_fit/locales/<lang>.json — edit them there."""
from __future__ import annotations

import json
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
LANGS = ("ru", "uz")
DEFAULT_LANG = "ru"

# City codes, in the order they are offered to sellers. Names are in the locale files: "city.<code>".
CITIES = (
    "tashkent", "samarkand", "bukhara", "andijan", "namangan", "fergana", "nukus",
    "karshi", "termez", "jizzakh", "gulistan", "navoi", "urgench", "nurafshon",
)


class I18n:
    def __init__(self, directory: Path = LOCALES_DIR):
        self.texts: dict[str, dict[str, str]] = {}
        for lang in LANGS:
            with open(directory / f"{lang}.json", encoding="utf-8") as f:
                self.texts[lang] = json.load(f)

    def t(self, lang: str | None, key: str, /, **kwargs) -> str:
        lang = lang if lang in self.texts else DEFAULT_LANG
        template = self.texts[lang].get(key) or self.texts[DEFAULT_LANG].get(key)
        if template is None:
            raise KeyError(f"missing text key: {key}")
        return template.format(**kwargs) if kwargs else template

    def variants(self, key: str) -> set[str]:
        """The same button text in every language — to recognise reply-keyboard presses."""
        return {self.texts[lang][key] for lang in LANGS if key in self.texts[lang]}

    def city(self, lang: str | None, code: str) -> str:
        try:
            return self.t(lang, f"city.{code}")
        except KeyError:
            return code
