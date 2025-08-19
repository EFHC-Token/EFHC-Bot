# 📂 backend/app/admin_routes.py — админ-эндпоинты EFHC
# -----------------------------------------------------------------------------
# Что делает:
#   • Проверка прав администратора:
#       - Суперадмин по TELEGRAM ID: settings.ADMIN_TELEGRAM_ID.
#       - Админ по NFT: при наличии заголовка X-Wallet-Address и совпадении
#         хотя бы одного токена с whitelist (таблица efhc_admin.admin_nft_whitelist).
#         Проверка выполняется через TonAPI (settings.NFT_PROVIDER_BASE_URL / API_KEY).
#   • Управление whitelist'ом NFT:
#       - Список, добавление, удаление.
#   • Ручные операции по пользователям:
#       - Начисление/списание EFHC/bonus/kWh,
#       - Установка/снятие внутреннего VIP-флага.
#   • Управление заданиями:
#       - Список всех задач,
#       - Создание, частичное обновление.
#   • Управление лотереями:
#       - Список всех,
#       - Создание, обновление/деактивация.
#   • Просмотр логов TonAPI-интеграции:
#       - Последние N событий (efhc_core.ton_events_log).
#
# ВАЖНО:
#   • Все админ-эндпоинты требуют заголовок X-Telegram-Id
#   • Для NFT-проверки нужен заголовок X-Wallet-Address (TON-адрес пользователя).
#   • Ничего не удаляем: это надстройка над текущей архитектурой.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional, Dict, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, text, and_

from .database import get_session
from .config import get_settings
from .models import (
    User,
    Balance,
    UserVIP,
    Task,
    Lottery,
    LotteryTicket,
    TonEventLog,
    AdminNFTWhitelist,
)

settings = get_settings()
router = APIRouter()

# ---------------------------------------------------------------------
# Утилиты округления и Decimal
# ---------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------
# Вспомогательная проверка прав администратора
# ---------------------------------------------------------------------
async def _fetch_account_nfts(owner: str) -> List[str]:
    """
    Возвращает список NFT-адресов, принадлежащих owner (TON-адрес), используя TonAPI.
    Возможные реализации TonAPI:
      - https://tonapi.io (v2)
    Мы запрашиваем все NFT аккаунта; затем сверяем с whitelist.
    """
    base = (settings.NFT_PROVIDER_BASE_URL or "https://tonapi.io").rstrip("/")
    url = f"{base}/v2/accounts/{owner}/nfts"
    headers = {}
    if settings.NFT_PROVIDER_API_KEY:
        # TonAPI использует Authorization: Bearer <token>
        headers["Authorization"] = f"Bearer {settings.NFT_PROVIDER_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers, params={"collection": settings.VIP_NFT_COLLECTION or None})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        # Для стабильности не валим всё — просто считаем что 0 NFT
        print(f"[EFHC][ADMIN][NFT] TonAPI request failed: {e}")
        return []

    # Структура TonAPI может меняться; в v2 обычно nfts -> items
    items = []
    if isinstance(data, dict):
        items = data.get("nfts") or data.get("items") or []
    addrs: List[str] = []

    for it in items:
        # Пробуем извлечь адрес NFT (chain repr)
        # Обычно: it['address'] или it['nft']['address'] — зависит от провайдера
        addr = it.get("address") or (it.get("nft") or {}).get("address")
        if addr:
            addrs.append(addr)

    return addrs


async def _is_admin_by_nft(db: AsyncSession, owner: Optional[str]) -> bool:
    """
    Возвращает True, если у owner есть хотя бы один NFT из whitelist.
    owner — TON адрес (строка). Если None/пусто → False.
    """
    if not owner:
        return False

    # Получим whitelist из БД
    q = await db.execute(select(AdminNFTWhitelist.nft_address))
    wl = {row[0] for row in q.all()}  # множество адресов токенов

    if not wl:
        # Нет ничего в whitelist — в таком случае NFT-доступ невозможен
        return False

    # Запрашиваем NFT аккаунта и проверяем пересечения
    user_nfts = await _fetch_account_nfts(owner)
    if not user_nfts:
        return False

    # Нормализуем к одному регистру (у адресов в TON обычно регистр важен, но для надежности приводим)
    wl_norm = {s.strip() for s in wl}
    user_nfts_norm = {s.strip() for s in user_nfts}

    inter = wl_norm.intersection(user_nfts_norm)
    return len(inter) > 0


