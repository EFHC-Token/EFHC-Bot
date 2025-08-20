# 📂 backend/app/shop_routes.py — модуль покупок (Shop) EFHC/VIP/NFT/Панели (ПОЛНАЯ ВЕРСИЯ, FixPack v1)
# -----------------------------------------------------------------------------
# Назначение:
#   • Пользовательский раздел Shop:
#       - Покупка EFHC за TON/USDT (после подтверждения — EFHC → user, списание с Банка).
#       - Покупка VIP (за TON/USDT) — ⚠️ статус VIP НЕ включается напрямую!
#           → После подтверждения создаётся manual-заявка на выдачу VIP NFT. VIP включится
#             ТОЛЬКО после того, как NFT окажется в кошельке пользователя и будет обнаружен
#             в ежедневной проверке (ежедневно в 00:00). Других способов получения VIP нет.
#       - Покупка VIP NFT (за TON/USDT): после подтверждения — создаётся заявка на ручную выдачу NFT.
#       - Покупка панелей за EFHC (+ бонусные EFHC): списание EFHC/bonus_EFHC user → Банк, создание панелей.
#   • Админ-операции по заказам Shop: список, approve/reject/cancel, ручное подтверждение оплаты.
#   • Все движения EFHC — строго через Банк (ID=362746228) и логируются в транзакционных таблицах.
#
# Важные бизнес-правила:
#   • 1 EFHC = 1 kWh (внутренняя единица учёта).
#   • Курсы TON/USDT внутри Shop используются только для оплаты EFHC/VIP/NFT — фактически
#     сумма/актив оплаты приходят из внешнего сервиса/админ-панели. EFHC внутри системы — без курсов.
#   • Покупка EFHC: после подтверждения оплаты TON/USDT → списание EFHC с Банка → начисление пользователю.
#   • Покупка VIP или VIP NFT: после подтверждения оплаты — создаётся manual-заявка на выдачу VIP NFT.
#       ⚠️ Включение/выключение VIP производится ТОЛЬКО ежедневной проверкой кошельков (00:00)
#          на наличие NFT коллекции EFHC. Никакого ручного включения VIP нигде нет.
#   • Панели (Panels): покупаются только за EFHC/bonus_EFHC:
#       - bonus_EFHC можно тратить ТОЛЬКО на панели.
#       - При покупке панели bonus_EFHC списываются у пользователя и зачисляются на бонус-счёт Банка EFHC.
#       - Остаток (если не хватает бонусных) списывается EFHC у пользователя и уходит на счёт Банка EFHC.
#       - Ограничение: на одного пользователя одновременно не более 1000 активных панелей.
#       - Срок действия панели — ВСЕГДА 180 дней. Архивация — планировщиком.
#
# Таблицы (DDL здесь же, idempotent):
#   efhc_core.shop_orders:
#       - id BIGSERIAL PK
#       - telegram_id BIGINT
#       - order_type TEXT CHECK IN ('efhc','vip','nft')
#       - efhc_amount NUMERIC(30,3) NULL
#       - pay_asset TEXT
#       - pay_amount NUMERIC(30,3)
#       - ton_address TEXT
#       - status TEXT CHECK IN ('pending','paid','completed','rejected','canceled','failed')
#       - idempotency_key TEXT UNIQUE
#       - tx_hash TEXT
#       - admin_id BIGINT
#       - comment TEXT
#       - created_at, paid_at, completed_at, updated_at TIMESTAMPTZ
#
#   efhc_core.manual_nft_requests:
#       - id BIGSERIAL PK
#       - telegram_id BIGINT
#       - wallet_address TEXT
#       - request_type TEXT DEFAULT 'vip_nft'
#       - order_id BIGINT NULL REFERENCES shop_orders(id)
#       - status TEXT CHECK IN ('open','processed','canceled') DEFAULT 'open'
#       - created_at TIMESTAMPTZ DEFAULT now()
#
#   efhc_core.panels:
#       - telegram_id BIGINT
#       - active BOOL
#       - activated_at TIMESTAMPTZ
#       - (опционально) expires_at TIMESTAMPTZ — если есть в модели
#
# Зависимости:
#   • database.get_session — сессия БД.
#   • config.get_settings — конфигурация (schema, admin ID и др.).
#   • models.User, Balance — ORM-модели.
#   • efhc_transactions: BANK_TELEGRAM_ID, credit_user_from_bank, debit_user_to_bank.
#
# Интеграция и UI:
#   • Frontend (React+Tailwind) отправляет заказы (Shop).
#   • Оплата TON/USDT — снаружи (клиент/провайдер). Подтверждение — webhook/админ.
#   • Панели — покупка списывает EFHC/bonus_EFHC, создаёт записи панелей.
#
# Важно:
#   • EFHC — через Банк, всё логируется.
#   • Округление -> три знака вниз (ROUND_DOWN).
#   • VIP = 1.07 применяется планировщиком в 00:30, только при подтверждённом NFT (00:00).
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Path
from pydantic import BaseModel, Field, condecimal
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .config import get_settings
from .models import User, Balance
from .efhc_transactions import (
    BANK_TELEGRAM_ID,
    credit_user_from_bank,   # банк -> user EFHC
    debit_user_to_bank,      # user -> банк EFHC
)

