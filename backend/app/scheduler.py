# 📂 backend/app/scheduler.py — Планировщик всех периодических задач EFHC
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Ежедневные начисления kWh по панелям (активным), с учётом VIP-множителя (+7%).
#   • Автоматическая деактивация панелей по истечении 180 дней ("панели в архив"), освобождение лимита 1000.
#   • Проверка NFT-владения (через nft_checker): добавляет/снимает статус VIP ежедневно перед начислением kWh.
#   • Работа с лотереями:
#       - Завершение лотерей, у которых достигнут лимит участников (target_participants).
#       - Выбор победителя (случайно из купивших билеты).
#   • Реферальная система (уровни достижения):
#       - Рассчитывает активных рефералов (купили хотя бы одну активную панель).
#       - Определяет текущий уровень: 0, 1, 2, 3, 4, 5 (с порогами 0,10,100,1000,3000,10000).
#       - При достижении нового уровня — разовое начисление бонуса EFHC за уровень (1/10/100/300/1000 соответственно).
#
# Где используется:
#   • Импортируется в main.py и запускается при старте приложения (через asyncio loop).
#   • Методы можно вызывать вручную (например, админом через admin_routes).
#
# Важные моменты:
#   • Панели храним в efhc_core.user_panel_lots (лоты покупок с датой),
#     а агрегированная таблица efhc_core.user_panels поддерживается для быстроты.
#     - Планировщик проверяет истечение 180 дней (DEACTIVATION_DAYS),
#       переносит лоты в архив efhc_core.user_panel_lots_archive и уменьшает суммарное count в user_panels.
#   • Валюта EFHC и kWh округляется до 3 знаков.
#   • Реферальные уровни и бонусы фиксированы (ниже).
#
# Требует таблицы из:
#   • user_routes.py → ensure_user_routes_tables() создает часть.
#   • Здесь создадим дополнительные (user_panel_lots / archive / referral_levels) idempotent.
#
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import async_session
from .nft_checker import user_has_efhc_nft  # Проверка NFT фактического владения (tonapi)
# from .ton_integration import process_incoming_payments  # при необходимости опрос TON событий (вынесено в main)

# -----------------------------------------------------------------------------
# Настройки
# -----------------------------------------------------------------------------
settings = get_settings()

SCHEMA_CORE = settings.DB_SCHEMA_CORE or "efhc_core"
SCHEMA_TASKS = settings.DB_SCHEMA_TASKS or "efhc_tasks"
SCHEMA_LOTTERY = settings.DB_SCHEMA_LOTTERY or "efhc_lottery"
SCHEMA_REFERRAL = settings.DB_SCHEMA_REFERRAL or "efhc_referrals"

PANEL_PRICE_EFHC = Decimal(str(settings.PANEL_PRICE_EFHC or "100"))      # 100 EFHC за панель
PANEL_DAILY_KWH = Decimal(str(settings.PANEL_DAILY_KWH or "0.598"))      # генерация kWh/сутки на 1 панель
PANEL_MAX_COUNT = int(settings.PANEL_MAX_COUNT or 1000)                   # лимит активных панелей

DEACTIVATION_DAYS = int(settings.PANEL_DEACTIVATION_DAYS or 180)         # срок жизни панели (дней)

VIP_MULTIPLIER = Decimal("1.07")   # VIP начисляет +7%

