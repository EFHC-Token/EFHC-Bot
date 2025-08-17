from pydantic import BaseSettings, AnyHttpUrl, validator
from typing import List, Optional, Dict, Literal
from functools import lru_cache
from pathlib import Path

# ВАЖНО:
# 1) Все секреты задаём через переменные окружения .env / Render / Vercel.
# 2) Здесь заданы безопасные значения по умолчанию и константы бизнес-логики.
# 3) Часовой пояс расчётов — UTC. Ежедневные задачи: 00:00 (проверка NFT), 00:30 (начисление кВт).

class Settings(BaseSettings):
    # ---------------------------------------------------------------------
    # БАЗОВОЕ ПРИЛОЖЕНИЕ
    # ---------------------------------------------------------------------
    PROJECT_NAME: str = "EFHC Bot"
    ENV: Literal["local", "dev", "prod"] = "local"
    API_V1_STR: str = "/api"
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    # Для локальной разработки можно разрешить http://localhost:3000 и т.п.
    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors(cls, v):
        if isinstance(v, str) and v:
            return [i.strip() for i in v.split(",")]
        return v

    # ---------------------------------------------------------------------
    # БАЗА ДАННЫХ (PostgreSQL/Supabase)
    # Придуманные названия баз/схем, как вы просили. Создадите их в Supabase.
    # ---------------------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/efhc_core"
    DB_SCHEMA_CORE: str = "efhc_core"          # ядро (users, panels, balances, tx)
    DB_SCHEMA_REFERRAL: str = "efhc_referrals" # реферальная система
    DB_SCHEMA_ADMIN: str = "efhc_admin"        # административные данные и журнал
    DB_SCHEMA_LOTTERY: str = "efhc_lottery"    # розыгрыши
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # ---------------------------------------------------------------------
    # TELEGRAM BOT
    # ---------------------------------------------------------------------
    # НЕ хардкодим токен. Возьмите из @BotFather и задайте TELEGRAM_BOT_TOKEN в окружении.
    TELEGRAM_BOT_TOKEN: str = "SET_ME_IN_ENV"  # пример: 8374691236:AAF... (НЕ хранить в репозитории)
    TELEGRAM_BOT_USERNAME: Optional[str] = "EnergySolarGameBot"
    TELEGRAM_ADMIN_ID: int = 362746228  # ваш Telegram ID
    TELEGRAM_WEBAPP_URL: Optional[str] = None  # URL фронтенда (Vercel) для кнопки WebApp
    TELEGRAM_COMMAND_PREFIX: str = "/"

    # Fast mode long polling (если без webhook)
    TELEGRAM_POLLING_INTERVAL: float = 1.0

    # ---------------------------------------------------------------------
    # БЕЗОПАСНОСТЬ/ДОСТУП
    # ---------------------------------------------------------------------
    # Двухфактор для админки: проверяем и Telegram ID, и владение NFT-админ-ключом.
    ADMIN_ENFORCE_NFT: bool = True
    ADMIN_NFT_COLLECTION_URL: str = "https://getgems.io/efhc-nft"
    # Конкретный NFT для админ-доступа:
    ADMIN_NFT_ITEM_URL: str = (
        "https://getgems.io/collection/EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0/"
        "EQDvvZCMEs5WIOrdO4r-F9NmsyEU5HyVN0uo1yfAqLG3qyLj"
    )

    # ---------------------------------------------------------------------
    # КОШЕЛЬКИ / СЕТИ
    # ---------------------------------------------------------------------
    # Пользователи привязывают кошельки (TON/USDT). EFHC — существующий токен.
    # В боте используем memo/комментарий с Telegram ID для пополнений.
    CHAIN_TON_ENABLED: bool = True
    CHAIN_USDT_ENABLED: bool = True
    CHAIN_EFHC_ENABLED: bool = True

    # Плейсхолдеры API (вы добавите реальные URL/ключи)
    GETGEMS_API_BASE: str = "https://tonapi.io"  # пример, замените на актуальный источник проверки NFT
    TON_API_BASE: str = "https://toncenter.com/api/v2/jsonRPC"
    USDT_API_BASE: str = "https://api.tronscan.org"  # пример, если USDT-TRC20
    EFHC_TOKEN_ADDRESS: Optional[str] = None        # адрес EFHC (если потребуется в проверках)

    # ---------------------------------------------------------------------
    # КОНСТАНТЫ ИГРЫ / ЭКОНОМИКИ
    # ---------------------------------------------------------------------
    # Балансы: EFHC и кВт — отдельные. Конвертация ТОЛЬКО кВт → EFHC (обратной нет).
    EFHC_DECIMALS: int = 3
    KWH_DECIMALS: int = 3
    ROUND_DECIMALS: int = 3

    # Панели
    PANEL_PRICE_EFHC: float = 100.0
    PANEL_LIFESPAN_DAYS: int = 180
    MAX_ACTIVE_PANELS_PER_USER: int = 1000

    # Генерация
    DAILY_GEN_BASE_KWH: float = 0.598
    VIP_MULTIPLIER: float = 1.07
    DAILY_GEN_VIP_KWH: float = 0.64  # фикс, как обсуждали. Используется как «упрощённый» вариант

    # Уровни (12 уровней) — для фронтенда и расчёта прогресса
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

    # Статусы пользователя
    ACTIVE_USER_FLAG_ON_FIRST_PANEL: bool = True

    # Обмен кВт → EFHC (курс 1:1, округление до 3 знаков)
    EXCHANGE_RATE_KWH_TO_EFHC: float = 1.0
    EXCHANGE_MIN_KWH: float = 0.001

    # Рефералы: 0.1 EFHC за каждого активного (купившего панель) реферала + пороговые бонусы
    REFERRAL_DIRECT_BONUS_EFHC: float = 0.1
    REFERRAL_MILESTONES: Dict[int, float] = {
        10: 1.0,
        100: 10.0,
        1000: 100.0,
        3000: 300.0,
        10000: 1000.0,
    }

    # Магазин (управляется из админки — эти дефолтные пакеты можно менять)
    SHOP_PACKAGES: List[Dict[str, str]] = [
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

    # Розыгрыши (лотереи) — включаются/выключаются из админки
    LOTTERY_ENABLED: bool = True
    LOTTERY_MAX_TICKETS_PER_USER: int = 10
    LOTTERY_TICKET_PRICE_EFHC: float = 1.0
    # Типы и дефолтные конфигурации (одновременно можно несколько активных)
    LOTTERY_DEFAULTS: List[Dict[str, str]] = [
        {"id": "lottery_vip", "title": "NFT VIP", "target_participants": "500", "prize_type": "VIP_NFT"},
        {"id": "lottery_panel", "title": "1 Панель", "target_participants": "200", "prize_type": "PANEL"},
    ]

    # График задач (UTC)
    SCHEDULE_NFT_CHECK_UTC: str = "00:00"   # проверка VIP NFT в кошельках
    SCHEDULE_ENERGY_ACCRUAL_UTC: str = "00:30"  # начисление кВт
    # Дополнительно пользователь может жать кнопку «обновить баланс» — мгновенная проверка

    # Лимиты/рейткеп (на всякий случай)
    RATE_LIMIT_USER_WRITE_PER_MIN: int = 30
    RATE_LIMIT_ADMIN_WRITE_PER_MIN: int = 60

    # Мультиязычность (8 языков)
    SUPPORTED_LANGS: List[str] = ["EN", "RU", "UA", "DE", "FR", "ES", "IT", "PL"]
    DEFAULT_LANG: str = "RU"

    # Путь к статике (на фронтенде — 12 gif уровней, логотипы и т.п.)
    # Здесь держим для справки — фронт сам загрузит из public/src/assets
    ASSETS_LEVELS_PATH_HINT: str = "frontend/src/assets/levels/level{1..12}.gif"

    class Config:
        case_sensitive = True
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """
    Глобальный singleton настроек. Используйте get_settings() в любом месте бэкенда:
        settings = get_settings()
    """
    settings = Settings()

    # Безопасные подсказки по конфигу
    if settings.TELEGRAM_BOT_TOKEN == "SET_ME_IN_ENV":
        print("[EFHC][WARN] TELEGRAM_BOT_TOKEN не задан. Установите его в переменных окружения.")

    # Создадим папку для локальных артефактов (например, временных экспортов) при локальной разработке
    if settings.ENV == "local":
        artifacts = Path(".local_artifacts")
        artifacts.mkdir(exist_ok=True)

    return settings
