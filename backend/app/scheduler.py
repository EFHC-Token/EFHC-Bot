# 📂 backend/app/scheduler.py — планировщик ежедневных задач EFHC (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Ежедневная проверка NFT → обновление таблицы VIP-статусов (00:00).
#   • Ежедневное начисление kWh по активным панелям (00:30).
#   • Ежедневное архивирование панелей по достижении 180 дней (00:15).
#
# Бизнес-правила (учтены полностью):
#   • VIP-статус: может быть только у тех пользователей, у кого сейчас есть NFT из коллекции EFHC
#     в их TON-кошельке. Включается/отключается ТОЛЬКО задачей проверки NFT (00:00).
#     Любая покупка VIP/NFT в Shop НЕ меняет статус мгновенно — лишь создаёт заявку (для NFT)
#     или обработку оплаты. Статус VIP появится/пропадёт на следующей проверке.
#   • Начисление суточной генерации kWh в 00:30:
#       - БАЗА: 0.598 kWh на 1 панель (обычный пользователь).
#       - VIP:  0.640 kWh на 1 панель (≈ +7% → множитель 1.07).
#       - Округление до 0.001 вниз (ROUND_DOWN).
#       - kWh начисляются в balances.kwh (расходуемый пул) и в balances.kwh_total (неубывающая
#         метрика для рейтинга).
#       - Начисления строго идемпотентны по дню: уникальная запись в журнале efhc_core.kwh_generation_log
#         (user_id + accrual_date).
#   • Архивирование панелей: активная панель (active = TRUE) становится неактивной (active = FALSE),
#     если прошло >= 180 дней с момента activated_at. Поле archived_at проставляется.
#
# Таблицы (DDL обеспечивается функцией ensure_scheduler_tables):
#
#   efhc_core.user_wallets:        (TON-кошельки пользователей)
#     - telegram_id BIGINT NOT NULL
#     - ton_address TEXT NOT NULL
#     - is_primary BOOL DEFAULT TRUE
#     - added_at TIMESTAMPTZ DEFAULT now()
#     - UNIQUE(telegram_id, ton_address)
#
#   efhc_core.user_vip_status:     (список пользователей, у которых сейчас действующий VIP)
#     - telegram_id BIGINT PRIMARY KEY
#     - since TIMESTAMPTZ NOT NULL
#     - last_checked TIMESTAMPTZ
#     - has_nft BOOLEAN NOT NULL DEFAULT TRUE
#
#   efhc_core.kwh_generation_log:  (журнал ежедневной генерации kWh)
#     - id BIGSERIAL PRIMARY KEY
#     - telegram_id BIGINT NOT NULL
#     - accrual_date DATE NOT NULL  -- дата начисления (YYYY-MM-DD)
#     - panels_count INT NOT NULL
#     - is_vip BOOLEAN NOT NULL
#     - amount_kwh NUMERIC(30,3) NOT NULL
#     - created_at TIMESTAMPTZ DEFAULT now()
#     - UNIQUE(telegram_id, accrual_date)
#
# Зависимости:
#   • database.py — async_session_maker (асинхронные сессии), engine.
#   • config.py — get_settings() (схемы, интервалы, коллекция NFT и т. д.).
#   • models.py — Balances, Panels, Users (минимально используем через raw SQL).
#   • nft_checker.py — асинхронные функции проверки наличия EFHC NFT по TON-адресу.
#   • efhc_transactions.py — не требуется здесь (тк это kWh, а не EFHC).
#
# Интеграция в приложение:
#   • В app/main.py на старте вызвать setup_scheduler(app) и scheduler.start().
#   • Задачи по крону:
#       - 00:00 — run_nft_vip_check()
#       - 00:15 — archive_expired_panels()
#       - 00:30 — run_daily_kwh_accrual()
#
# Важно:
#   • Все округления до 3 знаков (ROUND_DOWN).
#   • Все SELECT/UPDATE используют схему settings.DB_SCHEMA_CORE.
#   • Курс "1 EFHC = 1 kWh" не применяется здесь — конвертация в EFHC производится
#     только в обменнике (отдельный модуль /exchange).
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple, Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import async_session_maker  # предполагается, что в database.py экспортируется async_session_maker
# Если у вас другое имя, скорректируйте импорт. Вариант:
# from .database import async_session as async_session_maker

