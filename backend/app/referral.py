# 📂 backend/app/referral.py — реферальная логика (бонусы, активации)
# -----------------------------------------------------------------------------
# Согласно ТЗ:
# - прямой бонус 0.1 EFHC за каждого активного реферала (первую покупку панели),
# - пороговые бонусы за 10/100/1000/3000/10000 активных.
# - «Активным» считаем реферала после покупки первой панели.
# - Бонусы начисляются на main_balance (реальный).
# - История отражается в таблице transaction_logs.
#
# ПРИМЕЧАНИЕ: здесь не храним дерево/мульти-уровни — только прямые рефералы.

from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from decimal import Decimal
from .models import User, Referral, TransactionLog
from .config import get_settings
from .utils import q3, dec

settings = get_settings()

async def mark_user_active_and_reward_referrer(user_id: int, db: AsyncSession) -> None:
    """
    Вызывается в момент первой покупки панели пользователем user_id:
    1) помечает этого пользователя как активного (active_user = True),
    2) если у него есть реферер — начисляет 0.1 EFHC рефереру,
    3) проверяет достижения (10/100/...) активных, добавляет пороговый бонус.
    Всё в единой транзакции.
    """
    async with db.begin():
        # Найдем реферала в таблице связей
        res = await db.execute(select(Referral).where(Referral.user_id == user_id))
        ref = res.scalar_one_or_none()
        if not ref:
            # нет реферала — просто активируем пользователя
            await db.execute(update(User).where(User.id == user_id).values(active_user=True))
            return

        # Обновим активность пользователя
        await db.execute(update(User).where(User.id == user_id).values(active_user=True))

        # Начислим базовый бонус рефереру
        direct_bonus = q3(settings.REFERRAL_DIRECT_BONUS_EFHC)
        await _credit_main_balance(db, ref.referrer_user_id, direct_bonus, meta={"type": "referral_direct", "ref_user_id": user_id})

        # Считаем количество активных прямых рефералов у referrer
        res2 = await db.execute(
            select(func.count(User.id))
            .select_from(User)
            .join(Referral, Referral.user_id == User.id)
            .where(Referral.referrer_user_id == ref.referrer_user_id)
            .where(User.active_user == True)
        )
        active_count = int(res2.scalar_one() or 0)

        # Пороговые бонусы
        for threshold, bonus in settings.REFERRAL_MILESTONES.items():
            if active_count == threshold:
                b = q3(dec(bonus))
                await _credit_main_balance(db, ref.referrer_user_id, b, meta={"type": "referral_milestone", "threshold": threshold})

async def _credit_main_balance(db: AsyncSession, user_id: int, amount: Decimal, meta: dict | None = None):
    # Увеличим баланс и добавим запись в лог
    res_u = await db.execute(select(User).where(User.id == user_id).with_for_update())
    u = res_u.scalar_one_or_none()
    if not u:
        return
    new_main = q3(dec(u.main_balance) + amount)
    u.main_balance = new_main
    db.add(TransactionLog(
        user_id=user_id,
        op_type="referral_bonus",
        amount=amount,
        source="main",
        meta=meta or {}
    ))