async def require_admin(
    db: AsyncSession,
    x_telegram_id: Optional[str],
    x_wallet_address: Optional[str],
) -> Dict[str, Any]:
    """
    Проверка прав администратора.
    Возвращает словарь с флагами:
      {
        "is_admin": bool,
        "by": "super" | "nft" | None
      }
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    # 1) Супер-админ по Telegram ID
    if settings.ADMIN_TELEGRAM_ID and tg == int(settings.ADMIN_TELEGRAM_ID):
        return {"is_admin": True, "by": "super"}

    # 2) Админ по NFT (если задан кошелёк)
    if await _is_admin_by_nft(db, x_wallet_address):
        return {"is_admin": True, "by": "nft"}

    # 3) Не админ
    raise HTTPException(status_code=403, detail="Недостаточно прав: требуется админ NFT или супер-админ ID")


# ---------------------------------------------------------------------
# Схемы запросов/ответов (Pydantic)
# ---------------------------------------------------------------------
class WhoAmIResponse(BaseModel):
    is_admin: bool
    by: Optional[str] = Field(None, description="'super' или 'nft'")
    admin_telegram_id: Optional[int] = None
    vip_nft_collection: Optional[str] = None
    whitelist_count: int = 0


class WhitelistAddRequest(BaseModel):
    nft_address: str = Field(..., description="Адрес конкретного NFT-токена (GetGems/TonAPI формат)")
    comment: Optional[str] = Field(None, description="Комментарий (например, владелец)")


class CreditRequest(BaseModel):
    telegram_id: int = Field(..., description="Кому начислять")
    efhc: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько EFHC (основных) добавить")
    bonus: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько бонусных EFHC добавить")
    kwh: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько kWh добавить")


class DebitRequest(BaseModel):
    telegram_id: int = Field(..., description="У кого списывать")
    efhc: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько EFHC (основных) списать")
    bonus: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько бонусных EFHC списать")
    kwh: Optional[Decimal] = Field(Decimal("0.000"), description="Сколько kWh списать")


class VipSetRequest(BaseModel):
    telegram_id: int = Field(..., description="Кому ставим/снимаем VIP")
    enabled: bool = Field(..., description="True — выдать VIP, False — снять VIP")


class TaskCreateRequest(BaseModel):
    title: str
    url: Optional[str] = None
    reward_bonus_efhc: Decimal = Field(Decimal("1.000"), ge=Decimal("0.000"))
    active: bool = True


class TaskPatchRequest(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    reward_bonus_efhc: Optional[Decimal] = Field(None, ge=Decimal("0.000"))
    active: Optional[bool] = None


class LotteryCreateRequest(BaseModel):
    code: str = Field(..., description="Уникальный код лотереи (например, 'lottery_vip')")
    title: str = Field(..., description="Отображаемое имя")
    prize_type: str = Field(..., description="Тип приза: VIP_NFT / PANEL / EFHC и т.д.")
    target_participants: int = Field(100, ge=1)
    active: bool = True


class LotteryPatchRequest(BaseModel):
    title: Optional[str] = None
    prize_type: Optional[str] = None
    target_participants: Optional[int] = Field(None, ge=1)
    active: Optional[bool] = None


# ---------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------

@router.get("/admin/whoami", response_model=WhoAmIResponse)
async def admin_whoami(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Возвращает, является ли вызывающий админом.
    - Супер-админ: Telegram ID == settings.ADMIN_TELEGRAM_ID.
    - NFT-админ: владеет одним из NFT из whitelist (по X-Wallet-Address).
    Бот вызывает этот эндпоинт на /start для показа кнопки «🛠 Админ-панель».
    """
    # Не бросаем 403 здесь — вместо этого возвращаем флаг False, чтобы бот просто не показывал кнопку.
    is_admin = False
    by = None
    try:
        perm = await require_admin(db, x_telegram_id, x_wallet_address)
        is_admin = perm["is_admin"]
        by = perm["by"]
    except HTTPException:
        is_admin = False
        by = None

    # Сколько элементов в whitelist
    q = await db.execute(select(func.count()).select_from(AdminNFTWhitelist))
    wl_count = int(q.scalar() or 0)

    return WhoAmIResponse(
        is_admin=is_admin,
        by=by,
        admin_telegram_id=int(settings.ADMIN_TELEGRAM_ID) if settings.ADMIN_TELEGRAM_ID else None,
        vip_nft_collection=settings.VIP_NFT_COLLECTION,
        whitelist_count=wl_count
    )


