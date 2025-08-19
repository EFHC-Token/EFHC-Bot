# 📂 backend/app/shop_routes.py — Магазин (товары, покупка EFHC/VIP, конфиг, экспорт, уведомления)
# ------------------------------------------------------------------------------------------------
# Что делает модуль:
#   • Предоставляет API для магазина (Shop):
#       - /api/shop/config — отдает конфигурацию магазина (динамически из БД: shop_items)
#       - /api/shop/buy — создание заказа на покупку: EFHC за TON/USDT, EFHC за EFHC (внутренний баланс), VIP NFT
#       - /api/admin/shop/items — CRUD товаров магазина (админ-панель)
#       - /api/admin/shop/orders — просмотр заказов
#       - /api/admin/shop/orders/export — экспорт заказов (CSV/JSON)
#   • Создаёт таблицы shop_items и shop_orders при старте (idempotent).
#   • Отправляет уведомления администратору в Telegram при создании заказов и их оплате/начислении.
#   • Интегрируется с ton_integration.py через MEMO (comment) в транзакции:
#       - Формат: "tg={telegram_id} code={item.code} order {item_id}"
#       - В ton_integration.py мы уже поддержали parse shop memo + отношение заказа.
#
# Важные правила:
#   • EFHC и кВт — разные балансы.
#   • 1 EFHC = 1 кВт (фиксированный курс) — обратный обмен невозможен.
#   • Покупка панелей — только за EFHC (реализовано в panels_routes.py /purchase).
#   • Купить EFHC можно через Shop (за TON/USDT) или вводом EFHC (через TonAPI watcher).
#   • VIP NFT можно купить:
#       - За EFHC (внутренние 250 EFHC) → статус VIP, NFT не выдаём автоматически (только статус VIP)
#       - За TON/USDT → создается заказ, админ вручную выдает NFT, статус заказа "pending_nft_delivery"
#         (после вручения админом можно отметить статус "completed").
#
# Безопасность и аутентификация:
#   • Для всех пользовательских эндпоинтов — верификация Telegram WebApp initData (см. verify_webapp.py).
#     В рамках данного модуля — заглушка _get_user_from_request() через X-Telegram-ID (пожалуйста, замените
#     на verify_webapp.verify_telegram_auth при подключении фронтенда).
#   • Для административных эндпоинтов — проверка, что X-Telegram-ID == ADMIN_TELEGRAM_ID.
#
# Зависимости:
#   • FastAPI
#   • SQLAlchemy AsyncSession
#   • httpx для Telegram уведомлений
#   • Decimal для денежных/балансовых операций
#   • Настройки из config.py (get_settings)
#
# Интеграция с другими модулями:
#   • ton_integration.py — считывает входящие платежи TON/Jetton и по MEMO определяет заказ и его статус
#   • panels_routes.py — списывает EFHC при покупке панелей
#   • exchange_routes.py — конвертация kWh → EFHC
#   • scheduler.py — начисляет kWh ежедневно
#   • referrals_routes.py, ranks_routes.py — предоставляют реальные данные для рефералов и рейтинга (уже подключены)
# ------------------------------------------------------------------------------------------------

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_session

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
settings = get_settings()

# Админ чат/пользователь — куда отправлять уведомления
ADMIN_TELEGRAM_ID = int(getattr(settings, "ADMIN_TELEGRAM_ID", "0") or "0")
TELEGRAM_BOT_TOKEN = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
TON_WALLET_ADDRESS = (settings.TON_WALLET_ADDRESS or "").strip()
EFHC_DECIMALS = int(getattr(settings, "EFHC_DECIMALS", 3) or 3)