# -----------------------------------------------------------------------------
# Инициализация и логгер
# -----------------------------------------------------------------------------
router = APIRouter()
settings = get_settings()

logger = logging.getLogger("efhc.shop")
if not logger.handlers:
    import sys
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

# -----------------------------------------------------------------------------
# Константы/утилиты
# -----------------------------------------------------------------------------
DEC3 = Decimal("0.001")
VIP_MULTIPLIER = Decimal("1.07")  # VIP/NFT бонус = +7%
PANEL_PRICE_EFHC = Decimal(getattr(settings, "PANEL_PRICE_EFHC", "100.000"))  # 100 EFHC по умолчанию
PANELS_PER_USER_LIMIT = int(getattr(settings, "PANELS_PER_USER_LIMIT", 1000))  # лимит активных панелей на пользователя
PANEL_LIFETIME_DAYS = int(getattr(settings, "PANEL_LIFETIME_DAYS", 180))       # фиксировано 180 дней

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# DDL: shop_orders / manual_nft_requests (idempotent)
# -----------------------------------------------------------------------------
SHOP_ORDERS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.shop_orders (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    order_type TEXT NOT NULL CHECK (order_type IN ('efhc','vip','nft')),
    efhc_amount NUMERIC(30,3),
    pay_asset TEXT,
    pay_amount NUMERIC(30,3),
    ton_address TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','paid','completed','rejected','canceled','failed')) DEFAULT 'pending',
    idempotency_key TEXT UNIQUE,
    tx_hash TEXT,
    admin_id BIGINT,
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    paid_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""

MANUAL_NFT_REQUESTS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.manual_nft_requests (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    wallet_address TEXT,
    request_type TEXT NOT NULL DEFAULT 'vip_nft',
    order_id BIGINT REFERENCES {schema}.shop_orders(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('open','processed','canceled')) DEFAULT 'open',
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_shop_tables(db: AsyncSession) -> None:
    """
    Создаёт таблицы shop_orders и manual_nft_requests при необходимости.
    """
    await db.execute(text(SHOP_ORDERS_CREATE_SQL.format(schema=settings.DB_SCHEMA_CORE)))
    await db.execute(text(MANUAL_NFT_REQUESTS_CREATE_SQL.format(schema=settings.DB_SCHEMA_CORE)))
    await db.commit()

# -----------------------------------------------------------------------------
# Авторизация
# -----------------------------------------------------------------------------
async def require_user(x_telegram_id: Optional[str]) -> int:
    """
    Проверяет заголовок X-Telegram-Id, возвращает целочисленный ID пользователя.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    return int(x_telegram_id)

async def require_admin(
    db: AsyncSession,
    x_telegram_id: Optional[str],
) -> int:
    """
    Проверяет админ-права: супер-админ (config.ADMIN_TELEGRAM_ID) или Банк (BANK_TELEGRAM_ID).
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    if settings.ADMIN_TELEGRAM_ID and tg == int(settings.ADMIN_TELEGRAM_ID):
        return tg
    if tg == BANK_TELEGRAM_ID:
        return tg

    raise HTTPException(status_code=403, detail="Недостаточно прав")

# -----------------------------------------------------------------------------
# Pydantic-схемы
# -----------------------------------------------------------------------------
class CreateEFHCOrderRequest(BaseModel):
    """
    Заказ на покупку EFHC за TON/USDT.
    """
    efhc_amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сколько EFHC купить")
    pay_asset: str = Field(..., description="Чем платит: 'TON' или 'USDT'")
    pay_amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сколько заплатил во внешнем активе (для протокола)")
    ton_address: Optional[str] = Field(None, description="Адрес TON пользователя (если нужен)")
    idempotency_key: Optional[str] = Field(None, description="Ключ идемпотентности")
    telegram_id: Optional[int] = Field(None, description="Telegram ID (для сверки)")

class CreateVIPOrderRequest(BaseModel):
    """
    Заказ на покупку VIP (за TON/USDT).
    ⚠️ ВАЖНО: после подтверждения оплаты мы НЕ включаем VIP напрямую.
      Вместо этого создаётся manual-заявка на выдачу VIP NFT.
      VIP включится только после того, как NFT окажется в кошельке пользователя и
      ежедневная проверка (00:00) это подтвердит.
    """
    pay_asset: str = Field(..., description="Чем платит: 'TON' или 'USDT'")
    pay_amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сколько заплатил")
    ton_address: Optional[str] = Field(None, description="TON-адрес пользователя (куда будет выдан NFT)")
    idempotency_key: Optional[str] = Field(None, description="Ключ идемпотентности")
    telegram_id: Optional[int] = Field(None, description="Telegram ID (для сверки)")

class CreateNFTOrderRequest(BaseModel):
    """
    Заказ на покупку VIP NFT (за TON/USDT).
    После подтверждения оплаты создаётся manual заявка на выдачу NFT (request_type='vip_nft').
    """
    pay_asset: str = Field(..., description="Чем платит: 'TON' или 'USDT'")
    pay_amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сколько заплатил")
    ton_address: Optional[str] = Field(None, description="TON-адрес пользователя (куда выдать NFT)")
    idempotency_key: Optional[str] = Field(None, description="Ключ идемпотентности")
    telegram_id: Optional[int] = Field(None, description="Telegram ID (для сверки)")

class ShopOrderItem(BaseModel):
    """
    Элемент заказа в списках.
    """
    id: int
    telegram_id: int
    order_type: str
    efhc_amount: Optional[str]
    pay_asset: Optional[str]
    pay_amount: Optional[str]
    ton_address: Optional[str]
    status: str
    tx_hash: Optional[str]
    admin_id: Optional[int]
    comment: Optional[str]
    created_at: str
    paid_at: Optional[str]
    completed_at: Optional[str]

class WebhookPayNotifyRequest(BaseModel):
    """
    Нотификация внешнего сервиса об оплате:
      • По order_id или по idempotency_key.
      • tx_hash — опционально.
      • asset/amount — для протокола/аналитики (не критично для логики).
    """
    order_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    tx_hash: Optional[str] = Field(None, description="Хэш внешней оплаты (TON/USDT-транзакция)")
    asset: Optional[str] = Field(None, description="Актив оплаты: 'TON' или 'USDT'")
    amount: Optional[condecimal(gt=0, max_digits=30, decimal_places=3)] = None

class AdminOrderAction(BaseModel):
    """
    Действия админа над заказом: approve(=complete), reject, cancel, fail.
    """
    comment: Optional[str] = Field(None, description="Комментарий админа")
    tx_hash: Optional[str] = Field(None, description="Хэш оплаты (если фиксируем вручную)")

class PanelBuyRequest(BaseModel):
    """
    Покупка панелей за EFHC:
      • qty — количество панелей,
      • use_bonus_first — тратить сначала bonus_EFHC (True/False).
    """
    qty: int = Field(..., gt=0, le=100, description="Количество панелей за раз (защита от спама)")
    use_bonus_first: bool = Field(True, description="Использовать bonus_EFHC в приоритете")

# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------
async def _ensure_user_balance(db: AsyncSession, user_id: int) -> Balance:
    """
    Возвращает объект баланса пользователя, создаёт запись при необходимости.
    Также гарантирует существование записи в users (idempotent).
    """
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.users (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": user_id})
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": user_id})
    await db.commit()

    q = await db.execute(select(Balance).where(Balance.telegram_id == user_id))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=500, detail="Не удалось получить баланс пользователя")
    return bal

async def _lock_user_row(db: AsyncSession, user_id: int) -> None:
    """
    Мягкая блокировка (сериализация) операций по одному пользователю.
    Берём его строку баланса на UPDATE — чтобы параллельные покупки панелей не "обгоняли" друг друга.
    """
    await db.execute(
        text(f"SELECT 1 FROM {settings.DB_SCHEMA_CORE}.balances WHERE telegram_id = :tg FOR UPDATE"),
        {"tg": user_id}
    )

async def _count_active_panels_user(db: AsyncSession, user_id: int) -> int:
    """
    Возвращает число активных панелей на одного пользователя.
    Архивные/неактивные не считаются.
    """
    q = await db.execute(
        text(f"SELECT COUNT(*) FROM {settings.DB_SCHEMA_CORE}.panels WHERE telegram_id = :tg AND active = TRUE"),
        {"tg": user_id}
    )
    row = q.first()
    return int(row[0] if row and row[0] is not None else 0)

async def _insert_bonus_transfer_log(db: AsyncSession, from_id: int, to_id: int, amount: Decimal, reason: str) -> None:
    """
    Вносит запись в efhc_transfers_log для наглядности использования бонусных EFHC.
    Если лог-таблицы нет — откатываем только лог, основную покупку не валим.
    """
    try:
        await db.execute(
            text(f"""
                INSERT INTO {settings.DB_SCHEMA_CORE}.efhc_transfers_log
                    (from_id, to_id, amount, reason, ts)
                VALUES (:from_id, :to_id, :amount, :reason, NOW())
            """),
            {
                "from_id": int(from_id),
                "to_id": int(to_id),
                "amount": str(d3(Decimal(amount))),
                "reason": reason,
            }
        )
        await db.commit()
    except Exception:
        await db.rollback()

# -----------------------------------------------------------------------------
# Пользователь: создать заказ на покупку EFHC (за TON/USDT)
# -----------------------------------------------------------------------------
@router.post("/shop/orders/efhc", summary="Создать заказ на покупку EFHC (за TON/USDT)")
async def create_order_efhc(
    payload: CreateEFHCOrderRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Создаёт заказ на покупку EFHC за внешний актив (TON/USDT).
    По факту оплаты (webhook/админ) — EFHC списываются с Банка → начисляются пользователю.
    Idempotency: если idempotency_key уже существует — возвращаем существующий pending-заказ.
    """
    await ensure_shop_tables(db)
    user_id = await require_user(x_telegram_id)

    if payload.telegram_id is not None and int(payload.telegram_id) != user_id:
        raise HTTPException(status_code=400, detail="Telegram ID mismatch")

    pay_asset = (payload.pay_asset or "").upper().strip()
    if pay_asset not in ("TON", "USDT"):
        raise HTTPException(status_code=400, detail="pay_asset должен быть 'TON' или 'USDT'")

    efhc_amt = d3(Decimal(payload.efhc_amount))
    pay_amount = d3(Decimal(payload.pay_amount))

    # Идемпотентность
    if payload.idempotency_key:
        q = await db.execute(
            text(f"""
                SELECT id FROM {settings.DB_SCHEMA_CORE}.shop_orders
                WHERE idempotency_key = :ikey
            """),
            {"ikey": payload.idempotency_key},
        )
        row = q.first()
        if row:
            return {"ok": True, "order_id": int(row[0]), "status": "pending"}

    # Создание заказа
    q2 = await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.shop_orders
            (telegram_id, order_type, efhc_amount, pay_asset, pay_amount, ton_address, status, idempotency_key, created_at)
            VALUES (:tg, 'efhc', :efhc, :asset, :pamt, :addr, 'pending', :ikey, NOW())
            RETURNING id
        """),
        {
            "tg": user_id,
            "efhc": str(efhc_amt),
            "asset": pay_asset,
            "pamt": str(pay_amount),
            "addr": (payload.ton_address or ""),
            "ikey": payload.idempotency_key,
        }
    )
    order_id = int(q2.scalar_one())
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "pending"}

