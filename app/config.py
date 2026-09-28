import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
# tests point this at an empty file so nothing from the live .env (wallets, keys) leaks into them
ENV_FILE = os.environ.get("SH_ENV_FILE") or ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    env: str = "dev"  # dev | prod
    base_url: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = ROOT / "data"
    database_url: str = ""
    site_name: str = "SupplierHub"

    # --- nervixy ---
    nervixy_api_key: str = ""
    nervixy_base_url: str = "https://nervixy.digital/api.php"
    nervixy_webhook_secret: str = ""
    nervixy_mock: bool = False
    nervixy_discount_fallback: Decimal = Decimal("4.5")
    nervixy_low_balance_usd: Decimal = Decimal("50")
    nervixy_min_interval: float = 3.0  # seconds between requests to nervixy (they rate-limit strictly)

    # --- pricing ---
    client_discount: Decimal = Decimal("3.75")  # % off Steam nominal for the customer
    volatile_markup: Decimal = Decimal("0.5")  # % added for non-stable coins (conversion + volatility)
    min_order_usd: Decimal = Decimal("0.2")  # same minimum in every currency (converted by nervixy rates)
    max_order_rub: Decimal = Decimal("30000")  # nervixy's own maximum, RUB equivalent
    invoice_ttl_min: int = 30
    max_active_orders: int = 3
    min_deposit_usd: Decimal = Decimal("0.2")
    max_deposit_usd: Decimal = Decimal("10000")

    # --- telegram ---
    bot_token: str = ""
    admin_tg_ids: str = ""  # comma separated telegram user ids
    support_url: str = ""

    # --- built-in fail2ban (app/fail2ban.py) ---
    f2b_enabled: bool = True
    f2b_max_points: int = 10  # points within the window that get an IP banned (scanner hit = 5, bad API key = 2)
    f2b_window_min: int = 10
    f2b_ban_min: int = 60  # first ban; a repeat within a week lasts 24x longer (max 7 days)
    f2b_whitelist: str = ""  # your own IPs/subnets, comma separated: never banned (also skipped by the RDP guard)

    # --- backups: daily, checked, encrypted with this password and sent to the admins in Telegram ---
    backup_password: str = ""
    # --- morning report to the admins (hour, Moscow time; -1 = off) ---
    report_hour_msk: int = 9

    # --- database connection pool (SQLite: one writer at a time; readers in parallel) ---
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- chain data sources (all free/public) ---
    monitor_enabled: bool = True  # false = don't poll blockchains (local UI work with /api/dev/pay)
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_api_key: str = ""
    toncenter_url: str = "https://toncenter.com/api/v3"
    toncenter_api_key: str = ""
    eth_rpc_url: str = "https://ethereum-rpc.publicnode.com"
    bsc_rpc_url: str = "https://bsc-rpc.publicnode.com"
    sol_rpc_url: str = "https://api.mainnet-beta.solana.com"
    btc_api_url: str = "https://mempool.space/api"
    ltc_api_url: str = "https://litecoinspace.org/api"

    # --- receiving wallets (public addresses only; empty = method hidden) ---
    wallet_usdt_trc20: str = ""
    wallet_usdt_bep20: str = ""
    wallet_usdt_erc20: str = ""
    wallet_usdt_ton: str = ""
    wallet_usdt_sol: str = ""
    wallet_btc: str = ""
    wallet_eth: str = ""
    wallet_ltc: str = ""
    wallet_ton: str = ""
    wallet_trx: str = ""
    wallet_sol: str = ""

    @property
    def is_dev(self) -> bool:
        return self.env.lower() != "prod"

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{(self.data_dir / 'supplierhub.sqlite3').as_posix()}"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    def wallet(self, method_code: str) -> str:
        return getattr(self, f"wallet_{method_code}", "").strip()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
