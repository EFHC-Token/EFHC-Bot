# 📂 backend/app/user_routes.py
# -----------------------------------------------------------------------------
# API для обычных пользователей.
# Здесь реализованы все основные функции:
# - регистрация пользователя (по Telegram ID)
# - просмотр балансов (EFHC и бонусных)
# - покупка панелей (с комбинированным списанием бонус + основной баланс)
# - обмен кВт → EFHC
# - выполнение заданий (получение бонусов)
# - реферальная система
# - участие в розыгрышах
# -----------------------------------------------------------------------------

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from decimal import Decimal
import random

from .database import get_session
from .models import (
    User, Panel, Transaction, Task, TaskCompletion,
    Referral, Lottery, LotteryTicket
)
from .utils import dec, q3
from .config import get_settings
from .nft_checker import check_user_vip

router = APIRouter()
settings = get_settings()


# -----------------------------------------------------------------------------
# ХЕЛПЕР: получить текущего пользователя по Telegram ID
# -----------------------------------------------------------------------------
async def get_current_user(db: AsyncSession, x_tid: int) -> User:
    if not x_tid:
        raise HTTPException(401, "Missing X-Telegram-Id header")
    res = await db.execute(select(User).where(User.telegram_id == x_tid))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    return user


# -----------------------------------------------------------------------------
# Регистрация пользователя (первый вход)
# -----------------------------------------------------------------------------
@router.post("/user/register")
async def user_register(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
    username: str | None = None,
    wallet_ton: str | None = None
):
    if not x_tid:
        raise HTTPException(401, "Missing X-Telegram-Id header")

    res = await db.execute(select(User).where(User.telegram_id == x_tid))
    user = res.scalar_one_or_none()

    if user:
        return {"ok": True, "msg": "Already registered", "user_id": user.id}

    user = User(
        telegram_id=x_tid,
        username=username,
        wallet_ton=wallet_ton,
        balance_efhc=Decimal("0.000"),
        bonus_balance_efhc=Decimal("0.000"),
        balance_kwh=Decimal("0.000"),
    )
    db.add(user)
    await db.commit()
    return {"ok": True, "user_id": user.id}


# -----------------------------------------------------------------------------
# Баланс пользователя
# -----------------------------------------------------------------------------
@router.get("/user/balance")
async def user_balance(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)

    return {
        "efhc": f"{user.balance_efhc:.3f}",
        "bonus": f"{user.bonus_balance_efhc:.3f}",
        "kwh": f"{user.balance_kwh:.3f}",
    }


# -----------------------------------------------------------------------------
# Покупка панели
# -----------------------------------------------------------------------------
@router.post("/user/panels/buy")
async def user_buy_panel(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)

    price = Decimal(str(settings.PANEL_PRICE_EFHC))
    bonus_used = min(user.bonus_balance_efhc, price)  # сначала списываем бонусы
    main_used = price - bonus_used

    if user.balance_efhc < main_used:
        raise HTTPException(400, "Недостаточно EFHC")

    # Списываем
    user.bonus_balance_efhc -= bonus_used
    user.balance_efhc -= main_used

    # Создаём панель
    panel = Panel(user_id=user.id)
    db.add(panel)

    # Транзакция
    tx = Transaction(
        user_id=user.id,
        type="buy_panel",
        amount_efhc=price,
        comment=f"Покупка панели (списано {bonus_used} бонусов + {main_used} EFHC)"
    )
    db.add(tx)

    await db.commit()

    return {"ok": True, "panel_id": panel.id, "bonus_used": f"{bonus_used:.3f}", "main_used": f"{main_used:.3f}"}


# -----------------------------------------------------------------------------
# Обмен кВт → EFHC
# -----------------------------------------------------------------------------
@router.post("/user/exchange")
async def user_exchange(
    amount_kwh: Decimal,
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)

    if amount_kwh <= 0:
        raise HTTPException(400, "Неверная сумма")
    if user.balance_kwh < amount_kwh:
        raise HTTPException(400, "Недостаточно кВт")

    # курс всегда 1:1
    user.balance_kwh -= amount_kwh
    user.balance_efhc += amount_kwh

    tx = Transaction(
        user_id=user.id,
        type="exchange",
        amount_efhc=amount_kwh,
        comment="Обмен кВт → EFHC (1:1)"
    )
    db.add(tx)

    await db.commit()
    return {"ok": True, "exchanged": f"{amount_kwh:.3f}"}


