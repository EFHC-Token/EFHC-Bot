# 📂 backend/app/withdraw_routes.py — вывод EFHC с TON-интеграцией (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Пользовательский вывод EFHC на внешний кошелёк (EFHC Jetton в сети TON).
#   • Админ-панель: просмотр, approve/reject, отправка (manual/auto), статусы.
#   • Все списания EFHC у пользователя → на Банк (ID=362746228) при создании заявки.
#   • При reject — возврат EFHC пользователю из Банка (в полном объёме).
#   • Логирование всех движений EFHC выполняется в efhc_transactions.py (efhc_transfers_log).
#
# Бизнес-правила:
#   • В боте выводится ТОЛЬКО EFHC (никаких TON/USDT).
#   • EFHC — внутренняя единица, 1 EFHC = 1 kWh (курс как таковой не используется в выводе).
#   • bonus_EFHC НЕЛЬЗЯ выводить (только тратить на панели).
#   • При создании заявки на вывод мы сразу блокируем EFHC:
#       - переводим user → Банк (debit_user_to_bank), статус='pending'.
#       - это предотвращает двойное расходование.
#   • При Reject — возвращаем EFHC пользователю (Банк → user).
#   • При Approve — фиксируем одобрение (EFHC уже у Банка).
#   • При Send — выполняем реальную on-chain отправку EFHC Jetton (manual/auto), сохраняем tx_hash.
#
# Таблица efhc_core.withdrawals:
#   • id BIGSERIAL PK
#   • telegram_id BIGINT NOT NULL — кто вывел
#   • ton_address TEXT NOT NULL — адрес кошелька получателя (адрес кошелька в TON)
#   • amount_efhc NUMERIC(30,3) NOT NULL — сумма EFHC
#   • asset TEXT NOT NULL DEFAULT 'EFHC' — актив (фиксированно: 'EFHC')
#   • status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','sent','failed','canceled'))
#   • idempotency_key TEXT UNIQUE NULL — защита от дублей
#   • tx_hash TEXT NULL — хэш on-chain транзакции
#   • admin_id BIGINT NULL — кто одобрил/отправил
#   • comment TEXT NULL — комментарии админа
#   • created_at TIMESTAMPTZ DEFAULT now()
#   • approved_at TIMESTAMPTZ NULL
#   • sent_at TIMESTAMPTZ NULL
#   • updated_at TIMESTAMPTZ DEFAULT now()
#
# Зависимости:
#   • database.py — get_session.
#   • config.py — get_settings() (TON payout режимы, schema, admin IDs).
#   • models.py — Balance, User (ORM).
#   • efhc_transactions.py — операции EFHC через Банк и логи (credit/debit).
#   • admin_routes.py — админ-модуль.
#
# Важные настройки (config.py):
#   • ADMIN_TELEGRAM_ID — супер-админ.
#   • BANK_TELEGRAM_ID — 362746228 (Банк EFHC).
#   • DB_SCHEMA_CORE — 'efhc_core' (схема БД).
#   • WITHDRAW_MIN_EFHC, WITHDRAW_MAX_EFHC — лимиты (опционально).
#   • TON_PAYOUT_MODE — 'manual' (по умолчанию) | 'webhook'
#   • TON_PAYOUT_WEBHOOK_URL — если 'webhook', URL для внешнего сервиса.
#   • TON_PAYOUT_API_KEY — API-ключ для вебхука.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any

import re
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Path
from pydantic import BaseModel, Field, condecimal
from sqlalchemy import text, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .config import get_settings
from .models import Balance, User
from .efhc_transactions import (
    BANK_TELEGRAM_ID,
    credit_user_from_bank,
    debit_user_to_bank,
)

# -----------------------------------------------------------------------------
# Инициализация
# -----------------------------------------------------------------------------
router = APIRouter()
settings = get_settings()

