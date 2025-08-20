# 📂 backend/app/efhc_transactions.py — модуль транзакций EFHC
# -----------------------------------------------------------------------------
# Назначение:
#   • Единый интерфейс для всех операций с балансами EFHC пользователей и банка.
#   • Обеспечивает атомарность, идемпотентность (через idempotency_key), аудит (EFHCTransfersLog).
#   • Используется во всех ручках (shop_routes, withdraw_routes, exchange_routes, admin_routes).
#
# Бизнес-правила (резюме из последних сообщений):
#   • EFHC и kWh = NUMERIC(30,8), 8 знаков после точки (Decimal).
#   • Все EFHC-операции (списания, начисления) идут через EFHCTransfersLog.
#   • Банк EFHC — Telegram ID из AdminBankConfig.current_bank_telegram_id.
#   • Бонусные EFHC есть только у пользователей; при трате возвращаются в Банк (reason='shop_panel_bonus').
#   • Покупка панелей:
#       - сначала списываем bonus_efhc → Банк,
#       - затем недостающую часть efhc → Банк.
#   • Вывод EFHC:
#       - при создании заявки EFHC списываются user → Банк (reason='withdraw_lock').
#       - при отмене — возврат Банк → user (reason='withdraw_refund').
#   • Функции debit_user_to_bank / credit_user_from_bank / transfer_between_users — общий API.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, update

from .models import Balance, EFHCTransfersLog, AdminBankConfig

# -----------------------------------------------------------------------------
# Вспомогательные константы
# -----------------------------------------------------------------------------
DECIMAL_CTX = Decimal("0.00000001")  # 8 знаков после точки


# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------
def quantize_amount(amount: Decimal | str | float) -> Decimal:
    """
    Округляет сумму до 8 знаков после запятой.
    Используем для всех операций EFHC/kWh.
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(DECIMAL_CTX, rounding=ROUND_DOWN)


def get_current_bank_id(db: Session) -> int:
    """
    Возвращает актуальный Telegram ID банка (админ-счёт EFHC).
    """
    cfg = db.execute(select(AdminBankConfig).order_by(AdminBankConfig.updated_at.desc())).scalar_one_or_none()
    if not cfg:
        raise RuntimeError("Admin bank config not initialized")
    return cfg.current_bank_telegram_id


def log_transfer(
    db: Session,
    from_id: int,
    to_id: int,
    amount: Decimal,
    reason: str,
    idempotency_key: Optional[str] = None,
    meta: Optional[dict] = None,
) -> EFHCTransfersLog:
    """
    Создаёт запись в логе EFHCTransfersLog.
    """
    entry = EFHCTransfersLog(
        from_id=from_id,
        to_id=to_id,
        amount=quantize_amount(amount),
        reason=reason,
        idempotency_key=idempotency_key,
        meta=meta,
        ts=datetime.utcnow(),
    )
    db.add(entry)
    return entry


# -----------------------------------------------------------------------------
# Основные операции
# -----------------------------------------------------------------------------
def debit_user_to_bank(
    db: Session,
    user_id: int,
    amount: Decimal,
    reason: str,
    idempotency_key: Optional[str] = None,
    use_bonus: bool = False,
) -> None:
    """
    Списывает EFHC (или bonus_efhc) у пользователя и зачисляет в Банк.
    • reason: 'shop_panel_bonus', 'shop_panel_efhc', 'withdraw_lock' и т.п.
    • use_bonus=True → списываем с bonus_efhc, иначе с efhc.
    """
    amount = quantize_amount(amount)
    bank_id = get_current_bank_id(db)

    balance = db.get(Balance, user_id)
    if not balance:
        raise ValueError(f"User {user_id} has no balance")

    if use_bonus:
        if balance.bonus_efhc < amount:
            raise ValueError("Insufficient bonus EFHC")
        balance.bonus_efhc -= amount
    else:
        if balance.efhc < amount:
            raise ValueError("Insufficient EFHC")
        balance.efhc -= amount

    # Банк не хранит свой баланс здесь, т.к. банк = просто админ-идентификатор в логах.
    # Для консистентности: можем завести баланс банка как Balance(bank_id), если нужно.
    log_transfer(db, from_id=user_id, to_id=bank_id, amount=amount, reason=reason, idempotency_key=idempotency_key)


def credit_user_from_bank(
    db: Session,
    user_id: int,
    amount: Decimal,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    Начисляет EFHC пользователю от Банка.
    Используется для покупок EFHC, возвратов, вознаграждений.
    """
    amount = quantize_amount(amount)
    bank_id = get_current_bank_id(db)

    balance = db.get(Balance, user_id)
    if not balance:
        raise ValueError(f"User {user_id} has no balance")

    balance.efhc += amount

    log_transfer(db, from_id=bank_id, to_id=user_id, amount=amount, reason=reason, idempotency_key=idempotency_key)