@router.get("/admin/nft/whitelist")
async def admin_nft_whitelist_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Возвращает список NFT-токенов из whitelist.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(AdminNFTWhitelist).order_by(AdminNFTWhitelist.id.asc()))
    rows: List[AdminNFTWhitelist] = list(q.scalars().all())
    return [
        {
            "id": r.id,
            "nft_address": r.nft_address,
            "comment": r.comment,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/admin/nft/whitelist")
async def admin_nft_whitelist_add(
    payload: WhitelistAddRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Добавляет NFT-токен в whitelist.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    # Вставка с защитой от дублей по UniqueConstraint
    try:
        db.add(AdminNFTWhitelist(nft_address=payload.nft_address.strip(), comment=payload.comment))
        await db.commit()
    except Exception as e:
        await db.rollback()
        # Возможно, дубликат
        raise HTTPException(status_code=400, detail=f"Не удалось добавить: {e}")

    return {"ok": True}


@router.delete("/admin/nft/whitelist/{item_id}")
async def admin_nft_whitelist_delete(
    item_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Удаляет NFT-токен из whitelist по ID.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(AdminNFTWhitelist).where(AdminNFTWhitelist.id == item_id))
    row = q.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Элемент не найден")

    await db.delete(row)
    await db.commit()
    return {"ok": True}


# -------------------- Ручные операции по пользователям ------------------------

@router.post("/admin/users/credit")
async def admin_users_credit(
    payload: CreditRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Ручное начисление EFHC/bonus/kWh пользователю.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    tg = int(payload.telegram_id)
    # Убедимся, что баланс существует
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.users (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": tg}
    )
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": tg}
    )

    # Обновим значения
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=500, detail="Не удалось получить баланс")

    new_e = d3(Decimal(bal.efhc or 0) + Decimal(payload.efhc or 0))
    new_b = d3(Decimal(bal.bonus or 0) + Decimal(payload.bonus or 0))
    new_k = d3(Decimal(bal.kwh or 0) + Decimal(payload.kwh or 0))

    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(efhc=str(new_e), bonus=str(new_b), kwh=str(new_k))
    )
    await db.commit()
    return {"ok": True, "efhc": f"{new_e:.3f}", "bonus": f"{new_b:.3f}", "kwh": f"{new_k:.3f}"}


@router.post("/admin/users/debit")
async def admin_users_debit(
    payload: DebitRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Ручное списание EFHC/bonus/kWh.
    Проверяем, хватает ли суммы к списанию.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    tg = int(payload.telegram_id)
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=404, detail="Пользователь/баланс не найден")

    req_e = Decimal(payload.efhc or 0)
    req_b = Decimal(payload.bonus or 0)
    req_k = Decimal(payload.kwh or 0)

    cur_e = Decimal(bal.efhc or 0)
    cur_b = Decimal(bal.bonus or 0)
    cur_k = Decimal(bal.kwh or 0)

    if cur_e < req_e or cur_b < req_b or cur_k < req_k:
        raise HTTPException(status_code=400, detail="Недостаточно средств для списания")

    new_e = d3(cur_e - req_e)
    new_b = d3(cur_b - req_b)
    new_k = d3(cur_k - req_k)

    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(efhc=str(new_e), bonus=str(new_b), kwh=str(new_k))
    )
    await db.commit()
    return {"ok": True, "efhc": f"{new_e:.3f}", "bonus": f"{new_b:.3f}", "kwh": f"{new_k:.3f}"}


@router.post("/admin/users/vip")
async def admin_users_vip_set(
    payload: VipSetRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Установить/снять внутренний VIP (для повышенной генерации по панелям).
    Не путать с админ-доступом (он по NFT).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    tg = int(payload.telegram_id)
    q = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg))
    row: Optional[UserVIP] = q.scalar_one_or_none()

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


# ------------------------------ ЗАДАНИЯ ---------------------------------------

@router.get("/admin/tasks")
async def admin_tasks_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Список всех заданий (включая неактивные).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(Task).order_by(Task.id.asc()))
    rows: List[Task] = list(q.scalars().all())
    return [
        {
            "id": t.id,
            "title": t.title,
            "url": t.url,
            "reward_bonus_efhc": f"{Decimal(t.reward_bonus_efhc or 0):.3f}",
            "active": t.active,
            "created_at": t.created_at.isoformat(),
        }
        for t in rows
    ]


