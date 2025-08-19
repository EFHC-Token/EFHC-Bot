# 📂 backend/app/admin_routes.py — админ-модуль EFHC
# -----------------------------------------------------------------------------
# Назначение:
#   • Единый админ-модуль (объединённая версия без регресса), который:
#       - Проверяет права администратора (по Telegram ID и/или NFT whitelist).
#       - Управляет whitelist'ом NFT (просмотр, добавление, удаление).
#       - Выполняет ручные операции с пользователями (начисление/списание bonus/kWh,
#         установка VIP-флага) — EFHC строго через Банк.
#       - Управляет заданиями (CRUD) и начисляет бонусы/награды за задания с
#         обязательным списанием EFHC со счёта Банка.
#       - Управляет лотереями (CRUD).
#       - Просматривает логи TonAPI-интеграции.
#       - Осуществляет эмиссию/сжигание EFHC через Банк (mint/burn).
#       - Возвращает баланс Банка EFHC и позволяет его пополнять/уменьшать.
#
# Принцип «Банк EFHC» (telegram_id = 362746228):
#   • Любые движение EFHC происходит ТОЛЬКО через Банк:
#       - Начисление EFHC пользователю → списание EFHC с Банка.
#       - Списание EFHC у пользователя → зачисление EFHC в Банк.
#   • Минт добавляет EFHC на баланс Банка. Бёрн сжигает EFHC с баланса Банка.
#   • Никаких «EFHC из воздуха» или «в никуда».
#   • Для bonus/kWh — это внутренние показатели, НЕ EFHC, они обновляются напрямую.
#
# Проверка прав:
#   • Заголовок `X-Telegram-Id` обязателен (число).
#   • Супер-админ по settings.ADMIN_TELEGRAM_ID.
#   • NFT-админ — если привязанный в заголовке `X-Wallet-Address` кошелёк содержит
#     NFT из whitelist (таблица efhc_admin.admin_nft_whitelist; проверка через TonAPI).
#   • Банк (ID 362746228) также считается супер-админом.
#
# Связи с проектом:
#   • database.py — асинхронные сессии в БД.
#   • config.py — глобальные настройки и ID админов, TON-настройки.
#   • models.py — ORM-модели User, Balance, Task, Lottery, TonEventLog, AdminNFTWhitelist, UserVIP.
#   • efhc_transactions.py — примитивы движения EFHC через Банк, mint/burn, логи операций.
#
# Примечание:
#   • Ничего из ранее реализованных эндпоинтов НЕ удалено. Модули расширены:
#       - Добавлены /api/admin/mint, /api/admin/burn, /api/admin/tasks/award, /api/admin/bank/balance.
#       - Переработаны /api/admin/users/credit и /api/admin/users/debit: EFHC теперь всегда через Банк.
#       - Просмотр логов TonAPI сохранён.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional, Dict, Any

import httpx
from fastapi import (
    APIRouter, Depends, Header, HTTPException, status, Path, Query
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
    BANK_TELEGRAM_ID,            # 362746228 — ID Банка EFHC
    ensure_logs_table,           # создание efhc_core.mint_burn_log при необходимости
    mint_efhc,                   # минт EFHC → Банк
    burn_efhc,                   # бёрн EFHC ← Банк
    credit_user_from_bank,       # начисление EFHC пользователю со списанием со счёта Банка
    debit_user_to_bank,          # списание EFHC у пользователя с зачислением на счёт Банка
)

# -----------------------------------------------------------------------------
# Глобальные настройки и инициализация роутера
# -----------------------------------------------------------------------------
settings = get_settings()
router = APIRouter()

# -----------------------------------------------------------------------------
# Утилиты округления Decimal (3 знака)
# -----------------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    Используется при работах с bonus/kWh и EFHC (в их внутреннем представлении).
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# NFT-проверка TonAPI: нужен API key в config.settings.NFT_PROVIDER_API_KEY
# -----------------------------------------------------------------------------
async def _fetch_account_nfts(owner: str) -> List[str]:
    """
    Запрос к TonAPI: список NFT по адресу `owner`.
    GET /v2/accounts/{owner}/nfts
    Возвращает массив адресов NFT (строки).
    """
    base = (settings.NFT_PROVIDER_BASE_URL or "https://tonapi.io").rstrip("/")
    url = f"{base}/v2/accounts/{owner}/nfts"
    headers = {}
    if settings.NFT_PROVIDER_API_KEY:
        # Для tonapi.io допустимо Authorization: Bearer <token>
        headers["Authorization"] = f"Bearer {settings.NFT_PROVIDER_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        print(f"[EFHC][ADMIN][NFT] TonAPI request failed: {e}")
        return []

    items = data.get("items") or data.get("nfts") or []
    nft_addrs: List[str] = []
    for it in items:
        if not it:
            continue
        # В зависимости от версии TonAPI поле может называться по-разному
        addr = it.get("address") or (it.get("nft") or {}).get("address")
        if addr:
            nft_addrs.append(addr)
    return nft_addrs


