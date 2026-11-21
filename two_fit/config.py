"""Settings from environment variables (.env is loaded automatically)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    support_username: str
    db_path: Path
    commission_percent: int  # for a membership
    page_size: int
    welcome_image: Path
    logo: Path
    webapp_url: str = ""  # public https:// address of the Mini App; empty = Mini App is off
    webapp_host: str = "127.0.0.1"
    webapp_port: int = 8080
    promo_commission_percent: int = 30  # for a promo code
    # 1Fit price watcher: reads 1Fit's Tashkent prices and tells the admins when they change.
    fit_watch: bool = True
    fit_api_url: str = "https://api-uz.1fit.app/api/landing/v2/subscriptions/?city_id=1"
    fit_watch_times: str = "08:00,11:00,14:00,17:00,20:00"  # Tashkent time

    @property
    def webapp_enabled(self) -> bool:
        return self.webapp_url.startswith("https://")

    @property
    def support_url(self) -> str:
        return f"https://t.me/{self.support_username}"

    @property
    def operator(self) -> str:
        return f"@{self.support_username}"


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return frozenset(ids)


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")
    db_path = Path(os.getenv("DB_PATH", "data/two_fit.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
        support_username=os.getenv("SUPPORT_USERNAME", "s_sarvar").strip().lstrip("@"),
        db_path=db_path,
        commission_percent=int(os.getenv("COMMISSION_PERCENT", "15")),
        page_size=int(os.getenv("PAGE_SIZE", "5")),
        welcome_image=BASE_DIR / "assets" / "two-fit-description-640x360.png",
        logo=BASE_DIR / "assets" / "two-fit-logo.png",
        webapp_url=os.getenv("WEBAPP_URL", "").strip(),
        webapp_host=os.getenv("WEBAPP_HOST", "127.0.0.1").strip() or "127.0.0.1",
        webapp_port=int(os.getenv("WEBAPP_PORT", "8080")),
        promo_commission_percent=int(os.getenv("PROMO_COMMISSION_PERCENT", "30")),
        fit_watch=os.getenv("FIT_WATCH", "1").strip().lower() not in ("0", "false", "no", "off", ""),
        fit_api_url=os.getenv("FIT_API_URL", "").strip() or Config.fit_api_url,
        fit_watch_times=os.getenv("FIT_WATCH_TIMES", "").strip() or Config.fit_watch_times,
    )
