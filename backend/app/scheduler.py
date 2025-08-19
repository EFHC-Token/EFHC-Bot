# 📂 backend/app/scheduler.py — планировщик ежедневных задач EFHC (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Периодическая фонова логика проекта EFHC:
#       1) Синхронизация VIP-статуса (наличие NFT в кошельке) — каждый день 00:00.
#       2) Начисление ежедневных кВт (kWh) по активным панелям — каждый день 00:30.
#       3) Архивирование панелей с истёкшим сроком — ежедневно (перед начислением kWh).
#
# Главные правила:
#   • VIP активируется/снимается ТОЛЬКО через проверку NFT в кошельке (коллекция whitelist).
#   • Покупка VIP/NFT в магазине НЕ включает VIP автоматически — создаётся лишь заявка
#     на выдачу NFT, VIP появляется после обнаружения NFT в кошельке при регулярной проверке.
#   • Ограничение панелей: не более 1000 АКТИВНЫХ панелей одновременно на ОДНОГО пользователя
#     (архивные панели не считаются в лимит 1000).
#   • Начисления kWh — это внутренняя энергия (1 EFHC = 1 kWh). Начисляем только kWh.
#   • Бонус VIP/NFT +7% применяется к суммарной дневной генерации kWh пользователя.
#
# Тайминги (по требованиям):
#   • 00:00 — job_sync_vip_status: проверяем NFT кошельков пользователей и выставляем VIP.
#   • 00:30 — job_daily_kwh_accrual: начисляем kWh по активным панелям (с учётом VIP).
#
# Связи:
#   • database.py — асинхронные сессии и engine.
#   • config.py — настройки TonAPI, TIMEZONE, базовая генерация и пр.
#   • models.py — ORM модели: User, Balance, UserVIP, AdminNFTWhitelist.
#   • Панели: таблица efhc_core.panels (используется raw SQL).
#
# Важно:
#   • Этот модуль НЕ списывает/начисляет EFHC — только kWh (и VIP флаг в UserVIP).
#   • Обмен EFHC ↔ kWh, вывод EFHC, Shop и прочее — через соответствующие роуты и efhc_transactions.
#
# Подключение:
#   • В app/main.py на старте приложения вызовите start_scheduler(app) (под AsyncIO).
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any, Tuple

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text, select, update, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import get_engine, get_session
from .config import get_settings
from .models import (
    User,                 # ORM: efhc_core.users (telegram_id, wallet_address, ...)
    Balance,              # ORM: efhc_core.balances (telegram_id, efhc, bonus_efhc, kwh, ...)
    UserVIP,              # ORM: efhc_core.user_vip (telegram_id, since)
    AdminNFTWhitelist,    # ORM: efhc_admin.admin_nft_whitelist (nft_address, comment)
)

# -----------------------------------------------------------------------------
# Логгер
# -----------------------------------------------------------------------------
logger = logging.getLogger("efhc.scheduler")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# -----------------------------------------------------------------------------
# Глобальные настройки
# -----------------------------------------------------------------------------
settings = get_settings()

# Таймзона для Cron
TIMEZONE = getattr(settings, "TIMEZONE", "UTC") or "UTC"

# Базовая генерация kWh/сутки на одну панель (без VIP) — по заявлению (0.598 → VIP +7% ≈ 0.640)
BASE_KWH_PER_PANEL_PER_DAY = Decimal(str(getattr(settings, "BASE_KWH_PER_PANEL_PER_DAY", "0.598")))

# Множитель VIP (NFT)
VIP_MULTIPLIER = Decimal("1.07")  # +7% к генерации kWh

# Ограничение активных панелей на одного пользователя
MAX_ACTIVE_PANELS_PER_USER = 1000

# Округление для kWh
DEC3 = Decimal("0.001")


