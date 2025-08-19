# 📂 backend/app/efhc_transactions.py — Унифицированные транзакции EFHC через Банк
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Реализует базовые операции EFHC (списание/зачисление).
#   • Гарантирует: EFHC всегда идут через Банк (telegram_id = 362746228).
#   • Добавлены операции Минт/Бёрн монет (только админ, только Банк).
#   • Логирование всех минт/бёрн операций в efhc_core.mint_burn_log.
#
# Используется в:
#   • shop_routes.py — покупки EFHC пользователями.
#   • withdraw_routes.py — вывод EFHC.
#   • panels_logic.py — генерация энергии.
#   • referrals.py — бонусы.
#   • admin_routes.py — минт/бёрн EFHC.
# -----------------------------------------------------------------------------

from __future__ import annotations
from decimal import Decimal, ROUND_DOWN
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

# 📌 ID Банка EFHC (админский счёт-эмитент)
BANK_TELEGRAM_ID = 362746228

DEC3 = Decimal("0.001")

def _d3(x: Decimal) -> Decimal:
    """Округление до 3 знаков (EFHC/kWh/bonus)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# Подготовка таблиц (для логов минта/бёрна)
# -----------------------------------------------------------------------------
CREATE_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS efhc_core.mint_burn_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    admin_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,         -- 'MINT' или 'BURN'
    amount NUMERIC(30, 3) NOT NULL,
    comment TEXT
);
"""

async def ensure_logs_table(db: AsyncSession) -> None:
    """Создать таблицу логов минта/бёрна (если нет)."""
    await db.execute(text(CREATE_LOGS_SQL))
    await db.commit()


# -----------------------------------------------------------------------------
# Внутренние утилиты
# -----------------------------------------------------------------------------
async def _ensure_user(db: AsyncSession, telegram_id: int) -> None:
    """Убедиться, что пользователь существует в efhc_core.users и balances."""
    await db.execute(
        text("""INSERT INTO efhc_core.users (telegram_id) VALUES (:tg)
                ON CONFLICT (telegram_id) DO NOTHING"""),
        {"tg": telegram_id},
    )
    await db.execute(
        text("""INSERT INTO efhc_core.balances (telegram_id) VALUES (:tg)
                ON CONFLICT (telegram_id) DO NOTHING"""),
        {"tg": telegram_id},
    )


# -----------------------------------------------------------------------------
# Универсальные операции
# -----------------------------------------------------------------------------
async def transfer_efhc(
    db: AsyncSession,
    from_id: int,
    to_id: int,
    amount: Decimal,
) -> None:
    """
    Перевод EFHC строго между пользователями (включая BANK).
    Нельзя вызвать с amount <= 0.
    Нельзя "сжечь" или "создать" монеты напрямую.
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    amt = _d3(amount)
    await _ensure_user(db, from_id)
    await _ensure_user(db, to_id)

    # Списание у отправителя
    q1 = await db.execute(
        text("""UPDATE efhc_core.balances
                   SET efhc = efhc - :amt
                 WHERE telegram_id = :from_id
                   AND efhc >= :amt"""),
        {"amt": str(amt), "from_id": from_id},
    )
    if q1.rowcount == 0:
        raise ValueError("Insufficient balance on sender account")

    # Зачисление получателю
    await db.execute(
        text("""UPDATE efhc_core.balances
                   SET efhc = efhc + :amt
                 WHERE telegram_id = :to_id"""),
        {"amt": str(amt), "to_id": to_id},
    )


# -----------------------------------------------------------------------------
# Специализированные операции через Банк
# -----------------------------------------------------------------------------
async def credit_user_from_bank(db: AsyncSession, user_id: int, amount: Decimal) -> None:
    """Начислить EFHC пользователю (списываем с BANK)."""
    await transfer_efhc(db, BANK_TELEGRAM_ID, user_id, amount)


async def debit_user_to_bank(db: AsyncSession, user_id: int, amount: Decimal) -> None:
    """Списать EFHC у пользователя (зачисляем в BANK)."""
    await transfer_efhc(db, user_id, BANK_TELEGRAM_ID, amount)


async def debit_bank_for_withdraw(db: AsyncSession, amount: Decimal) -> None:
    """
    Списать EFHC с BANK для реальной выплаты через TON.
    """
    q1 = await db.execute(
        text("""UPDATE efhc_core.balances
                   SET efhc = efhc - :amt
                 WHERE telegram_id = :bank
                   AND efhc >= :amt"""),
        {"amt": str(_d3(amount)), "bank": BANK_TELEGRAM_ID},
    )
    if q1.rowcount == 0:
        raise ValueError("Insufficient EFHC balance in BANK")


# -----------------------------------------------------------------------------
# Минт и Бёрн (только Банк)
# -----------------------------------------------------------------------------
async def mint_efhc(db: AsyncSession, admin_id: int, amount: Decimal, comment: str = "") -> None:
    """
    Минт EFHC: добавить монеты на баланс Банка.
    Используется только админом.
    Логируем в mint_burn_log.
    """
    if amount <= 0:
        raise ValueError("Mint amount must be positive")

    amt = _d3(amount)
    await _ensure_user(db, BANK_TELEGRAM_ID)

    await db.execute(
        text("""UPDATE efhc_core.balances
                   SET efhc = efhc + :amt
                 WHERE telegram_id = :bank"""),
        {"amt": str(amt), "bank": BANK_TELEGRAM_ID},
    )

    await db.execute(
        text("""INSERT INTO efhc_core.mint_burn_log (admin_id, action_type, amount, comment)
                VALUES (:admin_id, 'MINT', :amt, :comment)"""),
        {"admin_id": admin_id, "amt": str(amt), "comment": comment},
    )


async def burn_efhc(db: AsyncSession, admin_id: int, amount: Decimal, comment: str = "") -> None:
    """
    Бёрн EFHC: сжечь монеты с баланса Банка.
    Используется только админом.
    Логируем в mint_burn_log.
    """
    if amount <= 0:
        raise ValueError("Burn amount must be positive")

    amt = _d3(amount)
    await _ensure_user(db, BANK_TELEGRAM_ID)

    q1 = await db.execute(
        text("""UPDATE efhc_core.balances
                   SET efhc = efhc - :amt
                 WHERE telegram_id = :bank
                   AND efhc >= :amt"""),
        {"amt": str(amt), "bank": BANK_TELEGRAM_ID},
    )
    if q1.rowcount == 0:
        raise ValueError("Insufficient EFHC balance in BANK for burn")

    await db.execute(
        text("""INSERT INTO efhc_core.mint_burn_log (admin_id, action_type, amount, comment)
                VALUES (:admin_id, 'BURN', :amt, :comment)"""),
        {"admin_id": admin_id, "amt": str(amt), "comment": comment},
    )
