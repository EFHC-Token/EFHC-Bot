# 📂 backend/app/admin_routes.py — админ-модуль EFHC
# -----------------------------------------------------------------------------
# Назначение:
#   • Предоставляет эндпоинты для администрирования:
#       - Проверка прав администратора (супер-админ по TELEGRAM_ID или NFT-админ).
#       - Управление whitelist'ом NFT (список, добавление, удаление).
#       - Ручные операции с пользователями (начисление/списание токенов, VIP-флаг).
#       - Управление заданиями (CRUD).
#       - Управление лотереями (CRUD).
#       - Просмотр логов TonAPI-интеграции.
#
# Как работает:
#   • Все запросы требуют заголовок `X-Telegram-Id`.
#   • Для NFT-проверки нужен заголовок `X-Wallet-Address` (TON-кошелёк).
#   • Супер-админ определяется по TELEGRAM_ID (settings.ADMIN_TELEGRAM_ID).
#   • NFT-админ определяется через наличие у него хотя бы одного токена
#     из whitelist (таблица efhc_admin.admin_nft_whitelist).
#   • NFT-проверка реализована через TonAPI (https://tonapi.io/v2/).
#
# Связи:
#   • database.py — сессии БД.
#   • config.py — переменные окружения (ADMIN_TELEGRAM_ID, NFT_PROVIDER_API_KEY и др).
#   • models.py — ORM-модели (User, Balance, Task, Lottery, TonEventLog и др).
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional, Dict, Any

import httpx
from fastapi import (
    APIRouter, Depends, Header, HTTPException, status, Path, Query
)
from pydantic import BaseModel, Field
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

# ---------------------------------------------------------------------
# Глобальные настройки
# ---------------------------------------------------------------------
settings = get_settings()
router = APIRouter()

# ---------------------------------------------------------------------
# Утилиты округления Decimal
# ---------------------------------------------------------------------
DEC3 = Decimal("0.001")  # до 3 знаков после запятой

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз.
    Используем для балансных операций (EFHC/bonus/kWh).
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------
# Проверка NFT-админства через TonAPI
# ---------------------------------------------------------------------
async def _fetch_account_nfts(owner: str) -> List[str]:
    """
    Получаем список NFT-адресов, принадлежащих аккаунту (TON-кошельку).
    Используется TonAPI v2: GET /v2/accounts/{owner}/nfts.
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

    # TonAPI v2: структура содержит либо `items`, либо `nfts`
    items = data.get("items") or data.get("nfts") or []
    return [it.get("address") or (it.get("nft") or {}).get("address") for it in items if it]


async def _is_admin_by_nft(db: AsyncSession, owner: Optional[str]) -> bool:
    """
    Проверка: владеет ли аккаунт хотя бы одним NFT из whitelist.
    """
    if not owner:
        return False

    # Получаем whitelist из БД
    q = await db.execute(select(AdminNFTWhitelist.nft_address))
    whitelist = {row[0].strip() for row in q.all() if row[0]}

    if not whitelist:
        return False

    # Получаем NFT пользователя
    user_nfts = {addr.strip() for addr in (await _fetch_account_nfts(owner))}
    return len(whitelist.intersection(user_nfts)) > 0


async def require_admin(
    db: AsyncSession,
    x_telegram_id: Optional[str],
    x_wallet_address: Optional[str],
) -> Dict[str, Any]:
    """
    Проверка прав администратора:
      - Супер-админ (по Telegram ID).
      - NFT-админ (по наличию NFT из whitelist).
    Если прав нет — выбрасывает HTTPException(403).
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)

    # Супер-админ
    if settings.ADMIN_TELEGRAM_ID and tg == int(settings.ADMIN_TELEGRAM_ID):
        return {"is_admin": True, "by": "super"}

    # NFT-админ
    if await _is_admin_by_nft(db, x_wallet_address):
        return {"is_admin": True, "by": "nft"}

    raise HTTPException(status_code=403, detail="Недостаточно прав")


# ---------------------------------------------------------------------
# Pydantic-схемы запросов/ответов
# ---------------------------------------------------------------------
class WhoAmIResponse(BaseModel):
    is_admin: bool
    by: Optional[str] = Field(None, description="Источник прав: 'super' или 'nft'")
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


# ---------------------------------------------------------------------
# Эндпоинты — админка
# ---------------------------------------------------------------------

