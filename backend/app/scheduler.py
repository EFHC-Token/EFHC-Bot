# 📂 backend/app/scheduler.py — планировщик начислений EFHC/kWh
# -----------------------------------------------------------------------------
# Что делает:
#   • Каждый день начисляет пользователям kWh в зависимости от количества
#     купленных солнечных панелей (efhc_core.panels).
#   • Если у пользователя установлен VIP-флаг (efhc_core.user_vip),
#     его генерация увеличивается на settings.VIP_MULTIPLIER (например, ×1.07).
#   • Логирует все начисления в efhc_core.daily_generation_log.
#   • Может быть вызван вручную (python -m backend.app.scheduler).
#   • Встраивается в FastAPI (app.add_event_handler("startup", ...)) для автоматического запуска.
#
# Важно:
#   • Мы не удаляем старый код, только добавляем и корректируем.
#   • Используем Decimal для точности и округляем до 3 знаков после запятой.
#   • Поддерживаем все переменные окружения (из config.py и Vercel).
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from datetime import datetime, date
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from .database import AsyncSessionLocal
from .config import get_settings
from .models import (
    User,
    Balance,
    UserVIP,
    Panel,
    DailyGenerationLog,  # добавим в models.py отдельную таблицу
)

settings = get_settings()

# ---------------------------------------------------------------------
# Утилиты Decimal с округлением до 3 знаков
# ---------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    """Округление Decimal до 3 знаков вниз (например, 1.0789 → 1.078)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# ---------------------------------------------------------------------
# Основная логика начислений
# ---------------------------------------------------------------------
async def run_daily_generation(db: AsyncSession, run_date: Optional[date] = None) -> None:
    """
    Начисляет kWh всем пользователям по количеству купленных панелей.
    - run_date: дата начисления (по умолчанию сегодня).
    - Если в таблице daily_generation_log уже есть запись для (telegram_id, run_date),
      то начисление повторно не делается (идемпотентность).
    """

    run_date = run_date or date.today()

    print(f"[EFHC][Scheduler] Запуск начислений за {run_date.isoformat()}")

    # Получаем всех пользователей
    q = await db.execute(select(User.telegram_id))
    users: List[int] = [row[0] for row in q.all()]

    if not users:
        print("[EFHC][Scheduler] Нет пользователей для начисления")
        return

    for tg in users:
        # Сколько панелей у пользователя
        q_panels = await db.execute(
            select(func.sum(Panel.count))
            .where(Panel.telegram_id == tg)
        )
        panels_count = q_panels.scalar() or 0

        if panels_count <= 0:
            continue  # нет панелей, нет начислений

        # Проверяем, VIP ли пользователь
        q_vip = await db.execute(
            select(UserVIP).where(UserVIP.telegram_id == tg)
        )
        is_vip = q_vip.scalar_one_or_none() is not None

        # Базовая генерация (например, 0.598 kWh за панель)
        base_gen_per_panel = Decimal(str(settings.DAILY_GEN_BASE_KWH))
        gen = base_gen_per_panel * Decimal(panels_count)

        # Увеличиваем для VIP (например, ×1.07)
        if is_vip:
            gen *= Decimal(str(settings.VIP_MULTIPLIER))

        gen = d3(gen)

        # Проверим, не начисляли ли уже за сегодня
        q_log = await db.execute(
            select(DailyGenerationLog).where(
                DailyGenerationLog.telegram_id == tg,
                DailyGenerationLog.run_date == run_date
            )
        )
        if q_log.scalar_one_or_none():
            continue  # уже начислено

        # Обновим баланс (kWh)
        q_bal = await db.execute(select(Balance).where(Balance.telegram_id == tg))
        bal: Optional[Balance] = q_bal.scalar_one_or_none()
        if not bal:
            # создадим баланс, если нет
            bal = Balance(telegram_id=tg, efhc=Decimal("0.000"), bonus=Decimal("0.000"), kwh=Decimal("0.000"))
            db.add(bal)
            await db.flush()

        new_kwh = d3(Decimal(bal.kwh or 0) + gen)
        await db.execute(
            update(Balance)
            .where(Balance.telegram_id == tg)
            .values(kwh=str(new_kwh))
        )

        # Добавим запись в лог
        log = DailyGenerationLog(
            telegram_id=tg,
            run_date=run_date,
            generated_kwh=gen,
            panels_count=panels_count,
            vip=is_vip,
            created_at=datetime.utcnow(),
        )
        db.add(log)

    await db.commit()
    print("[EFHC][Scheduler] Начисления завершены")


# ---------------------------------------------------------------------
# Утилиты для ручного запуска
# ---------------------------------------------------------------------
async def daily_job():
    """
    Утилита: запускает run_daily_generation в отдельной сессии.
    """
    async with AsyncSessionLocal() as session:
        await run_daily_generation(session)


# ---------------------------------------------------------------------
# Локальный запуск
# ---------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(daily_job())
