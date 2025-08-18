# 📂 backend/app/transactions.py — учёт транзакций EFHC, внутр. переводы, лотереи, задания
# -----------------------------------------------------------------------------
# Здесь собраны операции:
# - покупка панели (комбинированное списание bonus→main),
# - покупка лотерейных билетов за EFHC,
# - начисление бонусных EFHC за задания,
# - внутренняя передача EFHC «от админа к пользователю» при оплатах из внешних крипто-кошельков (Shop).
# Все операции атомарны (одна DB-транзакция), с логами в transaction_logs.

from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_
from decimal import Decimal
from .models import (
    User, Panel, TransactionLog,
    Lottery, LotteryTicket, Task, TaskCompletion, ShopPayment
)
from .utils import q3, dec, can_buy_more_tickets
from .config import get_settings
from .referral import mark_user_active_and_reward_referrer

settings = get_settings()
PANEL_PRICE = q3("100.000")

# --------------------
# Покупка панели
# --------------------
async def buy_panel(user_id: int, db: AsyncSession) -> dict:
    async with db.begin():
        res = await db.execute(select(User).where(User.id == user_id).with_for_update())
        u = res.scalar_one_or_none()
        if not u:
            return {"success": False, "error": "USER_NOT_FOUND"}

        bonus = dec(u.bonus_balance)
        main = dec(u.main_balance)
        total = q3(bonus + main)
        if total < PANEL_PRICE:
            return {
                "success": False,
                "error": "INSUFFICIENT_FUNDS",
                "message": f"Недостаточно средств: {total} EFHC (бонусных {q3(bonus)} + основных {q3(main)}). Нужно {PANEL_PRICE}."
            }

        # Списываем сначала бонусные
        use_bonus = q3(min(bonus, PANEL_PRICE))
        remaining = q3(PANEL_PRICE - use_bonus)
        use_main = q3(remaining if remaining > 0 else dec("0.000"))

        u.bonus_balance = q3(bonus - use_bonus)
        u.main_balance = q3(main - use_main)

        # Создаем панель
        p = Panel(user_id=u.id)
        db.add(p)
        await db.flush()  # p.id

        # Логи
        if use_bonus > 0:
            db.add(TransactionLog(user_id=u.id, op_type="buy_panel", amount=use_bonus, source="bonus", meta={"panel_id": p.id}))
        if use_main > 0:
            db.add(TransactionLog(user_id=u.id, op_type="buy_panel", amount=use_main, source="main", meta={"panel_id": p.id}))
        db.add(TransactionLog(user_id=u.id, op_type="buy_panel_summary", amount=PANEL_PRICE, source="combined", meta={"panel_id": p.id, "used_bonus": str(use_bonus), "used_main": str(use_main)}))

        # Если это первая панель — активируем и начисляем реф. бонусы
        if not u.active_user:
            await mark_user_active_and_reward_referrer(u.id, db)

        return {
            "success": True,
            "panel_id": p.id,
            "charged": {"bonus": str(use_bonus), "main": str(use_main), "total": str(PANEL_PRICE)},
            "balances_after": {
                "main_balance": str(u.main_balance),
                "bonus_balance": str(u.bonus_balance),
                "kwh_balance": str(u.kwh_balance),
                "total_generated_kwh": str(u.total_generated_kwh),
            }
        }

