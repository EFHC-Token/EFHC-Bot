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
    # ОБЩЕЕ ПРИЛОЖЕНИЕ
    # ---------------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"
    ENV: Literal["local", "dev", "prod"] = "dev"
    API_V1_STR: str = "/api"
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        if isinstance(v, str) and v:
            return [i.strip() for i in v.split(",")]
        return v

    # ---------------------------------------------------------------------
    # БАЗА ДАННЫХ (Neon / PostgreSQL)
    # ---------------------------------------------------------------------
    # ВНИМАНИЕ: для async SQLAlchemy требуется префикс postgresql+asyncpg://
    # Если вы передаёте обычный postgres:// из Vercel/Neon — мы превратим его в async автоматически.
    DATABASE_URL: str = "postgres://user:pass@host:5432/db?sslmode=require"

    DB_SCHEMA_CORE: str = "efhc_core"
    DB_SCHEMA_REFERRAL: str = "efhc_referrals"
    DB_SCHEMA_ADMIN: str = "efhc_admin"
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"
    DB_SCHEMA_TASKS: str = "efhc_tasks"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # ---------------------------------------------------------------------
    # TELEGRAM
    # ---------------------------------------------------------------------
    BOT_TOKEN: str = "SET_ME_IN_ENV"
    ADMIN_TELEGRAM_ID: int = 362746228
    TELEGRAM_WEBAPP_URL: Optional[str] = None
    TELEGRAM_POLLING_INTERVAL: float = 1.0
    WEBHOOK_ENABLED: bool = True
    WEBHOOK_SECRET: Optional[str] = None          # Для подписи вебхуков (рекомендуется)
    WEBHOOK_BASE_URL: Optional[str] = None        # https://your-domain.tld

    # ---------------------------------------------------------------------
    # ДОСТУП К АДМИНКЕ: NFT + Telegram ID
    # ---------------------------------------------------------------------
    ADMIN_ENFORCE_NFT: bool = True
    # Список NFT, дающих админ-доступ, хранится в БД (таблица admin_nft_whitelist),
    # но дефолтная коллекция для подсказки:
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"
    # Пример конкретного NFT с правами (будет также внесён в БД через админ-панель):
    ADMIN_NFT_ITEM_URL: str = (
        "https://getgems.io/collection/EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # ---------------------------------------------------------------------
    # СЕТИ / ИНТЕГРАЦИИ
    # ---------------------------------------------------------------------
    TON_WALLET_ADDRESS: str = "SET_BOT_TON_WALLET"
    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    # Плейсхолдеры API — замените на актуальные сервисы/ключи
    GETGEMS_API_BASE: str = "https://tonapi.io"           # пример, провайдер для проверки NFT
    TON_API_BASE: str = "https://toncenter.com/api/v2/jsonRPC"
    USDT_API_BASE: str = "https://api.tronscan.org"       # пример, если USDT-TRC20
    EFHC_TOKEN_ADDRESS: Optional[str] = None              # если понадобится

    # ---------------------------------------------------------------------
    # ИГРОВАЯ ЭКОНОМИКА
    # ---------------------------------------------------------------------
    EFHC_DECIMALS: int = 3
    KWH_DECIMALS: int = 3
    ROUND_DECIMALS: int = 3

    # Конвертация: 1 кВт = 1 EFHC (фиксировано идеологией проекта)
    KWH_TO_EFHC_RATE: float = 1.0
    EXCHANGE_MIN_KWH: float = 0.001

    # Панели
    PANEL_PRICE_EFHC: float = 100.0
    PANEL_LIFESPAN_DAYS: int = 180
    MAX_ACTIVE_PANELS_PER_USER: int = 1000
    DAILY_GEN_BASE_KWH: float = 0.598

    # VIP
    VIP_MULTIPLIER: float = 1.07
    DAILY_GEN_VIP_KWH: float = 0.64   # упрощённый вариант (если есть VIP NFT)

    # Рефералка
    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True
    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1
    REFERRAL_MILESTONES: Dict[int, float] = {
        10: 1.0,
        100: 10.0,
        1000: 100.0,
        3000: 300.0,
        10000: 1000.0,
    }

    # Лотереи/Розыгрыши
    LOTTERY_ENABLED: bool = True
    LOTTERY_MAX_TICKETS_PER_USER: int = 10
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0

    # Задания → бонусные EFHC
    TASKS_ENABLED: bool = True
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = 1.0  # по умолчанию 1 bonus EFHC
    TASK_PRICE_USD_DEFAULT: float = 0.3          # стоимость для рекламодателя (информативно)

    # Магазин (дефолт — редактируется из админки)
    SHOP_DEFAULTS: List[Dict[str, str]] = [
        {"id": "efhc_10_ton",   "label": "10 EFHC",   "pay_asset": "TON",  "price": "0.8"},
        {"id": "efhc_100_ton",  "label": "100 EFHC",  "pay_asset": "TON",  "price": "8"},
        {"id": "efhc_1000_ton", "label": "1000 EFHC", "pay_asset": "TON",  "price": "80"},
        {"id": "efhc_10_usdt",  "label": "10 EFHC",   "pay_asset": "USDT", "price": "3"},
        {"id": "efhc_100_usdt", "label": "100 EFHC",  "pay_asset": "USDT", "price": "30"},
        {"id": "efhc_1000_usdt","label": "1000 EFHC", "pay_asset": "USDT", "price": "300"},
        {"id": "vip_efhc",      "label": "VIP NFT",   "pay_asset": "EFHC", "price": "250"},
        {"id": "vip_ton",       "label": "VIP NFT",   "pay_asset": "TON",  "price": "20"},
        {"id": "vip_usdt",      "label": "VIP NFT",   "pay_asset": "USDT", "price": "50"},
    ]

    # Планировщик (UTC)
    SCHEDULE_NFT_CHECK_UTC: str = "00:00"
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"

    # Мультиязычность
    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]
    DEFAULT_LANG: str = "RU"

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"

@lru_cache()
def get_settings() -> Settings:
    settings = Settings()

    # Преобразуем синхронный postgres:// в async postgresql+asyncpg:// на лету, если нужно
    if settings.DATABASE_URL.startswith("postgres://"):
        settings.DATABASE_URL = settings.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

    if settings.BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] BOT_TOKEN не задан. Установите BOT_TOKEN в окружении.")

    if settings.ENV == "local":
        Path(".local_artifacts").mkdir(exist_ok=True)

    return settings