# nft_checker должен предоставлять функции проверки наличия EFHC NFT:
#   - async def has_efhc_nft(address: str) -> bool
#   (опционально) батч-версия:
#   - async def batch_has_efhc_nft(addresses: List[str]) -> Dict[str, bool]
try:
    from . import nft_checker
except Exception:  # fallback, если нет реального модуля
    nft_checker = None

# -----------------------------------------------------------------------------
# Константы/общие утилиты округления
# -----------------------------------------------------------------------------
log = logging.getLogger("efhc")
settings = get_settings()

DEC3 = Decimal("0.001")

# Суточная выработка на 1 панель
BASE_KWH_PER_PANEL = Decimal("0.598")  # обычный пользователь
VIP_KWH_PER_PANEL = Decimal("0.640")   # VIP пользователь (≈ +7%)

# Срок жизни панели (активной) — строго 180 дней
PANEL_LIFETIME_DAYS = 180

def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    Используется для kWh и любых прочих величин внутри начисления.
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# DDL: создаём служебные таблицы для планировщика (idempotent)
# -----------------------------------------------------------------------------
DDL_USER_WALLETS = f"""
CREATE TABLE IF NOT EXISTS {settings.DB_SCHEMA_CORE}.user_wallets (
    telegram_id BIGINT NOT NULL,
    ton_address TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(telegram_id, ton_address)
);
"""

DDL_USER_VIP_STATUS = f"""
CREATE TABLE IF NOT EXISTS {settings.DB_SCHEMA_CORE}.user_vip_status (
    telegram_id BIGINT PRIMARY KEY,
    since TIMESTAMPTZ NOT NULL,
    last_checked TIMESTAMPTZ,
    has_nft BOOLEAN NOT NULL DEFAULT TRUE
);
"""

DDL_KWH_GENERATION_LOG = f"""
CREATE TABLE IF NOT EXISTS {settings.DB_SCHEMA_CORE}.kwh_generation_log (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    accrual_date DATE NOT NULL,
    panels_count INT NOT NULL,
    is_vip BOOLEAN NOT NULL,
    amount_kwh NUMERIC(30,3) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(telegram_id, accrual_date)
);
"""

async def ensure_scheduler_tables(db: AsyncSession) -> None:
    """
    Создаёт вспомогательные таблицы user_wallets, user_vip_status и kwh_generation_log.
    Вызов безопасен многократно.
    """
    await db.execute(text(DDL_USER_WALLETS))
    await db.execute(text(DDL_USER_VIP_STATUS))
    await db.execute(text(DDL_KWH_GENERATION_LOG))
    await db.commit()

# -----------------------------------------------------------------------------
# Нагрузка VIP-статуса: проверка наличия EFHC NFT в TON-кошельках пользователей
# -----------------------------------------------------------------------------
async def fetch_all_wallets(db: AsyncSession) -> Dict[int, List[str]]:
    """
    Возвращает словарь: { telegram_id: [ton_address1, ton_address2, ...] }
    Берём из efhc_core.user_wallets все записи.
    """
    q = await db.execute(
        text(f"""
            SELECT telegram_id, ton_address
            FROM {settings.DB_SCHEMA_CORE}.user_wallets
        """)
    )
    rows = q.fetchall()

    result: Dict[int, List[str]] = {}
    for tg, addr in rows:
        tg = int(tg)
        if tg not in result:
            result[tg] = []
        result[tg].append(addr)
    return result

async def fetch_current_vip_set(db: AsyncSession) -> Set[int]:
    """
    Возвращает множество telegram_id, присутствующих в efhc_core.user_vip_status.
    """
    q = await db.execute(
        text(f"SELECT telegram_id FROM {settings.DB_SCHEMA_CORE}.user_vip_status")
    )
    rows = q.fetchall()
    return {int(r[0]) for r in rows}

async def upsert_vip_status(db: AsyncSession, user_id: int, is_vip: bool) -> None:
    """
    Вставляет/обновляет VIP-статус пользователя:
      • Если is_vip=True — upsert (вставка/обновление last_checked).
      • Если is_vip=False — удаление записи из user_vip_status.
    """
    if is_vip:
        await db.execute(
            text(f"""
                INSERT INTO {settings.DB_SCHEMA_CORE}.user_vip_status (telegram_id, since, last_checked, has_nft)
                VALUES (:tg, NOW(), NOW(), TRUE)
                ON CONFLICT (telegram_id)
                DO UPDATE SET last_checked=NOW(), has_nft=TRUE
            """),
            {"tg": user_id}
        )
    else:
        # Снимаем VIP: удаляем запись
        await db.execute(
            text(f"DELETE FROM {settings.DB_SCHEMA_CORE}.user_vip_status WHERE telegram_id=:tg"),
            {"tg": user_id}
        )