DEC3 = Decimal("0.001")
def d3(x: Decimal) -> Decimal:
    """Округляет Decimal до 3 знаков (для EFHC/kWh)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# Лотерея — цена билета — 1 EFHC (фиксировано по ТЗ)
LOTTERY_TICKET_PRICE_EFHC = Decimal("1.000")

# Реферальные уровни и бонусы (фиксированные по ТЗ)
# Уровень: (порог активных рефералов, разовый бонус EFHC при достижении)
REF_LEVELS = [
    (0, Decimal("0.000")),      # 0 -> +0 EFHC
    (10, Decimal("1.000")),     # 10 активных -> +1 EFHC
    (100, Decimal("10.000")),   # 100 -> +10 EFHC
    (1000, Decimal("100.000")), # 1000 -> +100 EFHC
    (3000, Decimal("300.000")), # 3000 -> +300 EFHC
    (10000, Decimal("1000.000"))# 10000 -> +1000 EFHC
]

# -----------------------------------------------------------------------------
# SQL для таблиц, которые этот модуль использует и (при необходимости) создаёт
# -----------------------------------------------------------------------------
CREATE_CORE_TABLES_SCHEDULER = f"""
-- Лоты покупок панелей: каждый лот ("корзина" из N панелей с общей датой покупки)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_panel_lots (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    count INT NOT NULL DEFAULT 0,         -- количество панелей в этом лоте
    purchased_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- дата покупки лота
    active BOOLEAN NOT NULL DEFAULT TRUE, -- активен ли лот (пока не истекли 180 дней)
    deactivated_at TIMESTAMPTZ NULL
);

-- Архив лотов (для истории и ограничения одновременных панелей)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_panel_lots_archive (
    id SERIAL PRIMARY KEY,
    lot_id INT NOT NULL,
    telegram_id BIGINT NOT NULL,
    count INT NOT NULL DEFAULT 0,
    purchased_at TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL
);

-- Суммарное количество активных панелей на пользователя (для быстрого доступа)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_panels (
    telegram_id BIGINT NOT NULL REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    level INT NOT NULL DEFAULT 1,
    count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (telegram_id, level)
);

-- Внутренняя таблица достижения реферальных уровней, текущий уровень
-- Позволяет избежать повторной выдачи бонусов
CREATE TABLE IF NOT EXISTS {SCHEMA_REFERRAL}.user_referral_levels (
    telegram_id BIGINT PRIMARY KEY,
    current_level INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_scheduler_tables(db: AsyncSession) -> None:
    """
    Создаёт дополнительно необходимые таблицы (idempotent):
      - efhc_core.user_panel_lots
      - efhc_core.user_panel_lots_archive
      - efhc_core.user_panels (аггрегат)
      - efhc_referrals.user_referral_levels
    """
    await db.execute(text(CREATE_CORE_TABLES_SCHEDULER))
    await db.commit()

# -----------------------------------------------------------------------------
# Вспомогательные функции для начислений, панелей, рефералов и VIP
# -----------------------------------------------------------------------------
async def _get_all_users(db: AsyncSession) -> List[int]:
    """Возвращает список всех пользователей (telegram_id) из ядра."""
    q = await db.execute(text(f"SELECT telegram_id FROM {SCHEMA_CORE}.users"))
    return [int(r[0]) for r in q.all() or []]

async def _get_active_panel_count(db: AsyncSession, telegram_id: int) -> int:
    """
    Возвращает суммарное количество активных панелей пользователя (по лотам).
    """
    q = await db.execute(
        text(f"""
            SELECT COALESCE(SUM(count), 0)
              FROM {SCHEMA_CORE}.user_panel_lots 
             WHERE telegram_id = :tg AND active = TRUE
        """),
        {"tg": telegram_id},
    )
    return int(q.scalar() or 0)

async def _set_user_panels_aggregate(db: AsyncSession, telegram_id: int, count: int) -> None:
    """
    Обновляет агрегатную запись {SCHEMA_CORE}.user_panels (level=1) по сумме активных лотов.
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.user_panels(telegram_id, level, count)
            VALUES (:tg, 1, :c)
            ON CONFLICT(telegram_id, level) DO UPDATE SET count = :c, updated_at = now()
        """),
        {"tg": telegram_id, "c": count},
    )

