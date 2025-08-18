# 📂 backend/app/scheduler.py
# -----------------------------------------------------------------------------
# Планировщик фоновых задач (ежедневные начисления и проверки)
# -----------------------------------------------------------------------------
# Используем APScheduler (AsyncIOScheduler), чтобы в фоне выполнять периодические
# задачи:
# 1. Каждый день в 00:00 UTC → проверка VIP NFT (у кого есть — получают бонус).
# 2. Каждый день в 00:30 UTC → начисление кВт пользователям с панелями.
# -----------------------------------------------------------------------------

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal

from .config import get_settings
from .database import async_session
from .models import User, Panel
from .nft_checker import check_user_vip

settings = get_settings()


# -----------------------------------------------------------------------------
# Инициализация планировщика
# -----------------------------------------------------------------------------
def init_scheduler(app: FastAPI):
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Задача 1. Проверка NFT каждый день в 00:00
    scheduler.add_job(
        verify_all_vip_nfts,
        CronTrigger.from_crontab("0 0 * * *"),  # 00:00 UTC
        id="vip_check"
    )

    # Задача 2. Начисление энергии каждый день в 00:30
    scheduler.add_job(
        accrue_energy_all,
        CronTrigger.from_crontab("30 0 * * *"),  # 00:30 UTC
        id="energy_accrual"
    )

    scheduler.start()
    print("[EFHC][Scheduler] Задачи запущены")
    return scheduler


# -----------------------------------------------------------------------------
# Задача: Проверка VIP NFT
# -----------------------------------------------------------------------------
async def verify_all_vip_nfts():
    """
    Проверяем всех пользователей на наличие VIP NFT.
    У кого есть NFT — обновляем user.is_vip = True.
    """
    async with async_session() as db:
        res = await db.execute(select(User))
        users = res.scalars().all()

        for u in users:
            has_vip = await check_user_vip(u.wallet_ton)
            u.is_vip = has_vip

        await db.commit()

    print("[EFHC][Scheduler] Проверка VIP NFT завершена")


# -----------------------------------------------------------------------------
# Задача: Начисление кВт
# -----------------------------------------------------------------------------
async def accrue_energy_all():
    """
    Каждый день начисляем энергию пользователям в зависимости от их панелей.
    - Если у пользователя есть VIP NFT → используется DAILY_GEN_VIP_KWH
    - Иначе DAILY_GEN_BASE_KWH
    """
    async with async_session() as db:
        res = await db.execute(select(User))
        users = res.scalars().all()

        for u in users:
            # Проверим активные панели
            res_p = await db.execute(select(Panel).where(Panel.user_id == u.id, Panel.is_active == True))
            panels = res_p.scalars().all()

            if not panels:
                continue

            daily_gen = settings.DAILY_GEN_VIP_KWH if u.is_vip else settings.DAILY_GEN_BASE_KWH
            total_kwh = Decimal(daily_gen) * Decimal(len(panels))

            u.balance_kwh = (u.balance_kwh or Decimal("0")) + total_kwh

        await db.commit()

    print("[EFHC][Scheduler] Начисление энергии завершено")