# -----------------------------------------------------------------------------
# Пользователь: создать заказ на покупку VIP (за TON/USDT)
# -----------------------------------------------------------------------------
@router.post("/shop/orders/vip", summary="Создать заказ на покупку VIP (за TON/USDT)")
async def create_order_vip(
    payload: CreateVIPOrderRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Создаёт заказ на покупку VIP.
    ⚠️ ВАЖНО: Мы не включаем VIP напрямую. После подтверждения оплаты админом/webhook —
      в approve создаётся manual заявка на выдачу VIP NFT (request_type='vip_nft').
      Статус VIP включится ТОЛЬКО после ежедневной проверки наличия NFT в кошельке пользователя (00:00).
    """
    await ensure_shop_tables(db)
    user_id = await require_user(x_telegram_id)

    if payload.telegram_id is not None and int(payload.telegram_id) != user_id:
        raise HTTPException(status_code=400, detail="Telegram ID mismatch")

    pay_asset = (payload.pay_asset or "").upper().strip()
    if pay_asset not in ("TON", "USDT"):
        raise HTTPException(status_code=400, detail="pay_asset должен быть 'TON' или 'USDT'")

    _ = d3(Decimal(payload.pay_amount))  # аналитика

    # Идемпотентность
    if payload.idempotency_key:
        q = await db.execute(
            text(f"SELECT id FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE idempotency_key=:ikey"),
            {"ikey": payload.idempotency_key}
        )
        row = q.first()
        if row:
            return {"ok": True, "order_id": int(row[0]), "status": "pending"}

    q2 = await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.shop_orders
            (telegram_id, order_type, efhc_amount, pay_asset, pay_amount, ton_address, status, idempotency_key, created_at)
            VALUES (:tg, 'vip', NULL, :asset, :pamt, :addr, 'pending', :ikey, NOW())
            RETURNING id
        """),
        {
            "tg": user_id,
            "asset": pay_asset,
            "pamt": str(d3(Decimal(payload.pay_amount))),
            "addr": (payload.ton_address or ""),
            "ikey": payload.idempotency_key,
        }
    )
    order_id = int(q2.scalar_one())
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "pending"}