async def _is_admin_by_nft(db: AsyncSession, owner: Optional[str]) -> bool:
    """
    Проверка: владеет ли адрес `owner` хоть одним NFT из whitelist (efhc_admin.admin_nft_whitelist).
    Прим.: whitelist хранит адреса NFT, сравнение по точному совпадению.
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
    Общая проверка админ-доступа для эндпоинтов:
      • Супер-админ по settings.ADMIN_TELEGRAM_ID.
      • Банк (BANK_TELEGRAM_ID = 362746228).
      • NFT-админ: если `x_wallet_address` владеет NFT из whitelist.

    В случае отсутствия прав — HTTP 403.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)

    # Супер-админ по конфигу
    if settings.ADMIN_TELEGRAM_ID and tg == int(settings.ADMIN_TELEGRAM_ID):
        return {"is_admin": True, "by": "super"}

    # Банк (тоже может выполнять админские действия)
    if tg == BANK_TELEGRAM_ID:
        return {"is_admin": True, "by": "bank"}

    # NFT-админ
    if await _is_admin_by_nft(db, x_wallet_address):
        return {"is_admin": True, "by": "nft"}

    raise HTTPException(status_code=403, detail="Недостаточно прав")

# -----------------------------------------------------------------------------
# Pydantic-схемы запросов и ответов
# -----------------------------------------------------------------------------
class WhoAmIResponse(BaseModel):
    is_admin: bool
    by: Optional[str] = Field(None, description="Источник прав: 'super'/'bank'/'nft'")
    admin_telegram_id: Optional[int] = None
    vip_nft_collection: Optional[str] = None
    whitelist_count: int = 0

class WhitelistAddRequest(BaseModel):
    nft_address: str = Field(..., description="TON-адрес NFT")
    comment: Optional[str] = None

class CreditRequest(BaseModel):
    telegram_id: int
    efhc: Optional[Decimal] = Decimal("0.000")
    bonus: Optional[Decimal] = Decimal("0.000")
    kwh: Optional[Decimal] = Decimal("0.000")

class DebitRequest(BaseModel):
    telegram_id: int
    efhc: Optional[Decimal] = Decimal("0.000")
    bonus: Optional[Decimal] = Decimal("0.000")
    kwh: Optional[Decimal] = Decimal("0.000")

class VipSetRequest(BaseModel):
    telegram_id: int
    enabled: bool

class TaskCreateRequest(BaseModel):
    title: str
    url: Optional[str] = None
    reward_bonus_efhc: Decimal = Decimal("1.000")
    active: bool = True

class TaskPatchRequest(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    reward_bonus_efhc: Optional[Decimal] = None
    active: Optional[bool] = None

class LotteryCreateRequest(BaseModel):
    code: str
    title: str
    prize_type: str
    target_participants: int = 100
    active: bool = True

class LotteryPatchRequest(BaseModel):
    title: Optional[str] = None
    prize_type: Optional[str] = None
    target_participants: Optional[int] = None
    active: Optional[bool] = None

# --- Mint/Burn/Tasks award ---
class MintRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC для минта (в Банк)")
    comment: Optional[str] = Field("", description="Комментарий к минту")

class BurnRequest(BaseModel):
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC для бёрна (из Банка)")
    comment: Optional[str] = Field("", description="Комментарий к бёрну")

class AwardTaskRequest(BaseModel):
    user_id: int = Field(..., description="Telegram ID пользователя")
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="EFHC за задания")
    reason: Optional[str] = Field("task_bonus", description="Описание/причина")

# -----------------------------------------------------------------------------
# Таблицы логов бонусов/заданий (idempotent)
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
    """
    await db.execute(text(TASKS_BONUS_CREATE_SQL))
    await db.commit()

