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
    promo_enabled: bool = False  # selling 1Fit promo codes: hidden until it is clear which codes are worth selling
    auto_price: bool = False  # a membership's price is the recommended one, not typed by the seller
    screenshot_required: bool = False  # the seller must attach a screenshot of the membership (on in production)
    # 1Fit price watcher: reads 1Fit's Tashkent prices and tells the admins when they change.
    fit_watch: bool = True
    fit_api_url: str = "https://api-uz.1fit.app/api/landing/v2/subscriptions/?city_id=1"
    fit_watch_times: str = "08:00,11:00,14:00,17:00,20:00"  # Tashkent time
    # The seller pays once to place a listing (Telegram Payments: Click or Payme). Off without a provider token.
    instagram_url: str = "https://www.instagram.com/2fit_uz/"  # sellers are asked to follow it; empty: not asked
    listing_fee: int = 30000  # сум
    payment_token: str = ""   # provider token from @BotFather → Payments
    # Payme directly (Merchant API): Payme calls POST /payme on our server; fiscal receipts need the IKPU.
    # Takes over from the Telegram invoice when the merchant id and the key are set.
    payme_merchant_id: str = ""
    payme_key: str = ""            # the cash desk key (test key in the sandbox); Payme may change it (ChangePassword)
    payme_test: bool = False       # checkout.test.paycom.uz instead of checkout.paycom.uz
    payme_account: str = "order_id"  # the account field set up in the Payme Business cash desk
    payme_ikpu: str = ""           # МХИК / ИКПУ code of the service, for the fiscal receipt
    payme_package_code: str = ""   # the package code tied to that IKPU (tasnif.soliq.uz)
    payme_vat_percent: int = 0     # НДС %, 0 when the merchant does not pay VAT
    # Until Payme works: the seller pays the operator by hand, an admin presses «Payment received».
    # Takes over from Payme and Telegram Payments while it is on.
    fee_manual: bool = False

    @property
    def payme_enabled(self) -> bool:
        return bool(self.payme_merchant_id and self.payme_key)

    @property
    def payments_on(self) -> bool:
        """Some way to take the fee is set up: Payme directly or a Telegram Payments provider."""
        return self.payme_enabled or bool(self.payment_token)

    @property
    def fee_enabled(self) -> bool:
        return self.listing_fee > 0 and (self.payments_on or self.fee_manual)

    @property
    def can_take_fee(self) -> bool:
        """A seller can pay an unpaid listing now: by hand through the operator, Payme or Telegram."""
        return self.fee_manual or self.payments_on

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
        promo_enabled=os.getenv("PROMO_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
        auto_price=os.getenv("AUTO_PRICE", "0").strip().lower() in ("1", "true", "yes", "on"),
        screenshot_required=os.getenv("SCREENSHOT_REQUIRED", "1").strip().lower() not in ("0", "false", "no", "off"),
        fit_watch=os.getenv("FIT_WATCH", "1").strip().lower() not in ("0", "false", "no", "off", ""),
        fit_api_url=os.getenv("FIT_API_URL", "").strip() or Config.fit_api_url,
        fit_watch_times=os.getenv("FIT_WATCH_TIMES", "").strip() or Config.fit_watch_times,
        instagram_url=os.getenv("INSTAGRAM_URL", Config.instagram_url).strip(),
        listing_fee=int(os.getenv("LISTING_FEE", "30000") or 0),
        payment_token=os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip(),
        payme_merchant_id=os.getenv("PAYME_MERCHANT_ID", "").strip(),
        payme_key=os.getenv("PAYME_KEY", "").strip(),
        payme_test=os.getenv("PAYME_TEST", "").strip().lower() in ("1", "true", "yes", "on"),
        payme_account=os.getenv("PAYME_ACCOUNT", "").strip() or Config.payme_account,
        payme_ikpu=os.getenv("PAYME_IKPU", "").strip(),
        payme_package_code=os.getenv("PAYME_PACKAGE_CODE", "").strip(),
        payme_vat_percent=int(os.getenv("PAYME_VAT_PERCENT", "0") or 0),
        fee_manual=os.getenv("FEE_MANUAL", "").strip().lower() in ("1", "true", "yes", "on"),
    )