@router.get("/admin/whoami", response_model=WhoAmIResponse)
async def admin_whoami(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address"),
):
    """
    Проверка прав текущего пользователя.
    Если админ — возвращает True и источник ('super' или 'nft').
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


# --- NFT Whitelist ---
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


# --- Ручные операции с пользователями ---
@router.post("/admin/users/credit")
async def admin_users_credit(payload: CreditRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Начисление EFHC/bonus/kWh пользователю."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)

    # Создаём записи, если их нет
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.users (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": tg})
    await db.execute(text(f"""
        INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
        VALUES (:tg) ON CONFLICT (telegram_id) DO NOTHING
    """), {"tg": tg})

    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=500, detail="Баланс не найден")

    new_e = d3(Decimal(bal.efhc or 0) + (payload.efhc or 0))
    new_b = d3(Decimal(bal.bonus or 0) + (payload.bonus or 0))
    new_k = d3(Decimal(bal.kwh or 0) + (payload.kwh or 0))

    await db.execute(update(Balance).where(Balance.telegram_id == tg).values(
        efhc=str(new_e), bonus=str(new_b), kwh=str(new_k)
    ))
    await db.commit()
    return {"ok": True, "efhc": str(new_e), "bonus": str(new_b), "kwh": str(new_k)}


@router.post("/admin/users/debit")
async def admin_users_debit(payload: DebitRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Списание EFHC/bonus/kWh (с проверкой остатков)."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    tg = int(payload.telegram_id)
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=404, detail="Баланс не найден")

    req_e, req_b, req_k = payload.efhc or 0, payload.bonus or 0, payload.kwh or 0
    cur_e, cur_b, cur_k = Decimal(bal.efhc or 0), Decimal(bal.bonus or 0), Decimal(bal.kwh or 0)

    if cur_e < req_e or cur_b < req_b or cur_k < req_k:
        raise HTTPException(status_code=400, detail="Недостаточно средств")

    new_e, new_b, new_k = d3(cur_e - req_e), d3(cur_b - req_b), d3(cur_k - req_k)

    await db.execute(update(Balance).where(Balance.telegram_id == tg).values(
        efhc=str(new_e), bonus=str(new_b), kwh=str(new_k)
    ))
    await db.commit()
    return {"ok": True, "efhc": str(new_e), "bonus": str(new_b), "kwh": str(new_k)}


@router.post("/admin/users/vip")
async def admin_users_vip_set(payload: VipSetRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Установка/снятие VIP-флага (влияет на генерацию)."""
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


# --- Tasks ---
@router.get("/admin/tasks")
async def admin_tasks_list(db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Список всех задач."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Task).order_by(Task.id.asc()))
    return [
        {"id": t.id, "title": t.title, "url": t.url,
         "reward_bonus_efhc": str(t.reward_bonus_efhc), "active": t.active,
         "created_at": t.created_at.isoformat()}
        for t in q.scalars().all()
    ]


@router.post("/admin/tasks")
async def admin_tasks_create(payload: TaskCreateRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Создать новое задание."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    t = Task(title=payload.title.strip(), url=payload.url,
             reward_bonus_efhc=d3(payload.reward_bonus_efhc), active=payload.active)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"ok": True, "id": t.id}


@router.patch("/admin/tasks/{task_id}")
async def admin_tasks_patch(task_id: int, payload: TaskPatchRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Обновление задания по ID."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Task).where(Task.id == task_id))
    t = q.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if payload.title is not None: t.title = payload.title.strip()
    if payload.url is not None: t.url = payload.url
    if payload.reward_bonus_efhc is not None: t.reward_bonus_efhc = d3(payload.reward_bonus_efhc)
    if payload.active is not None: t.active = payload.active
    await db.commit()
    return {"ok": True}


# --- Lotteries ---
@router.get("/admin/lotteries")
async def admin_lotteries_list(db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Список всех лотерей."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).order_by(Lottery.created_at.asc()))
    return [
        {"id": l.code, "title": l.title, "prize_type": l.prize_type,
         "target_participants": l.target_participants, "active": l.active,
         "tickets_sold": l.tickets_sold, "created_at": l.created_at.isoformat()}
        for l in q.scalars().all()
    ]


@router.post("/admin/lotteries")
async def admin_lottery_create(payload: LotteryCreateRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Создать новую лотерею."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).where(Lottery.code == payload.code))
    if q.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Код уже используется")
    l = Lottery(code=payload.code.strip(), title=payload.title.strip(),
                prize_type=payload.prize_type.strip(),
                target_participants=payload.target_participants, active=payload.active)
    db.add(l)
    await db.commit()
    return {"ok": True}


@router.patch("/admin/lotteries/{code}")
async def admin_lottery_patch(code: str, payload: LotteryPatchRequest, db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Обновление лотереи по коду."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(Lottery).where(Lottery.code == code))
    l = q.scalar_one_or_none()
    if not l:
        raise HTTPException(status_code=404, detail="Лотерея не найдена")
    if payload.title is not None: l.title = payload.title.strip()
    if payload.prize_type is not None: l.prize_type = payload.prize_type.strip()
    if payload.target_participants is not None: l.target_participants = payload.target_participants
    if payload.active is not None: l.active = payload.active
    await db.commit()
    return {"ok": True}


# --- Logs ---
@router.get("/admin/ton/logs")
async def admin_ton_logs(limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, alias="X-Wallet-Address")):
    """Последние N логов TonAPI-интеграции (таблица efhc_core.ton_events_log)."""
    await require_admin(db, x_telegram_id, x_wallet_address)
    q = await db.execute(select(TonEventLog).order_by(TonEventLog.processed_at.desc()).limit(limit))
    return [
        {"event_id": r.event_id, "ts": r.ts.isoformat() if r.ts else None,
         "action_type": r.action_type, "asset": r.asset, "amount": str(r.amount),
         "from": r.from_addr, "to": r.to_addr, "memo": r.memo,
         "telegram_id": r.telegram_id, "vip_requested": bool(r.vip_requested),
         "processed": bool(r.processed),
         "processed_at": r.processed_at.isoformat() if r.processed_at else None}
        for r in q.scalars().all()
    ]
