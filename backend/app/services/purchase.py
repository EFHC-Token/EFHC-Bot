# 📂 backend/app/services/purchase.py — покупка панели (атомарно)
# -----------------------------------------------------------------------------
# Что делает:
#   - Реализует комбинированное списание bonus + main EFHC.
#   - Создаёт панель, логи, обновляет статусы пользователя/рефералки.
# -----------------------------------------------------------------------------

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
from ..models import User, Panel, TransactionLog, Referral
from ..utils import dec, q3, split_spend
from ..config import get_settings

settings = get_settings()
PANEL_PRICE = dec(settings.PANEL_PRICE_EFHC)

async def buy_panel(user_id: int, db: AsyncSession) -> dict:
    async with db.begin():
        res = await db.execute(select(User).where(User.id == user_id).with_for_update())
        user = res.scalar_one_or_none()
        if not user:
            return {"success": False, "message": "User not found"}

        bonus = dec(user.bonus_balance or 0)
        main = dec(user.main_balance or 0)

        total = q3(bonus + main)
        if total < PANEL_PRICE:
            return {
                "success": False,
                "message": f"Недостаточно средств: {total} EFHC (бонусных {bonus} + основных {main}). Нужно {PANEL_PRICE}."
            }

        use_bonus, use_main = split_spend(PANEL_PRICE, bonus, main)
        user.bonus_balance = q3(bonus - use_bonus)
        user.main_balance = q3(main - use_main)

        panel = Panel(user_id=user.id, lifespan_days=settings.PANEL_LIFESPAN_DAYS,
                      daily_generation=dec(settings.DAILY_GEN_VIP_KWH) if user.has_vip else dec(settings.DAILY_GEN_BASE_KWH))
        db.add(panel)
        await db.flush()

        if use_bonus > 0:
            db.add(TransactionLog(user_id=user.id, op_type="buy_panel", amount=use_bonus, source="bonus", meta={"panel_id": panel.id}))
        if use_main > 0:
            db.add(TransactionLog(user_id=user.id, op_type="buy_panel", amount=use_main, source="main", meta={"panel_id": panel.id}))
        db.add(TransactionLog(user_id=user.id, op_type="buy_panel_summary", amount=PANEL_PRICE, source="combined",
                              meta={"panel_id": panel.id, "used_bonus": str(use_bonus), "used_main": str(use_main)}))

        # Становится активным пользователем (для рефералки)
        if settings.ACTIVE_USER_FLAG_ON_FIRST_PANEL and not user.is_active_user:
            user.is_active_user = True
            # отметим связь в рефералах, если есть
            if user.referred_by:
                # запись в таблицу referrals должна быть создана при регистрации — здесь можно обновить is_active
                ref_res = await db.execute(select(Referral).where(Referral.invited_id == user.id).with_for_update())
                ref = ref_res.scalar_one_or_none()
                if ref and not ref.is_active:
                    ref.is_active = True

    return {
        "success": True,
        "panel_id": panel.id,
        "charged": {"bonus": f"{use_bonus:.3f}", "main": f"{use_main:.3f}", "total": f"{PANEL_PRICE:.3f}"},
        "balances_after": {"bonus_balance": f"{user.bonus_balance:.3f}", "main_balance": f"{user.main_balance:.3f}"}
    }
