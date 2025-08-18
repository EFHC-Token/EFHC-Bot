# 📂 backend/app/exchange.py — обмен кВт → EFHC (курс 1:1, округление до 3 знаков)
# -----------------------------------------------------------------------------
# Обмен только в одну сторону: kWh → EFHC.
# - проверяем, что у пользователя достаточно kWh,
# - списываем kWh, начисляем main_balance (реальные EFHC),
# - логируем операцию.

from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
from .models import User, TransactionLog
from .config import get_settings
from .utils import q3, dec

settings = get_settings()

async def exchange_kwh_to_efhc(user_id: int, amount_kwh: Decimal, db: AsyncSession) -> dict:
    amt = q3(amount_kwh)
    if amt <= 0:
        return {"success": False, "error": "INVALID_AMOUNT"}

    async with db.begin():
        res = await db.execute(select(User).where(User.id == user_id).with_for_update())
        u = res.scalar_one_or_none()
        if not u:
            return {"success": False, "error": "USER_NOT_FOUND"}

        if dec(u.kwh_balance) < amt:
            return {"success": False, "error": "INSUFFICIENT_KWH"}

        # Списываем kWh, начисляем EFHC 1:1
        u.kwh_balance = q3(dec(u.kwh_balance) - amt)
        u.main_balance = q3(dec(u.main_balance) + amt)

        db.add(TransactionLog(
            user_id=user_id,
            op_type="exchange_kwh_to_efhc",
            amount=amt,
            source="kwh->main",
            meta={"rate": "1:1"}
        ))

    return {"success": True, "kwh_spent": str(amt), "efhc_acquired": str(amt), "balances_after": {
        "main_balance": str(u.main_balance),
        "bonus_balance": str(u.bonus_balance),
        "kwh_balance": str(u.kwh_balance),
        "total_generated_kwh": str(u.total_generated_kwh),
    }}

