# 📂 backend/app/user_routes.py — публичные пользовательские эндпоинты
# -----------------------------------------------------------------------------
# Что покрывает:
#   • POST   /api/user/register         — регистрация пользователя (idempotent)
#   • GET    /api/user/balance          — баланс EFHC/bonus/kWh + агрегаты (панели)
#   • POST   /api/user/panels/buy       — покупка панели (100 EFHC) с комбинированным списанием
#   • POST   /api/user/exchange         — обмен кВт → EFHC (1:1, минимум из настроек)
#   • GET    /api/user/tasks            — список доступных заданий + статус выполнения
#   • POST   /api/user/tasks/complete   — пометить задание выполненным (+бонусные EFHC)
#   • GET    /api/user/referrals        — список прямых рефералов (минимальный пример)
#   • GET    /api/user/lotteries        — список активных лотерей
#   • POST   /api/user/lottery/buy      — купить N билетов лотереи за EFHC
#
# Особенности:
#   • Пользователь идентифицируется через заголовок "X-Telegram-Id" (обязательно!)
#   • Все округления EFHC/kWh — до 3 знаков (Decimal).
#   • При первом обращении — создаём дефолтные записи (товары/задачи/лотереи) при отсутствии.
#
# Зависимости:
#   • database.get_session — для выдачи AsyncSession
#   • models.py — ORM-классы
#   • config.py — константы и настройки
#
# Безопасность:
#   • Это публичные эндпоинты для обычного пользователя.
#   • Админ-функции — в admin_routes.py (отдельно), проверяются по NFT whitelist.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional, Tuple, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, insert, func, text, and_

from .database import get_session
from .config import get_settings
from .models import (
    Base,
    User,
    Balance,
    UserPanel,
    UserVIP,
    Referral,
    ReferralStat,
    Task,
    UserTask,
    Lottery,
    LotteryTicket,
)

settings = get_settings()
router = APIRouter()

# ------------------------------------------------------------
# Утилиты округления
# ------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# ------------------------------------------------------------
# Инициализация дефолтных сущностей (однократная)
# ------------------------------------------------------------

async def ensure_defaults(db: AsyncSession) -> None:
    """
    Создаёт в БД дефолтные задания/лотереи при отсутствии.
    Вызывается лениво из эндпоинтов.
    """
    # ЗАДАНИЯ (если пусто — накидываем примерки)
    res = await db.execute(select(func.count()).select_from(Task))
    task_count = int(res.scalar() or 0)
    if task_count == 0 and settings.TASKS_ENABLED:
        # Пара демонстрационных заданий:
        tasks = [
            Task(title="Подпишись на канал", url="https://t.me/efhc_official", reward_bonus_efhc=Decimal("1.000")),
            Task(title="Репостни пост", url="https://t.me/efhc_official/1", reward_bonus_efhc=Decimal("0.500")),
        ]
        db.add_all(tasks)
        await db.commit()

    # ЛОТЕРЕИ (если пусто — из конфигурации LOTTERY_DEFAULTS)
    res = await db.execute(select(func.count()).select_from(Lottery))
    lot_count = int(res.scalar() or 0)
    if lot_count == 0 and settings.LOTTERY_ENABLED:
        for item in settings.LOTTERY_DEFAULTS:
            code = item.get("id") or item.get("code") or "lottery_code"
            title = item.get("title", "Prize")
            target = int(item.get("target_participants", "100"))
            prize_type = item.get("prize_type", "EFHC")
            db.add(Lottery(code=code, title=title, target_participants=target, prize_type=prize_type, active=True))
        await db.commit()


# ------------------------------------------------------------
# Вспомогательные функции пользователя / баланса
# ------------------------------------------------------------