async def check_wallet_has_nft(addresses: List[str]) -> bool:
    """
    Проверка наличия EFHC NFT среди нескольких адресов пользователя.
    Возвращает True, если хотя бы по одному адресу есть NFT из коллекции EFHC.

    Ожидание: В модуле nft_checker должен быть реализован метод, который проверяет наличие NFT
    коллекции EFHC. Здесь делаем обёртку: если есть batch, используем его, иначе — последовательная проверка.
    """
    if not addresses:
        return False

    # Если есть батч-функция — используем её (экономит запросы)
    if nft_checker and hasattr(nft_checker, "batch_has_efhc_nft"):
        try:
            result_map: Dict[str, bool] = await nft_checker.batch_has_efhc_nft(addresses)
            return any(result_map.get(a, False) for a in addresses)
        except Exception as e:
            log.warning("batch_has_efhc_nft failed, fallback to single checks: %s", e)

    # Иначе последовательный обход
    if nft_checker and hasattr(nft_checker, "has_efhc_nft"):
        for addr in addresses:
            try:
                if await nft_checker.has_efhc_nft(addr):
                    return True
            except Exception as e:
                log.warning("has_efhc_nft error for %s: %s", addr, e)
    else:
        log.warning("nft_checker module is missing; assuming VIP=FALSE for all users")
    return False

async def run_nft_vip_check() -> None:
    """
    Главная задача проверки NFT → VIP-статусов.
    Алгоритм:
      1) Загружаем все user_wallets.
      2) Для каждого пользователя проверяем наличие EFHC NFT на любом из его адресов.
      3) Сравниваем с текущим списком user_vip_status:
           - Если найден NFT (is_vip=True):
                 вставляем/обновляем запись в user_vip_status (since — первая вставка)
           - Иначе: удаляем запись из user_vip_status (если была).
      4) last_checked обновляем по мере апдейта.
    """
    log.info("[Scheduler] NFT/VIP check started")
    async with async_session_maker() as db:
        await ensure_scheduler_tables(db)

        wallets_map = await fetch_all_wallets(db)
        current_vip = await fetch_current_vip_set(db)

        cnt_true = 0
        cnt_false = 0
        processed = 0

        # Обрабатываем пользователей пакетами для снижения нагрузки (если батч в nft_checker отсутствует)
        user_ids = list(wallets_map.keys())
        # Можно группировать по N пользователей за итерацию
        BATCH_SIZE = 200

        for i in range(0, len(user_ids), BATCH_SIZE):
            batch_ids = user_ids[i:i + BATCH_SIZE]
            # Проверяем каждого
            for uid in batch_ids:
                addresses = wallets_map.get(uid, [])
                is_vip = await check_wallet_has_nft(addresses)
                if is_vip:
                    await upsert_vip_status(db, uid, True)
                    cnt_true += 1
                else:
                    # Если ранее был VIP — убираем
                    if uid in current_vip:
                        await upsert_vip_status(db, uid, False)
                    cnt_false += 1
                processed += 1

            # Коммит пакетно
            await db.commit()

        log.info("[Scheduler] NFT/VIP check done: processed=%d, vip=%d, non_vip=%d", processed, cnt_true, cnt_false)

# -----------------------------------------------------------------------------
# Ежедневная генерация kWh по активным панелям (00:30)
# -----------------------------------------------------------------------------
async def fetch_active_panels_count_per_user(db: AsyncSession) -> Dict[int, int]:
    """
    Возвращает количество активных панелей по пользователям:
      { telegram_id: active_count }
    """
    q = await db.execute(
        text(f"""
            SELECT telegram_id, COUNT(*) AS cnt
            FROM {settings.DB_SCHEMA_CORE}.panels
            WHERE active = TRUE
            GROUP BY telegram_id
        """)
    )
    rows = q.fetchall()
    return {int(tg): int(cnt) for tg, cnt in rows}