# --------------------
# Покупка лотерейных билетов
# --------------------
async def buy_lottery_tickets(user_id: int, lottery_id: int, n: int, db: AsyncSession) -> dict:
    if n <= 0 or n > settings.LOTTERY_MAX_TICKETS_PER_USER:
        return {"success": False, "error": "INVALID_TICKETS"}

    price_all = q3(dec(settings.LOTTERY_TICKET_PRICE_EFHC) * n)

    async with db.begin():
        # Блокируем пользователя и лотерею
        res_u = await db.execute(select(User).where(User.id == user_id).with_for_update())
        u = res_u.scalar_one_or_none()
        if not u:
            return {"success": False, "error": "USER_NOT_FOUND"}

        res_l = await db.execute(select(Lottery).where(Lottery.id == lottery_id).with_for_update())
        l = res_l.scalar_one_or_none()
        if not l or not l.is_active:
            return {"success": False, "error": "LOTTERY_INACTIVE"}

        # Сколько у пользователя уже билетов
        res_ct = await db.execute(select(func.count(LotteryTicket.id)).where(and_(LotteryTicket.user_id == user_id, LotteryTicket.lottery_id == lottery_id)))
        already = int(res_ct.scalar_one() or 0)
        if not can_buy_more_tickets(already, n):
            return {"success": False, "error": "TICKETS_LIMIT", "max_per_user": settings.LOTTERY_MAX_TICKETS_PER_USER}

        if dec(u.main_balance) < price_all:
            return {"success": False, "error": "INSUFFICIENT_FUNDS"}

        # Списываем только из основного EFHC
        u.main_balance = q3(dec(u.main_balance) - price_all)
        # Создаем билеты (номера = текущий total+1..)
        res_total = await db.execute(select(func.count(LotteryTicket.id)).where(LotteryTicket.lottery_id == lottery_id))
        total_before = int(res_total.scalar_one() or 0)
        created = []
        for i in range(n):
            ticket = LotteryTicket(lottery_id=lottery_id, user_id=user_id, ticket_number=total_before + 1 + i)
            db.add(ticket)
            created.append(ticket)

        # Лог списания
        db.add(TransactionLog(
            user_id=user_id,
            op_type="lottery_tickets_buy",
            amount=price_all,
            source="main",
            meta={"lottery_id": lottery_id, "tickets": n}
        ))

        return {"success": True, "bought": n, "your_total": already + n, "balances_after": {
            "main_balance": str(u.main_balance),
            "bonus_balance": str(u.bonus_balance),
            "kwh_balance": str(u.kwh_balance),
            "total_generated_kwh": str(u.total_generated_kwh),
        }}

# --------------------
# Начисление бонусных EFHC за выполненное задание
# --------------------
async def award_task_bonus(user_id: int, task_id: int, db: AsyncSession, proof: str | None = None) -> dict:
    async with db.begin():
        res_u = await db.execute(select(User).where(User.id == user_id).with_for_update())
        u = res_u.scalar_one_or_none()
        if not u:
            return {"success": False, "error": "USER_NOT_FOUND"}

        res_t = await db.execute(select(Task).where(Task.id == task_id).with_for_update())
        t = res_t.scalar_one_or_none()
        if not t or not t.is_active or (t.available_count <= 0):
            return {"success": False, "error": "TASK_NOT_AVAILABLE"}

        # Проверка — уже выполнял?
        res_c = await db.execute(select(TaskCompletion).where(TaskCompletion.user_id == user_id, TaskCompletion.task_id == task_id))
        if res_c.scalar_one_or_none():
            return {"success": False, "error": "ALREADY_DONE"}

        # Начисляем бонусные EFHC
        reward = q3(dec(t.reward_bonus_efhc))
        u.bonus_balance = q3(dec(u.bonus_balance) + reward)

        # Фиксируем выполнение
        comp = TaskCompletion(user_id=user_id, task_id=task_id, proof=proof)
        db.add(comp)

        # Уменьшаем доступное количество
        t.available_count -= 1

        # Лог
        db.add(TransactionLog(user_id=user_id, op_type="task_bonus", amount=reward, source="bonus", meta={"task_id": task_id}))

        return {"success": True, "reward": str(reward), "bonus_balance": str(u.bonus_balance)}

# --------------------
# Внутренняя «выплата» от Админа пользователю (после внешней оплаты в TON/USDT/EFHC)
# --------------------
async def admin_credit_user_from_shop(user_id: int, amount_efhc: Decimal, db: AsyncSession, shop_code: str, memo: str | None = None) -> dict:
    """
    Используется после факта входящей транзакции в кошелек бота/админа:
    - Админ «подтверждает» покупку (или авто-подтверждение по webhook),
    - переводим amount_efhc на основной баланс пользователя,
    - пишем TransactionLog source='admin_credit'.
    """
    amt = q3(amount_efhc)
    if amt <= 0:
        return {"success": False, "error": "INVALID_AMOUNT"}

    async with db.begin():
        res_u = await db.execute(select(User).where(User.id == user_id).with_for_update())
        u = res_u.scalar_one_or_none()
        if not u:
            return {"success": False, "error": "USER_NOT_FOUND"}

        u.main_balance = q3(dec(u.main_balance) + amt)
        db.add(TransactionLog(user_id=user_id, op_type="shop_credit", amount=amt, source="main", meta={"shop_code": shop_code, "memo": memo}))
        db.add(ShopPayment(user_id=user_id, code=shop_code, amount_efhc=amt, memo=memo, status="credited"))

        return {"success": True, "amount": str(amt), "main_balance": str(u.main_balance)}