# -----------------------------------------------------------------------------
# Пользователь: создать заказ на покупку VIP NFT (за TON/USDT)
# -----------------------------------------------------------------------------
@router.post("/shop/orders/nft", summary="Создать заказ на покупку VIP NFT (за TON/USDT)")
async def create_order_nft(
    payload: CreateNFTOrderRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Создаёт заказ на покупку VIP NFT.
    После подтверждения оплаты (approve) будет создана manual заявка на выдачу NFT (request_type='vip_nft').
    Стоимость VIP NFT задаётся в магазине/админ-панели (базово: 250 EFHC, 20 TON, 50 TON USDT),
    но сравнение/валидирование стоимости — вне EFHC-бэкенда (на стороне провайдера/админки).
    """
    await ensure_shop_tables(db)
    user_id = await require_user(x_telegram_id)

    if payload.telegram_id is not None and int(payload.telegram_id) != user_id:
        raise HTTPException(status_code=400, detail="Telegram ID mismatch")

    pay_asset = (payload.pay_asset or "").upper().strip()
    if pay_asset not in ("TON", "USDT"):
        raise HTTPException(status_code=400, detail="pay_asset должен быть 'TON' или 'USDT'")

    _ = d3(Decimal(payload.pay_amount))  # аналитика

    # Идемпотентность
    if payload.idempotency_key:
        q = await db.execute(
            text(f"SELECT id FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE idempotency_key=:ikey"),
            {"ikey": payload.idempotency_key}
        )
        row = q.first()
        if row:
            return {"ok": True, "order_id": int(row[0]), "status": "pending"}

    q2 = await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.shop_orders
            (telegram_id, order_type, efhc_amount, pay_asset, pay_amount, ton_address, status, idempotency_key, created_at)
            VALUES (:tg, 'nft', NULL, :asset, :pamt, :addr, 'pending', :ikey, NOW())
            RETURNING id
        """),
        {
            "tg": user_id,
            "asset": pay_asset,
            "pamt": str(d3(Decimal(payload.pay_amount))),
            "addr": (payload.ton_address or ""),
            "ikey": payload.idempotency_key,
        }
    )
    order_id = int(q2.scalar_one())
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "pending"}

