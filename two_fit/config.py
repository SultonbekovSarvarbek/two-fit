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
    commission_percent: int
    page_size: int
    welcome_image: Path
    logo: Path

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
        commission_percent=int(os.getenv("COMMISSION_PERCENT", "10")),
        page_size=int(os.getenv("PAGE_SIZE", "5")),
        welcome_image=BASE_DIR / "assets" / "two-fit-description-640x360.png",
        logo=BASE_DIR / "assets" / "two-fit-logo.png",
    )