# -----------------------------------------------------------------------------
# Константы/утилиты округления
# -----------------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    Применяем для EFHC значений (EFHC и kWh выводятся с точностью 0.001).
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# DDL: withdrawals (idempotent)
# -----------------------------------------------------------------------------
WITHDRAWALS_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.withdrawals (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    ton_address TEXT NOT NULL,
    amount_efhc NUMERIC(30,3) NOT NULL,
    asset TEXT NOT NULL DEFAULT 'EFHC',
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','sent','failed','canceled')),
    idempotency_key TEXT UNIQUE,
    tx_hash TEXT,
    admin_id BIGINT,
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    approved_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_withdrawals_table(db: AsyncSession) -> None:
    """
    Создаёт таблицу efhc_core.withdrawals при необходимости.
    """
    await db.execute(text(WITHDRAWALS_CREATE_SQL.format(schema=settings.DB_SCHEMA_CORE)))
    await db.commit()

# -----------------------------------------------------------------------------
# Валидация TON-адреса (простой формат)
# -----------------------------------------------------------------------------
TON_ADDR_RE = re.compile(r"^[EU][QqA-Za-z0-9_-]{46,66}$")

def validate_ton_address(addr: str) -> bool:
    """
    Упрощённая проверка адреса TON (base64url, EQ.../UQ..., 48—66 символов).
    EFHC Jetton отправляется на этот адрес в сети TON.
    """
    if not addr:
        return False
    addr = addr.strip()
    return bool(TON_ADDR_RE.match(addr))

# -----------------------------------------------------------------------------
# Pydantic-схемы
# -----------------------------------------------------------------------------
class CreateWithdrawRequest(BaseModel):
    """
    Запрос создания заявки на вывод:
      • ton_address — адрес кошелька (TON, куда уйдёт EFHC Jetton),
      • amount — сумма EFHC для вывода,
      • idempotency_key — чтобы не создать дубликаты,
      • (опц.) telegram_id — если приходит с фронта (проверяем на совпадение).
    """
    ton_address: str = Field(..., description="TON-адрес получателя EFHC")
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC к выводу")
    idempotency_key: Optional[str] = Field(None, description="Ключ идемпотентности (для защиты от дублей)")
    telegram_id: Optional[int] = Field(None, description="Telegram ID (если есть на фронте)")

class WithdrawItem(BaseModel):
    """
    Элемент списка/деталей заявок на вывод EFHC.
    """
    id: int
    telegram_id: int
    ton_address: str
    amount_efhc: str
    asset: str
    status: str
    tx_hash: Optional[str]
    comment: Optional[str]
    admin_id: Optional[int]
    created_at: str
    approved_at: Optional[str]
    sent_at: Optional[str]

class AdminWithdrawAction(BaseModel):
    """
    Действия админа без отправки (approve/reject/failed).
    """
    comment: Optional[str] = Field(None, description="Комментарий администратора")

class AdminSendRequest(AdminWithdrawAction):
    """
    Отправка выплаты (manual/webhook):
      • manual — админ вручную указывает tx_hash (обязателен).
      • webhook — внешний сервис отправляет EFHC Jetton, возвращает tx_hash.
    """
    tx_hash: Optional[str] = Field(None, description="Хэш транзакции (manual режим)")

# -----------------------------------------------------------------------------
# Сервис выплат EFHC в сети TON (manual/webhook)
# -----------------------------------------------------------------------------
class TonEFHCPayoutService:
    """
    Абстракция отправки EFHC (Jetton) в сети TON:
      • manual — админ вручную производит отправку и указывает tx_hash.
      • webhook — вызывается внешний сервис, который делает отправку и возвращает tx_hash.
    ВНИМАНИЕ: Курсы не используются. Мы отправляем ровно amount EFHC (Jetton),
              которые уже были списаны у пользователя при создании заявки.
    """
    def __init__(self, mode: str, webhook_url: Optional[str], api_key: Optional[str]):
        self.mode = (mode or "manual").lower().strip()
        self.webhook_url = webhook_url
        self.api_key = api_key

    async def send(self, to_address: str, amount: Decimal) -> str:
        """
        Выполняет отправку EFHC Jetton через webhook (если режим webhook).
        В manual-режиме — запрещаем (tx_hash должен быть указан админом вручную).
        """
        if self.mode == "manual":
            raise RuntimeError("Payout mode is MANUAL — tx_hash must be provided by admin")

        if not self.webhook_url:
            raise RuntimeError("TON_PAYOUT_WEBHOOK_URL is not configured")

        payload = {
            "asset": "EFHC",           # фиксируем актив для внешнего сервиса
            "to_address": to_address,  # адрес TON получателя
            "amount": str(d3(amount)), # сколько EFHC отправить
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(self.webhook_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()

        if not data or not data.get("ok"):
            raise RuntimeError(f"Payout webhook error: {data}")

        txh = data.get("tx_hash")
        if not txh:
            raise RuntimeError("Payout webhook did not return tx_hash")
        return txh

payout_service = TonEFHCPayoutService(
    mode=(settings.TON_PAYOUT_MODE or "manual"),
    webhook_url=settings.TON_PAYOUT_WEBHOOK_URL,
    api_key=settings.TON_PAYOUT_API_KEY,
)

# -----------------------------------------------------------------------------
# Авторизация (пользователь/админ)
# -----------------------------------------------------------------------------
async def require_user(x_telegram_id: Optional[str]) -> int:
    """
    Проверяет X-Telegram-Id, возвращает int Telegram ID.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    return int(x_telegram_id)

async def require_admin(db: AsyncSession, x_telegram_id: Optional[str]) -> int:
    """
    Проверяет админские права:
      • супер-админ (settings.ADMIN_TELEGRAM_ID),
      • Банк (BANK_TELEGRAM_ID).
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
# Пользователь: создать заявку на вывод EFHC
# -----------------------------------------------------------------------------
@router.post("/withdraw", summary="Создать заявку на вывод EFHC")
async def create_withdraw(
    payload: CreateWithdrawRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Создаёт заявку на вывод EFHC:
      • Проверка адреса и лимитов,
      • Списание EFHC user → Банк (блокировка средств до решения),
      • Создание записи в efhc_core.withdrawals (status='pending').
      • Idempotency: при наличии idempotency_key и уже существующей записи — возвращаем её.
    """
    await ensure_withdrawals_table(db)
    user_id = await require_user(x_telegram_id)

    # Если фронт передал telegram_id — сверим с заголовком
    if payload.telegram_id is not None and int(payload.telegram_id) != user_id:
        raise HTTPException(status_code=400, detail="Telegram ID mismatch")

    # Валидация TON-адреса (EFHC Jetton будет отправляться на этот адрес в сети TON)
    if not validate_ton_address(payload.ton_address):
        raise HTTPException(status_code=400, detail="Некорректный TON-адрес")

    # Лимиты вывода
    min_w = d3(Decimal(getattr(settings, "WITHDRAW_MIN_EFHC", "1.000")))
    max_w = d3(Decimal(getattr(settings, "WITHDRAW_MAX_EFHC", "1000000.000")))
    amount = d3(Decimal(payload.amount))
    if amount < min_w:
        raise HTTPException(status_code=400, detail=f"Минимальная сумма вывода: {min_w}")
    if amount > max_w:
        raise HTTPException(status_code=400, detail=f"Максимальная сумма вывода: {max_w}")

    # Проверка идемпотентности: если заявка с таким ключом уже есть — возвращаем её
    if payload.idempotency_key:
        q = await db.execute(
            text(f"""
                SELECT id, telegram_id, ton_address, amount_efhc, asset, status, tx_hash, comment, admin_id,
                       created_at, approved_at, sent_at
                FROM {settings.DB_SCHEMA_CORE}.withdrawals
                WHERE idempotency_key = :ikey
            """),
            {"ikey": payload.idempotency_key},
        )
        row = q.first()
        if row:
            # Ничего не списываем повторно — просто возвращаем существующую заявку
            return {
                "ok": True,
                "withdraw": {
                    "id": row[0],
                    "telegram_id": row[1],
                    "ton_address": row[2],
                    "amount_efhc": str(d3(Decimal(row[3]))),
                    "asset": row[4],
                    "status": row[5],
                    "tx_hash": row[6],
                    "comment": row[7],
                    "admin_id": row[8],
                    "created_at": row[9].isoformat() if row[9] else None,
                    "approved_at": row[10].isoformat() if row[10] else None,
                    "sent_at": row[11].isoformat() if row[11] else None,
                }
            }

    # Проверим баланс EFHC пользователя (именно EFHC, не бонус!)
    q2 = await db.execute(select(Balance).where(Balance.telegram_id == user_id))
    bal: Optional[Balance] = q2.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=400, detail="Баланс не найден")

    cur_efhc = d3(Decimal(bal.efhc or 0))
    if cur_efhc < amount:
        raise HTTPException(status_code=400, detail="Недостаточно EFHC для вывода")

    # Блокируем EFHC: user → Банк
    try:
        await debit_user_to_bank(db, user_id=user_id, amount=amount)
    except HTTPException as he:
        # Пробросим корректно
        raise he
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Не удалось заблокировать EFHC: {e}")

    # Создаём заявку: status=pending, asset='EFHC'
    try:
        q3 = await db.execute(
            text(f"""
                INSERT INTO {settings.DB_SCHEMA_CORE}.withdrawals
                (telegram_id, ton_address, amount_efhc, asset, status, idempotency_key, created_at)
                VALUES (:tg, :addr, :amt, 'EFHC', 'pending', :ikey, NOW())
                RETURNING id
            """),
            {
                "tg": user_id,
                "addr": payload.ton_address.strip(),
                "amt": str(amount),
                "ikey": payload.idempotency_key,
            },
        )
        wid = q3.scalar_one()
        await db.commit()
    except Exception as e:
        await db.rollback()
        # Важный откат: если заявку не записали — вернём EFHC пользователю.
        try:
            await credit_user_from_bank(db, user_id=user_id, amount=amount)
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Withdraw create failed; refund failed: {e} / {e2}")
        raise HTTPException(status_code=400, detail=f"Withdraw create failed: {e}")

    return {"ok": True, "withdraw_id": wid, "status": "pending"}

# -----------------------------------------------------------------------------
# Пользователь: список своих выводов
# -----------------------------------------------------------------------------
@router.get("/withdraws", summary="Список выводов текущего пользователя")
async def list_user_withdraws(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    limit: int = Query(50, ge=1, le=500),
):
    """
    Возвращает последние N выводов текущего пользователя.
    """
    user_id = await require_user(x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, ton_address, amount_efhc, asset, status, tx_hash, comment,
                   admin_id, created_at, approved_at, sent_at
            FROM {settings.DB_SCHEMA_CORE}.withdrawals
            WHERE telegram_id = :tg
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"tg": user_id, "lim": limit},
    )
    rows = q.fetchall()
    out: List[WithdrawItem] = []
    for r in rows:
        out.append(WithdrawItem(
            id=r[0],
            telegram_id=r[1],
            ton_address=r[2],
            amount_efhc=str(d3(Decimal(r[3]))),
            asset=r[4],
            status=r[5],
            tx_hash=r[6],
            comment=r[7],
            admin_id=r[8],
            created_at=r[9].isoformat() if r[9] else None,
            approved_at=r[10].isoformat() if r[10] else None,
            sent_at=r[11].isoformat() if r[11] else None,
        ))
    return {"ok": True, "items": [i.dict() for i in out]}

# -----------------------------------------------------------------------------
# Админ: список всех выводов
# -----------------------------------------------------------------------------
@router.get("/admin/withdraws", summary="Список всех заявок на вывод (админ)")
async def admin_list_withdraws(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
    status: Optional[str] = Query(None, regex="^(pending|approved|rejected|sent|failed|canceled)$"),
    user_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Возвращает список заявок с фильтрами (статус и/или user_id).
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    where_sql = "WHERE 1=1"
    params: Dict[str, Any] = {"lim": limit}

    if status:
        where_sql += " AND status = :st"
        params["st"] = status
    if user_id:
        where_sql += " AND telegram_id = :tg"
        params["tg"] = int(user_id)

    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, ton_address, amount_efhc, asset, status, tx_hash, comment,
                   admin_id, created_at, approved_at, sent_at
            FROM {settings.DB_SCHEMA_CORE}.withdrawals
            {where_sql}
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        params,
    )
    rows = q.fetchall()
    out: List[WithdrawItem] = []
    for r in rows:
        out.append(WithdrawItem(
            id=r[0],
            telegram_id=r[1],
            ton_address=r[2],
            amount_efhc=str(d3(Decimal(r[3]))),
            asset=r[4],
            status=r[5],
            tx_hash=r[6],
            comment=r[7],
            admin_id=r[8],
            created_at=r[9].isoformat() if r[9] else None,
            approved_at=r[10].isoformat() if r[10] else None,
            sent_at=r[11].isoformat() if r[11] else None,
        ))
    return {"ok": True, "items": [i.dict() for i in out]}

# -----------------------------------------------------------------------------
# Админ: детальная заявка
# -----------------------------------------------------------------------------
@router.get("/admin/withdraws/{withdraw_id}", summary="Детали заявки на вывод (админ)")
async def admin_get_withdraw(
    withdraw_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Возвращает детали одной заявки по ID.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, ton_address, amount_efhc, asset, status, tx_hash, comment,
                   admin_id, created_at, approved_at, sent_at
            FROM {settings.DB_SCHEMA_CORE}.withdrawals
            WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    r = q.first()
    if not r:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    item = WithdrawItem(
        id=r[0],
        telegram_id=r[1],
        ton_address=r[2],
        amount_efhc=str(d3(Decimal(r[3]))),
        asset=r[4],
        status=r[5],
        tx_hash=r[6],
        comment=r[7],
        admin_id=r[8],
        created_at=r[9].isoformat() if r[9] else None,
        approved_at=r[10].isoformat() if r[10] else None,
        sent_at=r[11].isoformat() if r[11] else None,
    )
    return {"ok": True, "item": item.dict()}

# -----------------------------------------------------------------------------
# Админ: approve заявки
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/approve", summary="Одобрить заявку (админ)")
async def admin_approve_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminWithdrawAction = AdminWithdrawAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Одобряет заявку:
      • Допустимо только для статуса 'pending'.
      • EFHC уже в Банке (заблокированы при создании).
      • Фиксируем status='approved', approved_at, admin_id, comment.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"SELECT status FROM {settings.DB_SCHEMA_CORE}.withdrawals WHERE id = :wid"),
        {"wid": withdraw_id},
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    st = row[0]
    if st != "pending":
        raise HTTPException(status_code=400, detail=f"Нельзя одобрить заявку в статусе {st}")

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
            SET status='approved', approved_at=NOW(), admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:wid
        """),
        {"aid": admin_id, "cmt": payload.comment, "wid": withdraw_id},
    )
    await db.commit()
    return {"ok": True, "status": "approved"}

# -----------------------------------------------------------------------------
# Админ: reject заявки (возврат EFHC пользователю)
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/reject", summary="Отклонить заявку (админ)")
async def admin_reject_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminWithdrawAction = AdminWithdrawAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Отклоняет заявку:
      • Допустимые статусы: 'pending' или 'approved' (ещё не отправлена).
      • Возвращаем EFHC пользователю из Банка.
      • Ставим статус='rejected', admin_id, comment.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT telegram_id, amount_efhc, status
            FROM {settings.DB_SCHEMA_CORE}.withdrawals
            WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    user_id = int(row[0])
    amt = d3(Decimal(row[1]))
    st = row[2]

    if st not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail=f"Нельзя отклонить заявку в статусе {st}")

    # Возврат EFHC пользователю (из Банка)
    try:
        await credit_user_from_bank(db, user_id=user_id, amount=amt)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Не удалось вернуть EFHC пользователю: {e}")

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
            SET status='rejected', admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:wid
        """),
        {"aid": admin_id, "cmt": payload.comment, "wid": withdraw_id},
    )
    await db.commit()
    return {"ok": True, "status": "rejected"}

# -----------------------------------------------------------------------------
# Админ: отправка EFHC (manual/webhook)
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/send", summary="Отправить EFHC (админ)")
async def admin_send_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminSendRequest = AdminSendRequest(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Отправка EFHC Jetton пользователю:
      • Разрешено только в статусе 'approved'.
      • manual — админ даёт tx_hash в payload.tx_hash.
      • webhook — внешний сервис выполняет перевод и возвращает tx_hash.
      • EFHC уже у Банка — дополнительного списания не делаем.
      • На успешную отправку статус становится 'sent'.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT telegram_id, ton_address, amount_efhc, asset, status
            FROM {settings.DB_SCHEMA_CORE}.withdrawals
            WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    user_id = int(row[0])
    to_addr = row[1]
    amt = d3(Decimal(row[2]))
    asset = row[3]  # всегда 'EFHC'
    st = row[4]

    if st != "approved":
        raise HTTPException(status_code=400, detail=f"Заявка должна быть в статусе 'approved', сейчас: {st}")

    tx_hash: Optional[str] = None
    if payout_service.mode == "manual":
        if not payload.tx_hash:
            raise HTTPException(status_code=400, detail="tx_hash обязателен в manual-режиме")
        tx_hash = payload.tx_hash.strip()
    else:
        # webhook режим: пытаемся отправить EFHC на to_addr
        try:
            tx_hash = await payout_service.send(to_address=to_addr, amount=amt)
        except Exception as e:
            await db.execute(
                text(f"""
                    UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
                    SET status='failed', admin_id=:aid, comment=:cmt, updated_at=NOW()
                    WHERE id=:wid
                """),
                {"aid": admin_id, "cmt": f"send failed: {e}", "wid": withdraw_id},
            )
            await db.commit()
            raise HTTPException(status_code=400, detail=f"Payout failed: {e}")

    # Фиксируем отправку
    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
            SET status='sent', tx_hash=:txh, admin_id=:aid, comment=:cmt,
                sent_at=NOW(), updated_at=NOW()
            WHERE id=:wid
        """),
        {"aid": admin_id, "cmt": payload.comment, "txh": tx_hash, "wid": withdraw_id},
    )
    await db.commit()
    return {"ok": True, "status": "sent", "tx_hash": tx_hash}

