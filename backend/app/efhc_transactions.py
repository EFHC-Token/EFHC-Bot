"""
Модуль управления транзакциями EFHC для всего бота.

⚡ Основные правила:
1. Все EFHC (обычные и бонусные) существуют только в одной системе — 
   через Банк EFHC (центральный счёт администратора).
2. Пользователи не могут "создавать" или "терять" EFHC вне банка:
   - Начисление EFHC пользователю = списание с банка.
   - Списание EFHC у пользователя = зачисление на банк.
3. Для бонусных EFHC — отдельное поле balances.bonus.
   Эти монеты ограничены и могут тратиться только на панели.
4. Все операции логируются в efhc_transfers_log:
   (from_id, to_id, amount, reason, created_at).
5. Минт и сжигание EFHC возможны только для администратора.
"""

from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import insert

from app.models import Balances, EFHCTransfersLog
from app.config import settings

# 🔹 ID банка EFHC (счёт администратора)
BANK_ID = 362746228

# 🔹 Константа для округления (3 знака после запятой)
DECIMAL_PLACES = Decimal("0.001")


# ==============================
# 🔹 Утилиты
# ==============================

def round_d3(value: Decimal) -> Decimal:
    """
    Универсальная функция округления EFHC/kWh до трёх знаков.
    Используем ROUND_DOWN (всегда вниз).
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(DECIMAL_PLACES, rounding=ROUND_DOWN)


async def log_transfer(db: AsyncSession, from_id: int, to_id: int, amount: Decimal, reason: str):
    """
    Записываем любую транзакцию EFHC в журнал (efhc_transfers_log).

    Аргументы:
        db      — сессия БД
        from_id — отправитель (0 = "система")
        to_id   — получатель (0 = "система")
        amount  — сумма EFHC (округляется до d3)
        reason  — причина (exchange, shop, withdraw, referral, bonus, mint, burn...)
    """
    stmt = insert(EFHCTransfersLog).values(
        from_id=from_id,
        to_id=to_id,
        amount=round_d3(amount),
        reason=reason,
        created_at=datetime.utcnow()
    )
    await db.execute(stmt)
    await db.commit()


# ==============================
# 🔹 Основные операции EFHC
# ==============================

async def credit_user_from_bank(db: AsyncSession, user_id: int, amount: Decimal, reason: str):
    """
    Начисление EFHC пользователю (списывается с банка).
    Используется для:
      - обменника kWh→EFHC
      - выигрышей лотереи
      - ручных начислений админом
    """
    amount = round_d3(amount)

    # Списываем у банка
    bank = await db.get(Balances, BANK_ID)
    bank.efhc -= amount

    # Зачисляем пользователю
    user = await db.get(Balances, user_id)
    user.efhc += amount

    await log_transfer(db, BANK_ID, user_id, amount, reason)


async def debit_user_to_bank(db: AsyncSession, user_id: int, amount: Decimal, reason: str):
    """
    Списание EFHC у пользователя (зачисляется на банк).
    Используется для:
      - покупок в магазине (панели, NFT/VIP)
      - заявок на вывод EFHC
      - покупки билетов лотереи
    """
    amount = round_d3(amount)

    user = await db.get(Balances, user_id)
    if user.efhc < amount:
        raise ValueError("Недостаточно EFHC на счету пользователя")
    user.efhc -= amount

    bank = await db.get(Balances, BANK_ID)
    bank.efhc += amount

    await log_transfer(db, user_id, BANK_ID, amount, reason)


# ==============================
# 🔹 Бонусные EFHC
# ==============================

async def credit_user_bonus_from_bank(db: AsyncSession, user_id: int, amount: Decimal, reason: str):
    """
    Начисление бонусных EFHC пользователю (из банка).
    Используется для:
      - заданий
      - реферальных бонусов
    """
    amount = round_d3(amount)

    bank = await db.get(Balances, BANK_ID)
    bank.efhc -= amount

    user = await db.get(Balances, user_id)
    user.bonus += amount

    await log_transfer(db, BANK_ID, user_id, amount, f"{reason}_bonus")


async def debit_user_bonus_to_bank(db: AsyncSession, user_id: int, amount: Decimal, reason: str):
    """
    Списание бонусных EFHC у пользователя (возврат в банк).
    Используется для:
      - покупки панелей бонусными монетами
    """
    amount = round_d3(amount)

    user = await db.get(Balances, user_id)
    if user.bonus < amount:
        raise ValueError("Недостаточно бонусных EFHC")
    user.bonus -= amount

    bank = await db.get(Balances, BANK_ID)
    bank.efhc += amount

    await log_transfer(db, user_id, BANK_ID, amount, f"{reason}_bonus")


# ==============================
# 🔹 Обменник kWh → EFHC
# ==============================

async def exchange_kwh_to_efhc(db: AsyncSession, user_id: int, kwh_amount: Decimal):
    """
    Обмен kWh на EFHC (1:1).
    - kwh_total (общая генерация) НЕ уменьшается → влияет на рейтинг.
    - kwh_available уменьшается.
    - EFHC начисляются пользователю (списываются с банка).
    """
    kwh_amount = round_d3(kwh_amount)

    user = await db.get(Balances, user_id)
    if user.kwh_available < kwh_amount:
        raise ValueError("Недостаточно kWh для обмена")

    # Списываем kWh
    user.kwh_available -= kwh_amount

    # EFHC: списание у банка → начисление пользователю
    bank = await db.get(Balances, BANK_ID)
    bank.efhc -= kwh_amount
    user.efhc += kwh_amount

    await log_transfer(db, BANK_ID, user_id, kwh_amount, "exchange_kwh_to_efhc")


# ==============================
# 🔹 Mint / Burn (только админ)
# ==============================

async def mint_to_bank(db: AsyncSession, admin_id: int, amount: Decimal, comment: str):
    """
    Минт EFHC (создание монет у банка).
    Только администратор (BANK_ID).
    """
    if admin_id != BANK_ID:
        raise PermissionError("Только админ может минтить EFHC")

    amount = round_d3(amount)

    bank = await db.get(Balances, BANK_ID)
    bank.efhc += amount

    await log_transfer(db, 0, BANK_ID, amount, f"mint:{comment}")


async def burn_from_bank(db: AsyncSession, admin_id: int, amount: Decimal, comment: str):
    """
    Сжигание EFHC (уменьшение монет у банка).
    Только администратор (BANK_ID).
    """
    if admin_id != BANK_ID:
        raise PermissionError("Только админ может сжигать EFHC")

    amount = round_d3(amount)

    bank = await db.get(Balances, BANK_ID)
    if bank.efhc < amount:
        raise ValueError("Недостаточно EFHC на счёте банка для сжигания")

    bank.efhc -= amount

    await log_transfer(db, BANK_ID, 0, amount, f"burn:{comment}")