# -----------------------------------------------------------------------------
# Пользователь: список своих заказов
# -----------------------------------------------------------------------------
@router.get("/shop/orders", summary="Список заказов текущего пользователя")
async def list_my_shop_orders(
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Возвращает последние N заказов текущего пользователя.
    """
    await ensure_shop_tables(db)
    user_id = await require_user(x_telegram_id)

    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, order_type, efhc_amount, pay_asset, pay_amount, ton_address, status,
                   tx_hash, admin_id, comment, created_at, paid_at, completed_at
            FROM {settings.DB_SCHEMA_CORE}.shop_orders
            WHERE telegram_id=:tg
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"tg": user_id, "lim": limit}
    )
    rows = q.fetchall()
    out: List[ShopOrderItem] = []
    for r in rows:
        out.append(ShopOrderItem(
            id=r[0],
            telegram_id=r[1],
            order_type=r[2],
            efhc_amount=(str(d3(Decimal(r[3]))) if r[3] is not None else None),
            pay_asset=r[4],
            pay_amount=(str(d3(Decimal(r[5]))) if r[5] is not None else None),
            ton_address=r[6],
            status=r[7],
            tx_hash=r[8],
            admin_id=r[9],
            comment=r[10],
            created_at=r[11].isoformat() if r[11] else None,
            paid_at=r[12].isoformat() if r[12] else None,
            completed_at=r[13].isoformat() if r[13] else None,
        ))
    return {"ok": True, "items": [i.dict() for i in out]}

# -----------------------------------------------------------------------------
# WEBHOOK: подтверждение оплаты от внешнего сервиса
# -----------------------------------------------------------------------------
@router.post("/shop/orders/pay/webhook", summary="Webhook подтверждения оплаты заказа")
async def webhook_order_paid(
    payload: WebhookPayNotifyRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Обрабатывает уведомление об оплате заказа от внешнего сервиса.
      • Ищет заказ по order_id или idempotency_key.
      • Ставит статус 'paid', фиксирует tx_hash, paid_at.
      • Не выполняет 'complete' — финализацию делает админ (approve).
    """
    await ensure_shop_tables(db)

    if not payload.order_id and not payload.idempotency_key:
        raise HTTPException(status_code=400, detail="order_id или idempotency_key обязателен")

    if payload.order_id:
        q = await db.execute(
            text(f"SELECT id, status FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE id=:oid"),
            {"oid": payload.order_id}
        )
    else:
        q = await db.execute(
            text(f"SELECT id, status FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE idempotency_key=:ikey"),
            {"ikey": payload.idempotency_key}
        )

    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    oid = int(row[0])
    cur_status = row[1]
    if cur_status not in ("pending", "failed"):
        # Повторный webhook не меняет статус
        return {"ok": True, "order_id": oid, "status": cur_status}

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
            SET status='paid', paid_at=NOW(), tx_hash=:txh, updated_at=NOW()
            WHERE id=:oid
        """),
        {"txh": payload.tx_hash, "oid": oid}
    )
    await db.commit()
    return {"ok": True, "order_id": oid, "status": "paid"}

# -----------------------------------------------------------------------------
# АДМИН: список всех заказов
# -----------------------------------------------------------------------------
@router.get("/admin/shop/orders", summary="Список всех shop-заказов (админ)")
async def admin_list_shop_orders(
    status: Optional[str] = Query(None, regex="^(pending|paid|completed|rejected|canceled|failed)$"),
    order_type: Optional[str] = Query(None, regex="^(efhc|vip|nft)$"),
    user_id: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Админский список заказов с фильтрами.
    """
    await ensure_shop_tables(db)
    _ = await require_admin(db, x_telegram_id)

    where_sql = "WHERE 1=1"
    params: Dict[str, Any] = {"lim": limit}

    if status:
        where_sql += " AND status=:st"
        params["st"] = status
    if order_type:
        where_sql += " AND order_type=:otype"
        params["otype"] = order_type
    if user_id:
        where_sql += " AND telegram_id=:tg"
        params["tg"] = int(user_id)

    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, order_type, efhc_amount, pay_asset, pay_amount, ton_address, status,
                   tx_hash, admin_id, comment, created_at, paid_at, completed_at
            FROM {settings.DB_SCHEMA_CORE}.shop_orders
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        params
    )
    rows = q.fetchall()
    out: List[ShopOrderItem] = []
    for r in rows:
        out.append(ShopOrderItem(
            id=r[0],
            telegram_id=r[1],
            order_type=r[2],
            efhc_amount=(str(d3(Decimal(r[3]))) if r[3] is not None else None),
            pay_asset=r[4],
            pay_amount=(str(d3(Decimal(r[5]))) if r[5] is not None else None),
            ton_address=r[6],
            status=r[7],
            tx_hash=r[8],
            admin_id=r[9],
            comment=r[10],
            created_at=r[11].isoformat() if r[11] else None,
            paid_at=r[12].isoformat() if r[12] else None,
            completed_at=r[13].isoformat() if r[13] else None,
        ))
    return {"ok": True, "items": [i.dict() for i in out]}

