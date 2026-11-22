"""Per-update context passed to every handler as ``ctx``."""
from __future__ import annotations

from typing import Any

from ..config import Config
from ..i18n import I18n
from ..money import Commission, calc_commission
from ..repo import Repo
from ..validators import PRICE_MAX, PRICE_MIN
from ..money import fmt_money


class Ctx:
    def __init__(self, repo: Repo, i18n: I18n, cfg: Config, user: dict[str, Any]):
        self.repo = repo
        self.i18n = i18n
        self.cfg = cfg
        self.user = user

    @property
    def user_id(self) -> int:
        return self.user["id"]

    @property
    def lang(self) -> str:
        return self.user.get("lang") or "ru"

    @property
    def is_admin(self) -> bool:
        return self.user_id in self.cfg.admin_ids

    def t(self, key: str, /, **kwargs: Any) -> str:
        defaults = {
            "operator": self.cfg.operator,
            "percent": self.cfg.commission_percent,
            "promo_percent": self.cfg.promo_commission_percent,
            "fees": self.fees(),
            "fee_text": self.fee_text(),
            "min": fmt_money(PRICE_MIN),
            "max": fmt_money(PRICE_MAX),
        }
        return self.i18n.t(self.lang, key, **{**defaults, **kwargs})

    def fee_text(self) -> str:
        """«30 000 сум, один раз» — what placing a listing costs; «бесплатно» while payments are off."""
        if not self.cfg.fee_enabled:
            return self.i18n.t(self.lang, "fee.free")
        return self.i18n.t(self.lang, "fee.paid", fee=fmt_money(self.cfg.listing_fee))

    def fees(self) -> str:
        """«15%» — or «15% for a membership, 30% for a promo code» while promo codes are on sale."""
        key = "fees.both" if self.cfg.promo_enabled else "fees.membership"
        return self.i18n.t(self.lang, key, percent=self.cfg.commission_percent,
                           promo_percent=self.cfg.promo_commission_percent)

    def percent(self, kind: str | None = None) -> int:
        """Today's rate: 15% for a membership, 30% for a promo code."""
        return self.cfg.promo_commission_percent if kind == "promo" else self.cfg.commission_percent

    def commission(self, price: int, kind: str | None = None, percent: int | None = None) -> Commission:
        return calc_commission(price, percent or self.percent(kind))

    def for_lang(self, lang: str | None, user_id: int = 0) -> "Ctx":
        """Same context, but speaking another user's language (for notifications)."""
        return Ctx(self.repo, self.i18n, self.cfg, {"id": user_id, "lang": lang or "ru"})