@router.post("/admin/tasks")
async def admin_tasks_create(
    payload: TaskCreateRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Создать новое задание.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    t = Task(
        title=payload.title.strip(),
        url=(payload.url or None),
        reward_bonus_efhc=d3(Decimal(payload.reward_bonus_efhc or 0)),
        active=bool(payload.active),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"ok": True, "id": t.id}


@router.patch("/admin/tasks/{task_id}")
async def admin_tasks_patch(
    task_id: int = Path(..., ge=1),
    payload: TaskPatchRequest = None,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Частичное обновление задания: title/url/reward/active.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(Task).where(Task.id == task_id))
    t: Optional[Task] = q.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задание не найдено")

    if payload.title is not None:
        t.title = payload.title.strip()
    if payload.url is not None:
        t.url = payload.url
    if payload.reward_bonus_efhc is not None:
        t.reward_bonus_efhc = d3(Decimal(payload.reward_bonus_efhc))
    if payload.active is not None:
        t.active = bool(payload.active)

    await db.commit()
    return {"ok": True}


# ------------------------------ ЛОТЕРЕИ ---------------------------------------

@router.get("/admin/lotteries")
async def admin_lotteries_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Список всех лотерей (включая неактивные).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(Lottery).order_by(Lottery.created_at.asc()))
    rows: List[Lottery] = list(q.scalars().all())
    return [
        {
            "id": l.code,
            "title": l.title,
            "prize_type": l.prize_type,
            "target_participants": l.target_participants,
            "active": l.active,
            "tickets_sold": l.tickets_sold,
            "created_at": l.created_at.isoformat(),
        }
        for l in rows
    ]


@router.post("/admin/lotteries")
async def admin_lottery_create(
    payload: LotteryCreateRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Создать новую лотерею (код уникален).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    # Проверим уникальность кода
    q = await db.execute(select(Lottery).where(Lottery.code == payload.code))
    if q.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Лотерея с таким кодом уже существует")

    l = Lottery(
        code=payload.code.strip(),
        title=payload.title.strip(),
        prize_type=payload.prize_type.strip(),
        target_participants=int(payload.target_participants),
        active=bool(payload.active),
    )
    db.add(l)
    await db.commit()
    return {"ok": True}


@router.patch("/admin/lotteries/{code}")
async def admin_lottery_patch(
    code: str,
    payload: LotteryPatchRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Обновление лотереи по коду: title/prize_type/target/active.
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(select(Lottery).where(Lottery.code == code))
    l: Optional[Lottery] = q.scalar_one_or_none()
    if not l:
        raise HTTPException(status_code=404, detail="Лотерея не найдена")

    if payload.title is not None:
        l.title = payload.title.strip()
    if payload.prize_type is not None:
        l.prize_type = payload.prize_type.strip()
    if payload.target_participants is not None:
        l.target_participants = int(payload.target_participants)
    if payload.active is not None:
        l.active = bool(payload.active)

    await db.commit()
    return {"ok": True}


# ------------------------------ ЛОГИ TON --------------------------------------

@router.get("/admin/ton/logs")
async def admin_ton_logs(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
    x_wallet_address: Optional[str] = Header(None, convert_underscores=False, alias="X-Wallet-Address"),
):
    """
    Последние N логов обработанных входящих TON/Jetton транзакций (таблица: efhc_core.ton_events_log).
    """
    await require_admin(db, x_telegram_id, x_wallet_address)

    q = await db.execute(
        select(TonEventLog).order_by(TonEventLog.processed_at.desc()).limit(limit)
    )
    rows: List[TonEventLog] = list(q.scalars().all())
    out = []
    for r in rows:
        out.append({
            "event_id": r.event_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "action_type": r.action_type,
            "asset": r.asset,
            "amount": f"{Decimal(r.amount or 0):.9f}" if r.amount is not None else None,
            "decimals": r.decimals,
            "from": r.from_addr,
            "to": r.to_addr,
            "memo": r.memo,
            "telegram_id": r.telegram_id,
            "parsed_amount_efhc": f"{Decimal(r.parsed_amount_efhc or 0):.3f}" if r.parsed_amount_efhc is not None else None,
            "vip_requested": bool(r.vip_requested),
            "processed": bool(r.processed),
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        })
    return out