# -----------------------------------------------------------------------------
# Админ: пометить как failed (ручная фиксация ошибки)
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/fail", summary="Пометить как failed (админ)")
async def admin_fail_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminWithdrawAction = AdminWithdrawAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Помечает заявку как failed (если при отправке произошла ошибка).
    Обычно используется в webhook-режиме.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"SELECT status FROM {settings.DB_SCHEMA_CORE}.withdrawals WHERE id=:wid"),
        {"wid": withdraw_id},
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    st = row[0]
    if st not in ("approved", "pending"):
        # Не меняем sent/rejected/canceled
        raise HTTPException(status_code=400, detail=f"Нельзя пометить как failed в статусе {st}")

    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
            SET status='failed', admin_id=:aid, comment=:cmt, updated_at=NOW()
            WHERE id=:wid
        """),
        {"aid": admin_id, "cmt": payload.comment, "wid": withdraw_id},
    )
    await db.commit()
    return {"ok": True, "status": "failed"}

# -----------------------------------------------------------------------------
# Важно:
#   • Везде соблюдён принцип «всё EFHC через Банк».
#   • bonus_EFHC не участвуют в выводе.
#   • TON/USDT ВВОД/ВЫВОД отсутствуют в этом модуле (используются только в Shop).
#   • EFHC Jetton отправляется на TON-адрес, но мы не выполняем конвертации и не используем курсы.
# -----------------------------------------------------------------------------
