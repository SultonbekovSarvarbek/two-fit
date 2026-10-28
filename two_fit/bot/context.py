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
            "min": fmt_money(PRICE_MIN),
            "max": fmt_money(PRICE_MAX),
        }
        return self.i18n.t(self.lang, key, **{**defaults, **kwargs})

    def commission(self, price: int) -> Commission:
        return calc_commission(price, self.cfg.commission_percent)

    def for_lang(self, lang: str | None, user_id: int = 0) -> "Ctx":
        """Same context, but speaking another user's language (for notifications)."""
        return Ctx(self.repo, self.i18n, self.cfg, {"id": user_id, "lang": lang or "ru"})
