# 📂 backend/app/config.py — глобальная конфигурация EFHC Bot
# -----------------------------------------------------------------------------
# Что делает файл:
#   - Загружает конфигурацию из .env / переменных окружения (Vercel, Render, локально).
#   - Хранит ВСЕ бизнес-константы (цены, бонусы, лимиты, расписания, уровни).
#   - Централизует все пути, токены, ключи API и настройки безопасности.
#
# ВАЖНО:
#   - Чувствительные данные (токены, пароли, ключи API) — только в .env.
#   - В коде могут оставаться только "заглушки" по умолчанию.
#   - НИЧЕГО не хардкодим (кроме дефолтов для разработки).
#
# Используется:
#   - database.py → подключение PostgreSQL
#   - bot.py → запуск Telegram-бота (token, webhook)
#   - scheduler.py → cron задачи (энергия, NFT)
#   - ton_integration.py → работа с TonAPI
#   - user_routes.py, admin_routes.py → API
#   - models.py/services.py → экономика, уровни, лимиты
# -----------------------------------------------------------------------------

from pydantic import BaseSettings, AnyHttpUrl, validator
from typing import List, Optional, Dict, Literal
from functools import lru_cache
from pathlib import Path


class Settings(BaseSettings):
    # -----------------------------------------------------------------
    # ОБЩЕЕ ПРИЛОЖЕНИЕ
    # -----------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"
    ENV: Literal["local", "dev", "prod"] = "dev"   # режим работы (local/dev/prod)
    API_V1_STR: str = "/api"
    DEBUG: bool = False

    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []    # CORS (разрешенные источники)

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        # можно задать строкой: "https://site1,https://site2"
        if isinstance(v, str) and v and v != "*":
            return [i.strip() for i in v.split(",")]
        return [] if v in (None, "", "*") else v

    # -----------------------------------------------------------------
    # БАЗА ДАННЫХ (PostgreSQL / Neon / Supabase)
    # -----------------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc_core"
    DATABASE_URL_LOCAL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc_core"

    DB_SCHEMA_CORE: str = "efhc_core"
    DB_SCHEMA_REFERRAL: str = "efhc_referrals"
    DB_SCHEMA_ADMIN: str = "efhc_admin"
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"
    DB_SCHEMA_TASKS: str = "efhc_tasks"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # -----------------------------------------------------------------
    # TELEGRAM / BOT
    # -----------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: str = "SET_ME_IN_ENV"     # ⚠️ задать в .env
    TELEGRAM_BOT_USERNAME: Optional[str] = "EnergySolarGameBot"
    TELEGRAM_WEBAPP_URL: Optional[str] = "https://efhc-bot.vercel.app"
    TELEGRAM_COMMAND_PREFIX: str = "/"
    TELEGRAM_POLLING_INTERVAL: float = 1.0

    ADMIN_TELEGRAM_ID: int = 362746228

    # webhook
    TELEGRAM_WEBHOOK_PATH: str = "/tg/webhook"
    TELEGRAM_WEBHOOK_SECRET: Optional[str] = "EFHC_webhook_9pX3"
    BASE_PUBLIC_URL: Optional[str] = None     # например: https://efhc-api.onrender.com

    # дубликаты для совместимости (старый код мог использовать BOT_TOKEN)
    BOT_TOKEN: str = "SET_ME_IN_ENV"
    ADMIN_TELEGRAM_ID_DUP: int = 362746228

    # -----------------------------------------------------------------
    # БЕЗОПАСНОСТЬ / ADMIN
    # -----------------------------------------------------------------
    ADMIN_ENFORCE_NFT: bool = True
    VIP_NFT_COLLECTION: Optional[str] = "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0"
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"
    ADMIN_NFT_ITEM_URL: str = (
        "https://getgems.io/collection/EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # -----------------------------------------------------------------
    # КОШЕЛЬКИ / СЕТИ
    # -----------------------------------------------------------------
    TON_WALLET_ADDRESS: str = "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW"

    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    GETGEMS_API_BASE: str = "https://tonapi.io"
    TON_API_BASE: str = "https://tonconsole.com"
    USDT_API_BASE: str = "https://apilist.tronscan.org"
    EFHC_TOKEN_ADDRESS: str = "EQDNcpWf9mXgSPDubDCzvuaX-juL4p8MuUwrQC-36sARRBuw"

    NFT_PROVIDER_BASE_URL: str = "https://tonapi.io"
    NFT_PROVIDER_API_KEY: Optional[str] = "AFHDHVSCO2J757YAAAAKXPGXRMOG4LK7323RPWTDSIIPEYK4EC47C45E2SG3KVLBCLNV7II"

    # -----------------------------------------------------------------
    # ЭКОНОМИКА / ИГРА
    # -----------------------------------------------------------------
    EFHC_DECIMALS: int = 3
    KWH_DECIMALS: int = 3
    ROUND_DECIMALS: int = 3
    KWH_TO_EFHC_RATE: float = 1.0
    EXCHANGE_RATE_KWH_TO_EFHC: float = 1.0
    EXCHANGE_MIN_KWH: float = 0.001

    PANEL_PRICE_EFHC: float = 100.0
    PANEL_LIFESPAN_DAYS: int = 180
    MAX_ACTIVE_PANELS_PER_USER: int = 1000

    DAILY_GEN_BASE_KWH: float = 0.598
    VIP_MULTIPLIER: float = 1.07
    DAILY_GEN_VIP_KWH: float = 0.64

    LEVELS: List[Dict[str, str]] = [
        {"idx": "1", "name": "Eco Initiate", "threshold_kwh": "0"},
        {"idx": "2", "name": "Hope Bringer", "threshold_kwh": "100"},
        {"idx": "3", "name": "Energy Seeker", "threshold_kwh": "300"},
        {"idx": "4", "name": "Nature's Voice", "threshold_kwh": "600"},
        {"idx": "5", "name": "Earth Ally", "threshold_kwh": "1000"},
        {"idx": "6", "name": "Climate Warrior", "threshold_kwh": "2000"},
        {"idx": "7", "name": "Green Sentinel", "threshold_kwh": "3500"},
        {"idx": "8", "name": "Planet Defender", "threshold_kwh": "5000"},
        {"idx": "9", "name": "Eco Champion", "threshold_kwh": "7500"},
        {"idx": "10", "name": "Planet Saver", "threshold_kwh": "10000"},
        {"idx": "11", "name": "Green Commander", "threshold_kwh": "15000"},
        {"idx": "12", "name": "Guardian of Earth", "threshold_kwh": "20000"},
    ]

    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True

    # -----------------------------------------------------------------
    # РЕФЕРАЛЫ
    # -----------------------------------------------------------------
    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1
    REFERRAL_MILESTONES: Dict[int, float] = {
        10: 1.0,
        50: 5.0,
        100: 10.0,
        500: 50.0,
        1000: 100.0,
        10000: 1000.0,
    }

    # -----------------------------------------------------------------
    # МАГАЗИН (дефолтные пакеты)
    # -----------------------------------------------------------------
    SHOP_DEFAULTS: List[Dict[str, str]] = [
        {"id": "efhc_10_ton", "label": "10 EFHC", "pay_asset": "TON", "price": "0.8"},
        {"id": "efhc_100_ton", "label": "100 EFHC", "pay_asset": "TON", "price": "8"},
        {"id": "efhc_1000_ton", "label": "1000 EFHC", "pay_asset": "TON", "price": "80"},
        {"id": "efhc_10_usdt", "label": "10 EFHC", "pay_asset": "USDT", "price": "3"},
        {"id": "efhc_100_usdt", "label": "100 EFHC", "pay_asset": "USDT", "price": "30"},
        {"id": "efhc_1000_usdt", "label": "1000 EFHC", "pay_asset": "USDT", "price": "300"},
        {"id": "vip_efhc", "label": "VIP NFT", "pay_asset": "EFHC", "price": "250"},
        {"id": "vip_ton", "label": "VIP NFT", "pay_asset": "TON", "price": "20"},
        {"id": "vip_usdt", "label": "VIP NFT", "pay_asset": "USDT", "price": "50"},
    ]

    # -----------------------------------------------------------------
    # ЛОТЕРЕИ
    # -----------------------------------------------------------------
    LOTTERY_ENABLED: bool = True
    LOTTERY_MAX_TICKETS_PER_USER: int = 10
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0
    LOTTERY_DEFAULTS: List[Dict[str, str]] = [
        {"id": "lottery_vip", "title": "NFT VIP", "target_participants": "500", "prize_type": "VIP_NFT"},
        {"id": "lottery_panel", "title": "1 Панель", "target_participants": "200", "prize_type": "PANEL"},
    ]

    # -----------------------------------------------------------------
    # ЗАДАНИЯ
    # -----------------------------------------------------------------
    TASKS_ENABLED: bool = True
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = 1.0
    TASK_PRICE_USD_DEFAULT: float = 0.3

    # -----------------------------------------------------------------
    # РАСПИСАНИЕ (UTC)
    # -----------------------------------------------------------------
    SCHEDULE_NFT_CHECK_UTC: str = "00:00"
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"

    # -----------------------------------------------------------------
    # ЛИМИТЫ
    # -----------------------------------------------------------------
    RATE_LIMIT_USER_WRITE_PER_MIN: int = 30
    RATE_LIMIT_ADMIN_WRITE_PER_MIN: int = 60

    # -----------------------------------------------------------------
    # ЛОКАЛИЗАЦИЯ
    # -----------------------------------------------------------------
    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]
    DEFAULT_LANG: str = "RU"

    # -----------------------------------------------------------------
    # ПРОЧЕЕ
    # -----------------------------------------------------------------
    ASSETS_LEVELS_PATH_HINT: str = "frontend/src/assets/levels/level{1..12}.gif"

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Глобальный singleton настроек"""
    s = Settings()

    # fix: postgres:// → postgresql+asyncpg://
    if s.DATABASE_URL.startswith("postgres://"):
        s.DATABASE_URL = s.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

    if s.ENV == "local":
        Path(".local_artifacts").mkdir(exist_ok=True)

    # подсказки по секретам
    if s.TELEGRAM_BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] TELEGRAM_BOT_TOKEN не задан!")
    if not s.TON_WALLET_ADDRESS:
        print("[EFHC][WARN] TON_WALLET_ADDRESS не задан!")
    if not s.NFT_PROVIDER_API_KEY:
        print("[EFHC][WARN] NFT_PROVIDER_API_KEY не задан!")

    return s
