# 📂 backend/app/admin_routes.py — админ-модуль EFHC (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Единый админ-модуль (консолидированная, полная версия без регресса), который:
#       - Проверяет права администратора (по Telegram ID и/или через NFT whitelist).
#       - Управляет whitelist'ом NFT (просмотр, добавление, удаление).
#       - Выполняет ручные операции с пользователями:
#           • EFHC и bonus_EFHC — ТОЛЬКО через Банк (ID 362746228).
#           • kWh — напрямую на балансе пользователя (это НЕ EFHC).
#       - Управляет заданиями (CRUD) и награждает bonus_EFHC через Банк.
#       - Управляет лотереями (CRUD).
#       - Просматривает логи TonAPI-интеграции.
#       - Осуществляет эмиссию/сжигание EFHC (mint/burn) через Банк с логированием.
#       - Возвращает баланс Банка EFHC.
#
# Важные бизнес-правила (учитывайте все связанные модули проекта):
#   • Банк EFHC имеет фиксированный ID = 362746228. Это админский счёт и общая «касса».
#   • Любые движения EFHC/bonus_EFHC происходят ТОЛЬКО через Банк:
#       - Начисление EFHC/bonus_EFHC пользователю → списание с Банка.
#       - Списание EFHC/bonus_EFHC у пользователя → зачисление в Банк.
#       - Никаких «EFHC из воздуха» или «в никуда» нет; всё логируется.
#   • bonus_EFHC выдаются за задания И МОГУТ БЫТЬ ИЗРАСХОДОВАНЫ ТОЛЬКО НА ПОКУПКУ ПАНЕЛЕЙ.
#     При покупке панели bonus_EFHC уходят user → Банк. Основные EFHC аналогично.
#   • Обменник (kWh → EFHC = 1:1): EFHC начисляются пользователю из Банка; kWh уменьшаются у пользователя.
#   • Магазин (TON/USDT → покупка EFHC/VIP/NFT/панелей):
#       - Только после подтверждённого внешнего платежа EFHC начисляются пользователю из Банка.
#   • Лотерея:
#       - Билеты оплачиваются EFHC (user → Банк).
#       - Приз EFHC: Банк → user.
#       - Приз panel: панель активируется победителю (EFHC не выдаются).
#       - Приз vip_nft: создаётся заявка на ручную выдачу NFT (EFHC не выдаются).
#
# Технические детали:
#   • Pydantic v1 (fastapi==0.103.2), SQLAlchemy 2.0 (async), PostgreSQL.
#   • Все операции EFHC/bonus_EFHC в других модулях ДОЛЖНЫ использовать efhc_transactions.py.
#     Здесь мы импортируем кредит/дебет и mint/burn из efhc_transactions.
#   • Таблица efhc_core.tasks_bonus_log (idempotent) создаётся здесь (DDL IF NOT EXISTS).
#
# Связи:
#   • database.py — get_session, ensure_schemas, engine, sessionmaker.
#   • config.py — get_settings() (BANK_TELEGRAM_ID, ADMIN_TELEGRAM_ID и пр.).
#   • models.py — ORM-модели (User, Balance, UserVIP, Task, Lottery, TonEventLog, AdminNFTWhitelist).
#   • efhc_transactions.py — централизованные операции EFHC/bonus_EFHC (через Банк), mint/burn, логи.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional, Dict, Any

import httpx
from fastapi import (
    APIRouter, Depends, Header, HTTPException, Query
)
from pydantic import BaseModel, Field, condecimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, text

from .database import get_session
from .config import get_settings
from .models import (
    User,
    Balance,
    UserVIP,
    Task,
    Lottery,
    TonEventLog,
    AdminNFTWhitelist,
)
from .efhc_transactions import (
    BANK_TELEGRAM_ID,             # 362746228 — ID Банка EFHC
    mint_efhc,                    # минт EFHC → Банк
    burn_efhc,                    # бёрн EFHC ← Банк
    credit_user_from_bank,        # начисление EFHC пользователю (Банк → user)
    debit_user_to_bank,           # списание EFHC у пользователя (user → Банк)
    credit_bonus_user_from_bank,  # начисление bonus_EFHC (Банк → user)
    debit_bonus_user_to_bank,     # списание bonus_EFHC (user → Банк)
)