def d3(x: Decimal) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой вниз (ROUND_DOWN).
    Используется для kWh и внутренних показателей.
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# DDL помощники: kWh-журнал и индексы (идемпотентно)
# -----------------------------------------------------------------------------
KWH_LOG_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.kwh_daily_log (
    id BIGSERIAL PRIMARY KEY,
    ts_date DATE NOT NULL,
    telegram_id BIGINT NOT NULL,
    panels_count INTEGER NOT NULL,
    base_kwh NUMERIC(30, 3) NOT NULL,
    vip_multiplier NUMERIC(10, 3) NOT NULL,
    added_kwh NUMERIC(30, 3) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Ускорим выборки
CREATE INDEX IF NOT EXISTS idx_kwh_daily_log_by_date_user
    ON {schema}.kwh_daily_log (ts_date, telegram_id);
"""

PANELS_TABLE_CREATE_SQL = """
-- Таблица панелей (если её ещё нет). В реальной схеме у вас ORM-модель.
-- Здесь оставим как идемпотентный DDL для совместимости.
CREATE TABLE IF NOT EXISTS {schema}.panels (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    activated_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NULL,
    archived_at TIMESTAMPTZ NULL
);

-- Индекс по пользователю/активности
CREATE INDEX IF NOT EXISTS idx_panels_user_active
    ON {schema}.panels (telegram_id, active);
"""

async def ensure_aux_tables(db: AsyncSession) -> None:
    """
    Создаёт вспомогательные таблицы (kWh log, panels) при необходимости.
    Ничего не ломает при повторном вызове.
    """
    schema = settings.DB_SCHEMA_CORE
    await db.execute(text(KWH_LOG_CREATE_SQL.format(schema=schema)))
    await db.execute(text(PANELS_TABLE_CREATE_SQL.format(schema=schema)))
    await db.commit()


# -----------------------------------------------------------------------------
# NFT-проверка (TonAPI) и получение whitelist
# -----------------------------------------------------------------------------
async def fetch_account_nfts(owner: str) -> List[str]:
    """
    Получаем список NFT-адресов на кошельке owner (TON), исп. TonAPI v2.
    Возвращаем массив адресов NFT.
    """
    base = (settings.NFT_PROVIDER_BASE_URL or "https://tonapi.io").rstrip("/")
    url = f"{base}/v2/accounts/{owner}/nfts"
    headers: Dict[str, str] = {}
    if settings.NFT_PROVIDER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.NFT_PROVIDER_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        logger.warning(f"[VIP][NFT] TonAPI request failed for owner={owner}: {e}")
        return []

    items = data.get("items") or data.get("nfts") or []
    out: List[str] = []
    for it in items:
        if not it:
            continue
        addr = it.get("address") or (it.get("nft") or {}).get("address")
        if addr:
            out.append(addr.strip())
    return out


async def get_whitelist_nfts(db: AsyncSession) -> List[str]:
    """
    Возвращает список адресов NFT из whitelist (таблица efhc_admin.admin_nft_whitelist).
    """
    q = await db.execute(select(AdminNFTWhitelist.nft_address))
    return [row[0].strip() for row in q.fetchall() if row[0]]


# -----------------------------------------------------------------------------
# Утилиты для VIP-статуса
# -----------------------------------------------------------------------------
async def set_user_vip(db: AsyncSession, tg_id: int, enable: bool) -> None:
    """
    Устанавливает/снимает VIP-флаг (таблица efhc_core.user_vip).
    Реализация: при enable=True — вставка при отсутствии; при False — удаление при наличии.
    Повторные вызовы для существующего состояния безопасны.
    """
    q = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg_id))
    row: Optional[UserVIP] = q.scalar_one_or_none()

    if enable:
        if not row:
            db.add(UserVIP(telegram_id=tg_id, since=datetime.utcnow()))
            await db.commit()
    else:
        if row:
            await db.delete(row)
            await db.commit()


async def is_user_vip(db: AsyncSession, tg_id: int) -> bool:
    """
    Возвращает True, если у пользователя включён VIP (запись в user_vip).
    """
    q = await db.execute(select(UserVIP).where(UserVIP.telegram_id == tg_id))
    return q.scalar_one_or_none() is not None


# -----------------------------------------------------------------------------
# Получение списка пользователей с кошельками
# -----------------------------------------------------------------------------
async def fetch_all_users_with_wallets(db: AsyncSession) -> List[Tuple[int, str]]:
    """
    Получает список (telegram_id, wallet_address) для пользователей,
    у которых задан wallet_address (TON). Предполагается колонка users.wallet_address.
    """
    q = await db.execute(
        text(f"""
            SELECT telegram_id, wallet_address
            FROM {settings.DB_SCHEMA_CORE}.users
            WHERE wallet_address IS NOT NULL AND length(wallet_address) > 0
        """)
    )
    rows = q.fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


# -----------------------------------------------------------------------------
# Панели: выборка активных панелей и архивирование
# -----------------------------------------------------------------------------
async def archive_expired_panels(db: AsyncSession) -> int:
    """
    Архивирует все панели, у которых expires_at < NOW() и active=TRUE.
    Возвращает, сколько панелей было архивировано.
    """
    q = await db.execute(
        text(f"""
            UPDATE {settings.DB_SCHEMA_CORE}.panels
            SET active=FALSE, archived_at=NOW()
            WHERE active=TRUE AND expires_at IS NOT NULL AND expires_at < NOW()
            RETURNING id
        """)
    )
    rows = q.fetchall() or []
    count = len(rows)
    if count:
        await db.commit()
    return count


async def count_active_panels(db: AsyncSession, tg_id: int) -> int:
    """
    Возвращает количество АКТИВНЫХ панелей пользователя (active=TRUE, archived=FALSE).
    """
    q = await db.execute(
        text(f"""
            SELECT COUNT(*) FROM {settings.DB_SCHEMA_CORE}.panels
            WHERE telegram_id=:tg AND active=TRUE
        """),
        {"tg": tg_id},
    )
    row = q.first()
    return int(row[0] if row else 0)


# -----------------------------------------------------------------------------
# Начисление kWh пользователю на баланс (bonus и EFHC не затрагиваем)
# -----------------------------------------------------------------------------
async def add_kwh_to_user(db: AsyncSession, tg_id: int, amount_kwh: Decimal, panels_count: int, vip_multiplier: Decimal) -> None:
    """
    Начисляет пользователю amount_kwh kWh на баланс. Записывает лог efhc_core.kwh_daily_log.
    Не трогает EFHC/bonus, VIP.
    """
    # ensure balance row
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": tg_id},
    )

    # fetch current balance
    q = await db.execute(select(Balance).where(Balance.telegram_id == tg_id))
    bal: Optional[Balance] = q.scalar_one_or_none()
    cur_kwh = Decimal(bal.kwh or 0) if bal else Decimal("0")

    new_kwh = d3(cur_kwh + amount_kwh)
    await db.execute(
        update(Balance)
        .where(Balance.telegram_id == tg_id)
        .values(kwh=str(new_kwh))
    )

    # log
    await db.execute(
        text(f"""
            INSERT INTO {settings.DB_SCHEMA_CORE}.kwh_daily_log
            (ts_date, telegram_id, panels_count, base_kwh, vip_multiplier, added_kwh)
            VALUES (CURRENT_DATE, :tg, :pcount, :base, :mult, :added)
        """),
        {
            "tg": tg_id,
            "pcount": panels_count,
            "base": str(d3(BASE_KWH_PER_PANEL_PER_DAY * Decimal(panels_count))),
            "mult": str(vip_multiplier),
            "added": str(d3(amount_kwh)),
        },
    )


# -----------------------------------------------------------------------------
# Задача 1: Синхронизация VIP статуса (каждый день в 00:00)
# -----------------------------------------------------------------------------
async def job_sync_vip_status() -> None:
    """
    Синхронизирует VIP-статус всех пользователей С КОШЕЛЬКАМИ:
      • Если кошелёк содержит хотя бы один NFT из whitelist → включаем VIP.
      • Иначе → снимаем VIP.
    VIP не устанавливается/снимается никаким другим способом.
    """
    logger.info("[VIP][SYNC] started")
    async_session: async_sessionmaker[AsyncSession] = get_session()
    async with async_session() as db:
        await ensure_aux_tables(db)  # на всякий случай, но здесь может быть лишним

        whitelist = set([a.strip() for a in (await get_whitelist_nfts(db))])
        if not whitelist:
            logger.warning("[VIP][SYNC] whitelist is empty — nobody will be VIP")
        users = await fetch_all_users_with_wallets(db)
        if not users:
            logger.info("[VIP][SYNC] no users with wallets")
            return

        processed = 0
        vip_on = 0
        vip_off = 0

        for tg_id, wallet in users:
            processed += 1
            try:
                user_nfts = set([a.strip() for a in (await fetch_account_nfts(wallet))])
                has = len(whitelist.intersection(user_nfts)) > 0
                current = await is_user_vip(db, tg_id)
                if has and not current:
                    await set_user_vip(db, tg_id, True)
                    vip_on += 1
                    logger.info(f"[VIP][SYNC] user={tg_id} VIP ON")
                elif (not has) and current:
                    await set_user_vip(db, tg_id, False)
                    vip_off += 1
                    logger.info(f"[VIP][SYNC] user={tg_id} VIP OFF")
                # если состояние не изменилось — ничего не делаем
            except Exception as e:
                logger.error(f"[VIP][SYNC] user={tg_id} error: {e}")

        logger.info(f"[VIP][SYNC] done: processed={processed}, vip_on={vip_on}, vip_off={vip_off}")


# -----------------------------------------------------------------------------
# Задача 2: Начисление ежедневных kWh (каждый день в 00:30)
# -----------------------------------------------------------------------------
async def job_daily_kwh_accrual() -> None:
    """
    Начисляет пользователям ежедневные kWh:
      • Вычисляем для каждого пользователя число АКТИВНЫХ панелей.
      • base = panels_count * BASE_KWH_PER_PANEL_PER_DAY;
      • если VIP — множитель 1.07;
      • начисляем kWh = d3(base * multiplier), пишем лог.
    Перед начислением автоматически архивируем просроченные панели.
    """
    logger.info("[KWH][ACCRUAL] started")
    async_session: async_sessionmaker[AsyncSession] = get_session()
    async with async_session() as db:
        await ensure_aux_tables(db)

        # Архивируем истёкшие
        try:
            archived_cnt = await archive_expired_panels(db)
            if archived_cnt:
                logger.info(f"[KWH][ACCRUAL] archived expired panels: {archived_cnt}")
        except Exception as e:
            logger.error(f"[KWH][ACCRUAL] archive expired panels error: {e}")

        # Список пользователей из panels (у кого есть активные панели)
        q = await db.execute(
            text(f"""
                SELECT DISTINCT telegram_id
                FROM {settings.DB_SCHEMA_CORE}.panels
                WHERE active=TRUE
            """)
        )
        user_rows = q.fetchall()
        if not user_rows:
            logger.info("[KWH][ACCRUAL] no users with active panels")
            return

        total_users = 0
        total_kwh = Decimal("0.000")

        for row in user_rows:
            tg_id = int(row[0])
            try:
                # Сколько активных панелей
                pcount = await count_active_panels(db, tg_id)
                if pcount <= 0:
                    continue
                # Нужно проверить, что не нарушен лимит 1000
                if pcount > MAX_ACTIVE_PANELS_PER_USER:
                    logger.warning(f"[KWH][ACCRUAL][WARN] user={tg_id} active panels={pcount} > limit={MAX_ACTIVE_PANELS_PER_USER}")
                    # Тут можно отправить уведомление админам/в лог. Начисление всё равно считаем по факту.

                # VIP?
                vip_flag = await is_user_vip(db, tg_id)
                mult = VIP_MULTIPLIER if vip_flag else Decimal("1.00")

                # Считаем kWh
                base = d3(BASE_KWH_PER_PANEL_PER_DAY * Decimal(pcount))
                add_kwh = d3(base * mult)

                if add_kwh <= 0:
                    continue

                await add_kwh_to_user(db, tg_id, add_kwh, panels_count=pcount, vip_multiplier=mult)
                total_users += 1
                total_kwh += add_kwh

                logger.info(f"[KWH][ACCRUAL] user={tg_id} panels={pcount} vip={vip_flag} added={str(add_kwh)} kWh")
            except Exception as e:
                logger.error(f"[KWH][ACCRUAL] user={tg_id} error: {e}")

        await db.commit()
        logger.info(f"[KWH][ACCRUAL] done: users={total_users}, total_kwh={str(d3(total_kwh))}")


# -----------------------------------------------------------------------------
# Инициализация и запуск планировщика
# -----------------------------------------------------------------------------
_scheduler: Optional[AsyncIOScheduler] = None

def _get_scheduler() -> AsyncIOScheduler:
    """
    Возвращает (или создаёт) AsyncIOScheduler, настроенный на TIMEZONE.
    """
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    return _scheduler


def start_scheduler(app=None) -> AsyncIOScheduler:
    """
    Запускает планировщик с двумя задачами:
      • VIP синхронизация — ежедневно в 00:00.
      • Начисление kWh — ежедневно в 00:30.
    Вызывается из app/main.py при старте приложения.
    """
    scheduler = _get_scheduler()

    # Удалим существующие задачи с такими id (перезапуск)
    for job_id in ("vip_sync_daily", "kwh_accrual_daily"):
        job = scheduler.get_job(job_id)
        if job:
            scheduler.remove_job(job_id)

    # VIP sync — 00:00 по таймзоне
    scheduler.add_job(
        lambda: asyncio.create_task(job_sync_vip_status()),
        trigger=CronTrigger(hour=0, minute=0, timezone=TIMEZONE),
        id="vip_sync_daily",
        replace_existing=True,
        name="Daily VIP NFT Sync (00:00)",
    )

    # kWh accrual — 00:30 по таймзоне
    scheduler.add_job(
        lambda: asyncio.create_task(job_daily_kwh_accrual()),
        trigger=CronTrigger(hour=0, minute=30, timezone=TIMEZONE),
        id="kwh_accrual_daily",
        replace_existing=True,
        name="Daily kWh Accrual (00:30)",
    )

    # Стартуем, если не запущен
    if not scheduler.running:
        scheduler.start()
        logger.info(f"[SCHEDULER] started with TIMEZONE={TIMEZONE}")

    return scheduler


# -----------------------------------------------------------------------------
# Ручные триггеры (по желанию) — можно подключить в админку:
# -----------------------------------------------------------------------------
async def run_vip_sync_now() -> None:
    """
    Выполнить VIP синхронизацию немедленно (можно дергать из админки).
    """
    await job_sync_vip_status()


async def run_kwh_accrual_now() -> None:
    """
    Выполнить начисление kWh немедленно (можно дергать из админки).
    """
    await job_daily_kwh_accrual()


# -----------------------------------------------------------------------------
# Примечания по интеграции:
# -----------------------------------------------------------------------------
# 1) В app/main.py:
#       from .scheduler import start_scheduler
#       @app.on_event("startup")
#       async def on_startup():
#           start_scheduler(app)
#
# 2) Убедитесь, что:
#       • config.TIMEZONE корректно задан (например, "Europe/Kyiv" или "UTC").
#       • В базе есть schema efhc_core и таблицы users/balances/user_vip/admin_nft_whitelist/panels.
#       • Панели: покупка через Shop должна проверять лимит <= 1000 active панелей (на одного пользователя),
#         и при активации указывать активность/срок/архив при истечении — чтобы job_daily_kwh_accrual
#         корректно считала энергию и архивировала.
#
# 3) ВНИМАНИЕ:
#       • Здесь мы НЕ трогаем EFHC-баланс, только kWh. Любые операции EFHC — через efhc_transactions
#         и только через Банк (ID = 362746228). Курс EFHC = 1 kWh, но не делайте прямой записи EFHC здесь.
#       • VIP-флаг только через проверку NFT (правило проекта). Любые попытки ручного включения VIP
#         вне этого процесса будут переопределены ежедневной синхронизацией.
#
# 4) Логи:
#       • kWh начисления фиксируются в efhc_core.kwh_daily_log (ts_date, telegram_id, panels_count, base_kwh, vip_multiplier, added_kwh).
#       • VIP включение/выключение отражается в логах INFO.
#
# -----------------------------------------------------------------------------