# -----------------------------------------------------------------------------
# АДМИН: APPROVE (=COMPLETE) заказа
# -----------------------------------------------------------------------------
@router.post("/admin/shop/orders/{order_id}/approve", summary="Одобрить (complete) заказ (админ)")
async def admin_approve_shop_order(
    order_id: int = Path(..., ge=1),
    payload: AdminOrderAction = AdminOrderAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id")
):
    """
    Одобряет заказ (выполняет конечное действие):
      • order_type='efhc': EFHC списываются с Банка → начисляются пользователю (credit_user_from_bank).
      • order_type='vip': ⚠️ НЕ включаем VIP напрямую. Создаём manual заявку на выдачу VIP NFT.
      • order_type='nft': создаётся manual заявка на выдачу VIP NFT.
    Предполагается, что статус заказа = 'paid' (или 'pending', если админ вручную проводит).
    """
    await ensure_shop_tables(db)
    admin_id = await require_admin(db, x_telegram_id)

    q = await db.execute(
        text(f"""
            SELECT telegram_id, order_type, efhc_amount, status, ton_address
            FROM {settings.DB_SCHEMA_CORE}.shop_orders
            WHERE id=:oid
        """),
        {"oid": order_id}
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    user_id = int(row[0])
    order_type = row[1]
    efhc_amount = d3(Decimal(row[2] or 0))
    status = row[3]
    ton_address = row[4]

    if status not in ("paid", "pending"):
        raise HTTPException(status_code=400, detail=f"Заказ должен быть 'pending' или 'paid', текущий: {status}")

    try:
        if order_type == "efhc":
            if efhc_amount <= 0:
                raise HTTPException(status_code=400, detail="Некорректная сумма EFHC в заказе")
            await credit_user_from_bank(db, user_id=user_id, amount=efhc_amount)

        elif order_type in ("vip", "nft"):
            # Не создаём дубликаты manual заявок: проверим, нет ли уже по этому order_id
            qx = await db.execute(
                text(f"""
                    SELECT id FROM {settings.DB_SCHEMA_CORE}.manual_nft_requests
                    WHERE order_id = :oid
                """),
                {"oid": order_id}
            )
            exists = qx.first()
            if not exists:
                await db.execute(
                    text(f"""
                        INSERT INTO {settings.DB_SCHEMA_CORE}.manual_nft_requests
                            (telegram_id, wallet_address, request_type, order_id, status, created_at)
                        VALUES (:tg, :wa, 'vip_nft', :oid, 'open', NOW())
                    """),
                    {"tg": user_id, "wa": ton_address or "", "oid": order_id}
                )
            # ВАЖНО: VIP НЕ включаем здесь. Он будет включён/выключен ночной проверкой при наличии/отсутствии NFT.

        else:
            raise HTTPException(status_code=400, detail=f"Неизвестный тип заказа: {order_type}")

        # Обновим статус заказа -> completed
        await db.execute(
            text(f"""
                UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
                SET status='completed', completed_at=NOW(), admin_id=:aid, comment=:cmt, tx_hash=COALESCE(tx_hash,:txh),
                    updated_at=NOW()
                WHERE id=:oid
            """),
            {"aid": admin_id, "cmt": (payload.comment or ""), "txh": (payload.tx_hash or None), "oid": order_id}
        )
        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        await db.execute(
            text(f"""
                UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
                SET status='failed', admin_id=:aid, comment=:cmt, updated_at=NOW()
                WHERE id=:oid
            """),
            {"aid": admin_id, "cmt": f"approve failed: {e}", "oid": order_id}
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Approve failed: {e}")

    return {"ok": True, "order_id": order_id, "status": "completed"}

# -----------------------------------------------------------------------------
# АДМИН: cancel/reject/fail
# -----------------------------------------------------------------------------
@router.post("/admin/shop/orders/{order_id}/reject", summary="Отклонить заказ (админ)")
async def admin_reject_shop_order(
    order_id: int = Path(..., ge=1),
    payload: AdminOrderAction = AdminOrderAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id")
):
    """
    Отклоняет заказ.
    Если это EFHC-заказ, EFHC ещё не списывались/начислялись до approve — возвратов EFHC не делаем.
    Возврат TON/USDT (если нужен) — на стороне провайдера.
    """
    await ensure_shop_tables(db)
    admin_id = await require_admin(db, x_telegram_id)

    q = await db.execute(
        text(f"SELECT status FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE id=:oid"),
        {"oid": order_id}
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    st = row[0]
    if st in ("completed", "canceled", "rejected"):
        return {"ok": True, "order_id": order_id, "status": st}

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
            SET status='rejected', admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:oid
        """),
        {"aid": admin_id, "cmt": payload.comment, "oid": order_id}
    )
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "rejected"}

@router.post("/admin/shop/orders/{order_id}/cancel", summary="Отменить заказ (админ)")
async def admin_cancel_shop_order(
    order_id: int = Path(..., ge=1),
    payload: AdminOrderAction = AdminOrderAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id")
):
    """
    Отмена заказа (для 'pending'/'paid').
    Внутренних EFHC-движений не производим.
    """
    await ensure_shop_tables(db)
    admin_id = await require_admin(db, x_telegram_id)

    q = await db.execute(
        text(f"SELECT status FROM {settings.DB_SCHEMA_CORE}.shop_orders WHERE id=:oid"),
        {"oid": order_id}
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    st = row[0]
    if st in ("completed", "canceled", "rejected"):
        return {"ok": True, "order_id": order_id, "status": st}

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
            SET status='canceled', admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:oid
        """),
        {"aid": admin_id, "cmt": payload.comment, "oid": order_id}
    )
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "canceled"}