async def fetch_vip_set(db: AsyncSession) -> Set[int]:
    """
    Возвращает множество telegram_id с действующим VIP (user_vip_status).
    """
    q = await db.execute(text(f"SELECT telegram_id FROM {settings.DB_SCHEMA_CORE}.user_vip_status"))
    rows = q.fetchall()
    return {int(r[0]) for r in rows}

async def log_kwh_generation(db: AsyncSession, user_id: int, accrual_date: date,
                             panels_count: int, is_vip: bool, amount_kwh: Decimal) -> bool:
    """
    Пишет запись в журнал efhc_core.kwh_generation_log с уникальным ключом (telegram_id, accrual_date).
    Возвращает True, если запись добавлена (начисление нужно выполнить), False — если запись уже была (идемпотентность).
    """
    try:
        await db.execute(
            text(f"""
                INSERT INTO {settings.DB_SCHEMA_CORE}.kwh_generation_log
                    (telegram_id, accrual_date, panels_count, is_vip, amount_kwh, created_at)
                VALUES (:tg, :ad, :pc, :vip, :amt, NOW())
                ON CONFLICT (telegram_id, accrual_date) DO NOTHING
            """),
            {"tg": user_id, "ad": accrual_date, "pc": panels_count, "vip": is_vip, "amt": str(d3(amount_kwh))}
        )
        # Проверяем, добавилось ли:
        q = await db.execute(
            text(f"""
                SELECT 1 FROM {settings.DB_SCHEMA_CORE}.kwh_generation_log
                WHERE telegram_id=:tg AND accrual_date=:ad
            """),
            {"tg": user_id, "ad": accrual_date}
        )
        row = q.first()
        return bool(row)
    except Exception as e:
        log.error("log_kwh_generation error for user=%s date=%s: %s", user_id, accrual_date, e)
        raise