# -----------------------------------------------------------------------------
# Эндпоинт: проверка прав
# -----------------------------------------------------------------------------
@router.get("/admin/whoami", response_model=WhoAmIResponse)
async def admin_whoami(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Возвращает информацию о правах текущего пользователя:
      - is_admin: True/False,
      - by: 'super'|'bank'|'nft' (источник прав),
      - admin_telegram_id: ID супер-админа из конфигурации,
      - vip_nft_collection: коллекция VIP NFT (если задана),
      - whitelist_count: количество NFT в whitelist.
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
# NFT Whitelist: список/добавление/удаление
# -----------------------------------------------------------------------------
@router.get("/admin/nft/whitelist")
async def admin_nft_whitelist_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """Возвращает список NFT-токенов в whitelist."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(AdminNFTWhitelist).order_by(AdminNFTWhitelist.id.asc()))
    return [
        {"id": r.id, "nft_address": r.nft_address, "comment": r.comment, "created_at": r.created_at.isoformat()}
        for r in q.scalars().all()
    ]

@router.post("/admin/nft/whitelist")
async def admin_nft_whitelist_add(
    payload: WhitelistAddRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """Добавляет NFT в whitelist (если ещё нет)."""
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
    """Удаляет NFT из whitelist по ID."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(AdminNFTWhitelist).where(AdminNFTWhitelist.id == item_id))
    row = q.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    await db.delete(row)
    await db.commit()
    return {"ok": True}

# -----------------------------------------------------------------------------
# Ручные операции с пользователями:
#  • EFHC — ТОЛЬКО через Банк (credit_user_from_bank/debit_user_to_bank).
#  • bonus, kWh — напрямую через balances.
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
      • EFHC — через Банк (списываем у Банка, начисляем пользователю).
      • bonus/kWh — обновляем напрямую.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)

    # ensure user rows
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

    # bonus / kWh — напрямую
    if (payload.bonus or 0) != 0 or (payload.kwh or 0) != 0:
        q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal: Optional[Balance] = q.scalar_one_or_none()
        if not bal:
            raise HTTPException(status_code=500, detail="Баланс не найден")
        new_b = d3(Decimal(bal.bonus or 0) + Decimal(payload.bonus or 0))
        new_k = d3(Decimal(bal.kwh or 0) + Decimal(payload.kwh or 0))
        await db.execute(update(Balance).where(Balance.telegram_id == tg).values(
            bonus=str(new_b), kwh=str(new_k)
        ))

    await db.commit()

    # Возвращаем текущие значения
    q2 = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal2: Optional[Balance] = q2.scalar_one_or_none()
    return {
        "ok": True,
        "telegram_id": tg,
        "efhc": str(d3(Decimal(bal2.efhc or 0))),
        "bonus": str(d3(Decimal(bal2.bonus or 0))),
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
      • EFHC — через Банк (списываем у пользователя, зачисляем в Банк).
      • bonus/kWh — напрямую.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)

    # Для EFHC — только через Банк
    efhc_amt = d3(Decimal(payload.efhc or 0))
    if efhc_amt > 0:
        await debit_user_to_bank(db, user_id=tg, amount=efhc_amt)

    # bonus/kWh — напрямую
    if (payload.bonus or 0) != 0 or (payload.kwh or 0) != 0:
        q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal: Optional[Balance] = q.scalar_one_or_none()
        if not bal:
            raise HTTPException(status_code=404, detail="Баланс не найден")

        req_b = Decimal(payload.bonus or 0)
        req_k = Decimal(payload.kwh or 0)
        cur_b = Decimal(bal.bonus or 0)
        cur_k = Decimal(bal.kwh or 0)
        if cur_b < req_b or cur_k < req_k:
            raise HTTPException(status_code=400, detail="Недостаточно bonus/kWh")

        new_b = d3(cur_b - req_b)
        new_k = d3(cur_k - req_k)
        await db.execute(update(Balance).where(Balance.telegram_id == tg).values(
            bonus=str(new_b), kwh=str(new_k)
        ))

    await db.commit()

    q2 = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal2: Optional[Balance] = q2.scalar_one_or_none()
    return {
        "ok": True,
        "telegram_id": tg,
        "efhc": str(d3(Decimal(bal2.efhc or 0))),
        "bonus": str(d3(Decimal(bal2.bonus or 0))),
        "kwh": str(d3(Decimal(bal2.kwh or 0))),
    }

# -----------------------------------------------------------------------------
# Установка/снятие VIP-флага вручную (влияет на +7% генерации)
# -----------------------------------------------------------------------------
@router.post("/admin/users/vip")
async def admin_users_vip_set(
    payload: VipSetRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Установка/снятие VIP-флага:
      • Не влияет на EFHC напрямую,
      • Включается/выключается VIP (учитывается генерацией 0.598 vs 0.64 кВт/сутки).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)
    q = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg))
    row = q.scalar_one_or_none()

    if payload.enabled:
        if not row:
            db.add(UserVIP(telegram_id=tg, since=datetime.utcnow()))
            await db.commit()
        return {"ok": True, "vip": True}
    else:
        if row:
            await db.delete(row)
            await db.commit()
        return {"ok": True, "vip": False}