def transfer_between_users(
    db: Session,
    from_user: int,
    to_user: int,
    amount: Decimal,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    Перевод EFHC между пользователями.
    """
    amount = quantize_amount(amount)

    from_balance = db.get(Balance, from_user)
    to_balance = db.get(Balance, to_user)
    if not from_balance or not to_balance:
        raise ValueError("Balances not found")

    if from_balance.efhc < amount:
        raise ValueError("Insufficient funds")

    from_balance.efhc -= amount
    to_balance.efhc += amount

    log_transfer(db, from_id=from_user, to_id=to_user, amount=amount, reason=reason, idempotency_key=idempotency_key)


# -----------------------------------------------------------------------------
# Операции, специфичные для бонусных EFHC и панелей
# -----------------------------------------------------------------------------
def spend_bonus_for_panels(
    db: Session,
    user_id: int,
    amount: Decimal,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    Списание бонусных EFHC при покупке панелей.
    • Списываем bonus_efhc у пользователя → возвращаем в Банк.
    • reason = 'shop_panel_bonus'
    """
    debit_user_to_bank(
        db=db,
        user_id=user_id,
        amount=amount,
        reason="shop_panel_bonus",
        idempotency_key=idempotency_key,
        use_bonus=True,
    )


def spend_regular_for_panels(
    db: Session,
    user_id: int,
    amount: Decimal,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    Списание обычных EFHC при покупке панелей.
    • Списываем efhc у пользователя → Банк.
    • reason = 'shop_panel_efhc'
    """
    debit_user_to_bank(
        db=db,
        user_id=user_id,
        amount=amount,
        reason="shop_panel_efhc",
        idempotency_key=idempotency_key,
        use_bonus=False,
    )


# -----------------------------------------------------------------------------
# Вывод EFHC
# -----------------------------------------------------------------------------
def lock_withdrawal(
    db: Session,
    user_id: int,
    amount: Decimal,
    withdraw_id: int,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    При создании заявки на вывод EFHC:
    • EFHC списываются user → Банк (блокируются).
    • reason = 'withdraw_lock'
    • meta = {"withdraw_id": withdraw_id}
    """
    amount = quantize_amount(amount)
    bank_id = get_current_bank_id(db)

    balance = db.get(Balance, user_id)
    if not balance or balance.efhc < amount:
        raise ValueError("Insufficient EFHC")

    balance.efhc -= amount

    log_transfer(
        db,
        from_id=user_id,
        to_id=bank_id,
        amount=amount,
        reason="withdraw_lock",
        idempotency_key=idempotency_key,
        meta={"withdraw_id": withdraw_id},
    )


def refund_withdrawal(
    db: Session,
    user_id: int,
    amount: Decimal,
    withdraw_id: int,
    idempotency_key: Optional[str] = None,
) -> None:
    """
    Возврат EFHC пользователю при отклонении/отмене заявки.
    • reason = 'withdraw_refund'
    • meta = {"withdraw_id": withdraw_id}
    """
    amount = quantize_amount(amount)
    bank_id = get_current_bank_id(db)

    balance = db.get(Balance, user_id)
    if not balance:
        raise ValueError(f"User {user_id} has no balance")

    balance.efhc += amount

    log_transfer(
        db,
        from_id=bank_id,
        to_id=user_id,
        amount=amount,
        reason="withdraw_refund",
        idempotency_key=idempotency_key,
        meta={"withdraw_id": withdraw_id},
    )


# -----------------------------------------------------------------------------
# TODO: в будущем здесь можно добавить:
#   • airdrop_bonus(db, user_id, amount, reason="airdrop_bonus")
#   • admin_adjustment(db, user_id, amount, reason="admin_adjustment")
#   • начисления по реферальной системе (через bonus_efhc)
# -----------------------------------------------------------------------------
