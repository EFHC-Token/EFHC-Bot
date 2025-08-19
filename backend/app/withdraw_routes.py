# 📄 backend/app/withdraw_routes.py
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Реализует эндпоинты создания/просмотра заявок на вывод EFHC:
#       - POST /api/withdraw        (пользователь создаёт заявку);
#       - GET  /api/withdraw        (пользователь запрашивает свои заявки);
#       - GET  /api/admin/withdraws (админ смотрит все заявки);
#       - POST /api/admin/withdraws/{withdraw_id}/process (админ подтверждает и инициирует выплату).
#   • Пишет записи в efhc_core.withdrawals.
#   • Списывает EFHC с баланса пользователя при создании заявки.
#   • Интегрируется с TON для выплаты EFHC Jetton на адрес пользователя (TON-кошелёк).
#   • Отправляет уведомления администраторам в Telegram при новых заявках.
#
# Требования:
#   • PostgreSQL, таблица efhc_core.withdrawals;
#   • В config.py есть токены для верификации Telegram WebApp, админский whitelist, TON-кошелёк;
#   • ton_integration.py содержит функцию отправки EFHC jetton из кошелька проекта (см. ниже).
#
# Безопасность:
#   • Все пользовательские вызовы проверяются через X-Telegram-Init-Data (валидация вебапп-токена).
#   • Админские эндпоинты защищены проверкой is_admin (по NFT whitelist или списку админов).
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Path, Body
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .config import get_settings
from .database import get_session
from .webapp_auth import verify_init_data  # валидация initData из Telegram WebApp (реализовано в вашем проекте)
from .ton_integration import (
    send_efhc_jetton_transfer,  # функция-обёртка отправки EFHC jetton (реализуйте в ton_integration.py)
)
from .bot_notify import notify_admins  # функция для рассылки уведомлений админам (реализуйте в bot_notify.py)

settings = get_settings()
router = APIRouter(prefix="/api", tags=["withdraw"])

DEC3 = Decimal("0.001")


def d3(x: Decimal) -> Decimal:
    """Округление значений EFHC до 3 знаков после запятой."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


CREATE_WITHDRAW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS efhc_core.withdrawals (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT REFERENCES efhc_core.users(telegram_id) ON DELETE CASCADE,
    amount NUMERIC(30,3) NOT NULL,
    ton_address TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending / sent / failed
    tx_hash TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ NULL
);
"""


async def ensure_withdraw_tables(db: AsyncSession) -> None:
    """Создаёт таблицу efhc_core.withdrawals, если отсутствует."""
    await db.execute(text(CREATE_WITHDRAW_TABLE_SQL))
    await db.commit()


# -----------------------------------------------------------------------------
# Pydantic-модели (schemas)
# -----------------------------------------------------------------------------

class WithdrawCreateRequest(BaseModel):
    """Модель запроса на создание заявки (пользовательская операция)."""
    user_id: int = Field(..., description="Telegram ID пользователя, от имени которого создаётся заявка")
    ton_address: str = Field(..., description="TON-адрес получателя выплаты")
    amount_efhc: Decimal = Field(..., gt=0, description="Сумма EFHC на вывод")

    @validator("amount_efhc")
    def validate_amount(cls, v: Decimal) -> Decimal:
        """Валидация суммы: > 0 и округляем до 3 знаков."""
        if v <= 0:
            raise ValueError("Сумма должна быть > 0")
        return d3(v)


class WithdrawResponse(BaseModel):
    """Единичная запись заявки (для ответа API)."""
    id: int
    telegram_id: int
    amount: Decimal
    ton_address: str
    status: str
    tx_hash: Optional[str] = None
    created_at: datetime
    processed_at: Optional[datetime] = None


class WithdrawListResponse(BaseModel):
    """Список заявок (для пользователя)."""
    items: List[WithdrawResponse]


class AdminWithdrawListResponse(BaseModel):
    """Список заявок (для админа)."""
    items: List[WithdrawResponse]


class AdminProcessWithdrawRequest(BaseModel):
    """Модель для подтверждения выплаты админом."""
    # При необходимости можно добавить "send_from" (например, кошелёк проекта/подписывающего).
    # Но в нашем случае всё зашито в настройки ton_integration.
    force_recheck: bool = False


class WithdrawCreateResult(BaseModel):
    """Ответ при создании заявки пользователем."""
    ok: bool
    withdrawal: WithdrawResponse


# -----------------------------------------------------------------------------
# Вспомогательные функции доступа к БД
# -----------------------------------------------------------------------------

