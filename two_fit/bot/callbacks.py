"""Inline-button payloads (Telegram limits callback data to 64 bytes)."""
from aiogram.filters.callback_data import CallbackData

NOOP = "noop"


class LangCb(CallbackData, prefix="lang"):
    code: str


class CatCb(CallbackData, prefix="cat"):
    page: int = 0
    city: str = ""


class CityMenuCb(CallbackData, prefix="cty"):
    city: str = ""


class CardCb(CallbackData, prefix="card"):
    id: int
    page: int = 0
    city: str = ""


class SellCb(CallbackData, prefix="sl"):
    action: str
    value: str = ""


class MyCb(CallbackData, prefix="my"):
    action: str
    id: int = 0
    page: int = 0


class AdmCb(CallbackData, prefix="ad"):
    action: str
    id: int = 0
    page: int = 0
    extra: str = ""


class FitPriceCb(CallbackData, prefix="fp"):
    action: str  # show | check | apply | ignore
    tok: str = ""
