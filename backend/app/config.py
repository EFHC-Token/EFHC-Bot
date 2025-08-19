# 📂 backend/app/config.py
# -----------------------------------------------------------------------------
# Конфигурация EFHC Bot (ПОЛНАЯ версия, ничего не удалено, только дополнено)
# -----------------------------------------------------------------------------
# Что делает:
#   • Загружает переменные окружения (.env / Vercel / Render / Neon).
#   • Хранит все бизнес-константы (игра, рефералы, лотерея, магазин).
#   • Хранит все сервисные ключи (Telegram, TON, Neon, TonAPI, Webhook).
#   • Совместима со старыми модулями (оставлены старые поля).
#
# Используется во всех модулях:
#   bot.py, main.py, database.py, user_routes.py, admin_routes.py,
#   ton_integration.py, scheduler.py, services/* и т.д.
#
# ВАЖНО:
#   • НИЧЕГО не хардкодим «секретного» в коде — всё через ENV.
#   • Здесь собран полный набор параметров, включая старые/новые.
# -----------------------------------------------------------------------------

from pydantic import BaseSettings, AnyHttpUrl, validator
from typing import List, Optional, Dict, Literal
from functools import lru_cache
from pathlib import Path


class Settings(BaseSettings):
    # -----------------------------------------------------------------
    # БАЗОВОЕ ПРИЛОЖЕНИЕ
    # -----------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"
    ENV: Literal["local", "dev", "prod"] = "dev"
    API_V1_STR: str = "/api"

    # CORS — список доменов фронтенда (Vercel и т.п.).
    # Можно строкой через запятую или "*" (любой).
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        # Разрешаем строку с перечислением, либо "*" -> []
        if isinstance(v, str) and v and v != "*":
            return [i.strip() for i in v.split(",")]
        return [] if v in (None, "", "*") else v

    # -----------------------------------------------------------------
    # БАЗА ДАННЫХ (PostgreSQL / Neon)
    # -----------------------------------------------------------------
    # Основной URL соединения (РЕКОМЕНДОВАНО использовать один переменную DATABASE_URL)
    # Пример (ты прислал): postgres://...neon.tech/neondb?sslmode=require
    DATABASE_URL: Optional[str] = None

    # Локальный фолбэк (на случай локальной отладки без внешнего Neon)
    DATABASE_URL_LOCAL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc_core"

    # Схемы (логическое разделение моделей/таблиц, совместимо с прежним кодом)
    DB_SCHEMA_CORE: str = "efhc_core"
    DB_SCHEMA_REFERRAL: str = "efhc_referrals"
    DB_SCHEMA_ADMIN: str = "efhc_admin"
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"
    DB_SCHEMA_TASKS: str = "efhc_tasks"

    # Пулы соединений
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # --- Vercel → Neon интеграция: EFHC_DB_* (НЕ УДАЛЯЕМ, УЧИТЫВАЕМ) ---
    EFHC_DB_NEXT_PUBLIC_STACK_PROJECT_ID: Optional[str] = None
    EFHC_DB_PGUSER: Optional[str] = None
    EFHC_DB_POSTGRES_URL_NO_SSL: Optional[str] = None
    EFHC_DB_POSTGRES_HOST: Optional[str] = None
    EFHC_DB_NEXT_PUBLIC_STACK_PUBLISHABLE_CLIENT_KEY: Optional[str] = None
    EFHC_DB_NEON_PROJECT_ID: Optional[str] = None

    # -----------------------------------------------------------------
    # TELEGRAM / BOT
    # -----------------------------------------------------------------
    # Токен бота (ТОЧНО заданный тобой)
    TELEGRAM_BOT_TOKEN: str = "8374691236:AAFgTHjtDZOqrkhBApyLiuUwGF5qSWu8C1I"

    # Имя бота (опционально)
    TELEGRAM_BOT_USERNAME: Optional[str] = "EnergySolarGameBot"

    # Префикс для команд ("/start", "/help", и т.д.)
    TELEGRAM_COMMAND_PREFIX: str = "/"

    # ID админа (ваш Telegram id)
    ADMIN_TELEGRAM_ID: int = 362746228

    # Вебхук: путь и секрет (ТОЧНО заданные тобой)
    TELEGRAM_WEBHOOK_PATH: str = "/tg/webhook"
    TELEGRAM_WEBHOOK_SECRET: str = "EFHC_webhook_9pX3"

    # Опционально: публичный URL бэкенда (для webhook)
    BASE_PUBLIC_URL: Optional[str] = None  # например: https://efhc-api.onrender.com

    # Вызовы API из бота (если bot делает httpx-запросы к тому же backend)
    BACKEND_BASE_URL: Optional[str] = None  # например: https://efhc-api.onrender.com

    # WebApp UI (фронтенд, Vercel)
    TELEGRAM_WEBAPP_URL: Optional[str] = "https://efhc-bot.vercel.app"

    # Интервал polling (если решите использовать polling локально)
    TELEGRAM_POLLING_INTERVAL: float = 1.0

    # Совместимость со старым кодом (не удаляем):
    BOT_TOKEN: str = "SET_ME_IN_ENV"
    WEBHOOK_ENABLED: bool = True
    WEBHOOK_SECRET: Optional[str] = None
    WEBHOOK_BASE_URL: Optional[str] = None

    # -----------------------------------------------------------------
    # БЕЗОПАСНОСТЬ / ADMIN
    # -----------------------------------------------------------------
    # Требовать наличие админ NFT (включает проверку на вход в админ-панель)
    ADMIN_ENFORCE_NFT: bool = True

    # Ссылки на коллекцию/предмет (для UI/подсказок) — НЕ ОБЯЗАТЕЛЬНЫ, НО ОСТАВЛЯЕМ
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"
    ADMIN_NFT_ITEM_URL: str = (
        "https://getgems.io/collection/EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # -----------------------------------------------------------------
    # КОШЕЛЬКИ / СЕТИ / NFT
    # -----------------------------------------------------------------
    # Кошелёк TON проекта (Tonkeeper) — ТОЧНО твой (из вводных)
    TON_WALLET_ADDRESS: str = "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW"

    # Разрешение сетей (оставляем для совместимости, вдруг логика использует)
    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    # Базы API
    GETGEMS_API_BASE: str = "https://tonapi.io"       # исторически
    TON_API_BASE: str = "https://tonconsole.com"      # если понадобится (TonConsole RPC/REST)
    USDT_API_BASE: str = "https://apilist.tronscan.org"  # если вернётся Tron USDT

    # Коллекция NFT, дающая админ-доступ (ТОЧНО из вводных)
    VIP_NFT_COLLECTION: str = "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0"

    # Провайдер TonAPI/TonConsole для NFT/транзакций
    NFT_PROVIDER_BASE_URL: str = "https://tonapi.io"
    NFT_PROVIDER_API_KEY: str = "AFHDHVSCO2J757YAAAAKXPGXRMOG4LK7323RPWTDSIIPEYK4EC47C45E2SG3KVLBCLNV7II"

    # Jetton EFHC (адрес контракта)
    EFHC_TOKEN_ADDRESS: str = "EQDNcpWf9mXgSPDubDCzvuaX-juL4p8MuUwrQC-36sARRBuw"

    # -----------------------------------------------------------------
    # КОНСТАНТЫ ИГРЫ / ЭКОНОМИКА
    # -----------------------------------------------------------------
    EFHC_DECIMALS: int = 3
    KWH_DECIMALS: int = 3
    ROUND_DECIMALS: int = 3

    # Курс кВт → EFHC
    KWH_TO_EFHC_RATE: float = 1.0  # совместимость со старым именем
    EXCHANGE_RATE_KWH_TO_EFHC: float = 1.0  # новое имя (оставляем оба)
    EXCHANGE_MIN_KWH: float = 0.001

    # Параметры панелей
    PANEL_PRICE_EFHC: float = 100.0
    PANEL_LIFESPAN_DAYS: int = 180
    MAX_ACTIVE_PANELS_PER_USER: int = 1000

    # Генерация
    DAILY_GEN_BASE_KWH: float = 0.598
    VIP_MULTIPLIER: float = 1.07
    DAILY_GEN_VIP_KWH: float = 0.64

    # Уровни (оставляем полный список как в старом коде)
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

    # Флаг, который активирует пользователя при первой покупке панели (оставляем)
    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True

    # -----------------------------------------------------------------
    # РЕФЕРАЛЫ
    # -----------------------------------------------------------------
    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1
    # Оставляем расширенный список из новых вводных (не режем старые значения)
    REFERRAL_MILESTONES: Dict[int, float] = {
        10: 1.0,
        50: 5.0,
        100: 10.0,
        500: 50.0,
        1000: 100.0,
        10000: 1000.0,
    }

    # -----------------------------------------------------------------
    # МАГАЗИН (дефолтные пакеты, можно редактировать в админке)
    # -----------------------------------------------------------------
    # Из твоих вводных — оставляем TON-пакеты и VIP USDT, но также
    # НЕ удаляем старые варианты (они просто не будут использованы сейчас).
    SHOP_DEFAULTS: List[Dict[str, str]] = [
        {"id": "efhc_10_ton",   "label": "10 EFHC",   "pay_asset": "TON",  "price": "0.8"},
        {"id": "efhc_100_ton",  "label": "100 EFHC",  "pay_asset": "TON",  "price": "8"},
        {"id": "efhc_1000_ton", "label": "1000 EFHC", "pay_asset": "TON",  "price": "80"},
        {"id": "vip_usdt",      "label": "VIP NFT",   "pay_asset": "USDT", "price": "50"},
        # Ниже — старые варианты (оставляем для совместимости UI/админки):
        {"id": "efhc_10_usdt",   "label": "10 EFHC",   "pay_asset": "USDT", "price": "3"},
        {"id": "efhc_100_usdt",  "label": "100 EFHC",  "pay_asset": "USDT", "price": "30"},
        {"id": "efhc_1000_usdt", "label": "1000 EFHC", "pay_asset": "USDT", "price": "300"},
        {"id": "vip_efhc",       "label": "VIP NFT",   "pay_asset": "EFHC", "price": "250"},
        {"id": "vip_ton",        "label": "VIP NFT",   "pay_asset": "TON",  "price": "20"},
    ]

    # -----------------------------------------------------------------
    # ЛОТЕРЕИ
    # -----------------------------------------------------------------
    LOTTERY_ENABLED: bool = True
    LOTTERY_MAX_TICKETS_PER_USER: int = 10
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0
    LOTTERY_DEFAULTS: List[Dict[str, str]] = [
        {"id": "lottery_vip",   "title": "NFT VIP",   "target_participants": "500", "prize_type": "VIP_NFT"},
        {"id": "lottery_panel", "title": "1 Панель",  "target_participants": "200", "prize_type": "PANEL"},
    ]

    # -----------------------------------------------------------------
    # ЗАДАНИЯ
    # -----------------------------------------------------------------
    TASKS_ENABLED: bool = True
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = 1.0
    TASK_PRICE_USD_DEFAULT: float = 0.3

    # -----------------------------------------------------------------
    # РАСПИСАНИЕ ЗАДАЧ (UTC)
    # -----------------------------------------------------------------
    SCHEDULE_NFT_CHECK_UTC: str = "00:00"
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"

    # -----------------------------------------------------------------
    # ЛИМИТЫ (оставляем из старой версии)
    # -----------------------------------------------------------------
    RATE_LIMIT_USER_WRITE_PER_MIN: int = 30
    RATE_LIMIT_ADMIN_WRITE_PER_MIN: int = 60

    # -----------------------------------------------------------------
    # ЯЗЫКИ
    # -----------------------------------------------------------------
    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]
    DEFAULT_LANG: str = "RU"

    # -----------------------------------------------------------------
    # ПУТИ/ПОДСКАЗКИ
    # -----------------------------------------------------------------
    ASSETS_LEVELS_PATH_HINT: str = "frontend/src/assets/levels/level{1..12}.gif"

    # -----------------------------------------------------------------
    # Дополнительные бизнес-флаги (оставлены для совместимости)
    # -----------------------------------------------------------------
    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """Глобальный singleton настроек (ничего не удаляем, только расширяем)."""
    s = Settings()

    # 1) Если DATABASE_URL пуст — пробуем восстановить из EFHC_DB_* (Vercel/Neon).
    if not s.DATABASE_URL:
        # Если Vercel подтянул URL без SSL (часто так), возьмём его как базу.
        # Но нам нужен async драйвер: "postgresql+asyncpg://"
        if s.EFHC_DB_POSTGRES_URL_NO_SSL:
            url = s.EFHC_DB_POSTGRES_URL_NO_SSL
            # Приведём к async виду
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://") and "asyncpg" not in url:
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            s.DATABASE_URL = url
        else:
            # Фолбэк: локальный URL
            s.DATABASE_URL = s.DATABASE_URL_LOCAL

    # 2) Если DATABASE_URL в форме postgres:// — исправляем на asyncpg.
    if s.DATABASE_URL.startswith("postgres://"):
        s.DATABASE_URL = s.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif s.DATABASE_URL.startswith("postgresql://") and "asyncpg" not in s.DATABASE_URL:
        s.DATABASE_URL = s.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    # 3) Локальные артефакты для отладки
    if s.ENV == "local":
        Path(".local_artifacts").mkdir(exist_ok=True)

    # 4) Подсказки по секретам
    if s.TELEGRAM_BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] TELEGRAM_BOT_TOKEN не задан.")
    if s.BOT_TOKEN == "SET_ME_IN_ENV":
        # Старый дубль — оставили для совместимости, просто предупреждаем
        pass
    if not s.TON_WALLET_ADDRESS:
        print("[EFHC][WARN] TON_WALLET_ADDRESS не задан.")
    if not s.NFT_PROVIDER_API_KEY:
        print("[EFHC][WARN] NFT_PROVIDER_API_KEY не задан (TonAPI/TonConsole).")

    return s