# -----------------------------------------------------------------------------
# Инициализация
# -----------------------------------------------------------------------------
settings = get_settings()
router = APIRouter()

# -----------------------------------------------------------------------------
# Утилиты округления Decimal
# -----------------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    Используется при операциях с EFHC/bonus_EFHC и kWh.
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# NFT-проверка TonAPI (TonAPI v2)
# -----------------------------------------------------------------------------
async def _fetch_account_nfts(owner: str) -> List[str]:
    """
    Получает адреса NFT, принадлежащих `owner` (TON-кошелёк), через TonAPI v2:
        GET /v2/accounts/{owner}/nfts

    Возвращает список NFT-адресов (строк). В случае ошибки — пустой список.
    """
    base = (settings.NFT_PROVIDER_BASE_URL or "https://tonapi.io").rstrip("/")
    url = f"{base}/v2/accounts/{owner}/nfts"
    headers = {}
    if settings.NFT_PROVIDER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.NFT_PROVIDER_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        print(f"[EFHC][ADMIN][NFT] TonAPI request failed: {e}")
        return []

    # Структура TonAPI может иметь ключ items или nfts
    items = data.get("items") or data.get("nfts") or []
    addrs: List[str] = []
    for it in items:
        if not it:
            continue
        addr = it.get("address") or (it.get("nft") or {}).get("address")
        if addr:
            addrs.append(addr)
    return addrs

async def _is_admin_by_nft(db: AsyncSession, owner: Optional[str]) -> bool:
    """
    Проверка admin-доступа через NFT whitelist:
      • Если кошелёк `owner` обладает хотя бы одним NFT из efhc_admin.admin_nft_whitelist — доступ разрешён.
    """
    if not owner:
        return False

    q = await db.execute(select(AdminNFTWhitelist.nft_address))
    whitelist = {row[0].strip() for row in q.all() if row[0]}
    if not whitelist:
        return False

    user_nfts = {addr.strip() for addr in (await _fetch_account_nfts(owner))}
    return len(whitelist.intersection(user_nfts)) > 0

