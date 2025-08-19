# 📂 backend/app/config.py — отвечает за конфигурацию и переменные окружения
# -----------------------------------------------------------------------------
# Что делает файл:
#   - Загружает конфигурацию из .env/переменных окружения.
#   - Хранит бизнес-константы (цены, множители, расписания).
#   - Все чувствительные данные здесь НЕ хардкодим — только через .env.
#
# Как связано с другими файлами:
#   - database.py читает DATABASE_URL.
#   - models.py/сервисы используют константы (PANEL_PRICE_EFHC, VIP_MULTIPLIER и пр.).
#   - bot.py, scheduler.py, nft_checker.py, admin_routes.py и т.д. читают эти настройки.
#
# Как менять:
#   - Заполняйте .env в Vercel/Render или локально (см. .env.example).
#   - Все «магические» числа вынесены в настройки: можно безопасно корректировать.
# -----------------------------------------------------------------------------

from pydantic import BaseSettings, AnyHttpUrl, validator
from typing import List, Optional, Dict, Literal
from functools import lru_cache
from pathlib import Path

class Settings(BaseSettings):
    # ---------------------------------------------------------------------
    # БАЗОВОЕ ПРИЛОЖЕНИЕ
    # ---------------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"
    ENV: Literal["local", "dev", "prod"] = "dev"  # Для локальной разработки можно менять на "local"
    API_V1_STR: str = "/api"
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        if isinstance(v, str) and v:
            return [i.strip() for i in v.split(",")]
        return v

    # ---------------------------------------------------------------------
    # БАЗА ДАННЫХ (PostgreSQL/Supabase)
    # ---------------------------------------------------------------------
    DATABASE_URL_LOCAL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc_core"
    DATABASE_URL: str = "postgres://user:pass@host:5432/db?sslmode=require"
    DB_SCHEMA_CORE: str = "efhc_core"
    DB_SCHEMA_REFERRAL: str = "efhc_referrals"
    DB_SCHEMA_ADMIN: str = "efhc_admin"
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"
    DB_SCHEMA_TASKS: str = "efhc_tasks"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # ---------------------------------------------------------------------
    # TELEGRAM / BOT
    # ---------------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: str = "SET_ME_IN_ENV"
    TELEGRAM_BOT_USERNAME: Optional[str] = "EnergySolarGameBot"
    TELEGRAM_ADMIN_ID: int = 362746228
    TELEGRAM_WEBAPP_URL: Optional[str] = None
    TELEGRAM_COMMAND_PREFIX: str = "/"

    BOT_TOKEN: str = "SET_ME_IN_ENV"
    ADMIN_TELEGRAM_ID: int = 362746228
    TELEGRAM_POLLING_INTERVAL: float = 1.0
    WEBHOOK_ENABLED: bool = True
    WEBHOOK_SECRET: Optional[str] = None
    WEBHOOK_BASE_URL: Optional[str] = None

    # ---------------------------------------------------------------------
    # БЕЗОПАСНОСТЬ / ADMIN
    # ---------------------------------------------------------------------
    ADMIN_ENFORCE_NFT: bool = True
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"
    ADMIN_NFT_ITEM_URL: str = (
        "https://getgems.io/collection/EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # ---------------------------------------------------------------------
    # КОШЕЛЬКИ / СЕТИ
    # ---------------------------------------------------------------------
    TON_WALLET_ADDRESS: str = "SET_BOT_TON_WALLET"
    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    GETGEMS_API_BASE: str = "https://tonapi.io"
    TON_API_BASE: str = "https://toncenter.com/api/v2/jsonRPC"
    USDT_API_BASE: str = "https://api.tronscan.org"
    EFHC_TOKEN_ADDRESS: Optional[str] = None

    # ---------------------------------------------------------------------
    # КОНСТАНТЫ ИГРЫ / ЭКОНОМИКА
    # ---------------------------------------------------------------------
    EFHC_DECIMALS: int = 3
    KWH_DECIMALS: int = 3
    ROUND_DECIMALS: int = 3
    KWH_TO_EFHC_RATE: float = 1.0
    EXCHANGE_MIN_KWH: float = 0.001

    PANEL_PRICE_EFHC: float = 100.0
    PANEL_LIFESPAN_DAYS: int = 180
    MAX_ACTIVE_PANELS_PER_USER: int = 1000

    DAILY_GEN_BASE_KWH: float = 0.598
    VIP_MULTIPLIER: float = 1.07
    DAILY_GEN_VIP_KWH: float = 0.64

    LEVELS: List[Dict[str, str]] = [
        {"idx": "1",  "name": "Eco Initiate",      "threshold_kwh": "0"},
        {"idx": "2",  "name": "Hope Bringer",      "threshold_kwh": "100"},
        {"idx": "3",  "name": "Energy Seeker",     "threshold_kwh": "300"},
        {"idx": "4",  "name": "Nature's Voice",    "threshold_kwh": "600"},
        {"idx": "5",  "name": "Earth Ally",        "threshold_kwh": "1000"},
        {"idx": "6",  "name": "Climate Warrior",   "threshold_kwh": "2000"},
        {"idx": "7",  "name": "Green Sentinel",    "threshold_kwh": "3500"},
        {"idx": "8",  "name": "Planet Defender",   "threshold_kwh": "5000"},
        {"idx": "9",  "name": "Eco Champion",      "threshold_kwh": "7500"},
        {"idx": "10", "name": "Planet Saver",      "threshold_kwh": "10000"},
        {"idx": "11", "name": "Green Commander",   "threshold_kwh": "15000"},
        {"idx": "12", "name": "Guardian of Earth", "threshold_kwh": "20000"},
    ]

    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True
    EXCHANGE_RATE_KWH_TO_EFHC: float = 1.0

    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1
    REFERRAL_MILESTONES: Dict[int, float] = {
        10: 1.0,
        50: 5.0,
        100: 10.0,
        500: 50.0,
        1000: 100.0,
        10000: 1000.0,
    }

    SHOP_DEFAULTS: List[Dict[str, str]] = [
        {"id": "efhc_10_ton",   "label": "10 EFHC",   "pay_asset": "TON",  "price": "0.8"},
        {"id": "efhc_100_ton",  "label": "100 EFHC",  "pay_asset": "TON",  "price": "8"},
        {"id": "efhc_1000_ton", "label": "1000 EFHC", "pay_asset": "TON",  "price": "80"},
        {"id": "vip_usdt",      "label": "VIP NFT",   "pay_asset": "USDT", "price": "50"},
    ]

    LOTTERY_ENABLED: bool = True
    LOTTERY_MAX_TICKETS_PER_USER: int = 10
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0
    LOTTERY_DEFAULTS: List[Dict[str, str]] = [
        {"id": "lottery_vip", "title": "NFT VIP", "target_participants": "500", "prize_type": "VIP_NFT"},
        {"id": "lottery_panel", "title": "1 Панель", "target_participants": "200", "prize_type": "PANEL"},
    ]

    TASKS_ENABLED: bool = True
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = 1.0
    TASK_PRICE_USD_DEFAULT: float = 0.3

    SCHEDULE_NFT_CHECK_UTC: str = "00:00"
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"

    RATE_LIMIT_USER_WRITE_PER_MIN: int = 30
    RATE_LIMIT_ADMIN_WRITE_PER_MIN: int = 60

    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]
    DEFAULT_LANG: str = "RU"

    ASSETS_LEVELS_PATH_HINT: str = "frontend/src/assets/levels/level{1..12}.gif"

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    if settings.TELEGRAM_BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] TELEGRAM_BOT_TOKEN не задан. Установите его в переменных окружения.")
    if settings.DATABASE_URL.startswith("postgres://"):
        settings.DATABASE_URL = settings.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    if settings.BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] BOT_TOKEN не задан. Установите BOT_TOKEN в окружении.")
    if settings.ENV == "local":
        artifacts = Path(".local_artifacts")
        artifacts.mkdir(exist_ok=True)
    return settings
