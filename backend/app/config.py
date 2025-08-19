# -----------------------------------------------------------------------------
# 📂 backend/app/config.py
# -----------------------------------------------------------------------------
# Конфигурационный файл проекта EFHC Bot.
# Содержит все настройки: база данных (Neon/PostgreSQL), Telegram-бот (aiogram),
# CORS, TON/NFT/Jetton, игровая экономика (панели, генерация, уровни),
# магазин (Shop), рефералы, лотереи, задания, админ-панель и WebApp.
# Все секреты берутся из ENV (.env / Vercel / Render / Neon). Значения по
# умолчанию оставлены для совместимости/локальной отладки.
# -----------------------------------------------------------------------------

from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import AnyHttpUrl, BaseSettings, validator


class Settings(BaseSettings):
    """
    Глобальные настройки EFHC Bot (Pydantic BaseSettings).
    Используются во всех модулях: main.py, bot.py, database.py, user_routes.py,
    admin_routes.py, ton_integration.py, scheduler.py, services/* и фронтенд WebApp.
    """

    # -----------------------------------------------------------------
    # ОБЩЕЕ ПРИЛОЖЕНИЕ
    # -----------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"                     # Имя проекта (UI/логи/OpenAPI)
    ENV: Literal["local", "dev", "prod"] = "dev"       # Текущее окружение
    API_V1_STR: str = "/api"                           # Префикс REST API

    # CORS (источники фронтенда). Поддерживаются legacy-строки и валидные URL:
    FRONTEND_ORIGINS: List[str] = [                    # Источники из старой версии
        "http://localhost:5173",
        "https://yourfrontend.app",
    ]
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []        # Источники в формате URL

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        """
        Позволяет задавать BACKEND_CORS_ORIGINS строкой в ENV (CSV).
        Спец-значение "*" трактуется как пустой список (разрешения настраиваются в приложении).
        """
        if isinstance(v, str) and v and v != "*":
            return [i.strip() for i in v.split(",")]
        return [] if v in (None, "", "*") else v

    # -----------------------------------------------------------------
    # БАЗА ДАННЫХ (PostgreSQL / Neon)
    # -----------------------------------------------------------------
    DATABASE_URL: Optional[str] = None                 # Основной DSN (из ENV)
    DATABASE_URL_LOCAL: str = (                        # Фолбэк для локальной отладки
        "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc"
    )

    # Схемы БД (логическое разделение таблиц):
    DB_SCHEMA_CORE: str = "efhc_core"                  # Базовые сущности/операции
    DB_SCHEMA_ADMIN: str = "efhc_admin"                # Админ-панель/служебные данные
    DB_SCHEMA_REFERRAL: str = "efhc_referrals"         # Реферальная система
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"            # Лотереи/розыгрыши
    DB_SCHEMA_TASKS: str = "efhc_tasks"                # Задания (tasks)

    # Пулы соединений (SQLAlchemy async engine):
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # Переменные для интеграции Vercel → Neon (оставлены для совместимости):
    EFHC_DB_NEXT_PUBLIC_STACK_PROJECT_ID: Optional[str] = None
    EFHC_DB_PGUSER: Optional[str] = None
    EFHC_DB_POSTGRES_URL_NO_SSL: Optional[str] = None  # Часто выдаётся Vercel без SSL
    EFHC_DB_POSTGRES_HOST: Optional[str] = None
    EFHC_DB_NEXT_PUBLIC_STACK_PUBLISHABLE_CLIENT_KEY: Optional[str] = None
    EFHC_DB_NEON_PROJECT_ID: Optional[str] = None

    # -----------------------------------------------------------------
    # TELEGRAM BOT / WEBHOOK (aiogram)
    # -----------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: str = (                        # Токен бота (из ENV в проде)
        "8374691236:AAFgTHjtDZOqrkhBApyLiuUwGF5qSWu8C1I"
    )
    TELEGRAM_BOT_USERNAME: Optional[str] = "EnergySolarGameBot"  # Ник бота
    TELEGRAM_COMMAND_PREFIX: str = "/"                 # Префикс команд

    ADMIN_TELEGRAM_ID: Optional[int] = 362746228       # Главный админ (доступ/уведомления)
    BANK_TELEGRAM_ID: int = 362746228                  # Аккаунт «банк EFHC» (единый счёт)

    TELEGRAM_WEBHOOK_PATH: str = "/tg/webhook"         # Путь вебхука (актуальный)
    TELEGRAM_WEBHOOK_PATH_LEGACY: str = "/telegram/webhook"  # Путь вебхука (легаси)
    TELEGRAM_WEBHOOK_SECRET: Optional[str] = "EFHC_webhook_9pX3"  # Секрет заголовка вебхука

    BASE_PUBLIC_URL: Optional[str] = None              # Публичный URL бэкенда (для setWebhook)
    BACKEND_BASE_URL: Optional[str] = None             # Базовый URL для внутренних вызовов бота
    TELEGRAM_WEBAPP_URL: Optional[str] = "https://efhc-bot.vercel.app"  # URL WebApp (Vercel)
    TELEGRAM_POLLING_INTERVAL: float = 1.0             # Интервал polling (локально)

    # Алиасы для совместимости со старым кодом:
    BOT_TOKEN: str = "SET_ME_IN_ENV"                   # Старое имя токена
    WEBHOOK_ENABLED: bool = True                       # Флаг включения вебхука (legacy)
    WEBHOOK_BASE_URL: Optional[str] = None             # База вебхука (legacy)
    WEBHOOK_SECRET: Optional[str] = None               # Алиас к TELEGRAM_WEBHOOK_SECRET

    # -----------------------------------------------------------------
    # АДМИНКА / ДОСТУП
    # -----------------------------------------------------------------
    ADMIN_ENFORCE_NFT: bool = True                     # Требовать NFT для входа в админку
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"  # Подсказка в UI
    ADMIN_NFT_ITEM_URL: str = (                        # Пример предмета для UI/FAQ
        "https://getgems.io/collection/"
        "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # -----------------------------------------------------------------
    # TON / NFT / JETTONS
    # -----------------------------------------------------------------
    TON_WALLET_ADDRESS: str = (                        # Кошелёк TON проекта (Tonkeeper)
        "UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW"
    )

    GETGEMS_API_BASE: str = "https://tonapi.io"       # Исторический базовый API
    TON_API_BASE: str = "https://tonconsole.com"      # Альтернативный RPC/REST
    USDT_API_BASE: str = "https://apilist.tronscan.org"  # Поддержка USDT (Tron) при необходимости

    VIP_NFT_COLLECTION: str = (                        # Коллекция NFT с VIP/админ доступом
        "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0"
    )
    NFT_PROVIDER_BASE_URL: str = "https://tonapi.io"  # Провайдер NFT/транзакций
    NFT_PROVIDER_API_KEY: Optional[str] = None         # Ключ провайдера (из ENV)

    EFHC_TOKEN_ADDRESS: str = (                        # Jetton EFHC (адрес контракта)
        "EQDNcpWf9mXgSPDubDCzvuaX-juL4p8MuUwrQC-36sARRBuw"
    )

    # -----------------------------------------------------------------
    # ИГРА / ЭКОНОМИКА (панели, генерация, уровни)
    # -----------------------------------------------------------------
    EFHC_DECIMALS: int = 3                             # Точность отображения EFHC
    KWH_DECIMALS: int = 3                              # Точность отображения кВт·ч
    ROUND_DECIMALS: int = 3                            # Округления в UI/отчётах

    KWH_TO_EFHC_RATE: float = 1.0                      # Курс: 1 EFHC = 1 кВт·ч (legacy имя)
    EXCHANGE_RATE_KWH_TO_EFHC: float = 1.0             # Курс: 1 EFHC = 1 кВт·ч (новое имя)
    EXCHANGE_MIN_KWH: float = 0.001                    # Минимум в обменнике

    PANEL_PRICE_EFHC: float = 100.0                    # Цена панели (EFHC)
    PANEL_LIFESPAN_DAYS: int = 180                     # Срок службы панели (дни)
    MAX_ACTIVE_PANELS_PER_USER: int = 1000             # Лимит панелей на пользователя

    DAILY_GEN_BASE_KWH: float = 0.598                  # Базовая суточная генерация
    VIP_MULTIPLIER: float = 1.07                       # VIP бонус (строго +7%)
    DAILY_GEN_VIP_KWH: float = 0.64                    # Ориентир для фронта (≈ 0.598 * 1.07)

    LEVELS: List[Dict[str, str]] = [                   # Уровни прогресса (для рейтинга/доступов)
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

    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True       # Активация пользователя при первой покупке панели

    # -----------------------------------------------------------------
    # РЕФЕРАЛЫ
    # -----------------------------------------------------------------
    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1            # Бонус за прямую рекомендацию (EFHC)
    REFERRAL_MILESTONES: Dict[int, float] = {          # Накопительные бонусы за достижения сети
        10: 1.0,
        50: 5.0,
        100: 10.0,
        500: 50.0,
        1000: 100.0,
        10000: 1000.0,
    }

    # -----------------------------------------------------------------
    # МАГАЗИН (Shop)
    # -----------------------------------------------------------------
    SHOP_DEFAULTS: List[Dict[str, str]] = [            # Дефолтные позиции (можно переопределять в БД)
        {"id": "efhc_10_ton",   "label": "10 EFHC",   "pay_asset": "TON",  "price": "0.8"},
        {"id": "efhc_100_ton",  "label": "100 EFHC",  "pay_asset": "TON",  "price": "8"},
        {"id": "efhc_1000_ton", "label": "1000 EFHC", "pay_asset": "TON",  "price": "80"},
        {"id": "vip_usdt",      "label": "VIP NFT",   "pay_asset": "USDT", "price": "50"},
        # Совместимость/альтернативы:
        {"id": "efhc_10_usdt",   "label": "10 EFHC",   "pay_asset": "USDT", "price": "3"},
        {"id": "efhc_100_usdt",  "label": "100 EFHC",  "pay_asset": "USDT", "price": "30"},
        {"id": "efhc_1000_usdt", "label": "1000 EFHC", "pay_asset": "USDT", "price": "300"},
        {"id": "vip_efhc",       "label": "VIP NFT",   "pay_asset": "EFHC", "price": "250"},
        {"id": "vip_ton",        "label": "VIP NFT",   "pay_asset": "TON",  "price": "20"},
    ]

    # -----------------------------------------------------------------
    # ЛОТЕРЕИ
    # -----------------------------------------------------------------
    LOTTERY_ENABLED: bool = True                       # Включает подсистему лотерей
    LOTTERY_MAX_TICKETS_PER_USER: int = 10             # Лимит билетов на пользователя
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0             # Цена билета в EFHC
    LOTTERY_DEFAULTS: List[Dict[str, str]] = [         # Дефолтные лоты (можно переопределять в БД)
        {"id": "lottery_vip",   "title": "NFT VIP",   "target_participants": "500", "prize_type": "VIP_NFT"},
        {"id": "lottery_panel", "title": "1 Панель",  "target_participants": "200", "prize_type": "PANEL"},
    ]

    # -----------------------------------------------------------------
    # ЗАДАНИЯ (Tasks)
    # -----------------------------------------------------------------
    TASKS_ENABLED: bool = True                         # Включает подсистему заданий
    TASK_REWARD_BONUS_EFHC_DEFAULT: float = 1.0        # Дефолтный бонус за выполнение
    TASK_PRICE_USD_DEFAULT: float = 0.3                # Базовая "стоимость" задания в USD (для расчётов)

    # -----------------------------------------------------------------
    # РАСПИСАНИЕ (UTC)
    # -----------------------------------------------------------------
    SCHEDULE_NFT_CHECK_UTC: str = "00:00"              # Периодическая проверка NFT (cron)
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"         # Начисление генерации энергии (cron)

    # -----------------------------------------------------------------
    # ЛИМИТЫ
    # -----------------------------------------------------------------
    RATE_LIMIT_USER_WRITE_PER_MIN: int = 30            # Анти-спам лимит пользовательских операций
    RATE_LIMIT_ADMIN_WRITE_PER_MIN: int = 60           # Лимит админских операций

    # -----------------------------------------------------------------
    # ЯЗЫК / ЛОКАЛИЗАЦИЯ
    # -----------------------------------------------------------------
    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]  # Поддерживаемые языки
    DEFAULT_LANG: str = "RU"                            # Язык по умолчанию

    # -----------------------------------------------------------------
    # ПОДСКАЗКИ ДЛЯ UI / ПРОЧЕЕ
    # -----------------------------------------------------------------
    ASSETS_LEVELS_PATH_HINT: str = "frontend/src/assets/levels/level{1..12}.gif"  # Путь к ассетам уровней

    class Config:
        """
        Настройки BaseSettings: чтение .env, чувствительность к регистру, кодировка.
        """
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

    # ---------------------------- УТИЛИТЫ -----------------------------
    def effective_cors_origins(self) -> List[str]:
        """
        Возвращает объединённый список CORS-источников из FRONTEND_ORIGINS и BACKEND_CORS_ORIGINS.
        Используется в main.py для настройки CORSMiddleware.
        """
        legacy = list(self.FRONTEND_ORIGINS or [])
        strict = [str(u) for u in (self.BACKEND_CORS_ORIGINS or [])]
        merged: List[str] = []
        for x in [*legacy, *strict]:
            if x and x not in merged:
                merged.append(x)
        return merged

    def export_public_frontend_config(self) -> Dict[str, str]:
        """
        Возвращает безопасный срез настроек для фронтенда (без секретов),
        используется в endpoint-е наподобие GET /api/public/config для WebApp.
        """
        return {
            "projectName": self.PROJECT_NAME,
            "apiPrefix": self.API_V1_STR,
            "webAppUrl": (self.TELEGRAM_WEBAPP_URL or ""),
            "panelPriceEFHC": str(self.PANEL_PRICE_EFHC),
            "panelLifespanDays": str(self.PANEL_LIFESPAN_DAYS),
            "maxPanelsPerUser": str(self.MAX_ACTIVE_PANELS_PER_USER),
            "vipMultiplier": str(self.VIP_MULTIPLIER),
            "exchangeRateKwhToEfhc": str(self.EXCHANGE_RATE_KWH_TO_EFHC),
            "levelsCount": str(len(self.LEVELS)),
            "lotteryEnabled": str(self.LOTTERY_ENABLED),
            "tasksEnabled": str(self.TASKS_ENABLED),
        }


@lru_cache()
def get_settings() -> Settings:
    """
    Возвращает singleton конфигурации с постобработкой:
    - Восстанавливает DATABASE_URL из EFHC_DB_POSTGRES_URL_NO_SSL, если нужно.
    - Приводит DSN к async-драйверу "postgresql+asyncpg://".
    - Создаёт вспомогательные директории в ENV=local.
    - Поддерживает алиас WEBHOOK_SECRET → TELEGRAM_WEBHOOK_SECRET.
    """
    s = Settings()

    # 1) Если DATABASE_URL отсутствует — пробуем URL без SSL из Vercel/Neon.
    if not s.DATABASE_URL:
        if s.EFHC_DB_POSTGRES_URL_NO_SSL:
            url = s.EFHC_DB_POSTGRES_URL_NO_SSL
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif url.startswith("postgresql://") and "asyncpg" not in url:
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            s.DATABASE_URL = url
        else:
            s.DATABASE_URL = s.DATABASE_URL_LOCAL

    # 2) Приведение DSN к asyncpg, если необходимо.
    if s.DATABASE_URL.startswith("postgres://"):
        s.DATABASE_URL = s.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif s.DATABASE_URL.startswith("postgresql://") and "asyncpg" not in s.DATABASE_URL:
        s.DATABASE_URL = s.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    # 3) Локальные артефакты для отладки (ENV=local).
    if s.ENV == "local":
        Path(".local_artifacts").mkdir(exist_ok=True)

    # 4) Поддержка алиаса legacy WEBHOOK_SECRET → TELEGRAM_WEBHOOK_SECRET.
    if not s.TELEGRAM_WEBHOOK_SECRET and s.WEBHOOK_SECRET:
        s.TELEGRAM_WEBHOOK_SECRET = s.WEBHOOK_SECRET

    return s