async def require_admin(
    db: AsyncSession,
    x_telegram_id: Optional[str],
    x_wallet_address: Optional[str],
) -> Dict[str, Any]:
    """
    Проверка прав администратора:
      • Супер-админ по settings.ADMIN_TELEGRAM_ID.
      • Банк (ID = 362746228) — тоже админ.
      • NFT-админ — если в кошельке `X-Wallet-Address` есть NFT из whitelist.

    В случае отсутствия прав — HTTP 403.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)

    # Супер-админ по конфигурации
    if settings.ADMIN_TELEGRAM_ID and tg == int(settings.ADMIN_TELEGRAM_ID):
        return {"is_admin": True, "by": "super"}

    # Банк — также имеет доступ
    if tg == BANK_TELEGRAM_ID:
        return {"is_admin": True, "by": "bank"}

    # NFT-админ
    if await _is_admin_by_nft(db, x_wallet_address):
        return {"is_admin": True, "by": "nft"}

    raise HTTPException(status_code=403, detail="Недостаточно прав")

# -----------------------------------------------------------------------------
# Pydantic-схемы запросов/ответов
# -----------------------------------------------------------------------------
class WhoAmIResponse(BaseModel):
    """
    Ответ на запрос /admin/whoami — подтверждает админ-доступ и показывает источник.
    """
    is_admin: bool
    by: Optional[str] = Field(None, description="Источник прав: 'super'|'bank'|'nft'")
    admin_telegram_id: Optional[int] = None
    vip_nft_collection: Optional[str] = None
    whitelist_count: int = 0

class WhitelistAddRequest(BaseModel):
    """
    Запрос на добавление NFT в whitelist.
    """
    nft_address: str = Field(..., description="TON-адрес NFT (полный адрес токена)")
    comment: Optional[str] = Field(None, description="Комментарий для админ-панели")

class CreditRequest(BaseModel):
    """
    Ручное начисление средств пользователю:
      • EFHC — начисляются ТОЛЬКО со счёта Банка (Банк → user).
      • bonus_EFHC — начисляются ТОЛЬКО со счёта Банка (Банк → user).
      • kWh — внутренний показатель, начисляется напрямую.
    """
    telegram_id: int
    efhc: Optional[Decimal] = Decimal("0.000")
    bonus: Optional[Decimal] = Decimal("0.000")
    kwh: Optional[Decimal] = Decimal("0.000")

class DebitRequest(BaseModel):
    """
    Ручное списание средств у пользователя:
      • EFHC — списываются ТОЛЬКО на счёт Банка (user → Банк).
      • bonus_EFHC — списываются ТОЛЬКО на счёт Банка (user → Банк).
      • kWh — внутренний показатель, списывается напрямую.
    """
    telegram_id: int
    efhc: Optional[Decimal] = Decimal("0.000")
    bonus: Optional[Decimal] = Decimal("0.000")
    kwh: Optional[Decimal] = Decimal("0.000")

class VipSetRequest(BaseModel):
    """
    Включение/отключение VIP-флага (увеличивает генерацию на +7%).
    """
    telegram_id: int
    enabled: bool

class TaskCreateRequest(BaseModel):
    """
    Создание задания (награда в bonus_EFHC).
    """
    title: str
    url: Optional[str] = None
    reward_bonus_efhc: Decimal = Decimal("1.000")
    active: bool = True

class TaskPatchRequest(BaseModel):
    """
    Частичное обновление задания.
    """
    title: Optional[str] = None
    url: Optional[str] = None
    reward_bonus_efhc: Optional[Decimal] = None
    active: Optional[bool] = None

class LotteryCreateRequest(BaseModel):
    """
    Создание лотереи.
    """
    code: str
    title: str
    prize_type: str  # 'efhc'|'panel'|'vip_nft'
    target_participants: int = 100
    active: bool = True

class LotteryPatchRequest(BaseModel):
    """
    Частичное обновление лотереи.
    """
    title: Optional[str] = None
    prize_type: Optional[str] = None
    target_participants: Optional[int] = None
    active: Optional[bool] = None

class MintRequest(BaseModel):
    """
    Запрос на минт EFHC (в Банк).
    """
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC для минта (в Банк)")
    comment: Optional[str] = Field("", description="Комментарий к минту")

class BurnRequest(BaseModel):
    """
    Запрос на бёрн EFHC (счёт Банка).
    """
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC для бёрна (из Банка)")
    comment: Optional[str] = Field("", description="Комментарий к бёрну")

class AwardTaskRequest(BaseModel):
    """
    Начисление bonus_EFHC пользователю за задания (через Банк).
    """
    user_id: int = Field(..., description="Telegram ID пользователя")
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма bonus_EFHC")
    reason: Optional[str] = Field("task_bonus", description="Описание/причина (для логов)")

# -----------------------------------------------------------------------------
# Таблица логов вознаграждений за задания (если нет — создаём)
# -----------------------------------------------------------------------------
TASKS_BONUS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS efhc_core.tasks_bonus_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    admin_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount NUMERIC(30, 3) NOT NULL,
    reason TEXT,
    CHECK (amount >= 0)
);
"""

async def ensure_tasks_bonus_table(db: AsyncSession) -> None:
    """
    Создаёт таблицу efhc_core.tasks_bonus_log при необходимости.
    Используется для журналирования начислений bonus_EFHC за задания.
    """
    await db.execute(text(TASKS_BONUS_CREATE_SQL))
    await db.commit()