@router.post("/admin/shop/orders/{order_id}/fail", summary="Пометить заказ failed (админ)")
async def admin_fail_shop_order(
    order_id: int = Path(..., ge=1),
    payload: AdminOrderAction = AdminOrderAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id")
):
    """
    Помечает заказ как failed (например, ошибка на стороне провайдера).
    """
    await ensure_shop_tables(db)
    admin_id = await require_admin(db, x_telegram_id)

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.shop_orders
            SET status='failed', admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:oid
        """),
        {"aid": admin_id, "cmt": payload.comment, "oid": order_id}
    )
    await db.commit()
    return {"ok": True, "order_id": order_id, "status": "failed"}

# -----------------------------------------------------------------------------
# Покупка ПАНЕЛЕЙ за EFHC (+ bonus_EFHC)
# -----------------------------------------------------------------------------
@router.post("/shop/panels/buy", summary="Покупка панелей за EFHC/bonus_EFHC")
async def shop_buy_panels(
    payload: PanelBuyRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id")
):
    """
    Создаёт указанное количество панелей для пользователя:
      • Проверяет лимит активных панелей на пользователя (<= 1000).
      • Считает стоимость: qty * PANEL_PRICE_EFHC.
      • Списывает EFHC и/или bonus_EFHC с пользователя:
          - Если use_bonus_first=True: сначала бонус, затем EFHC.
          - Иначе наоборот: сначала EFHC, затем бонус.
        Бонусные EFHC зачисляются на БАНК (баланс bonus Банка). Обычные EFHC переводятся user → Банк.
      • Создаёт записи панелей: (telegram_id, active=TRUE, activated_at=NOW()).
        Срок жизни панели — 180 дней (архивирование — планировщиком).
      • Логирует факт использования bonus_EFHC в efhc_transfers_log (reason='shop_panel_bonus').
    """
    user_id = await require_user(x_telegram_id)
    await ensure_shop_tables(db)

    qty = int(payload.qty)
    if qty < 1:
        raise HTTPException(status_code=400, detail="Количество панелей должно быть >= 1")

    # Баланс пользователя (и гарантированная запись в balances/users)
    bal = await _ensure_user_balance(db, user_id)

    # Сериализация операций по пользователю (мягкая блокировка)
    await _lock_user_row(db, user_id)

    # Проверка лимита активных панелей на пользователя после блокировки
    active_for_user = await _count_active_panels_user(db, user_id)
    if active_for_user + qty > PANELS_PER_USER_LIMIT:
        allowed = max(0, PANELS_PER_USER_LIMIT - active_for_user)
        raise HTTPException(
            status_code=400,
            detail=f"Лимит активных панелей для пользователя достигнут ({active_for_user}/{PANELS_PER_USER_LIMIT}). Доступно: {allowed}"
        )

    total_cost = d3(PANEL_PRICE_EFHC * Decimal(qty))

    # Текущие остатки
    cur_efhc = d3(Decimal(bal.efhc or 0))
    cur_bonus = d3(Decimal(getattr(bal, "bonus", 0) or 0))  # поле 'bonus' — бонусные EFHC

    # Расклад оплаты
    use_bonus_first = bool(payload.use_bonus_first)
    pay_bonus = Decimal("0.000")
    pay_efhc = Decimal("0.000")

    if use_bonus_first:
        pay_bonus = min(cur_bonus, total_cost)
        rest = d3(total_cost - pay_bonus)
        pay_efhc = rest
    else:
        pay_efhc = min(cur_efhc, total_cost)
        rest = d3(total_cost - pay_efhc)
        pay_bonus = rest

    if pay_efhc > cur_efhc or pay_bonus > cur_bonus:
        raise HTTPException(status_code=400, detail="Недостаточно EFHC/bonus_EFHC для покупки панелей")

    # Транзакция: списание и создание панелей
    try:
        # 1) EFHC (обычные) -> Банк
        if pay_efhc > 0:
            await debit_user_to_bank(db, user_id=user_id, amount=d3(pay_efhc))

        # 2) bonus_EFHC: user.bonus -=, bank.bonus +=
        if pay_bonus > 0:
            # уменьшаем бонус у пользователя
            await db.execute(
                text(f"""
                    UPDATE {settings.DB_SCHEMA_CORE}.balances
                    SET bonus = (COALESCE(bonus,'0')::numeric - :amt)::text
                    WHERE telegram_id = :tg
                """),
                {"amt": str(d3(pay_bonus)), "tg": user_id}
            )
            # гарантируем запись Банка в balances
            await db.execute(
                text(f"""
                    INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id, bonus)
                    VALUES (:bank, '0')
                    ON CONFLICT (telegram_id) DO NOTHING
                """),
                {"bank": BANK_TELEGRAM_ID}
            )
            # увеличиваем бонус Банка
            await db.execute(
                text(f"""
                    UPDATE {settings.DB_SCHEMA_CORE}.balances
                    SET bonus = (COALESCE(bonus,'0')::numeric + :amt)::text
                    WHERE telegram_id = :bank
                """),
                {"amt": str(d3(pay_bonus)), "bank": BANK_TELEGRAM_ID}
            )
            # логирование бонусного расхода
            await _insert_bonus_transfer_log(db, from_id=user_id, to_id=BANK_TELEGRAM_ID, amount=pay_bonus, reason="shop_panel_bonus")

        # 3) Создание панелей (active=TRUE, activated_at=NOW())
        for _ in range(qty):
            await db.execute(
                text(f"""
                    INSERT INTO {settings.DB_SCHEMA_CORE}.panels (telegram_id, active, activated_at)
                    VALUES (:tg, TRUE, NOW())
                """),
                {"tg": user_id}
            )

        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Покупка панелей не удалась: {e}")

    # Текущие остатки после покупки
    q2 = await db.execute(select(Balance).where(Balance.telegram_id == user_id))
    nb: Optional[Balance] = q2.scalar_one_or_none()
    return {
        "ok": True,
        "panels_bought": qty,
        "total_cost_efhc": str(total_cost),
        "paid_by_efhc": str(d3(pay_efhc)),
        "paid_by_bonus": str(d3(pay_bonus)),
        "limit_per_user": PANELS_PER_USER_LIMIT,
        "balance_after": {
            "efhc": str(d3(Decimal(nb.efhc or 0))) if nb else "0.000",
            "bonus": str(d3(Decimal(getattr(nb, "bonus", 0) or 0))) if nb else "0.000",
            "kwh": str(d3(Decimal(getattr(nb, "kwh", 0) or 0))) if nb else "0.000",
        },
        "note": f"Срок жизни каждой панели — {PANEL_LIFETIME_DAYS} дней. VIP множитель (+7%) применяется только при наличии NFT в кошельке."
    }