# -----------------------------------------------------------------------------
# Список заданий
# -----------------------------------------------------------------------------
@router.get("/user/tasks")
async def user_tasks(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)
    res = await db.execute(select(Task).where(Task.is_active == True))
    tasks = res.scalars().all()

    # Отмечаем выполненные
    out = []
    for t in tasks:
        done = await db.execute(select(TaskCompletion).where(
            TaskCompletion.user_id == user.id, TaskCompletion.task_id == t.id
        ))
        completed = bool(done.scalar_one_or_none())
        out.append({
            "id": t.id,
            "title": t.title,
            "url": t.url,
            "reward": f"{t.reward_bonus_efhc:.3f}",
            "completed": completed
        })
    return out


# -----------------------------------------------------------------------------
# Завершение задания
# -----------------------------------------------------------------------------
@router.post("/user/tasks/complete")
async def user_task_complete(
    task_id: int,
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)

    # проверим задание
    res = await db.execute(select(Task).where(Task.id == task_id))
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    # проверим, не делал ли уже
    res2 = await db.execute(select(TaskCompletion).where(
        TaskCompletion.user_id == user.id, TaskCompletion.task_id == task.id
    ))
    if res2.scalar_one_or_none():
        raise HTTPException(400, "Уже выполнено")

    # выдаём бонус
    user.bonus_balance_efhc += task.reward_bonus_efhc
    comp = TaskCompletion(user_id=user.id, task_id=task.id)
    db.add(comp)

    tx = Transaction(
        user_id=user.id,
        type="task_bonus",
        amount_efhc=task.reward_bonus_efhc,
        comment=f"Бонус за задание: {task.title}"
    )
    db.add(tx)

    await db.commit()
    return {"ok": True, "reward": f"{task.reward_bonus_efhc:.3f}"}


# -----------------------------------------------------------------------------
# Рефералы
# -----------------------------------------------------------------------------
@router.get("/user/referrals")
async def user_referrals(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)
    res = await db.execute(select(Referral).where(Referral.referrer_id == user.id))
    refs = res.scalars().all()

    return [{"id": r.id, "telegram_id": r.referred_telegram_id, "active": r.is_active} for r in refs]


# -----------------------------------------------------------------------------
# Розыгрыши
# -----------------------------------------------------------------------------
@router.get("/user/lotteries")
async def user_lotteries(
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    res = await db.execute(select(Lottery).where(Lottery.is_active == True))
    lots = res.scalars().all()
    out = []
    for l in lots:
        res2 = await db.execute(select(LotteryTicket).where(LotteryTicket.lottery_id == l.id))
        tickets = res2.scalars().all()
        out.append({
            "id": l.id,
            "title": l.title,
            "target": l.target_participants,
            "tickets_sold": len(tickets)
        })
    return out


@router.post("/user/lottery/buy")
async def user_lottery_buy(
    lottery_id: int,
    count: int,
    db: AsyncSession = Depends(get_session),
    x_tid: int | None = Header(None),
):
    user = await get_current_user(db, x_tid)

    res = await db.execute(select(Lottery).where(Lottery.id == lottery_id))
    lot = res.scalar_one_or_none()
    if not lot or not lot.is_active:
        raise HTTPException(404, "Lottery not active")

    if count <= 0 or count > settings.LOTTERY_MAX_TICKETS_PER_USER:
        raise HTTPException(400, "Неверное количество билетов")

    price = Decimal(settings.LOTTERY_TICKET_PRICE_EFHC) * count
    if user.balance_efhc < price:
        raise HTTPException(400, "Недостаточно EFHC")

    # списание
    user.balance_efhc -= price

    tickets = []
    for _ in range(count):
        ticket = LotteryTicket(user_id=user.id, lottery_id=lot.id, ticket_number=random.randint(1000, 9999))
        db.add(ticket)
        tickets.append(ticket)

    tx = Transaction(
        user_id=user.id,
        type="lottery_buy",
        amount_efhc=price,
        comment=f"Покупка {count} билетов в лотерею {lot.title}"
    )
    db.add(tx)

    await db.commit()

    return {"ok": True, "tickets": [t.ticket_number for t in tickets]}