# -----------------------------------------------------------------------------
# Tasks — CRUD
# -----------------------------------------------------------------------------
@router.get("/admin/tasks")
async def admin_tasks_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """Список всех задач."""
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
    """Создать новое задание."""
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
    """Обновление задания по ID."""
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
# Lotteries — CRUD
# -----------------------------------------------------------------------------
@router.get("/admin/lotteries")
async def admin_lotteries_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """Список всех лотерей."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).order_by(Lottery.created_at.asc()))
    return [
        {
            "id": l.code,
            "title": l.title,
            "prize_type": l.prize_type,
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
    """Создать новую лотерею."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).where(Lottery.code == payload.code))
    if q.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Код уже используется")
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
    """Обновление лотереи по коду."""
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
# Просмотр логов TonAPI
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
# Дополнительно: Админские операции Банка — mint/burn/tasks award/balance
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
    Всё логируется в efhc_core.mint_burn_log.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    await ensure_logs_table(db)
    try:
        await mint_efhc(db, admin_id=int(x_telegram_id), amount=Decimal(payload.amount), comment=payload.comment or "")
        q = await db.execute(
            text("SELECT efhc FROM efhc_core.balances WHERE telegram_id = :bank"),
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
    Всё логируется в efhc_core.mint_burn_log.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    await ensure_logs_table(db)
    try:
        await burn_efhc(db, admin_id=int(x_telegram_id), amount=Decimal(payload.amount), comment=payload.comment or "")
        q = await db.execute(
            text("SELECT efhc FROM efhc_core.balances WHERE telegram_id = :bank"),
            {"bank": BANK_TELEGRAM_ID},
        )
        row = q.first()
        bank_balance = str(row[0] if row and row[0] is not None else "0.000")
        await db.commit()
        return {"ok": True, "bank_balance_efhc": bank_balance}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Burn error: {e}")

@router.post("/admin/tasks/award", summary="Начислить EFHC за задания (из Банка)")
async def api_admin_tasks_award(
    payload: AwardTaskRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Начисление EFHC пользователю за задания/бонусы:
      • EFHC списываются с Банка → начисляются пользователю (credit_user_from_bank).
      • Операция логируется в efhc_core.tasks_bonus_log.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)
    await ensure_tasks_bonus_table(db)
    try:
        amt = d3(Decimal(payload.amount))
        await credit_user_from_bank(db, user_id=int(payload.user_id), amount=amt)
        # Лог бонусов/заданий
        await db.execute(
            text("""INSERT INTO efhc_core.tasks_bonus_log (admin_id, user_id, amount, reason)
                    VALUES (:admin_id, :user_id, :amt, :reason)"""),
            {
                "admin_id": int(x_telegram_id),
                "user_id": int(payload.user_id),
                "amt": str(amt),
                "reason": payload.reason or "task_bonus",
            },
        )
        # Баланс Банка для отчётности
        q = await db.execute(
            text("SELECT efhc FROM efhc_core.balances WHERE telegram_id = :bank"),
            {"bank": BANK_TELEGRAM_ID},
        )
        row = q.first()
        bank_balance = str(row[0] if row and row[0] is not None else "0.000")
        await db.commit()
        return {
            "ok": True,
            "user_id": int(payload.user_id),
            "amount": str(amt),
            "reason": payload.reason or "task_bonus",
            "bank_balance_efhc": bank_balance,
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Tasks award error: {e}")

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
        text("SELECT efhc FROM efhc_core.balances WHERE telegram_id = :bank"),
        {"bank": BANK_TELEGRAM_ID},
    )
    row = q.first()
    bank_balance = str(row[0] if row and row[0] is not None else "0.000")
    return {"ok": True, "bank_balance_efhc": bank_balance}

# -----------------------------------------------------------------------------
# Интеграция:
#   • Подключить роутер в main.py:
#       from .admin_routes import router as admin_router
#       app.include_router(admin_router)
#
#   • Все EFHC-операции в других модулях (Panels, Exchange, Shop, Withdrawals,
#     Referrals awards) ДОЛЖНЫ использовать efhc_transactions.credit_user_from_bank /
#     debit_user_to_bank для соблюдения принципа «всё через Банк».
#
#   • Важно помнить:
#       - Покупка панели: user -> Банк (100 EFHC).
#       - Конвертация kWh->EFHC: Банк -> user (1:1).
#       - Переводы между пользователями: допускаются user->user без Банка, если разрешены,
#         но если запрашиваете «только через Банк», можно проксировать через Банк:
#             debit_user_to_bank(A), credit_user_from_bank(B).
#       - Вывод EFHC: при approve списываем у пользователя → зачисляем в Банк,
#         далее TON выплата из внешнего кошелька, а расход учитывается вне EFHC учёта.
#       - Shop (покупка за TON/USDT): Банк -> user (после подтверждённого внешнего платежа).
# -----------------------------------------------------------------------------