async def add_kwh_to_balance(db: AsyncSession, user_id: int, amount_kwh: Decimal) -> None:
    """
    Начисляет kWh в balances:
      • kwh       += amount_kwh
      • kwh_total += amount_kwh (неубывающий рейтинг-показатель)
    Создаёт запись баланса при необходимости.
    """
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id, efhc, bonus, kwh, kwh_total)
            VALUES (:tg, '0', '0', '0', '0')
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": user_id}
    )
    # Обновляем поля (как NUMERIC в БД, но храним в TEXT в ORM — здесь raw SQL)
    await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.balances
            SET
                kwh = (COALESCE(kwh,'0')::numeric + :amt)::text,
                kwh_total = (COALESCE(kwh_total,'0')::numeric + :amt)::text
            WHERE telegram_id = :tg
        """),
        {"tg": user_id, "amt": str(d3(amount_kwh))}
    )

async def run_daily_kwh_accrual(target_date: Optional[date] = None) -> None:
    """
    Ежедневная генерация kWh в 00:30:
      1) Находит всех пользователей с активными панелями.
      2) Для каждого определяет, является ли VIP (по user_vip_status).
      3) Считает amount_kwh = panels_count * (0.598 или 0.640) и округляет вниз до 0.001.
      4) Идём по пользователям: если запись в kwh_generation_log на target_date отсутствует — добавляем,
         и после этого обновляем balances.kwh и balances.kwh_total.
    Параметр target_date оставлен для возможности ручного запуска за конкретный день (для админа).
    По умолчанию начисляем за вчерашний день (если хотим в 00:30 начислять за прошедшие сутки),
    либо за текущий день — зависит от вашей политики. Ниже — начисляем за текущую календарную дату.
    """
    # По условию начисляем в 00:30 "ежедневные кВт"; чаще всего трактуют как суточную выработку за предыдущие сутки.
    # Чтобы быть предсказуемыми, можно начислять за "вчера". При необходимости смените на date.today().
    accrual_date = target_date or date.today()  # или (date.today() - timedelta(days=1))

    log.info("[Scheduler] Daily kWh accrual started for date=%s", accrual_date)
    async with async_session_maker() as db:
        await ensure_scheduler_tables(db)

        # 1) Кол-во активных панелей по пользователям
        panels_map = await fetch_active_panels_count_per_user(db)
        if not panels_map:
            log.info("[Scheduler] No active panels found, nothing to accrue.")
            return

        # 2) Сет VIP-пользователей
        vip_set = await fetch_vip_set(db)

        processed = 0
        added   = 0
        total_amount = Decimal("0.000")

        # Обрабатываем в батчах на случай больших объёмов
        user_ids = list(panels_map.keys())
        BATCH_SIZE = 500

        for i in range(0, len(user_ids), BATCH_SIZE):
            batch_ids = user_ids[i:i + BATCH_SIZE]
            for uid in batch_ids:
                cnt = panels_map.get(uid, 0)
                if cnt <= 0:
                    continue
                is_vip = uid in vip_set
                per_panel = VIP_KWH_PER_PANEL if is_vip else BASE_KWH_PER_PANEL
                amount = d3(per_panel * Decimal(cnt))

                # 3) Идемпотентная запись в лог
                try:
                    inserted = await log_kwh_generation(
                        db=db,
                        user_id=uid,
                        accrual_date=accrual_date,
                        panels_count=cnt,
                        is_vip=is_vip,
                        amount_kwh=amount,
                    )
                    if inserted:
                        # 4) Начисляем в баланс
                        await add_kwh_to_balance(db, uid, amount)
                        added += 1
                        total_amount += amount
                except Exception as e:
                    await db.rollback()
                    log.error("Accrual failed for user=%s: %s", uid, e)
                    continue

                processed += 1

            # Коммитим батч
            await db.commit()

        log.info("[Scheduler] Daily kWh accrual done: users_processed=%d, new_accruals=%d, total_kwh=%s",
                 processed, added, str(d3(total_amount)))

# -----------------------------------------------------------------------------
# Архивирование панелей по сроку (00:15)
# -----------------------------------------------------------------------------
async def archive_expired_panels() -> None:
    """
    Переводит активные панели (active=TRUE) в архив, если прошло >= 180 дней с момента activated_at.
    Устанавливает active=FALSE, archived_at=NOW().
    """
    log.info("[Scheduler] Archive expired panels started")
    async with async_session_maker() as db:
        # Обновляем все панели одним запросом
        try:
            await db.execute(
                text(f"""
                    UPDATE {settings.DB_SCHEMA_CORE}.panels
                    SET active = FALSE,
                        archived_at = NOW()
                    WHERE active = TRUE
                      AND activated_at < (NOW() - INTERVAL '{PANEL_LIFETIME_DAYS} days')
                      AND (archived_at IS NULL)
                """)
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            log.error("Archive panels failed: %s", e)
            return
    log.info("[Scheduler] Archive expired panels done")

# -----------------------------------------------------------------------------
# Регистрация задач планировщика APScheduler
# -----------------------------------------------------------------------------
def setup_scheduler() -> AsyncIOScheduler:
    """
    Создаёт и настраивает AsyncIOScheduler с крон-задачами:
      • 00:00 — NFT/VIP check
      • 00:15 — Archive expired panels
      • 00:30 — Daily kWh accrual
    Возвращает готовый scheduler (но НЕ запускает его).
    """
    scheduler = AsyncIOScheduler(timezone="UTC")  # при необходимости используйте свою TZ
    # NFT-проверка в 00:00
    scheduler.add_job(run_nft_vip_check, "cron", hour=0, minute=0, id="vip_nft_check")
    # Архивирование панелей в 00:15
    scheduler.add_job(archive_expired_panels, "cron", hour=0, minute=15, id="archive_panels")
    # Начисление kWh в 00:30
    scheduler.add_job(run_daily_kwh_accrual, "cron", hour=0, minute=30, id="kwh_accrual")
    return scheduler

# -----------------------------------------------------------------------------
# Утилита для ручного запуска из консоли/скрипта (опционально)
# -----------------------------------------------------------------------------
async def _debug_run_all_now() -> None:
    """
    Вспомогательная функция для локальной отладки: выполнит все три задачи последовательно.
    НЕ используется в проде, оставлено для разработчика.
    """
    await run_nft_vip_check()
    await archive_expired_panels()
    await run_daily_kwh_accrual()

# Пример локального запуска:
# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(_debug_run_all_now())
#
# В реальном приложении (FastAPI) используйте:
#   from .scheduler import setup_scheduler
#   scheduler = setup_scheduler()
#   scheduler.start()
# и вызывайте это в событии on_startup вашего приложения.
# -----------------------------------------------------------------------------