# Глобальные коды для Shop (для совместимости с ton_integration использования "code=")
# Мы позволим хранить всё в DB (shop_items), но коды для EFHC-пакетов будем использовать:
# EFHC_10_TON, EFHC_100_TON, EFHC_1000_TON, VIP_TON, VIP_USDT, VIP_EFHC, EFHC_? _USDT ...
DEFAULT_ITEMS_PRESET = [
    # EFHC пакеты за TON:
    {
        "code": "EFHC_10_TON",
        "title": "10 EFHC за 0.8 TON",
        "description": "Пакет 10 EFHC",
        "price_asset": "TON",
        "price_amount": "0.8",
        "efhc_amount": "10",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    {
        "code": "EFHC_100_TON",
        "title": "100 EFHC за 8 TON",
        "description": "Пакет 100 EFHC",
        "price_asset": "TON",
        "price_amount": "8",
        "efhc_amount": "100",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    {
        "code": "EFHC_1000_TON",
        "title": "1000 EFHC за 80 TON",
        "description": "Пакет 1000 EFHC",
        "price_asset": "TON",
        "price_amount": "80",
        "efhc_amount": "1000",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    # EFHC пакеты за USDT:
    {
        "code": "EFHC_10_USDT",
        "title": "10 EFHC за 3 USDT",
        "description": "Пакет 10 EFHC",
        "price_asset": "USDT",
        "price_amount": "3",
        "efhc_amount": "10",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    {
        "code": "EFHC_100_USDT",
        "title": "100 EFHC за 30 USDT",
        "description": "Пакет 100 EFHC",
        "price_asset": "USDT",
        "price_amount": "30",
        "efhc_amount": "100",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    {
        "code": "EFHC_1000_USDT",
        "title": "1000 EFHC за 300 USDT",
        "description": "Пакет 1000 EFHC",
        "price_asset": "USDT",
        "price_amount": "300",
        "efhc_amount": "1000",
        "is_active": True,
        "is_vip": False,
        "is_internal": False,
    },
    # VIP NFT (несколько опций)
    {
        "code": "VIP_EFHC",
        "title": "VIP NFT за 250 EFHC",
        "description": "Оформление VIP статуса (+7%) через покупку NFT",
        "price_asset": "EFHC",
        "price_amount": "250",
        "efhc_amount": None,  # За EFHC сразу списываем EFHC с внутреннего баланса и ставим VIP
        "is_active": True,
        "is_vip": True,
        "is_internal": True,   # Покупка за EFHC (внутренняя операция)
    },
    {
        "code": "VIP_TON",
        "title": "VIP NFT за 20 TON",
        "description": "Оформление VIP статуса (+7%) через покупку NFT",
        "price_asset": "TON",
        "price_amount": "20",
        "efhc_amount": None,
        "is_active": True,
        "is_vip": True,
        "is_internal": False,
    },
    {
        "code": "VIP_USDT",
        "title": "VIP NFT за 50 USDT",
        "description": "Оформление VIP статуса (+7%) через покупку NFT",
        "price_asset": "USDT",
        "price_amount": "50",
        "efhc_amount": None,
        "is_active": True,
        "is_vip": True,
        "is_internal": False,
    },
]

# Округление для EFHC (три знака) и денег (тоже fixed decimals, где нужно)
DEC3 = Decimal("0.001")

def _d3(x: Decimal) -> Decimal:
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------------
# SQL-схемы: shop_items, shop_orders
# ---------------------------------------------------------------------------
CREATE_SHOP_TABLES_SQL = """
-- Товары магазина: храним код, заголовок, цены, активность, флаги (VIP/внутр.)
CREATE TABLE IF NOT EXISTS efhc_core.shop_items (
    id SERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT NULL,
    price_asset TEXT NOT NULL,      -- "EFHC" | "TON" | "USDT"
    price_amount NUMERIC(30, 3) NOT NULL,
    efhc_amount NUMERIC(30, 3) NULL, -- Для EFHC пакетов — сколько EFHC зачислить
    is_active BOOLEAN DEFAULT TRUE,
    is_vip BOOLEAN DEFAULT FALSE,    -- Это VIP NFT?
    is_internal BOOLEAN DEFAULT FALSE, -- Внутреннее списание EFHC?
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Заказы магазина
CREATE TABLE IF NOT EXISTS efhc_core.shop_orders (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,            -- Telegram ID
    item_id INT NOT NULL REFERENCES efhc_core.shop_items(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'awaiting_payment', -- awaiting_payment|paid|completed|pending_nft_delivery|cancelled
    amount NUMERIC(30, 3) NOT NULL,     -- стоимость (в price_asset)
    efhc_amount NUMERIC(30, 3) NULL,    -- для EFHC пакетов — объем EFHC
    price_asset TEXT NOT NULL,          -- "EFHC" | "TON" | "USDT"
    memo TEXT NULL,                     -- мемо для платежа (tonapi inject)
    external_tx TEXT NULL,              -- ссылка/hash внешней транзакции (опционально)
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""


async def ensure_shop_tables(db: AsyncSession) -> None:
    """
    Создаёт таблицы магазина при необходимости (idempotent). Также заполняет shop_items
    пресетом по умолчанию, если в базе пусто.
    """
    await db.execute(text(CREATE_SHOP_TABLES_SQL))
    await db.commit()

    # Проверим есть ли записи в shop_items
    q = await db.execute(text("SELECT COUNT(*) FROM efhc_core.shop_items"))
    cnt = q.scalar() or 0
    if cnt == 0:
        # Заселим DEFAULT_ITEMS_PRESET
        for it in DEFAULT_ITEMS_PRESET:
            await db.execute(
                text("""
                    INSERT INTO efhc_core.shop_items(
                        code, title, description, price_asset, price_amount,
                        efhc_amount, is_active, is_vip, is_internal
                    )
                    VALUES (:code, :title, :desc, :asset, :amt, :efhc_amt, :active, :vip, :internal)
                    ON CONFLICT (code) DO NOTHING
                """),
                {
                    "code": it["code"],
                    "title": it["title"],
                    "desc": it.get("description"),
                    "asset": it["price_asset"],
                    "amt": it["price_amount"],
                    "efhc_amt": it["efhc_amount"],
                    "active": it["is_active"],
                    "vip": it["is_vip"],
                    "internal": it["is_internal"],
                },
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Pydantic модели (запросы/ответы)
# ---------------------------------------------------------------------------
class ShopItem(BaseModel):
    """
    Полная информация о товаре.
    Используется при отдаче в /api/shop/config и в админке.
    """
    id: int
    code: str
    title: str
    description: Optional[str]
    price_asset: str
    price_amount: Decimal
    efhc_amount: Optional[Decimal] = None
    is_active: bool
    is_vip: bool
    is_internal: bool


class ShopItemCreate(BaseModel):
    """
    Модель для создания/обновления товара через админ-API.
    """
    code: str = Field(..., description="Уникальный код товара, например EFHC_100_TON")
    title: str
    description: Optional[str] = None
    price_asset: str = Field(..., regex=r"^(EFHC|TON|USDT)$")
    price_amount: Decimal
    efhc_amount: Optional[Decimal] = None
    is_active: bool = True
    is_vip: bool = False
    is_internal: bool = False

    @validator("price_amount")
    def price_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Цена должна быть положительной")
        return _d3(v)  # округляем до 3 знаков

    @validator("efhc_amount")
    def efhc_non_negative(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v < 0:
            raise ValueError("EFHC amount не может быть отрицательным")
        return _d3(v) if v is not None else None


class ShopConfigResponse(BaseModel):
    """
    Отдаётся в /api/shop/config — список доступных товаров и техническая информация.
    """
    items: List[ShopItem]
    ton_wallet: Optional[str] = None   # Адрес TON кошелька проекта
    efhc_token_address: Optional[str] = None
    usdt_jetton_address: Optional[str] = None
    memo_hint: str = Field(default="Use memo: tg=<id> code=<item_code> order <item_id>")


class ShopBuyRequest(BaseModel):
    """
    Запрос на покупку товара (с фронтенда).
    Важно: user_id не должен приходить напрямую от клиента — используем verify_webapp.
    Временно поддерживаем X-Telegram-ID хедер, но требуем к замене на initData.
    """
    item_code: str


class ShopBuyResponse(BaseModel):
    """
    Ответ на /api/shop/buy:
      - order_id — созданный заказ
      - instructions — подробная инструкция в зависимости от способа оплаты
    """
    ok: bool
    order_id: Optional[int] = None
    error: Optional[str] = None
    instructions: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Авторизация/утилиты
# ---------------------------------------------------------------------------
async def _get_user_from_request(request: Request) -> int:
    """
    Временная заглушка: получаем Telegram ID из заголовка X-Telegram-ID.
    В продакшене замените на проверку Telegram initData (verify_webapp.py).
    """
    tg_id = request.headers.get("X-Telegram-ID")
    if not tg_id or not tg_id.isdigit():
        raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-Telegram-ID header")
    return int(tg_id)


def _ensure_admin(request: Request) -> None:
    """
    Проверка, что вызвавший — админ. Используем X-Telegram-ID == ADMIN_TELEGRAM_ID.
    """
    tg = request.headers.get("X-Telegram-ID")
    if (not tg) or (not tg.isdigit()) or (int(tg) != ADMIN_TELEGRAM_ID):
        raise HTTPException(status_code=403, detail="Forbidden: admin only")


async def _telegram_notify_admin(text: str) -> None:
    """
    Отправка уведомления администратору в Telegram. Используем BOT_TOKEN и ADMIN_TELEGRAM_ID.
    """
    if not TELEGRAM_BOT_TOKEN or not ADMIN_TELEGRAM_ID:
        return  # уведомления не настроены
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_TELEGRAM_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception:
        # Ошибки не фатальны для API
        pass


# ---------------------------------------------------------------------------
# Инициализация API роутера
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api", tags=["shop"])


# ---------------------------------------------------------------------------
# Пользовательский эндпоинт: /api/shop/config — список товаров, кошелек, memo
# ---------------------------------------------------------------------------
@router.get("/shop/config", response_model=ShopConfigResponse)
async def shop_config(
    request: Request,
    db: AsyncSession = Depends(get_session)
) -> ShopConfigResponse:
    """
    Возвращает конфигурацию магазина:
      - список shop_items (только is_active=true)
      - адрес TON кошелька проекта (TON_WALLET_ADDRESS)
      - memo инструкцию "tg=<id> code=<item_code> order <item_id>" (пример)
    Данные берутся из базы (таблица shop_items). Если пусто — создаётся сет по умолчанию.
    """
    await ensure_shop_tables(db)
    # Выбираем только активные товары
    q = await db.execute(
        text("""
            SELECT id, code, title, description, price_asset, price_amount,
                   efhc_amount, is_active, is_vip, is_internal
              FROM efhc_core.shop_items
             WHERE is_active = TRUE
             ORDER BY id ASC
        """)
    )
    rows = q.fetchall() or []
    items: List[ShopItem] = []
    for r in rows:
        items.append(
            ShopItem(
                id=r[0],
                code=r[1],
                title=r[2],
                description=r[3],
                price_asset=r[4],
                price_amount=Decimal(r[5]),
                efhc_amount=Decimal(r[6]) if r[6] is not None else None,
                is_active=bool(r[7]),
                is_vip=bool(r[8]),
                is_internal=bool(r[9]),
            )
        )
    return ShopConfigResponse(
        items=items,
        ton_wallet=TON_WALLET_ADDRESS or None,
        efhc_token_address=getattr(settings, "EFHC_TOKEN_ADDRESS", None),
        usdt_jetton_address=getattr(settings, "USDT_JETTON_ADDRESS", None),
        memo_hint="Use memo: tg=<id> code=<item_code> order <item_id>"
    )


# ---------------------------------------------------------------------------
# Пользовательский эндпоинт: /api/shop/buy — создание заказа/инструкций
# ---------------------------------------------------------------------------
@router.post("/shop/buy", response_model=ShopBuyResponse)
async def shop_buy(
    request: Request,
    payload: ShopBuyRequest,
    db: AsyncSession = Depends(get_session)
) -> ShopBuyResponse:
    """
    Создаёт заказ на покупку товара из shop_items по item_code.
    Логика:
      - Ищем item по коду.
      - Если price_asset == EFHC и is_internal == True → покупка за внутренний баланс EFHC:
            • Проверяем, что у пользователя хватает EFHC.
            • Списываем EFHC.
            • Если товар is_vip == True → ставим пользователю VIP.
            • Записываем shop_orders со статусом 'completed'.
            • Уведомляем админа.
      - Если price_asset == TON/USDT → создаём заказ со статусом 'awaiting_payment'
            • Генерируем memo: "tg=<user_id> code=<item_code> order <item_id>"
            • Возвращаем инструкции для фронтенда (кошелек/адрес/мемо/сумма).
            • Уведомляем админа.
      - Возвращаем { ok: true, order_id, instructions: {...} }.
    """
    user_id = await _get_user_from_request(request)
    await ensure_shop_tables(db)

    # Найдем товар по коду
    q_item = await db.execute(
        text("""
            SELECT id, code, title, description, price_asset, price_amount,
                   efhc_amount, is_active, is_vip, is_internal
              FROM efhc_core.shop_items
             WHERE code = :c
             LIMIT 1
        """),
        {"c": payload.item_code.strip().upper()}
    )
    row = q_item.fetchone()
    if not row:
        return ShopBuyResponse(ok=False, error="Item not found")
    if not row[7]:
        return ShopBuyResponse(ok=False, error="Item inactive")

    item_id = row[0]
    code = row[1]
    title = row[2]
    price_asset = row[4]
    price_amount = _d3(Decimal(row[5]))
    efhc_amount = Decimal(row[6]) if row[6] is not None else None
    is_vip = bool(row[8])
    is_internal = bool(row[9])

    # Если покупка за EFHC (внутренняя)
    if price_asset == "EFHC" and is_internal:
        # Проверка баланса EFHC у пользователя (для этого ensure user)
        await db.execute(
            text("""
                INSERT INTO efhc_core.users (telegram_id)
                VALUES (:tg)
                ON CONFLICT(telegram_id) DO NOTHING;
            """),
            {"tg": user_id}
        )
        await db.execute(
            text("""
                INSERT INTO efhc_core.balances (telegram_id)
                VALUES (:tg)
                ON CONFLICT(telegram_id) DO NOTHING;
            """),
            {"tg": user_id}
        )
        # Получим баланс EFHC
        q_bal = await db.execute(
            text("""
                SELECT efhc FROM efhc_core.balances
                 WHERE telegram_id = :tg
            """),
            {"tg": user_id}
        )
        efhc_balance = Decimal(q_bal.scalar() or 0)

        if efhc_balance < price_amount:
            return ShopBuyResponse(ok=False, error="Insufficient EFHC balance")

        # Списываем EFHC
        new_balance = _d3(efhc_balance - price_amount)
        await db.execute(
            text("""
                UPDATE efhc_core.balances
                   SET efhc = :new_bal
                 WHERE telegram_id = :tg
            """),
            {"new_bal": str(new_balance), "tg": user_id}
        )

        # Создаем заказ: status='completed', efhc_amount = None
        # Если это VIP — отметим VIP в user_vip
        await db.execute(
            text("""
                INSERT INTO efhc_core.shop_orders (user_id, item_id, status, amount, efhc_amount, price_asset, memo)
                VALUES (:u, :item, 'completed', :amt, :efhc, 'EFHC', :memo)
            """),
            {
                "u": user_id,
                "item": item_id,
                "amt": str(price_amount),
                "efhc": str(efhc_amount) if efhc_amount is not None else None,
                "memo": f"tg={user_id} code={code} order {item_id}"
            }
        )

        if is_vip:
            # Устанавливаем VIP флаг
            await db.execute(
                text("""
                    INSERT INTO efhc_core.user_vip (telegram_id)
                    VALUES (:tg)
                    ON CONFLICT (telegram_id) DO NOTHING
                """),
                {"tg": user_id}
            )

        await db.commit()

        # Уведомление админа
        await _telegram_notify_admin(
            text=(
                f"🛒 <b>Внутренняя покупка за EFHC (completed)</b>\n"
                f"User: <code>{user_id}</code>\n"
                f"Item: <b>{title}</b> (code={code})\n"
                f"Price: {price_amount} EFHC\n"
                f"{'VIP granted' if is_vip else ''}"
            )
        )

        return ShopBuyResponse(
            ok=True,
            order_id=None,  # В данном варианте заказ уже выполнен, возвратим None (или можно вернуть ID)
            instructions={"status": "completed", "message": "Purchase completed with internal EFHC balance"}
        )

    # Иначе — создаём заказ "awaiting_payment" и отдаём реквизиты для TON/USDT
    memo = f"tg={user_id} code={code} order {item_id}"
    await db.execute(
        text("""
            INSERT INTO efhc_core.shop_orders (user_id, item_id, status, amount, efhc_amount, price_asset, memo)
            VALUES (:u, :item, 'awaiting_payment', :amt, :efhc, :asset, :memo)
            RETURNING id
        """),
        {
            "u": user_id,
            "item": item_id,
            "amt": str(price_amount),
            "efhc": str(efhc_amount) if efhc_amount is not None else None,
            "asset": price_asset,
            "memo": memo
        }
    )
    q_newid = await db.execute(text("SELECT LASTVAL()"))
    new_order_id = int(q_newid.scalar() or 0)

    await db.commit()

    # Уведомление админа
    await _telegram_notify_admin(
        text=(
            f"🛒 <b>Новый заказ в магазине</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Item: <b>{title}</b> (code={code})\n"
            f"Price: {price_amount} {price_asset}\n"
            f"Order ID: {new_order_id}\n"
            f"Memo: <code>{memo}</code>\n"
            f"Status: awaiting_payment"
        )
    )

    # Инструкции по оплате для пользователя
    # В случае TON: адрес TON_WALLET_ADDRESS, memo, сумма = price_amount
    # В случае USDT: адрес EFHC_TOKEN_ADDRESS/USDT_JETTON_ADDRESS — вряд ли (обычно другой адрес). Оставим единый адрес TON.
    instr = {
        "pay_asset": price_asset,
        "amount": str(price_amount),
        "address": TON_WALLET_ADDRESS,
        "memo": memo,
        "order_id": new_order_id,
        "note": "After successful blockchain payment, your order will be processed automatically."
    }

    return ShopBuyResponse(ok=True, order_id=new_order_id, instructions=instr)


# ---------------------------------------------------------------------------
# Админ эндпоинты: товары, заказы, экспорт
# ---------------------------------------------------------------------------
@router.get("/admin/shop/items", response_model=List[ShopItem])
async def admin_list_shop_items(
    request: Request,
    db: AsyncSession = Depends(get_session)
) -> List[ShopItem]:
    """
    Админ: список всех товаров (включая неактивные).
    """
    _ensure_admin(request)
    await ensure_shop_tables(db)
    q = await db.execute(
        text("""
            SELECT id, code, title, description, price_asset, price_amount,
                   efhc_amount, is_active, is_vip, is_internal
              FROM efhc_core.shop_items
             ORDER BY id ASC
        """)
    )
    rows = q.fetchall() or []
    res: List[ShopItem] = []
    for r in rows:
        res.append(
            ShopItem(
                id=r[0],
                code=r[1],
                title=r[2],
                description=r[3],
                price_asset=r[4],
                price_amount=Decimal(r[5]),
                efhc_amount=Decimal(r[6]) if r[6] is not None else None,
                is_active=bool(r[7]),
                is_vip=bool(r[8]),
                is_internal=bool(r[9]),
            )
        )
    return res


@router.post("/admin/shop/items", response_model=Dict[str, Any])
async def admin_create_or_update_item(
    request: Request,
    payload: ShopItemCreate,
    db: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """
    Админ: создать или обновить товар по его 'code' (upsert).
    """
    _ensure_admin(request)
    await ensure_shop_tables(db)

    # Upsert по code
    await db.execute(
        text("""
            INSERT INTO efhc_core.shop_items(
                code, title, description, price_asset, price_amount,
                efhc_amount, is_active, is_vip, is_internal
            )
            VALUES (:code, :title, :desc, :asset, :amt, :efhc, :active, :vip, :internal)
            ON CONFLICT (code) DO UPDATE
                SET title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    price_asset = EXCLUDED.price_asset,
                    price_amount = EXCLUDED.price_amount,
                    efhc_amount = EXCLUDED.efhc_amount,
                    is_active = EXCLUDED.is_active,
                    is_vip = EXCLUDED.is_vip,
                    is_internal = EXCLUDED.is_internal,
                    updated_at = now()
        """),
        {
            "code": payload.code.strip().upper(),
            "title": payload.title,
            "desc": payload.description,
            "asset": payload.price_asset.strip().upper(),
            "amt": str(_d3(payload.price_amount)),
            "efhc": str(_d3(payload.efhc_amount)) if payload.efhc_amount is not None else None,
            "active": payload.is_active,
            "vip": payload.is_vip,
            "internal": payload.is_internal,
        }
    )
    await db.commit()
    return {"ok": True}


@router.delete("/admin/shop/items/{code}", response_model=Dict[str, Any])
async def admin_delete_item(
    code: str,
    request: Request,
    db: AsyncSession = Depends(get_session)
) -> Dict[str, Any]:
    """
    Админ: удаление товара по code (можно и флаг is_active=false использовать вместо удаления).
    """
    _ensure_admin(request)
    await db.execute(
        text("DELETE FROM efhc_core.shop_items WHERE code = :c"),
        {"c": code.strip().upper()}
    )
    await db.commit()
    return {"ok": True}


@router.get("/admin/shop/orders", response_model=List[Dict[str, Any]])
async def admin_list_orders(
    request: Request,
    status_filter: Optional[str] = Query(None, description="Фильтр по статусу (awaiting_payment|paid|...)"),
    db: AsyncSession = Depends(get_session)
) -> List[Dict[str, Any]]:
    """
    Админ: список заказов магазин (возможен фильтр по статусу).
    """
    _ensure_admin(request)
    await ensure_shop_tables(db)
    if status_filter:
        q = await db.execute(
            text("""
                SELECT o.id, o.user_id, o.item_id, o.status, o.amount, o.efhc_amount, o.price_asset, o.memo,
                       o.external_tx, o.created_at, o.updated_at,
                       i.code, i.title
                  FROM efhc_core.shop_orders o
                  JOIN efhc_core.shop_items  i ON i.id = o.item_id
                 WHERE o.status = :s
                 ORDER BY o.created_at DESC
            """),
            {"s": status_filter},
        )
    else:
        q = await db.execute(
            text("""
                SELECT o.id, o.user_id, o.item_id, o.status, o.amount, o.efhc_amount, o.price_asset, o.memo,
                       o.external_tx, o.created_at, o.updated_at,
                       i.code, i.title
                  FROM efhc_core.shop_orders o
                  JOIN efhc_core.shop_items  i ON i.id = o.item_id
                 ORDER BY o.created_at DESC
            """)
        )
    rows = q.fetchall() or []
    res: List[Dict[str, Any]] = []
    for r in rows:
        res.append({
            "id": r[0],
            "user_id": r[1],
            "item_id": r[2],
            "status": r[3],
            "amount": float(r[4]),
            "efhc_amount": float(r[5]) if r[5] is not None else None,
            "price_asset": r[6],
            "memo": r[7],
            "external_tx": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
            "updated_at": r[10].isoformat() if r[10] else None,
            "item_code": r[11],
            "item_title": r[12],
        })
    return res


@router.get("/admin/shop/orders/export")
async def admin_export_orders(
    request: Request,
    format: str = Query("csv", regex=r"^(csv|json)$", description="Формат экспорта: csv | json"),
    db: AsyncSession = Depends(get_session)
) -> Response:
    """
    Админ: экспорт заказов (CSV/JSON).
    Поля:
        id, user_id, item_code, item_title, status, price_asset, amount, efhc_amount, memo, external_tx, created_at, updated_at
    """
    _ensure_admin(request)
    await ensure_shop_tables(db)

    q = await db.execute(
        text("""
            SELECT o.id, o.user_id, i.code, i.title, o.status, o.price_asset, o.amount, o.efhc_amount,
                   o.memo, o.external_tx, o.created_at, o.updated_at
              FROM efhc_core.shop_orders o
              JOIN efhc_core.shop_items  i ON i.id = o.item_id
             ORDER BY o.created_at DESC
        """)
    )
    rows = q.fetchall() or []

    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "user_id": r[1],
            "item_code": r[2],
            "item_title": r[3],
            "status": r[4],
            "price_asset": r[5],
            "amount": float(r[6]),
            "efhc_amount": float(r[7]) if r[7] is not None else None,
            "memo": r[8],
            "external_tx": r[9],
            "created_at": r[10].isoformat() if r[10] else None,
            "updated_at": r[11].isoformat() if r[11] else None,
        })

    if format == "json":
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json",
            status_code=200,
        )

    # CSV
    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=[
            "id", "user_id", "item_code", "item_title", "status", "price_asset", "amount",
            "efhc_amount", "memo", "external_tx", "created_at", "updated_at"
        ]
    )
    writer.writeheader()
    for row in data:
        writer.writerow(row)

    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Примечания по интеграции с другими модами (референсы):
# ---------------------------------------------------------------------------
#
# 1) ton_integration.py:
#    - Реализует watcher, который читает события TonAPI.
#    - Если в платеже присутствует memo: "tg=<id> code=<item_code> order <item_id>", то
#      автоматически обновляет статус заказа в shop_orders:
#         • Для EFHC-пакетов → начисляет EFHC, статус 'completed'.
#         • Для VIP_TON/VIP_USDT → статус 'pending_nft_delivery', уведомляет админа.
#
# 2) referrals_routes.py:
#    - отдает реальные данные реф. системы:
#      GET /api/referrals?user_id=...
#      (подключено ранее: подсчитывает total, active, bonus_kwh, уровень, список рефералов)
#
# 3) panels_routes.py:
#    - покупка панелей за EFHC (цена = EFHC 100, лимит 1000 активных).
#    - генерация 0.598 или 0.64 kWh в зависимости от VIP: scheduler.py.
#
# 4) ranks_routes.py:
#    - рейтинг общая генерация (только панели) и рефералы (топ 100).
#
# 5) exchange_routes.py:
#    - обмен kWh → EFHC (1:1).
#
# 6) admin-панель:
#    - читает shop_items и shop_orders (через /admin/shop/... эндпоинты).
#    - может управлять товарами магазина (создавать, редактировать, удалять),
#      а также экспортировать заказы.
#
# 7) Уведомления Telegram администратору:
#    - см. _telegram_notify_admin: отправляется при создании заказа и при внутренней покупке EFHC/VIP.
#
# Всё выше соответствует нашей бизнес-логике:
#   • EFHC и kWh — разные балансы, обмен только в одну сторону.
#   • VIP добавляет +7% к генерации панелей (scheduler.py).
#   • Лимит 1000 панелей, архив через 180 дней.
#   • Покупка VIP NFT: за EFHC (внутренний), за TON/USDT (инвойс).
#   • Поддержка Shop: расширяемый ассортимент (shop_items), управление через админ API.
#
# Вопросы/Расширения:
#   - Нужно ли отметить заявку "withdraw EFHC" как заказ? (Можно отдельной таблицей "withdrawals")
# ------------------------------------------------------------------------------------------------
