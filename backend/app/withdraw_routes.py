# 📂 backend/app/withdraw_routes.py — вывод EFHC с TON-интеграцией (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Пользовательский вывод EFHC на TON-кошелёк (выплаты в TON/USDT по настройке).
#   • Админ-панель: просмотр, approve/reject, отправка (manual/auto), статусы.
#   • Все списания EFHC у пользователя → на Банк (ID=362746228).
#   • При reject — возврат EFHC пользователю из Банка (в полном объёме).
#   • Логирование всех движений EFHC выполняется в efhc_transactions.py (efhc_transfers_log).
#
# Бизнес-правила:
#   • EFHC — внутренняя единица, 1 EFHC = 1 kWh (по правилам проекта).
#   • КУРСЫ НЕ НУЖНЫ (по требованию). Для вывода считаем "1 EFHC = 1 TON" (или USDT),
#     если включён авто-режим выплат. Значение можно уточнить отдельно.
#   • bonus_EFHC НЕЛЬЗЯ выводить (они тратятся только на покупку панелей).
#   • При создании заявки на вывод мы сразу "блокируем" EFHC:
#       - фактически переводи́м их user → Банк (debit_user_to_bank), статус=Pending.
#       - Это предотвращает двойное расходование до одобрения.
#   • При Reject — возвращаем EFHC (Банк → user).
#   • При Approve — фиксируем одобрение (ничего по EFHC уже не делаем — они у Банка).
#   • При Send — выполняем реальную выплату (manual/auto-мод), сохраняем tx_hash, статус=Sent.
#
# Таблица efhc_core.withdrawals:
#   • id BIGSERIAL PK
#   • telegram_id BIGINT NOT NULL — кто вывел
#   • ton_address TEXT NOT NULL — адрес кошелька получателя
#   • amount_efhc NUMERIC(30,3) NOT NULL — сумма EFHC
#   • asset TEXT NOT NULL DEFAULT 'TON' — какой актив отправляем (TON/USDT)
#   • status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','sent','failed','canceled'))
#   • idempotency_key TEXT UNIQUE NULL — чтобы не создавать дубликаты
#   • tx_hash TEXT NULL — хэш транзакции (после отправки)
#   • admin_id BIGINT NULL — кто одобрил/отправил
#   • comment TEXT NULL — комментарии админа
#   • created_at TIMESTAMPTZ DEFAULT now()
#   • approved_at TIMESTAMPTZ NULL
#   • sent_at TIMESTAMPTZ NULL
#   • updated_at TIMESTAMPTZ DEFAULT now()
#
# Зависимости:
#   • database.py — get_session, engine и прочее.
#   • config.py — get_settings() (TON-кошелёк, payout режимы).
#   • models.py — Balance, User (ORM), но здесь мы активно используем raw SQL для таблицы withdrawals.
#   • efhc_transactions.py — операции EFHC через Банк (debit/credit), логирование.
#   • admin_routes.py — отдельный модуль админки (уже подключён).
#
# Важные настройки (config.py):
#   • ADMIN_TELEGRAM_ID — супер-админ.
#   • BANK_TELEGRAM_ID — 362746228 (жёстко зашит и в efhc_transactions).
#   • DB_SCHEMA_CORE — 'efhc_core' (по умолчанию).
#   • WITHDRAW_MIN_EFHC, WITHDRAW_MAX_EFHC — лимиты (можно задать в .env).
#   • TON_PAYOUT_MODE — 'manual' (по умолчанию) | 'webhook'
#   • TON_PAYOUT_WEBHOOK_URL — если 'webhook', URL для внешнего сервиса выплат.
#   • TON_PAYOUT_API_KEY — API-ключ для вебхука (если требуется).
#
# Интеграция:
#   • Подключить роутер в main.py:
#         from .withdraw_routes import router as withdraw_router
#         app.include_router(withdraw_router, prefix="/api")
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
    Округляет Decimal до 3 знаков после запятой вниз.
    Применяем для EFHC значений.
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
    asset TEXT NOT NULL DEFAULT 'TON',
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
    Создаёт таблицу withdrawals при необходимости (efhc_core.withdrawals).
    """
    await db.execute(text(WITHDRAWALS_CREATE_SQL.format(schema=settings.DB_SCHEMA_CORE)))
    await db.commit()

# -----------------------------------------------------------------------------
# Валидация TON-адреса (простая)
# -----------------------------------------------------------------------------
TON_ADDR_RE = re.compile(r"^[EU][QqA-Za-z0-9_-]{46,66}$")

def validate_ton_address(addr: str) -> bool:
    """
    Простая проверка TON-адреса.
    Разрешаются стандартные форматы base64url (EQ..., UQ...) и длина 48-66 символов.
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
      • ton_address — адрес получателя,
      • amount — сумма EFHC для вывода (не bonus_EFHC),
      • (опц.) asset — 'TON' (по умолчанию) или 'USDT' (Jetton), если поддерживается.
      • (опц.) idempotency_key — для защиты от дубликатов.
      • (опц.) telegram_id — в форме фронта есть поле; сверяем с заголовком.
    """
    ton_address: str = Field(..., description="TON-адрес получателя")
    amount: condecimal(gt=0, max_digits=30, decimal_places=3) = Field(..., description="Сумма EFHC для вывода")
    asset: Optional[str] = Field("TON", description="Актив выплаты: 'TON' или 'USDT'")
    idempotency_key: Optional[str] = Field(None, description="Ключ идемпотентности")
    telegram_id: Optional[int] = Field(None, description="Telegram ID (из формы)")

class WithdrawItem(BaseModel):
    """
    Элемент списка выводов для пользователя/админа.
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
    Действия админа:
      • approve — подтверждение (ничего не списываем — уже списано при создании),
      • reject — отказ (возврат EFHC пользователю),
      • send — отправка выплаты (manual/auto).
    """
    comment: Optional[str] = Field(None, description="Комментарий администратора")

class AdminSendRequest(AdminWithdrawAction):
    """
    Отправка выплаты:
      • manual mode: нужно передать tx_hash,
      • webhook mode: можно не указывать tx_hash — он вернётся из внешнего сервиса.
    """
    tx_hash: Optional[str] = Field(None, description="Хэш транзакции (для manual режима)")

# -----------------------------------------------------------------------------
# Сервис выплат (manual/webhook)
# -----------------------------------------------------------------------------
class TonPayoutService:
    """
    Абстракция сервиса выплат:
      • manual — админ сам вручную выплачивает и указывает tx_hash.
      • webhook — вызываем внешний сервис (settings.TON_PAYOUT_WEBHOOK_URL),
                  который выполняет перевод и возвращает tx_hash.
    """
    def __init__(self, mode: str, webhook_url: Optional[str], api_key: Optional[str]):
        self.mode = (mode or "manual").lower().strip()
        self.webhook_url = webhook_url
        self.api_key = api_key

    async def send(self, asset: str, to_address: str, amount: Decimal) -> str:
        """
        Выполняет отправку (или имитирует) и возвращает tx_hash.
        Если mode=manual — вызывать нельзя (вернём исключение).
        """
        if self.mode == "manual":
            raise RuntimeError("Payout mode is MANUAL — tx_hash must be provided by admin")

        # Webhook-режим
        if not self.webhook_url:
            raise RuntimeError("TON_PAYOUT_WEBHOOK_URL is not configured")

        payload = {
            "asset": asset,
            "to_address": to_address,
            "amount": str(d3(amount)),
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(self.webhook_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()

        # Ожидаем { ok: true, tx_hash: "..." }
        if not data or not data.get("ok"):
            raise RuntimeError(f"Payout webhook error: {data}")

        txh = data.get("tx_hash")
        if not txh:
            raise RuntimeError("Payout webhook did not return tx_hash")
        return txh

payout_service = TonPayoutService(
    mode=(settings.TON_PAYOUT_MODE or "manual"),
    webhook_url=settings.TON_PAYOUT_WEBHOOK_URL,
    api_key=settings.TON_PAYOUT_API_KEY,
)

# -----------------------------------------------------------------------------
# Авторизация: проверка для пользователя и админа
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
    Проверяет, что X-Telegram-Id — админ (супер-админ или Банк).
    NFT-админ в withdraw (управление выплатами) по умолчанию не используется,
    при необходимости можно расширить.
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
# Пользователь: создать заявку на вывод
# -----------------------------------------------------------------------------
@router.post("/withdraw", summary="Создать заявку на вывод EFHC")
async def create_withdraw(
    payload: CreateWithdrawRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Создаёт заявку на вывод EFHC:
      • Валидация входных данных.
      • Проверка лимитов.
      • Списание EFHC с пользователя в Банк (блокировка средств) — debit_user_to_bank(user → Банк).
      • Создание записи в efhc_core.withdrawals со статусом 'pending'.
      • Idempotency: если передан idempotency_key и он существует — вернём существующий объект.
    """
    await ensure_withdrawals_table(db)

    user_id = await require_user(x_telegram_id)

    # Если фронт передал telegram_id в форме — проверим, что совпадает (защита от ошибок)
    if payload.telegram_id is not None and int(payload.telegram_id) != user_id:
        raise HTTPException(status_code=400, detail="Telegram ID mismatch")

    # Простая валидация TON адреса
    if not validate_ton_address(payload.ton_address):
        raise HTTPException(status_code=400, detail="Некорректный TON-адрес")

    # Лимиты
    min_w = d3(Decimal(getattr(settings, "WITHDRAW_MIN_EFHC", "1.000")))
    max_w = d3(Decimal(getattr(settings, "WITHDRAW_MAX_EFHC", "1000000.000")))
    amount = d3(Decimal(payload.amount))
    if amount < min_w:
        raise HTTPException(status_code=400, detail=f"Минимальная сумма вывода: {min_w}")
    if amount > max_w:
        raise HTTPException(status_code=400, detail=f"Максимальная сумма вывода: {max_w}")

    asset = (payload.asset or "TON").upper().strip()
    if asset not in ("TON", "USDT"):
        raise HTTPException(status_code=400, detail="Asset must be 'TON' or 'USDT'")

    # Идемпотентность: если уже есть заявка с таким ключом (и не отменённая),
    # возвращаем её (не списываем второй раз).
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

    # Проверим баланс EFHC (именно EFHC, не bonus_EFHC)
    q = await db.execute(select(Balance).where(Balance.telegram_id == user_id))
    bal = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=400, detail="Баланс не найден, невозможно создать вывод")

    cur_efhc = d3(Decimal(bal.efhc or 0))
    if cur_efhc < amount:
        raise HTTPException(status_code=400, detail="Недостаточно EFHC для вывода")

    # Списываем EFHC user → Банк (блокируем средства)
    try:
        await debit_user_to_bank(db, user_id=user_id, amount=amount)
    except HTTPException as he:
        # Пробросим корректно
        raise he
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to lock EFHC for withdraw: {e}")

    # Создаём запись в withdrawals: status=pending
    try:
        q = await db.execute(
            text(f"""
                INSERT INTO {settings.DB_SCHEMA_CORE}.withdrawals
                (telegram_id, ton_address, amount_efhc, asset, status, idempotency_key, created_at)
                VALUES (:tg, :addr, :amt, :asset, 'pending', :ikey, NOW())
                RETURNING id
            """),
            {
                "tg": user_id,
                "addr": payload.ton_address.strip(),
                "amt": str(amount),
                "asset": asset,
                "ikey": payload.idempotency_key,
            },
        )
        wid = q.scalar_one()
        await db.commit()
    except Exception as e:
        await db.rollback()
        # Если запись не создалась — вернём средства пользователю (отката нет автоматом).
        try:
            await credit_user_from_bank(db, user_id=user_id, amount=amount)
        except Exception as e2:
            # Критическая ситуация: средства "застряли" в Банке, но заявки нет.
            # Не скрываем ошибку, пусть админ разберётся; но мы попробовали.
            raise HTTPException(status_code=500, detail=f"Withdraw create failed; refund failed too: {e} / {e2}")

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
    Возвращает последние N выводов пользователя (включая pending, sent и т.п.).
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
    Возвращает список заявок с фильтрами: статус и/или user_id.
    Для админ-панели.
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
    Возвращает детали одной заявки.
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
      • Если статус != pending, вернём ошибку.
      • EFHC уже на Банке с момента создания (блокировка).
      • Фиксируем статус=approved; approved_at=NOW(); admin_id.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT status FROM {settings.DB_SCHEMA_CORE}.withdrawals WHERE id = :wid
        """),
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
# Админ: reject заявки
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
      • Возвращаем EFHC пользователю: Банк → user (на ту же сумму).
      • Ставим статус='rejected', проставляем admin_id, comment.
    """
    admin_id = await require_admin(db, x_telegram_id)
    await ensure_withdrawals_table(db)

    q = await db.execute(
        text(f"""
            SELECT telegram_id, amount_efhc, status
            FROM {settings.DB_SCHEMA_CORE}.withdrawals WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    row = q.first()
    if not row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    user_id, amt, st = int(row[0]), d3(Decimal(row[1])), row[2]

    if st not in ("pending", "approved"):
        raise HTTPException(status_code=400, detail=f"Нельзя отклонить заявку в статусе {st}")

    # Возврат EFHC пользователю (из Банка)
    try:
        await credit_user_from_bank(db, user_id=user_id, amount=amt)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Возврат EFHC пользователю не удался: {e}")

    # Обновляем статус
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
# Админ: отправка выплаты (manual/webhook)
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/send", summary="Отправить выплату (админ)")
async def admin_send_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminSendRequest = AdminSendRequest(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Отправка выплаты:
      • Допустимые статусы: 'approved' (одобрена, но не отправлена).
      • manual: admin обязан указать tx_hash в payload.tx_hash.
      • webhook: вызываем внешний сервис, который вернёт tx_hash.
      • На EFHC это не влияет (они уже у Банка).
      • Статус меняется на 'sent' при успехе.
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
    user_id, to_addr, amt, asset, st = int(row[0]), row[1], d3(Decimal(row[2])), row[3], row[4]

    if st != "approved":
        raise HTTPException(status_code=400, detail=f"Заявка должна быть в статусе 'approved', сейчас: {st}")

    tx_hash: Optional[str] = None
    # В зависимости от режима выплат:
    if payout_service.mode == "manual":
        # tx_hash должен дать админ
        if not payload.tx_hash:
            raise HTTPException(status_code=400, detail="tx_hash обязателен в manual-режиме")
        tx_hash = payload.tx_hash.strip()
    else:
        # webhook-режим: попробуем отправить
        try:
            tx_hash = await payout_service.send(asset=asset, to_address=to_addr, amount=amt)
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

    # Обновляем заявку → sent
    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.withdrawals
            SET status='sent', tx_hash=:txh, admin_id=:aid, comment=:cmt, sent_at=NOW(), updated_at=NOW()
            WHERE id=:wid
        """),
        {"aid": admin_id, "cmt": payload.comment, "txh": tx_hash, "wid": withdraw_id},
    )
    await db.commit()
    return {"ok": True, "status": "sent", "tx_hash": tx_hash}

# -----------------------------------------------------------------------------
# Админ: пометить как failed (в ручном режиме фиксации ошибок)
# -----------------------------------------------------------------------------
@router.post("/admin/withdraws/{withdraw_id}/fail", summary="Пометить выплату как failed (админ)")
async def admin_fail_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminWithdrawAction = AdminWithdrawAction(),
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, alias="X-Telegram-Id"),
):
    """
    Помечает заявку как failed (если возникли проблемы при отправке).
    Обычно используется при webhook-режиме.
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
        # Разумно ограничить — не меняем sent/rejected
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
#   • bonus_EFHC не используется в выводе вообще.
#   • Обменник/Shop/Панели/Лотереи/Рефералы должны вызывать операции из efhc_transactions.
# -----------------------------------------------------------------------------