async def _credit_kwh(db: AsyncSession, telegram_id: int, amount_kwh: Decimal) -> None:
    """
    Начисляет пользователю kWh на баланс.
    """
    await db.execute(
        text(f"""
            UPDATE {SCHEMA_CORE}.balances
               SET kwh = COALESCE(kwh, 0) + :amt
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id, "amt": str(d3(amount_kwh))},
    )

async def _set_user_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Устанавливает VIP пользователю (если уже есть — оставляет).
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.user_vip (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )

async def _unset_user_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Снимает VIP у пользователя.
    """
    await db.execute(
        text(f"DELETE FROM {SCHEMA_CORE}.user_vip WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )

async def _user_is_vip(db: AsyncSession, telegram_id: int) -> bool:
    """
    Проверяет есть ли VIP у пользователя.
    """
    q = await db.execute(
        text(f"SELECT 1 FROM {SCHEMA_CORE}.user_vip WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    return q.scalar() is not None

async def _deactivate_expired_panels(db: AsyncSession) -> int:
    """
    Находит все активные лоты панелей, у которых с момента purchase прошло >= 180 дней,
    переносит их в архив и уменьшает агрегат {SCHEMA_CORE}.user_panels по пользователю.
    Возвращает количество деактивированных лотов.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DEACTIVATION_DAYS)

    # Выберем все истёкшие активные лоты
    q = await db.execute(
        text(f"""
            SELECT id, telegram_id, count, purchased_at
              FROM {SCHEMA_CORE}.user_panel_lots
             WHERE active = TRUE
               AND purchased_at <= :cutoff
        """),
        {"cutoff": cutoff},
    )
    lots = q.all() or []
    deactivated = 0

    for lot in lots:
        lot_id, telegram_id, cnt, purchased_at = int(lot[0]), int(lot[1]), int(lot[2]), lot[3]
        # 1) Сделаем active=FALSE, зафиксируем дату
        await db.execute(
            text(f"""
                UPDATE {SCHEMA_CORE}.user_panel_lots
                   SET active = FALSE, deactivated_at = now()
                 WHERE id = :lot
            """),
            {"lot": lot_id},
        )
        # 2) Перенесём в архив
        await db.execute(
            text(f"""
                INSERT INTO {SCHEMA_CORE}.user_panel_lots_archive(lot_id, telegram_id, count, purchased_at, archived_at)
                VALUES (:lot, :tg, :cnt, :pa, now())
            """),
            {"lot": lot_id, "tg": telegram_id, "cnt": cnt, "pa": purchased_at},
        )
        # 3) Обновим агрегат (уменьшится на cnt)
        active_total = await _get_active_panel_count(db, telegram_id)
        await _set_user_panels_aggregate(db, telegram_id, active_total)

        deactivated += 1

    await db.commit()
    return deactivated

async def _sync_vip_by_nft(db: AsyncSession) -> int:
    """
    Синхронизирует статус VIP у пользователей по факту владения NFT.
    Для каждого пользователя — если есть NFT → VIP ставим, иначе снимаем.
    Возвращает количество пользователей с изменённым статусом.
    """
    users = await _get_all_users(db)
    changed = 0

    # Нам нужен кошелёк (wallet_address) пользователя, если вы храните другой источник — адаптируйте.
    for tg in users:
        q = await db.execute(
            text(f"SELECT wallet_address FROM {SCHEMA_CORE}.users WHERE telegram_id = :tg"),
            {"tg": tg},
        )
        row = q.fetchone()
        wallet = row[0] if row else None

        # Если кошелька нет — пропускаем (возможно позже подтянется из фронта)
        if not wallet:
            # В текущей версии, без кошелька мы VIP не можем подтвердить — снимаем VIP
            prev_vip = await _user_is_vip(db, tg)
            if prev_vip:
                await _unset_user_vip(db, tg)
                changed += 1
            continue

        # Проверяем NFT (владение)
        has_nft = await user_has_efhc_nft(wallet)
        prev_vip = await _user_is_vip(db, tg)
        if has_nft and (not prev_vip):
            await _set_user_vip(db, tg)
            changed += 1
        elif (not has_nft) and prev_vip:
            await _unset_user_vip(db, tg)
            changed += 1

    await db.commit()
    return changed

async def _daily_accrual_kwh(db: AsyncSession) -> int:
    """
    Начисляет ежедневную энергию kWh пользователям по активным панелям:
      kWh_add = PANEL_DAILY_KWH * count
    Если VIP → умножаем сумму на 1.07.
    Возвращает количество пользователей, которым начислено.
    """
    users = await _get_all_users(db)
    affected = 0

    for tg in users:
        # Суммарное число активных панелей
        cnt = await _get_active_panel_count(db, tg)
        if cnt <= 0:
            # Для чистоты обновим агрегат (мог ранее быть неактуальным)
            await _set_user_panels_aggregate(db, tg, 0)
            continue

        # Начисление
        base = PANEL_DAILY_KWH * Decimal(cnt)

        # VIP?
        vip = await _user_is_vip(db, tg)
        if vip:
            base = base * VIP_MULTIPLIER

        amount_add = d3(base)
        if amount_add > 0:
            await _credit_kwh(db, tg, amount_add)
            # обновим агрегат just to be consistent
            await _set_user_panels_aggregate(db, tg, cnt)
            affected += 1

    await db.commit()
    return affected

async def _lotteries_check_and_close(db: AsyncSession) -> int:
    """
    Проверяет активные лотереи: если tickets_sold >= target_participants → закрываем,
    выбираем победителя случайно из всех купивших билеты (равномерно).
    Возвращает число закрытых лотерей.
    """
    q = await db.execute(
        text(f"""
            SELECT code, title, tickets_sold, target_participants
              FROM {SCHEMA_LOTTERY}.lotteries
             WHERE active = TRUE
        """)
    )
    items = q.all() or []
    closed = 0

    for r in items:
        code, title, sold, target = r[0], r[1], int(r[2] or 0), int(r[3] or 0)
        if sold < target:
            continue

        # Выбираем победителя:
        # Введём схему: у кого больше куплено билетов, у того больше шансов. 
        # Затем случайный выбор выигрыша Tickets Weighted.
        q_tickets = await db.execute(
            text(f"""
                SELECT telegram_id
                  FROM {SCHEMA_LOTTERY}.lottery_tickets
                 WHERE lottery_code = :code
            """),
            {"code": code},
        )
        rows_tk = q_tickets.all() or []
        if not rows_tk:
            # На всякий случай — без билетов никого не выбираем, закрываем без победителя
            await db.execute(
                text(f"""
                    UPDATE {SCHEMA_LOTTERY}.lotteries
                       SET active = FALSE, closed_at = now(), winner_telegram_id = NULL
                     WHERE code = :code
                """),
                {"code": code},
            )
            closed += 1
            continue

        # Список telegram_id всех билетов (могут повторяться по количеству купленных)
        ids = [int(rr[0]) for rr in rows_tk]
        # Выбираем случайно
        winner = random.choice(ids)

        # Закрываем лотерею, фиксируем победителя
        await db.execute(
            text(f"""
                UPDATE {SCHEMA_LOTTERY}.lotteries
                   SET active = FALSE, closed_at = now(), winner_telegram_id = :win
                 WHERE code = :code
            """),
            {"code": code, "win": winner},
        )
        closed += 1

    await db.commit()
    return closed

async def _referral_achievement_calc(db: AsyncSession) -> int:
    """
    Пересчитывает уровни достижений по рефералам. Активные рефералы — это те, у кого есть >=1 активная панель.
    Присваивает самый высокий подходящий уровень и при превышении предыдущего — 1 раз начисляет приз EFHC.
    Возвращает количество пользователей, у которых обновился уровень или был начислен бонус.
    """
    changed = 0

    # Получим всех пользователей, у которых есть рефералы
    q = await db.execute(
        text(f"""
            SELECT DISTINCT referred_by
              FROM {SCHEMA_REFERRAL}.referrals
             WHERE referred_by IS NOT NULL
        """)
    )
    referrers = [int(r[0]) for r in q.all() or []]
    if not referrers:
        return 0

    # Подготовим существующие уровни
    q_levels = await db.execute(
        text(f"""
            SELECT telegram_id, current_level 
              FROM {SCHEMA_REFERRAL}.user_referral_levels
        """)
    )
    level_map = {int(r[0]): int(r[1]) for r in q_levels.all() or []}

    for referrer in referrers:
        # Считаем активных рефералов
        q_ref = await db.execute(
            text(f"""
                SELECT r.telegram_id
                  FROM {SCHEMA_REFERRAL}.referrals r
                 WHERE r.referred_by = :ref
            """),
            {"ref": referrer},
        )
        refs = [int(rr[0]) for rr in q_ref.all() or []]
        if not refs:
            # У referrer нет рефералов фактически
            # Скорректируем уровень, если есть запись
            prev = level_map.get(referrer, 0)
            if prev != 0:
                await db.execute(
                    text(f"""
                        INSERT INTO {SCHEMA_REFERRAL}.user_referral_levels(telegram_id, current_level, updated_at)
                        VALUES (:tg, 0, now())
                        ON CONFLICT(telegram_id) DO UPDATE SET current_level = 0, updated_at = now()
                    """),
                    {"tg": referrer}
                )
                changed += 1
            continue

        # Определяем активных по критерию: суммарное активное количество панелей >0 (по лотам)
        active_count = 0
        for r_tg in refs:
            ac = await _get_active_panel_count(db, r_tg)
            if ac > 0:
                active_count += 1

        # Выбираем новый уровень согласно REF_LEVELS
        new_level = 0
        for i, (threshold, bonus) in enumerate(REF_LEVELS):
            if active_count >= threshold:
                new_level = i
            else:
                break

        prev_level = level_map.get(referrer, 0)

        # Если уровень выше прежнего — начисляем разовый бонус за каждый новый достигнутый уровень
        if new_level > prev_level:
            # Суммарный бонус за переход со старого на новый
            total_bonus = Decimal("0.000")
            for j in range(prev_level + 1, new_level + 1):
                _, b = REF_LEVELS[j]
                total_bonus += b

            if total_bonus > 0:
                await db.execute(
                    text(f"""
                        UPDATE {SCHEMA_CORE}.balances
                           SET efhc = COALESCE(efhc, 0) + :b
                         WHERE telegram_id = :tg
                    """),
                    {"tg": referrer, "b": str(d3(total_bonus))},
                )

            # Обновим уровень
            await db.execute(
                text(f"""
                    INSERT INTO {SCHEMA_REFERRAL}.user_referral_levels(telegram_id, current_level, updated_at)
                    VALUES (:tg, :lvl, now())
                    ON CONFLICT(telegram_id) DO UPDATE SET current_level = :lvl, updated_at = now()
                """),
                {"tg": referrer, "lvl": new_level},
            )
            changed += 1
        elif new_level < prev_level:
            # Если активных стало меньше — по ТЗ можно понижать или нет. 
            # Здесь понижаем уровень (без возврата ранее выданных бонусов).
            await db.execute(
                text(f"""
                    INSERT INTO {SCHEMA_REFERRAL}.user_referral_levels(telegram_id, current_level, updated_at)
                    VALUES (:tg, :lvl, now())
                    ON CONFLICT(telegram_id) DO UPDATE SET current_level = :lvl, updated_at = now()
                """),
                {"tg": referrer, "lvl": new_level},
            )
            changed += 1
        else:
            # Без изменений
            pass

    await db.commit()
    return changed

# -----------------------------------------------------------------------------
# Публичные функции для запуска задач планировщика
# -----------------------------------------------------------------------------
async def daily_vip_and_kwh_job(db: AsyncSession) -> Dict[str, int]:
    """
    Композитная задача:
      1) Синхронизировать VIP по NFT (добавить/снять у кого есть/нет NFT).
      2) Деактивировать просроченные панели (180 дней) и перенести в архив.
      3) Начислить kWh для всех пользователей по активным панелям с учетом VIP=1.07.

    Возвращает словарь с метриками { "vip_changed": ..., "panels_deactivated": ..., "users_accrued": ... }
    """
    await ensure_scheduler_tables(db)

    # 1) VIP → NFT
    vip_changed = await _sync_vip_by_nft(db)

    # 2) Деактивация панелей
    panels_deactivated = await _deactivate_expired_panels(db)

    # 3) Начисление kWh
    users_accrued = await _daily_accrual_kwh(db)

    return {
        "vip_changed": vip_changed,
        "panels_deactivated": panels_deactivated,
        "users_accrued": users_accrued,
    }

async def lotteries_job(db: AsyncSession) -> Dict[str, int]:
    """
    Задача: проверить активные лотереи и завершить при достижении порога участников.
    Возвращает {"lotteries_closed": int}
    """
    await ensure_scheduler_tables(db)
    closed = await _lotteries_check_and_close(db)
    return {"lotteries_closed": closed}

async def referral_achievements_job(db: AsyncSession) -> Dict[str, int]:
    """
    Задача: пересчёт реферальных уровней и начисление разовых бонусов.
    Возвращает {"users_changed": int}
    """
    await ensure_scheduler_tables(db)
    changed = await _referral_achievement_calc(db)
    return {"users_changed": changed}

# -----------------------------------------------------------------------------
# Основной цикл планировщика (можно вызывать из main.py)
# -----------------------------------------------------------------------------
async def scheduler_loop() -> None:
    """
    Бесконечный асинхронный цикл планировщика.
    В продакшн можно заменить на APScheduler cron trigger с конкретным временем (например, 13:00 UTC).
    Здесь — примерные интервалы:
      - Каждый день в 12:50 UTC: VIP/NFT sync + деактивация + начисление kWh.
      - Каждый час: лотереи (или каждые 10 минут — на ваш вкус).
      - Каждый день в 13:00 UTC: пересчитать реферальные уровни и бонусы.
    Интервалы адаптируйте по важности и нагрузке.
    """
    print("[SCHED] Scheduler started")
    while True:
        try:
            now = datetime.now(timezone.utc)
            hour = now.hour
            minute = now.minute

            # Блок 1: ежедневно близко к 12:50 UTC — VIP + деактивация + начисления kWh
            # Вы можете выставить точное время в конфиге, здесь — пример.
            if hour == 12 and minute in (50, 51, 52):  # 12:50-12:52 UTC — три попытки выполнения
                async with async_session() as db:
                    metrics = await daily_vip_and_kwh_job(db)
                    print(f"[SCHED] daily_vip_and_kwh_job: {metrics}")

            # Блок 2: лотереи — проверяем каждые 10 минут
            if (minute % 10) == 0:
                async with async_session() as db:
                    l_metrics = await lotteries_job(db)
                    if l_metrics.get("lotteries_closed", 0) > 0:
                        print(f"[SCHED] lotteries_job: {l_metrics}")

            # Блок 3: реферальные уровни и бонусы — каждый день в 13:00 UTC
            if hour == 13 and minute in (0, 1, 2):
                async with async_session() as db:
                    r_metrics = await referral_achievements_job(db)
                    print(f"[SCHED] referral_achievements_job: {r_metrics}")

        except Exception as e:
            print(f"[SCHED] Error in scheduler loop: {e}")

        # Спим минуту (60 сек)
        await asyncio.sleep(60)

# -----------------------------------------------------------------------------
# Вспомогательная функция разовой ручной отработки всех задач (для тестов/админов)
# -----------------------------------------------------------------------------
async def run_all_jobs_once() -> Dict[str, Any]:
    """
    Запускает все основные задачи последовательно один раз.
    Удобно для ручных запусков и тестирования.
    """
    async with async_session() as db:
        await ensure_scheduler_tables(db)
        a = await daily_vip_and_kwh_job(db)
        b = await lotteries_job(db)
        c = await referral_achievements_job(db)

    return {"daily_vip_kwh": a, "lotteries": b, "referrals": c}