async def ensure_user_and_balance(db: AsyncSession, telegram_id: int, username: Optional[str] = None) -> None:
    """
    Убедиться, что пользователь и его баланс существуют (idempotent).
    """
    # users
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.users (telegram_id, username)
            VALUES (:tg, :un)
            ON CONFLICT (telegram_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, {settings.DB_SCHEMA_CORE}.users.username)
        """),
        {"tg": telegram_id, "un": username or None}
    )
    # balances
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id}
    )
    await db.commit()


async def get_balance_snapshot(db: AsyncSession, telegram_id: int) -> Dict[str, str]:
    """
    Отдаёт словарь с балансом в строках (с округлением до 3 знаков).
    """
    q = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    row: Optional[Balance] = q.scalar_one_or_none()
    if not row:
        # На всякий случай: создаём
        await ensure_user_and_balance(db, telegram_id)
        q = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
        row = q.scalar_one_or_none()

    efhc = Decimal(row.efhc or 0)
    bonus = Decimal(row.bonus or 0)
    kwh = Decimal(row.kwh or 0)
    return {
        "efhc": f"{d3(efhc):.3f}",
        "bonus": f"{d3(bonus):.3f}",
        "kwh": f"{d3(kwh):.3f}",
    }


async def count_active_panels(db: AsyncSession, telegram_id: int) -> int:
    """
    Подсчёт активных (не истёкших) панелей пользователя.
    """
    now = datetime.utcnow()
    q = await db.execute(
        select(func.count()).select_from(UserPanel).where(
            and_(UserPanel.telegram_id == telegram_id, UserPanel.active == True, UserPanel.expires_at > now)
        )
    )
    return int(q.scalar() or 0)


# ------------------------------------------------------------
# Схемы (Pydantic) для запросов/ответов
# ------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: Optional[str] = Field(None, description="Telegram username без @ (может быть пустым)")


class ExchangeRequest(BaseModel):
    amount_kwh: Decimal = Field(..., description="Сколько кВт обменять на EFHC (1:1). Минимум из EXCHANGE_MIN_KWH.")


class LotteryBuyRequest(BaseModel):
    lottery_id: str = Field(..., description="Код лотереи (Lottery.code)")
    count: int = Field(..., ge=1, le=100, description="Сколько билетов купить за раз")


# ------------------------------------------------------------
# Маршруты
# ------------------------------------------------------------

@router.post("/user/register")
async def user_register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Регистрация пользователя. Idempotent.
    Требует заголовок X-Telegram-Id.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    await ensure_defaults(db)
    await ensure_user_and_balance(db, tg, payload.username)

    return {"ok": True, "telegram_id": tg}


@router.get("/user/balance")
async def user_balance(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Текущий баланс пользователя: EFHC, бонусы, kWh + количество активных панелей.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    await ensure_defaults(db)
    await ensure_user_and_balance(db, tg)

    bal = await get_balance_snapshot(db, tg)
    panels_cnt = await count_active_panels(db, tg)

    # Проверим VIP-флаг (внутренний)
    r_vip = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg))
    vip = bool(r_vip.scalar_one_or_none())

    return {
        "efhc": bal["efhc"],
        "bonus": bal["bonus"],
        "kwh": bal["kwh"],
        "panels_active": panels_cnt,
        "vip": vip,
        "panel_price": f"{Decimal(settings.PANEL_PRICE_EFHC):.3f}",
    }


@router.post("/user/panels/buy")
async def user_panels_buy(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Покупка панели за 100 EFHC с комбинированным списанием:
      • сначала бонусные EFHC,
      • затем основной баланс.
    Возвращает, сколько списано из каждого кошелька.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    await ensure_user_and_balance(db, tg)

    # Читаем баланс
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=500, detail="Balance not found")

    price = Decimal(settings.PANEL_PRICE_EFHC)
    total = Decimal(bal.efhc or 0) + Decimal(bal.bonus or 0)
    if total < price:
        raise HTTPException(
            status_code=400,
            detail=f"Недостаточно EFHC. Требуется {price:.3f}, доступно {total:.3f} (бонус {Decimal(bal.bonus or 0):.3f} + основной {Decimal(bal.efhc or 0):.3f})"
        )

    # Ограничим по количеству активных панелей
    active_cnt = await count_active_panels(db, tg)
    if active_cnt >= settings.MAX_ACTIVE_PANELS_PER_USER:
        raise HTTPException(status_code=400, detail="Достигнут лимит активных панелей")

    # Списываем: сначала с бонусных
    bonus_av = Decimal(bal.bonus or 0)
    main_av = Decimal(bal.efhc or 0)

    use_bonus = min(bonus_av, price)
    rest = price - use_bonus
    use_main = rest if rest > 0 else Decimal("0.000")

    # Обновим баланс
    new_bonus = d3(bonus_av - use_bonus)
    new_main = d3(main_av - use_main)

    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(bonus=str(new_bonus), efhc=str(new_main))
    )

    # Добавим панель
    now = datetime.utcnow()
    expires = now + timedelta(days=int(settings.PANEL_LIFESPAN_DAYS))
    daily_gen = Decimal(settings.DAILY_GEN_BASE_KWH)
    # Если VIP — можем подменить daily_gen (опционально):
    r_vip = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg))
    if r_vip.scalar_one_or_none():
        # либо умножить на VIP_MULTIPLIER, либо использовать фикс settings.DAILY_GEN_VIP_KWH
        daily_gen = Decimal(settings.DAILY_GEN_VIP_KWH)

    db.add(UserPanel(
        telegram_id=tg,
        price_eFHC=str(d3(price)),
        purchased_at=now,
        expires_at=expires,
        active=True,
        daily_gen_kwh=str(d3(daily_gen)),
    ))

    await db.commit()

    return {
        "ok": True,
        "bonus_used": f"{d3(use_bonus):.3f}",
        "main_used": f"{d3(use_main):.3f}",
        "panel_expires_at": expires.isoformat(),
        "panels_active": active_cnt + 1,
    }


@router.post("/user/exchange")
async def user_exchange_kwh_to_efhc(
    payload: ExchangeRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Обмен кВт → EFHC (1:1). Минимум — EXCHANGE_MIN_KWH.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Telegram-Id header required")

    tg = int(x_telegram_id)
    amount = d3(payload.amount_kwh)

    if amount < Decimal(str(settings.EXCHANGE_MIN_KWH)):
        raise HTTPException(status_code=400, detail=f"Минимальный обмен: {settings.EXCHANGE_MIN_KWH}")

    # Проверим наличие kWh
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = q.scalar_one_or_none()
    if not bal:
        raise HTTPException(status_code=500, detail="Balance not found")

    kwh = Decimal(bal.kwh or 0)
    if kwh < amount:
        raise HTTPException(status_code=400, detail=f"Недостаточно кВт. Доступно {kwh:.3f}")

    # Списываем kWh, добавляем EFHC 1:1
    new_kwh = d3(kwh - amount)
    new_efhc = d3(Decimal(bal.efhc or 0) + amount)

    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(kwh=str(new_kwh), efhc=str(new_efhc))
    )
    await db.commit()

    return {"ok": True, "efhc_added": f"{amount:.3f}", "kwh_spent": f"{amount:.3f}"}


@router.get("/user/tasks")
async def user_tasks_list(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Список заданий (активные), с пометкой — выполнено ли пользователем.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    tg = int(x_telegram_id)

    await ensure_defaults(db)
    # Возьмём все активные задания
    q = await db.execute(select(Task).where(Task.active == True).order_by(Task.id.asc()))
    tasks: List[Task] = list(q.scalars().all())

    # Выборка статуса пользователя по каждому заданию
    out = []
    for t in tasks:
        qu = await db.execute(
            select(UserTask).where(UserTask.task_id == t.id, UserTask.telegram_id == tg)
        )
        ut: Optional[UserTask] = qu.scalar_one_or_none()
        out.append({
            "id": t.id,
            "title": t.title,
            "url": t.url,
            "reward": f"{Decimal(t.reward_bonus_efhc or 0):.3f}",
            "completed": bool(ut.completed) if ut else False
        })

    return out


@router.post("/user/tasks/complete")
async def user_task_complete(
    task_id: int,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Помечает задание выполненным и начисляет бонусные EFHC.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    tg = int(x_telegram_id)

    # Проверим, что задача существует и активна
    qt = await db.execute(select(Task).where(Task.id == task_id, Task.active == True))
    t = qt.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задание не найдено или неактивно")

    # Проверим запись user_task
    qu = await db.execute(
        select(UserTask).where(UserTask.task_id == task_id, UserTask.telegram_id == tg)
    )
    ut: Optional[UserTask] = qu.scalar_one_or_none()

    if ut and ut.completed:
        return {"ok": True, "already_completed": True}

    now = datetime.utcnow()
    reward = Decimal(t.reward_bonus_efhc or 0)

    if not ut:
        ut = UserTask(task_id=t.id, telegram_id=tg, completed=True, completed_at=now)
        db.add(ut)
    else:
        ut.completed = True
        ut.completed_at = now

    # Начисляем бонусные EFHC
    qbal = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = qbal.scalar_one_or_none()
    if not bal:
        await ensure_user_and_balance(db, tg)
        qbal = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal = qbal.scalar_one_or_none()

    new_bonus = d3(Decimal(bal.bonus or 0) + reward)
    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(bonus=str(new_bonus))
    )

    await db.commit()
    return {"ok": True, "reward_bonus": f"{reward:.3f}"}


@router.get("/user/referrals")
async def user_referrals(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Минимальная выдача прямых рефералов (демо).
    Примечание: механика приглашений/трекеров не реализована здесь (обычно через deep link).
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    tg = int(x_telegram_id)

    # Покажем просто, кого он пригласил и активен ли
    q = await db.execute(
        select(Referral).where(Referral.inviter_id == tg).order_by(Referral.created_at.desc())
    )
    rows: List[Referral] = list(q.scalars().all())

    return [
        {"invitee_id": r.invitee_id, "active": r.active, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/user/lotteries")
async def user_lotteries(
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Список активных лотерей для фронта/бота.
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")

    await ensure_defaults(db)

    q = await db.execute(
        select(Lottery).where(Lottery.active == True).order_by(Lottery.created_at.asc())
    )
    lots: List[Lottery] = list(q.scalars().all())

    return [
        {
            "id": l.code,
            "title": l.title,
            "target": l.target_participants,
            "tickets_sold": l.tickets_sold,
            "prize_type": l.prize_type,
        }
        for l in lots
    ]


@router.post("/user/lottery/buy")
async def user_lottery_buy(
    payload: LotteryBuyRequest,
    db: AsyncSession = Depends(get_session),
    x_telegram_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Продажа билетов лотереи.
    Цена — settings.LOTTERY_TICKET_PRICE_EFHC за 1 шт (с EFHC).
    Ограничение — settings.LOTTERY_MAX_TICKETS_PER_USER за один запрос (уже валидировано в Pydantic).
    """
    if not x_telegram_id or not x_telegram_id.isdigit():
        raise HTTPException(status_code=400, detail="X-Telegram-Id header required")
    tg = int(x_telegram_id)

    # Лотерея существует и активна?
    ql = await db.execute(select(Lottery).where(Lottery.code == payload.lottery_id, Lottery.active == True))
    lot: Optional[Lottery] = ql.scalar_one_or_none()
    if not lot:
        raise HTTPException(status_code=404, detail="Лотерея не найдена или завершена")

    count = int(payload.count)
    if count < 1 or count > int(settings.LOTTERY_MAX_TICKETS_PER_USER):
        raise HTTPException(status_code=400, detail=f"Количество билетов должно быть от 1 до {settings.LOTTERY_MAX_TICKETS_PER_USER}")

    # Сколько EFHC надо списать
    price_per = Decimal(settings.LOTTERY_TICKET_PRICE_EFHC)
    total_price = d3(price_per * Decimal(count))

    # Проверим баланс
    qb = await db.execute(select(Balance).where(Balance.telegram_id == tg))
    bal: Optional[Balance] = qb.scalar_one_or_none()
    if not bal:
        await ensure_user_and_balance(db, tg)
        qb = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal = qb.scalar_one_or_none()

    efhc = Decimal(bal.efhc or 0)
    if efhc < total_price:
        raise HTTPException(status_code=400, detail=f"Недостаточно EFHC. Нужно {total_price:.3f}, доступно {efhc:.3f}")

    # Списываем EFHC
    new_efhc = d3(efhc - total_price)
    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg)
        .values(efhc=str(new_efhc))
    )

    # Создаём билеты
    now = datetime.utcnow()
    for _ in range(count):
        db.add(LotteryTicket(lottery_code=lot.code, telegram_id=tg, purchased_at=now))

    # Увеличиваем счётчик проданных
    lot.tickets_sold = (lot.tickets_sold or 0) + count

    await db.commit()
    return {"ok": True, "tickets_bought": count, "efhc_spent": f"{total_price:.3f}"}