# -----------------------------------------------------------------------------
# Эндпоинты — проверка прав
# -----------------------------------------------------------------------------
@router.get("/admin/whoami", response_model=WhoAmIResponse)
async def admin_whoami(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Возвращает информацию о правах текущего пользователя:
      • is_admin: True/False,
      • by: 'super'|'bank'|'nft' (источник прав),
      • admin_telegram_id: ID супер-админа из конфигурации,
      • vip_nft_collection: коллекция VIP NFT (если задана),
      • whitelist_count: количество элементов в whitelist NFT.
    """
    is_admin, by = False, None
    try:
        perm = await require_admin(db, x_telegram_id, x_wallet_address)
        is_admin, by = perm["is_admin"], perm["by"]
    except HTTPException:
        pass

    q = await db.execute(select(func.count()).select_from(AdminNFTWhitelist))
    wl_count = int(q.scalar() or 0)

    return WhoAmIResponse(
        is_admin=is_admin,
        by=by,
        admin_telegram_id=int(settings.ADMIN_TELEGRAM_ID) if settings.ADMIN_TELEGRAM_ID else None,
        vip_nft_collection=settings.VIP_NFT_COLLECTION,
        whitelist_count=wl_count,
    )

# -----------------------------------------------------------------------------
# NFT Whitelist — список / добавление / удаление
# -----------------------------------------------------------------------------
@router.get("/admin/nft/whitelist")
async def admin_nft_whitelist_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Возвращает список NFT-токенов в whitelist:
      • id — внутренний идентификатор,
      • nft_address — адрес NFT (TON),
      • comment — комментарий,
      • created_at — дата добавления.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(AdminNFTWhitelist).order_by(AdminNFTWhitelist.id.asc()))
    return [
        {
            "id": r.id,
            "nft_address": r.nft_address,
            "comment": r.comment,
            "created_at": r.created_at.isoformat()
        }
        for r in q.scalars().all()
    ]

@router.post("/admin/nft/whitelist")
async def admin_nft_whitelist_add(
    payload: WhitelistAddRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Добавляет NFT в whitelist (если ещё нет). Уникальность адреса контролируется на уровне БД.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    db.add(AdminNFTWhitelist(nft_address=payload.nft_address.strip(), comment=payload.comment))
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Не удалось добавить: {e}")
    return {"ok": True}

@router.delete("/admin/nft/whitelist/{item_id}")
async def admin_nft_whitelist_delete(
    item_id: int,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Удаляет NFT из whitelist по ID.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(AdminNFTWhitelist).where(AdminNFTWhitelist.id == item_id))
    row = q.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    await db.delete(row)
    await db.commit()
    return {"ok": True}

# -----------------------------------------------------------------------------
# Ручные операции смещены к «всё через Банк»:
#   • EFHC — use credit_user_from_bank / debit_user_to_bank
#   • bonus_EFHC — use credit_bonus_user_from_bank / debit_bonus_user_to_bank
#   • kWh — внутренний показатель, изменяем напрямую
# -----------------------------------------------------------------------------
@router.post("/admin/users/credit")
async def admin_users_credit(
    payload: CreditRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Начисление пользователю:
      • EFHC — Банк → user,
      • bonus_EFHC — Банк → user (для задач/акций),
      • kWh — напрямую (НЕ EFHC).

    Все операции EFHC/bonus_EFHC логируются в efhc_core.efhc_transfers_log.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    tg = int(payload.telegram_id)

    # Гарантируем наличие записей в users/balances
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.users (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": tg})
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": tg})

    # EFHC через Банк
    efhc_amt = d3(Decimal(payload.efhc or 0))
    if efhc_amt > 0:
        await credit_user_from_bank(db, user_id=tg, amount=efhc_amt)

    # bonus_EFHC через Банк
    bonus_amt = d3(Decimal(payload.bonus or 0))
    if bonus_amt > 0:
        await credit_bonus_user_from_bank(db, user_id=tg, amount=bonus_amt)

    # kWh — напрямую
    kwh_amt = d3(Decimal(payload.kwh or 0))
    if kwh_amt != 0:
        q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal: Optional[Balance] = q.scalar_one_or_none()
        if not bal:
            raise HTTPException(status_code=500, detail="Баланс не найден")
        new_k = d3(Decimal(bal.kwh or 0) + kwh_amt)
        await db.execute(update(Balance).where(Balance.telegram_id == tg).values(kwh=str(new_k)))

    await db.commit()

    # Возвращаем актуальные значения
    q2 = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal2: Optional[Balance] = q2.scalar_one_or_none()
    return {
        "ok": True,
        "telegram_id": tg,
        "efhc": str(d3(Decimal(bal2.efhc or 0))),
        "bonus_efhc": str(d3(Decimal(bal2.bonus_efhc or 0))),
        "kwh": str(d3(Decimal(bal2.kwh or 0))),
    }

@router.post("/admin/users/debit")
async def admin_users_debit(
    payload: DebitRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Списание у пользователя:
      • EFHC — user → Банк,
      • bonus_EFHC — user → Банк,
      • kWh — напрямую.

    Все EFHC/bonus_EFHC списания логируются в efhc_core.efhc_transfers_log.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    tg = int(payload.telegram_id)

    # EFHC через Банк
    efhc_amt = d3(Decimal(payload.efhc or 0))
    if efhc_amt > 0:
        await debit_user_to_bank(db, user_id=tg, amount=efhc_amt)

    # bonus_EFHC через Банк
    bonus_amt = d3(Decimal(payload.bonus or 0))
    if bonus_amt > 0:
        await debit_bonus_user_to_bank(db, user_id=tg, amount=bonus_amt)

    # kWh — списываем напрямую
    kwh_amt = d3(Decimal(payload.kwh or 0))
    if kwh_amt != 0:
        q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal: Optional[Balance] = q.scalar_one_or_none()
        if not bal:
            raise HTTPException(status_code=404, detail="Баланс не найден")
        cur_k = Decimal(bal.kwh or 0)
        if cur_k < kwh_amt:
            raise HTTPException(status_code=400, detail="Недостаточно kWh")
        new_k = d3(cur_k - kwh_amt)
        await db.execute(update(Balance).where(Balance.telegram_id == tg).values(kwh=str(new_k)))

    await db.commit()

    q2 = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal2: Optional[Balance] = q2.scalar_one_or_none()
    return {
        "ok": True,
        "telegram_id": tg,
        "efhc": str(d3(Decimal(bal2.efhc or 0))),
        "bonus_efhc": str(d3(Decimal(bal2.bonus_efhc or 0))),
        "kwh": str(d3(Decimal(bal2.kwh or 0))),
    }

# -----------------------------------------------------------------------------
# VIP-флаг: влияет на генерацию (+7%) — не трогает прямо EFHC.
# -----------------------------------------------------------------------------
@router.post("/admin/users/vip")
async def admin_users_vip_set(
    payload: VipSetRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Установка/снятие VIP-флага. Генерация энергии (kWh) учитывает VIP как множитель 1.07.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)

    q = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg))
    row = q.scalar_one_or_none()

    if payload.enabled:
        if not row:
            # Создаём запись о VIP
            db.add(UserVIP(telegram_id=tg, since=datetime.utcnow()))
            await db.commit()
        return {"ok": True, "vip": True}
    else:
        if row:
            # Удаляем VIP-флаг
            await db.delete(row)
            await db.commit()
        return {"ok": True, "vip": False}

# -----------------------------------------------------------------------------
# Задания — CRUD
# -----------------------------------------------------------------------------
@router.get("/admin/tasks")
async def admin_tasks_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Список всех заданий с основными полями.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Task).order_by(Task.id.asc()))
    return [
        {
            "id": t.id,
            "title": t.title,
            "url": t.url,
            "reward_bonus_efhc": str(t.reward_bonus_efhc),
            "active": t.active,
            "created_at": t.created_at.isoformat()
        }
        for t in q.scalars().all()
    ]

@router.post("/admin/tasks")
async def admin_tasks_create(
    payload: TaskCreateRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Создаёт новое задание. Награда фиксируется в поле reward_bonus_efhc (bonus_EFHC).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    t = Task(
        title=payload.title.strip(),
        url=payload.url,
        reward_bonus_efhc=d3(payload.reward_bonus_efhc),
        active=payload.active
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"ok": True, "id": t.id}

@router.patch("/admin/tasks/{task_id}")
async def admin_tasks_patch(
    task_id: int,
    payload: TaskPatchRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Частичное обновление задания по ID.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Task).where(Task.id == task_id))
    t = q.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if payload.title is not None:
        t.title = payload.title.strip()
    if payload.url is not None:
        t.url = payload.url
    if payload.reward_bonus_efhc is not None:
        t.reward_bonus_efhc = d3(payload.reward_bonus_efhc)
    if payload.active is not None:
        t.active = payload.active

    await db.commit()
    return {"ok": True}

# -----------------------------------------------------------------------------
# Лотереи — CRUD (сам розыгрыш и билеты реализуются в отдельных модулях)
# -----------------------------------------------------------------------------
@router.get("/admin/lotteries")
async def admin_lotteries_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Список всех лотерей (с основными полями).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).order_by(Lottery.created_at.asc()))
    return [
        {
            "id": l.code,
            "title": l.title,
            "prize_type": l.prize_type,
            "prize_value": str(l.prize_value),
            "target_participants": l.target_participants,
            "active": l.active,
            "tickets_sold": l.tickets_sold,
            "created_at": l.created_at.isoformat()
        }
        for l in q.scalars().all()
    ]

@router.post("/admin/lotteries")
async def admin_lottery_create(
    payload: LotteryCreateRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Создаёт новую лотерею (без розыгрыша). Розыгрыш/продажа билетов — в отдельном модуле.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).where(Lottery.code == payload.code))
    if q.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Код лотереи уже используется")

    l = Lottery(
        code=payload.code.strip(),
        title=payload.title.strip(),
        prize_type=payload.prize_type.strip(),
        target_participants=payload.target_participants,
        active=payload.active
    )
    db.add(l)
    await db.commit()
    return {"ok": True}

@router.patch("/admin/lotteries/{code}")
async def admin_lottery_patch(
    code: str,
    payload: LotteryPatchRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Частичное обновление лотереи по коду.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).where(Lottery.code == code))
    l = q.scalar_one_or_none()
    if not l:
        raise HTTPException(status_code=404, detail="Лотерея не найдена")

    if payload.title is not None:
        l.title = payload.title.strip()
    if payload.prize_type is not None:
        l.prize_type = payload.prize_type.strip()
    if payload.target_participants is not None:
        l.target_participants = payload.target_participants
    if payload.active is not None:
        l.active = payload.active

    await db.commit()
    return {"ok": True}

# -----------------------------------------------------------------------------
# Логи TonAPI
# -----------------------------------------------------------------------------
@router.get("/admin/ton/logs")
async def admin_ton_logs(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Возвращает последние N логов TonAPI-интеграции (efhc_core.ton_events_log).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(TonEventLog).order_by(TonEventLog.processed_at.desc()).limit(limit))
    return [
        {
            "event_id": r.event_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "action_type": r.action_type,
            "asset": r.asset,
            "amount": str(r.amount),
            "from": r.from_addr,
            "to": r.to_addr,
            "memo": r.memo,
            "telegram_id": r.telegram_id,
            "vip_requested": bool(r.vip_requested),
            "processed": bool(r.processed),
            "processed_at": r.processed_at.isoformat() if r.processed_at else None
        }
        for r in q.scalars().all()
    ]

# -----------------------------------------------------------------------------
# Минт/Бёрн EFHC (только Банк) + Баланс Банка
# -----------------------------------------------------------------------------
@router.post("/admin/mint", summary="Минт EFHC на счёт Банка")
async def api_admin_mint(
    payload: MintRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Минт EFHC: добавляет EFHC на баланс Банка (telegram_id=362746228).
    Вся операция логируется (efhc_core.mint_burn_log).
    """
    perm = await require_admin(db, x_telegram_id, x_wallet_address)
    try:
        amount = d3(Decimal(payload.amount))
        await mint_efhc(db, admin_id=int(x_telegram_id), amount=amount, comment=payload.comment or "")
        # Возвращаем текущий баланс Банка
        q = await db.execute(
            text(f"SELECT efhc FROM {settings.DB_SCHEMA_CORE}.balances WHERE telegram_id = :bank"),
            {"bank": BANK_TELEGRAM_ID},
        )
        row = q.first()
        bank_balance = str(row[0] if row and row[0] is not None else "0.000")
        await db.commit()
        return {"ok": True, "bank_balance_efhc": bank_balance}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Mint error: {e}")

@router.post("/admin/burn", summary="Бёрн EFHC со счёта Банка")
async def api_admin_burn(
    payload: BurnRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Бёрн EFHC: сжигает EFHC с баланса Банка.
    Логируется (efhc_core.mint_burn_log).
    """
    perm = await require_admin(db, x_telegram_id, x_wallet_address)
    try:
        amount = d3(Decimal(payload.amount))
        await burn_efhc(db, admin_id=int(x_telegram_id), amount=amount, comment=payload.comment or "")
        # Возвращаем текущий баланс Банка
        q = await db.execute(
            text(f"SELECT efhc FROM {settings.DB_SCHEMA_CORE}.balances WHERE telegram_id = :bank"),
            {"bank": BANK_TELEGRAM_ID},
        )
        row = q.first()
        bank_balance = str(row[0] if row and row[0] is not None else "0.000")
        await db.commit()
        return {"ok": True, "bank_balance_efhc": bank_balance}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Burn error: {e}")

@router.get("/admin/bank/balance", summary="Баланс Банка EFHC")
async def api_admin_bank_balance(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Возвращает текущий баланс EFHC Банка (внутренний учётный счёт), ID=362746228.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(
        text(f"SELECT efhc, bonus_efhc FROM {settings.DB_SCHEMA_CORE}.balances WHERE telegram_id = :bank"),
        {"bank": BANK_TELEGRAM_ID},
    )
    row = q.first()
    bank_efhc = str(row[0] if row and row[0] is not None else "0.000")
    bank_bonus = str(row[1] if row and row[1] is not None else "0.000")
    return {"ok": True, "bank_balance_efhc": bank_efhc, "bank_balance_bonus_efhc": bank_bonus}

# -----------------------------------------------------------------------------
# Начисления bonus_EFHC за задания (через Банк) + лог tasks_bonus_log
# -----------------------------------------------------------------------------
@router.post("/admin/tasks/award", summary="Начислить bonus_EFHC за задания (из Банка)")
async def api_admin_tasks_award(
    payload: AwardTaskRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Начисляет пользователю bonus_EFHC за задания/активности:
      • Банк → user (bonus_EFHC отдельный баланс),
      • Логируется в efhc_core.tasks_bonus_log,
      • Доступна только из админки.

    ВНИМАНИЕ: bonus_EFHC расходуются ТОЛЬКО на панели (shop/panels).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    await ensure_tasks_bonus_table(db)

    try:
        amt = d3(Decimal(payload.amount))
        # Собственно кредитование bonus_EFHC из Банка
        await credit_bonus_user_from_bank(db, user_id=int(payload.user_id), amount=amt)

        # Логируем (кроме efhc_transfers_log ещё и специализированный лог по заданиям)
        await db.execute(
            text("""
                INSERT INTO efhc_core.tasks_bonus_log (admin_id, user_id, amount, reason)
                VALUES (:admin_id, :user_id, :amt, :reason)
            """),
            {
                "admin_id": int(x_telegram_id),
                "user_id": int(payload.user_id),
                "amt": str(amt),
                "reason": payload.reason or "task_bonus",
            },
        )

        # Возвращаем баланс Банка для отчётности
        q = await db.execute(
            text(f"SELECT efhc, bonus_efhc FROM {settings.DB_SCHEMA_CORE}.balances WHERE telegram_id = :bank"),
            {"bank": BANK_TELEGRAM_ID},
        )
        row = q.first()
        bank_efhc = str(row[0] if row and row[0] is not None else "0.000")
        bank_bonus = str(row[1] if row and row[1] is not None else "0.000")
        await db.commit()
        return {
            "ok": True,
            "user_id": int(payload.user_id),
            "amount": str(amt),
            "reason": payload.reason or "task_bonus",
            "bank_balance_efhc": bank_efhc,
            "bank_balance_bonus_efhc": bank_bonus,
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Tasks award error: {e}")

# -----------------------------------------------------------------------------
# Вспомогания (связь с остальной логикой):
#   • ВНИМАНИЕ: Покупка панели (и билетов лотерей), Shop, Обменник, Withdraw —
#     реализуются в других модулях (user_routes.py, shop_routes.py, withdraw_routes.py).
#   • Там необходимо использовать efhc_transactions.* для соблюдения принципа «всё через Банк».
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Пример включения роутера в FastAPI:
#   from .admin_routes import router as admin_router
#   app.include_router(admin_router, prefix="/api")
# -----------------------------------------------------------------------------