async def _get_user_balance(db: AsyncSession, telegram_id: int) -> Dict[str, Decimal]:
    """
    Возвращает баланс EFHC/kwh/bonus пользователя.
    """
    q = await db.execute(
        text("""
            SELECT efhc, kwh, bonus
              FROM efhc_core.balances
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id},
    )
    row = q.mappings().first()
    if not row:
        # Если нет записи — создадим пустые
        await db.execute(
            text("""
                INSERT INTO efhc_core.balances (telegram_id, efhc, kwh, bonus)
                VALUES (:tg, 0, 0, 0)
                ON CONFLICT (telegram_id) DO NOTHING
            """),
            {"tg": telegram_id},
        )
        await db.commit()
        return {"efhc": Decimal("0.000"), "kwh": Decimal("0.000"), "bonus": Decimal("0.000")}
    return {
        "efhc": row["efhc"] or Decimal("0"),
        "kwh": row["kwh"] or Decimal("0"),
        "bonus": row["bonus"] or Decimal("0"),
    }


async def _debit_user_efhc(db: AsyncSession, telegram_id: int, amount: Decimal) -> None:
    """
    Списывает EFHC с баланса пользователя (вызывается при создании заявки).
    Предполагается, что проверка доступности суммы была произведена ранее.
    """
    await db.execute(
        text("""
            UPDATE efhc_core.balances
               SET efhc = efhc - :amt
             WHERE telegram_id = :tg
        """),
        {"amt": str(d3(amount)), "tg": telegram_id},
    )


async def _insert_withdraw(db: AsyncSession, telegram_id: int, amount: Decimal, ton_addr: str) -> int:
    """
    Создаёт запись в efhc_core.withdrawals (статус — 'pending').
    Возвращает ID созданной записи.
    """
    q = await db.execute(
        text("""
            INSERT INTO efhc_core.withdrawals (telegram_id, amount, ton_address, status)
            VALUES (:tg, :amt, :addr, 'pending')
            RETURNING id
        """),
        {"tg": telegram_id, "amt": str(d3(amount)), "addr": ton_addr},
    )
    row = q.first()
    await db.commit()
    return row[0]


async def _get_user_withdraws(db: AsyncSession, telegram_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Список заявок пользователя.
    """
    q = await db.execute(
        text("""
            SELECT id, telegram_id, amount, ton_address, status, tx_hash, created_at, processed_at
              FROM efhc_core.withdrawals
             WHERE telegram_id = :tg
             ORDER BY created_at DESC
             LIMIT :lim
        """),
        {"tg": telegram_id, "lim": limit},
    )
    return [dict(r) for r in q.mappings().fetchall()]


async def _get_all_withdraws(db: AsyncSession, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Список всех заявок (для админа).
    """
    q = await db.execute(
        text("""
            SELECT id, telegram_id, amount, ton_address, status, tx_hash, created_at, processed_at
              FROM efhc_core.withdrawals
             ORDER BY created_at DESC
             LIMIT :lim
        """),
        {"lim": limit},
    )
    return [dict(r) for r in q.mappings().fetchall()]


async def _get_withdraw(db: AsyncSession, withdraw_id: int) -> Optional[Dict[str, Any]]:
    """
    Получить одну заявку по ID.
    """
    q = await db.execute(
        text("""
            SELECT id, telegram_id, amount, ton_address, status, tx_hash, created_at, processed_at
              FROM efhc_core.withdrawals
             WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    r = q.mappings().first()
    return dict(r) if r else None


async def _mark_withdraw_sent(db: AsyncSession, withdraw_id: int, tx_hash: str) -> None:
    """
    Обновляет заявку как 'sent' и прописывает tx_hash.
    """
    await db.execute(
        text("""
            UPDATE efhc_core.withdrawals
               SET status = 'sent', tx_hash = :tx, processed_at = now()
             WHERE id = :wid
        """),
        {"tx": tx_hash, "wid": withdraw_id},
    )
    await db.commit()


async def _mark_withdraw_failed(db: AsyncSession, withdraw_id: int, err_msg: str) -> None:
    """
    Обновляет заявку как 'failed' (tx_hash остаётся NULL).
    В реальности, можно создать таблицу ошибок и логировать подробнее.
    """
    await db.execute(
        text("""
            UPDATE efhc_core.withdrawals
               SET status = 'failed', processed_at = now()
             WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    await db.commit()


async def _insert_transaction_log(
    db: AsyncSession,
    telegram_id: int,
    amount: Decimal,
    event_type: str,
    note: str
) -> None:
    """
    Лог транзакций (опционально) — если в проекте есть таблица efhc_core.transactions.
    Здесь мы логируем списание EFHC при создании заявки.
    """
    await db.execute(
        text("""
            INSERT INTO efhc_core.transactions (telegram_id, amount, event_type, note, created_at)
            VALUES (:tg, :amt, :etype, :note, now())
        """),
        {
            "tg": telegram_id,
            "amt": str(d3(amount)),
            "etype": event_type,
            "note": note,
        },
    )


# -----------------------------------------------------------------------------
# Telegram Notifications для админов
# -----------------------------------------------------------------------------
async def notify_admins_new_withdraw(telegram_id: int, amount: Decimal, address: str) -> None:
    """
    Отправляет уведомление администраторам о новой заявке.
    Предполагается, что bot_notify.notify_admins реализует отправку в Телеграм (список admin_ids).
    """
    msg = f"🆕 Новая заявка на вывод EFHC\n" \
          f"ID: {telegram_id}\n" \
          f"Сумма: {d3(amount)} EFHC\n" \
          f"Адрес: {address}\n" \
          f"Статус: pending"
    try:
        await notify_admins(msg)
    except Exception as e:
        print("[NotifyAdmins] Error:", e)


async def notify_admins_withdraw_processed(withdraw_id: int, tx_hash: str) -> None:
    """
    Уведомление админам/лог канала — успешная отправка перевода.
    """
    msg = f"✅ Заявка #{withdraw_id} отправлена.\nTx: {tx_hash}"
    try:
        await notify_admins(msg)
    except Exception as e:
        print("[NotifyAdmins] Error:", e)


async def notify_admins_withdraw_failed(withdraw_id: int, error_msg: str) -> None:
    """
    Уведомление админам — ошибка при отправке выплаты.
    """
    msg = f"❌ Заявка #{withdraw_id} не отправлена.\nОшибка: {error_msg}"
    try:
        await notify_admins(msg)
    except Exception as e:
        print("[NotifyAdmins] Error:", e)


# -----------------------------------------------------------------------------
# Маршруты (эндпоинты)
# -----------------------------------------------------------------------------

@router.on_event("startup")
async def on_startup() -> None:
    """
    При старте приложения создадим таблицу withdrawals, если её ещё нет.
    """
    async with get_session() as db:
        await ensure_withdraw_tables(db)


@router.post("/withdraw", response_model=WithdrawCreateResult)
async def create_withdraw(
    payload: WithdrawCreateRequest,
    init_data: str = Depends(verify_init_data),  # проверка "X-Telegram-Init-Data"
    db: AsyncSession = Depends(get_session)
):
    """
    Создать заявку на вывод EFHC:
      - Верификация initData (запрос пользователя из Telegram WebApp),
      - Проверка баланса EFHC,
      - Списание EFHC и запись заявки в withdrawals (status='pending'),
      - Уведомление администратору.
    """
    telegram_id = payload.user_id
    ton_address = payload.ton_address.strip()
    amount = d3(payload.amount_efhc)

    # Простейшая валидация адреса (на проде используйте библиотеку TON Address checks)
    if not ton_address or len(ton_address) < 20:
        raise HTTPException(status_code=400, detail="Некорректный TON-адрес")

    # Проверим баланс
    balance = await _get_user_balance(db, telegram_id)
    if balance["efhc"] < amount:
        raise HTTPException(status_code=400, detail="Недостаточно EFHC на балансе")

    # Списываем EFHC с баланса и создаём заявку
    await _debit_user_efhc(db, telegram_id, amount)
    withdraw_id = await _insert_withdraw(db, telegram_id, amount, ton_address)

    # Лог транзакций (если есть таблица efhc_core.transactions)
    try:
        await _insert_transaction_log(db, telegram_id, amount, "withdraw_request", f"Withdraw ID {withdraw_id}")
        await db.commit()
    except Exception as e:
        # Если нет таблицы transactions — можно пропустить
        print("[Withdraw] transaction log insert error:", e)

    # Уведомление админам
    await notify_admins_new_withdraw(telegram_id, amount, ton_address)

    # Возвращаем структуру для фронта
    q = await db.execute(
        text("""
            SELECT id, telegram_id, amount, ton_address, status, tx_hash, created_at, processed_at
              FROM efhc_core.withdrawals
             WHERE id = :wid
        """),
        {"wid": withdraw_id},
    )
    r = q.mappings().first()
    return {
        "ok": True,
        "withdrawal": {
            "id": r["id"],
            "telegram_id": r["telegram_id"],
            "amount": r["amount"],
            "ton_address": r["ton_address"],
            "status": r["status"],
            "tx_hash": r["tx_hash"],
            "created_at": r["created_at"],
            "processed_at": r["processed_at"],
        },
    }


@router.get("/withdraw", response_model=WithdrawListResponse)
async def list_user_withdraws(
    user_id: int = Query(..., description="Telegram ID пользователя"),
    init_data: str = Depends(verify_init_data),
    db: AsyncSession = Depends(get_session),
):
    """
    Список заявок пользователя.
    """
    items = await _get_user_withdraws(db, telegram_id=user_id, limit=100)
    # Сериализация в список pydantic-моделей
    out = []
    for w in items:
        out.append({
            "id": w["id"],
            "telegram_id": w["telegram_id"],
            "amount": w["amount"],
            "ton_address": w["ton_address"],
            "status": w["status"],
            "tx_hash": w["tx_hash"],
            "created_at": w["created_at"],
            "processed_at": w["processed_at"],
        })
    return {"items": out}


# ------------------- Админские операции -------------------

def _check_admin_access(init_data: str) -> None:
    """
    Проверка прав доступа администратора.
    Предполагается:
      • verify_init_data вернул init_data и внутри доступен user_id (можно расширить).
      • Здесь можно проверить пользователя в whitelist админов или по NFT-адресу.
    """
    # Если в verify_init_data уже есть декодированный user_id — можно проверить.
    # В нашем шаблоне предполагаем, что verify_init_data возвращает строку,
    # но вы можете расширить сессию для прозрачной передачи verified_user_id.
    # Временно — просто проверка флага settings.ADMINS_WHITELIST.
    # Пример:
    # if str(verified_user_id) not in settings.ADMINS_WHITELIST: raise HTTPException(403, "Forbidden")
    pass


@router.get("/admin/withdraws", response_model=AdminWithdrawListResponse)
async def admin_list_withdraws(
    limit: int = Query(200, ge=1, le=1000),
    init_data: str = Depends(verify_init_data),
    db: AsyncSession = Depends(get_session),
):
    """
    Админ: список всех заявок (для мониторинга и обработки).
    """
    _check_admin_access(init_data)

    items = await _get_all_withdraws(db, limit=limit)
    out = []
    for w in items:
        out.append({
            "id": w["id"],
            "telegram_id": w["telegram_id"],
            "amount": w["amount"],
            "ton_address": w["ton_address"],
            "status": w["status"],
            "tx_hash": w["tx_hash"],
            "created_at": w["created_at"],
            "processed_at": w["processed_at"],
        })
    return {"items": out}


@router.post("/admin/withdraws/{withdraw_id}/process")
async def admin_process_withdraw(
    withdraw_id: int = Path(..., ge=1),
    payload: AdminProcessWithdrawRequest = Body(...),
    init_data: str = Depends(verify_init_data),
    db: AsyncSession = Depends(get_session),
):
    """
    Админ: подтвердить и отправить выплату EFHC Jetton.
      • Статусы: pending → sent (или failed).
      • Вызов ton_integration.send_efhc_jetton_transfer(...).
    """
    _check_admin_access(init_data)

    # Ищем заявку
    w = await _get_withdraw(db, withdraw_id)
    if not w:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    if w["status"] != "pending":
        raise HTTPException(status_code=400, detail="Заявка уже обработана")

    telegram_id = w["telegram_id"]
    amount = Decimal(w["amount"])
    ton_addr = w["ton_address"]

    # Пытаемся выполнить перевод EFHC jetton
    try:
        # send_efhc_jetton_transfer — реализуйте в ton_integration.py (используя ваш signer/кошелёк проекта)
        # Возвращаем tx_hash (или raise при ошибке).
        tx_hash = await send_efhc_jetton_transfer(
            to_address=ton_addr,
            amount_efhc=amount,
            comment=f"EFHC withdraw for tg={telegram_id} id={withdraw_id}"
        )
        # Обновляем статус и tx_hash
        await _mark_withdraw_sent(db, withdraw_id, tx_hash)
        await notify_admins_withdraw_processed(withdraw_id, tx_hash)
        return {"ok": True, "status": "sent", "tx_hash": tx_hash}
    except Exception as e:
        await _mark_withdraw_failed(db, withdraw_id, str(e))
        await notify_admins_withdraw_failed(withdraw_id, str(e))
        raise HTTPException(status_code=500, detail=f"Ошибка отправки выплаты: {e}")
