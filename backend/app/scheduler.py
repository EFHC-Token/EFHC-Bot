# 📂 backend/app/scheduler.py — фоновый планировщик задач EFHC (энергия, VIP, лотереи)
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Начисляет ежедневную энергию (кВт) по установленным "панелям" пользователя:
#       - Отдельная таблица каталога панелей: efhc_core.panel_catalog (level -> daily_kwh)
#       - Таблица пользовательских панелей: efhc_core.user_panels (telegram_id, level, count)
#       - Таблица начислений-логов: efhc_core.panel_accrual_log (для предотвращения повторов в один день)
#       - VIP множитель = +7% (1.07), если у пользователя есть user_vip или админ-NFT (не путать с SUP admin).
#   • Переодически синхронизирует VIP флаг на основе NFT (через nft_checker.py):
#       - Если у пользователя есть NFT из коллекции EFHC → выставляем внутренний VIP-флаг (user_vip).
#       - Это не влияет на админ-права; админ-права обрабатываются отдельно (admin_routes).
#   • Обслуживает лотереи:
#       - Выбирает победителей, если число билетов достигло порога в лотерее и она активна.
#       - Запоминает победителя и завершает лотерею.
#
# Как используется:
#   • В main.py запускаем в фоне (пример):
#         asyncio.create_task(scheduler_loop(accrual_hour_utc=0, poll_interval=60))
#
# Предпосылки:
#   • Модули:
#       - database.get_session() — фабрика сессий.
#       - config.get_settings() — все переменные окружения и схема БД по умолчанию.
#       - nft_checker.is_vip_by_nft(db, telegram_id) — проверка наличия NFT в whitelist коллекции.
#       - models.py — декларативные модели SQLAlchemy (пригодятся в будущем; здесь используем SQL).
#   • Таблицы (создаём idempotent):
#       efhc_core.users(telegram_id), efhc_core.balances (efhc, bonus, kwh),
#       efhc_core.user_vip (vip флаг),
#       efhc_core.panel_catalog (описание уровней панелей и дневная генерация),
#       efhc_core.user_panels (сколько панелей каждого уровня у пользователя),
#       efhc_core.panel_accrual_log (лог начислений на дату),
#       efhc_lottery.lotteries, efhc_lottery.lottery_tickets (работа с лотереями).
#
# Важно:
#   • Начисление производится один раз в сутки (например, в 00:00 UTC).
#     Чтобы избежать двойного начисления, мы ведём log efhc_core.panel_accrual_log
#     с ключом (accrual_date, telegram_id).
#   • VIP множитель 1.07 (НЕ 2.0!). В проекте были вопросы — подтверждаю корректно: +7%.
#   • Ограничение 1000 панелей — соблюдается на уровне API /user/panels/buy; здесь проверка по факту.
#   • Уровни панелей и ежедневная генерация (daily_kwh):
#       - В идеале берём из таблицы efhc_core.panel_catalog (level INT UNIQUE, daily_kwh NUMERIC).
#       - Если таблица пустая — начисление энергии будет пропущено, чтобы избежать произвольных допущений.
#       - ⇨ Требуется от вас подтвердить точные значения "daily_kwh" для каждого уровня (1..N).
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_session
from .nft_checker import is_vip_by_nft  # Предполагается: есть функция проверки NFT-владения
# Примечание: если nft_checker имеет иные имена/аргументы — необходимо скорректировать импорт/вызов.

# -----------------------------------------------------------------------------
# Глобальные настройки и константы
# -----------------------------------------------------------------------------
settings = get_settings()

# Схемы БД — берутся из config, чтобы быть совместимыми с архитектурой:
SCHEMA_CORE = settings.DB_SCHEMA_CORE or "efhc_core"
SCHEMA_LOTTERY = settings.DB_SCHEMA_LOTTERY or "efhc_lottery"
SCHEMA_TASKS = settings.DB_SCHEMA_TASKS or "efhc_tasks"
SCHEMA_ADMIN = settings.DB_SCHEMA_ADMIN or "efhc_admin"
SCHEMA_REFERRAL = settings.DB_SCHEMA_REFERRAL or "efhc_referrals"

# Флаг: VIP множитель (всегда +7%), полностью подтверждено ранее
VIP_MULTIPLIER = Decimal("1.07")

# Округления:
DEC3 = Decimal("0.001")  # kWh/EFHC/bonus
def d3(x: Decimal) -> Decimal:
    """Округление до 3 знаков после запятой (для kWh, EFHC, bonus)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# SQL — создание таблиц (idempotent)
# -----------------------------------------------------------------------------
CREATE_SCHEMA_CORE_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_CORE};
"""

CREATE_PANEL_CATALOG_SQL = f"""
-- Каталог уровней панелей, определяющий дневную генерацию (kWh) по уровню
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.panel_catalog (
    id SERIAL PRIMARY KEY,
    level INT UNIQUE NOT NULL,
    title TEXT NULL,
    daily_kwh NUMERIC(30, 3) NOT NULL CHECK (daily_kwh >= 0),
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

CREATE_USER_PANELS_SQL = f"""
-- У пользователя может быть несколько панелей разных уровней
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_panels (
    telegram_id BIGINT NOT NULL,
    level INT NOT NULL,
    count INT NOT NULL CHECK (count >= 0),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (telegram_id, level),
    FOREIGN KEY (telegram_id) REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE
);
"""

CREATE_PANEL_ACCRUAL_LOG_SQL = f"""
-- Лог ежедневных начислений (чтобы не начислить второй раз за одну дату)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.panel_accrual_log (
    accrual_date DATE NOT NULL,
    telegram_id BIGINT NOT NULL,
    total_kwh NUMERIC(30, 3) NOT NULL DEFAULT 0,
    vip_applied BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (accrual_date, telegram_id),
    FOREIGN KEY (telegram_id) REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE
);
"""

CREATE_USER_VIP_SQL = f"""
-- Таблица VIP-флагов (внутренний VIP для +7%)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_vip (
    telegram_id BIGINT PRIMARY KEY REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    since TIMESTAMPTZ DEFAULT now()
);
"""

CREATE_BALANCES_SQL = f"""
-- Балансы EFHC/bonus/kWh
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.balances (
    telegram_id BIGINT PRIMARY KEY REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    efhc NUMERIC(30, 3) DEFAULT 0,
    bonus NUMERIC(30, 3) DEFAULT 0,
    kwh  NUMERIC(30, 3) DEFAULT 0
);
"""

CREATE_USERS_SQL = f"""
-- Пользователи EFHC: регистрируется при первом обращении к боту или при поступлении платежа
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

CREATE_LOTTERY_TABLES_SQL = f"""
-- Лотерейные таблицы (минимально необходимая схема)
CREATE SCHEMA IF NOT EXISTS {SCHEMA_LOTTERY};

CREATE TABLE IF NOT EXISTS {SCHEMA_LOTTERY}.lotteries (
    id SERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,        -- уникальный код лотереи (например, 'lottery_vip')
    title TEXT NOT NULL,              -- отображаемое имя
    prize_type TEXT NOT NULL,         -- 'VIP_NFT', 'PANEL', 'EFHC', 'OTHER'
    target_participants INT NOT NULL DEFAULT 100,
    tickets_sold INT NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    winner_telegram_id BIGINT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    closed_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS {SCHEMA_LOTTERY}.lottery_tickets (
    id SERIAL PRIMARY KEY,
    lottery_code TEXT NOT NULL REFERENCES {SCHEMA_LOTTERY}.lotteries(code) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL,
    purchased_at TIMESTAMPTZ DEFAULT now()
);
"""


async def ensure_scheduler_tables(db: AsyncSession) -> None:
    """
    Создаёт схемы/таблицы для ежедневных начислений, VIP, лотерей — если не созданы (idempotent).
    Вызывается из scheduler_loop() и любых фоновых задач.
    """
    # порядок важен: сначала схема/пользователи/балансы/VIP, затем каталоги/пользовательские панели/лог
    await db.execute(text(CREATE_SCHEMA_CORE_SQL))
    await db.execute(text(CREATE_USERS_SQL))
    await db.execute(text(CREATE_BALANCES_SQL))
    await db.execute(text(CREATE_USER_VIP_SQL))
    await db.execute(text(CREATE_PANEL_CATALOG_SQL))
    await db.execute(text(CREATE_USER_PANELS_SQL))
    await db.execute(text(CREATE_PANEL_ACCRUAL_LOG_SQL))
    await db.execute(text(CREATE_LOTTERY_TABLES_SQL))
    await db.commit()


# -----------------------------------------------------------------------------
# Помощники: проверка/проставление VIP, обеспечение баланса/пользователя
# -----------------------------------------------------------------------------
async def _ensure_user_exists(db: AsyncSession, telegram_id: int) -> None:
    """
    Убедиться, что юзер существует и у него есть запись в balances.
    Это используется перед любой операцией зачисления (например, kWh).
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.users (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )

async def _set_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Проставить внутренний VIP-флаг (user_vip), если его ещё нет.
    """
    await _ensure_user_exists(db, telegram_id)
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.user_vip (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )

async def _unset_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Снять внутренний VIP-флаг (user_vip) с пользователя.
    """
    await db.execute(
        text(f"""
            DELETE FROM {SCHEMA_CORE}.user_vip
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id},
    )


async def _has_internal_vip(db: AsyncSession, telegram_id: int) -> bool:
    """
    Проверяет, есть ли у пользователя внутренний VIP-флаг (user_vip).
    Возвращает True/False.
    """
    q = await db.execute(
        text(f"""
            SELECT 1 FROM {SCHEMA_CORE}.user_vip
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id},
    )
    return q.scalar() is not None


# -----------------------------------------------------------------------------
# VIP синхронизация на основе NFT (nft_checker.is_vip_by_nft)
# -----------------------------------------------------------------------------
async def sync_vip_by_nft(db: AsyncSession, telegram_id: int, wallet_address: Optional[str]) -> bool:
    """
    Синхронизировать VIP по NFT:
      - Если у пользователя по wallet_address есть NFT из нужной коллекции (nft_checker),
        проставляем user_vip (если его не было).
      - Если NFT нет — снимаем user_vip (если хотим строго следовать владению NFT).
    Возвращает: True (есть VIP сейчас) / False (нет VIP).
    """
    # Если кошелёк не указан — считаем что NFT не проверяем (VIP только вручную/через оплату).
    if not wallet_address:
        # В этом случае ничего не меняем. Возвращаем, есть ли текущий внутренний VIP
        return await _has_internal_vip(db, telegram_id)

    # Проверяем через nft_checker (внутри он обращается к TonAPI, whitelist в admin_nft_whitelist)
    vip_now = await is_vip_by_nft(db, owner=wallet_address)
    if vip_now:
        await _set_vip(db, telegram_id)
        return True
    else:
        # По требованию проекта можно либо снимать VIP, либо оставлять (вопрос к бизнес-логике).
        # Здесь — снимаем, чтобы VIP четко соответствовал владению NFT.
        await _unset_vip(db, telegram_id)
        return False


# -----------------------------------------------------------------------------
# Начисление ежедневной энергии по панелям
# -----------------------------------------------------------------------------
async def _fetch_panel_catalog(db: AsyncSession) -> Dict[int, Decimal]:
    """
    Возвращает словарь {level -> daily_kwh} из efhc_core.panel_catalog.
    Если таблица пуста — возвращает пустой словарь.
    Внимание: значения должны быть подтверждены/заведены через админку или миграцию.
    """
    q = await db.execute(
        text(f"""
            SELECT level, daily_kwh
              FROM {SCHEMA_CORE}.panel_catalog
             ORDER BY level ASC
        """)
    )
    out: Dict[int, Decimal] = {}
    for level, daily_kwh in q.all() or []:
        try:
            out[int(level)] = d3(Decimal(daily_kwh or 0))
        except Exception:
            # Игнорируем некорректные записи
            continue
    return out

async def _fetch_all_user_panels(db: AsyncSession) -> List[Dict[str, Any]]:
    """
    Выгружает список всех записей user_panels:
      [{telegram_id, level, count}, ...]
    """
    q = await db.execute(
        text(f"""
            SELECT telegram_id, level, count
              FROM {SCHEMA_CORE}.user_panels
             WHERE count > 0
             ORDER BY telegram_id ASC, level ASC
        """)
    )
    rows = []
    for tg, level, count in q.all() or []:
        rows.append({"telegram_id": int(tg), "level": int(level), "count": int(count)})
    return rows

async def _was_accrued_for_date(db: AsyncSession, tg: int, accrual_date: date) -> bool:
    """
    Проверяет, было ли уже начисление за дату для конкретного пользователя.
    """
    q = await db.execute(
        text(f"""
            SELECT 1
              FROM {SCHEMA_CORE}.panel_accrual_log
             WHERE accrual_date = :ad AND telegram_id = :tg
        """),
        {"ad": accrual_date, "tg": tg},
    )
    return q.scalar() is not None

async def _log_accrual(db: AsyncSession, tg: int, accrual_date: date, total_kwh: Decimal, vip_applied: bool) -> None:
    """
    Записывает факт начисления энергии в panel_accrual_log.
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.panel_accrual_log (accrual_date, telegram_id, total_kwh, vip_applied)
            VALUES (:ad, :tg, :k, :vip)
            ON CONFLICT (accrual_date, telegram_id) DO NOTHING
        """),
        {"ad": accrual_date, "tg": tg, "k": str(d3(total_kwh)), "vip": vip_applied},
    )

async def _credit_kwh(db: AsyncSession, tg: int, kwh_amount: Decimal) -> None:
    """
    Начисляет kWh пользователю (в balances.kwh).
    """
    await _ensure_user_exists(db, tg)
    await db.execute(
        text(f"""
            UPDATE {SCHEMA_CORE}.balances
               SET kwh = COALESCE(kwh, 0) + :k
             WHERE telegram_id = :tg
        """),
        {"k": str(d3(kwh_amount)), "tg": tg},
    )

async def accrue_daily_energy(db: AsyncSession, accrual_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Ежедневное начисление энергии (kWh) по панелям.
    Использует efhc_core.panel_catalog для получения 'daily_kwh' панелей по уровням.
    Применяет VIP множитель 1.07 (если есть user_vip).
    Записывает лог (panel_accrual_log) во избежание двойного начисления.
    Возвращает итоговую статистику:
      {
        "ok": True,
        "processed_users": N,
        "total_kwh": "..." (строка, 3 знака)
      }
    """
    # Дата начисления — актуальная дата по UTC
    if accrual_date is None:
        accrual_date = datetime.now(timezone.utc).date()

    # Загружаем каталог панелей (уровни → daily_kwh)
    catalog = await _fetch_panel_catalog(db)
    if not catalog:
        # Не найден каталог — ничего не начисляем, логируем предупреждение
        print("[EFHC][SCHEDULER] WARNING: panel_catalog is empty — daily accrual skipped.")
        return {"ok": False, "processed_users": 0, "total_kwh": "0.000"}

    rows = await _fetch_all_user_panels(db)
    processed_users = 0
    total_kwh_sum = Decimal("0.000")

    # Сгруппируем по telegram_id, чтобы за пользователя начислить разом
    by_user: Dict[int, List[Dict[str, int]]] = {}
    for r in rows:
        tg = r["telegram_id"]
        by_user.setdefault(tg, []).append(r)

    for tg, items in by_user.items():
        # Проверяем: если уже было начислено за дату — пропускаем
        if await _was_accrued_for_date(db, tg, accrual_date):
            continue

        # Суммируем kWh по всем уровням и количествам пользователя
        base_kwh = Decimal("0.000")
        for it in items:
            lvl = it["level"]
            cnt = it["count"]
            # Ограничение в 1000 панелей соблюдается при покупках; здесь эта проверка не критична,
            # но можно дополнительно защититься:
            if cnt < 0:
                cnt = 0
            if cnt > 1000:
                cnt = 1000
            # Определяем daily_kwh из каталога
            daily_kwh = catalog.get(lvl, Decimal("0.000"))
            base_kwh += Decimal(cnt) * daily_kwh

        # VIP множитель — проверяем внутренний флаг user_vip
        vip_flag = await _has_internal_vip(db, tg)
        if vip_flag:
            base_kwh = d3(base_kwh * VIP_MULTIPLIER)

        # Начисляем kWh, если есть что начислять
        if base_kwh > 0:
            await _credit_kwh(db, tg, base_kwh)
            await _log_accrual(db, tg, accrual_date, base_kwh, vip_flag)
            processed_users += 1
            total_kwh_sum += base_kwh

    # Фиксируем транзакцию после пачки пользователей
    await db.commit()

    return {"ok": True, "processed_users": processed_users, "total_kwh": f"{d3(total_kwh_sum):.3f}"}


# -----------------------------------------------------------------------------
# Лотереи — проверка и выбор победителя
# -----------------------------------------------------------------------------
async def _fetch_active_lotteries(db: AsyncSession) -> List[Dict[str, Any]]:
    """
    Возвращает список активных лотерей (из efhc_lottery.lotteries).
    """
    q = await db.execute(
        text(f"""
            SELECT code, title, prize_type, target_participants, tickets_sold, active, winner_telegram_id
              FROM {SCHEMA_LOTTERY}.lotteries
             WHERE active = TRUE
        """)
    )
    rows = []
    for code, title, prize_type, target_participants, tickets_sold, active, winner_tid in q.all() or []:
        rows.append({
            "code": code,
            "title": title,
            "prize_type": prize_type,
            "target_participants": int(target_participants or 0),
            "tickets_sold": int(tickets_sold or 0),
            "active": bool(active),
            "winner_telegram_id": winner_tid
        })
    return rows

async def _get_lottery_tickets(db: AsyncSession, code: str) -> List[int]:
    """
    Возвращает список telegram_id всех купленных билетов для лотереи code.
    Каждый билет — один элемент (могут повторяться).
    """
    q = await db.execute(
        text(f"""
            SELECT telegram_id
              FROM {SCHEMA_LOTTERY}.lottery_tickets
             WHERE lottery_code = :code
             ORDER BY id ASC
        """),
        {"code": code},
    )
    return [int(row[0]) for row in q.all() or []]

async def _set_lottery_winner(db: AsyncSession, code: str, winner_tid: int) -> None:
    """
    Устанавливает победителя в лотерее и деактивирует её.
    """
    await db.execute(
        text(f"""
            UPDATE {SCHEMA_LOTTERY}.lotteries
               SET winner_telegram_id = :tid,
                   active = FALSE,
                   closed_at = now()
             WHERE code = :code
        """),
        {"tid": winner_tid, "code": code},
    )

async def draw_lotteries(db: AsyncSession) -> Dict[str, Any]:
    """
    Проверяет активные лотереи и, если число участников достигло target_participants,
    выбирает победителя случайным образом из имеющихся билетов. После выбора
    деактивирует лотерею (active = FALSE) и фиксирует winner_telegram_id.

    Возвращает статистику:
    {
      "ok": True,
      "lotteries_closed": N
    }
    """
    import random

    active_list = await _fetch_active_lotteries(db)
    closed_count = 0

    for lot in active_list:
        code = lot["code"]
        target = lot["target_participants"]
        sold = lot["tickets_sold"]

        # Если текущих билетов недостаточно — пропускаем
        if sold < target:
            continue

        # Получаем все билеты и выбираем случайный элемент
        tickets = await _get_lottery_tickets(db, code)
        if not tickets:
            continue

        winner = random.choice(tickets)
        await _set_lottery_winner(db, code, winner)
        closed_count += 1

    await db.commit()
    return {"ok": True, "lotteries_closed": closed_count}


# -----------------------------------------------------------------------------
# Публичные методы для планировщика: суточная сессия и тикер
# -----------------------------------------------------------------------------
async def run_daily_energy_accrual(accrual_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Запуск ежедневного накопления энергии с созданием сессии (вне FastAPI контекста).
    Полезно вызывать из крон-джоба (schedule) или из main.py при наступлении часа X (например 00:00 UTC).
    """
    async with get_session() as db:
        await ensure_scheduler_tables(db)
        result = await accrue_daily_energy(db, accrual_date=accrual_date)
        # Коммит произойдет внутри accrue_daily_energy (на случай поэтапных операций).
    return result

async def run_lottery_draw() -> Dict[str, Any]:
    """
    Запуск процедуры выбора победителей по лотереям (где достигнут порог участников).
    """
    async with get_session() as db:
        await ensure_scheduler_tables(db)
        result = await draw_lotteries(db)
        # Коммит внутри draw_lotteries
    return result


# -----------------------------------------------------------------------------
# Периодическая синхронизация VIP по NFT (по всем пользователям с привязанным кошельком)
# -----------------------------------------------------------------------------
async def _fetch_users_with_wallets(db: AsyncSession) -> List[Tuple[int, Optional[str]]]:
    """
    Получает список пользователей и привязанных кошельков.
    Важно: в этой версии мы предполагаем, что таблица efhc_core.users имеет доп. поле wallet_address,
    которое хранит TON адрес пользователя. Если его нет — требуется добавить миграцию.
    Если у вас используется отдельная таблица для связки user->wallet, адаптируйте запрос.

    Возвращает список [(telegram_id, wallet_address), ...].
    """
    # ВНИМАНИЕ: Если в вашей модели users нет поля wallet_address, замените на вашу реализацию.
    # Здесь оставлено как расширение.
    try:
        q = await db.execute(
            text(f"""
                SELECT telegram_id, wallet_address
                  FROM {SCHEMA_CORE}.users
                 WHERE wallet_address IS NOT NULL
            """)
        )
        return [(int(tid), str(w)) for tid, w in q.all() or []]
    except Exception:
        # Если таблица не содержит wallet_address — возвращаем пусто
        return []

async def sync_all_vip_from_nft() -> Dict[str, Any]:
    """
    Перебирает всех пользователей с привязанными кошельками и синхронизирует VIP по NFT.
    Если у пользователя есть NFT из whitelist коллекции — выдаём user_vip,
    иначе (по текущей логике) снимаем VIP.

    Возвращает статистику:
    {
      "ok": True,
      "processed": N,
      "vip_on": K,     # у скольких VIP включён после синхронизации
      "vip_off": M,    # у скольких VIP выключен после синхронизации
    }
    """
    on, off = 0, 0
    processed = 0

    async with get_session() as db:
        await ensure_scheduler_tables(db)
        pairs = await _fetch_users_with_wallets(db)
        for (tg, wa) in pairs:
            processed += 1
            vip_now = await sync_vip_by_nft(db, telegram_id=tg, wallet_address=wa)
            if vip_now:
                on += 1
            else:
                off += 1
        await db.commit()

    return {"ok": True, "processed": processed, "vip_on": on, "vip_off": off}


# -----------------------------------------------------------------------------
# Главный цикл планировщика (loop): вызовы по расписанию
# -----------------------------------------------------------------------------
async def scheduler_loop(
    accrual_hour_utc: int = 0,
    poll_interval: int = 60,
    run_vip_sync_each_hours: int = 6,
    run_lottery_check_each_minutes: int = 10,
) -> None:
    """
    Фоновый цикл планировщика:
      - Раз в сутки (в accrual_hour_utc по UTC) начисляет энергию по панелям.
      - Каждые run_vip_sync_each_hours — синхронизирует VIP по NFT (если есть кошельки).
      - Каждые run_lottery_check_each_minutes — проверяет, где можно завершить лотереи.
      - Цикл устойчив к исключениям: ошибки логируются, выполнение продолжается.

    Аргументы:
      - accrual_hour_utc: час (0–23), когда делаем начисление kWh. По умолчанию 0 — полночь UTC.
      - poll_interval: период основного тикера в секундах (например, 60 секунд).
      - run_vip_sync_each_hours: период для синхронизации VIP по NFT, каждые N часов.
      - run_lottery_check_each_minutes: период проверки лотерей, каждые N минут.

    Использование:
      - в main.py:
            asyncio.create_task(scheduler_loop(accrual_hour_utc=0, poll_interval=60))
    """
    # Текущие счётчики времени для VIP и лотерей
    vip_sync_timer = 0
    lottery_timer = 0

    print(
        f"[EFHC][SCHEDULER] Loop started: "
        f"accrual_hour_utc={accrual_hour_utc}, poll_interval={poll_interval}s, "
        f"vip_sync_each={run_vip_sync_each_hours}h, lottery_check_each={run_lottery_check_each_minutes}m"
    )

    # Небольшая начальная задержка для корректной инициализации сервиса
    await asyncio.sleep(3)

    while True:
        try:
            now = datetime.now(timezone.utc)

            # 1) Если наступил час начисления (и в текущие сутки не начисляли)
            # Для простоты сравниваем только часы: начинаем раз в сутки в выбранный час.
            # Чтобы избежать повторов, accrue_daily_energy проверяет log по дате.
            if now.hour == accrual_hour_utc:
                try:
                    # Начисляем за текущую дату (UTC)
                    res = await run_daily_energy_accrual(accrual_date=now.date())
                    print(f"[EFHC][SCHEDULER] Daily accrual: {res}")
                except Exception as e:
                    print(f"[EFHC][SCHEDULER] ERROR in daily accrual: {e}")

            # 2) Синхронизация VIP по NFT (каждые N часов)
            vip_sync_timer += poll_interval
            if vip_sync_timer >= run_vip_sync_each_hours * 3600:
                vip_sync_timer = 0
                try:
                    res = await sync_all_vip_from_nft()
                    print(f"[EFHC][SCHEDULER] VIP sync result: {res}")
                except Exception as e:
                    print(f"[EFHC][SCHEDULER] ERROR in VIP sync: {e}")

            # 3) Проверка лотерей (каждые N минут)
            lottery_timer += poll_interval
            if lottery_timer >= run_lottery_check_each_minutes * 60:
                lottery_timer = 0
                try:
                    res = await run_lottery_draw()
                    print(f"[EFHC][SCHEDULER] Lottery draw: {res}")
                except Exception as e:
                    print(f"[EFHC][SCHEDULER] ERROR in lottery draw: {e}")

        except Exception as e:
            # Никогда не выходим из цикла из-за исключений
            print(f"[EFHC][SCHEDULER] Unexpected error in loop: {e}")
        finally:
            await asyncio.sleep(poll_interval)


# -----------------------------------------------------------------------------
# Ручной вызов задач (для админки / отладки)
# -----------------------------------------------------------------------------
async def manual_accrual_for_date(accrual_date: Optional[date] = None) -> Dict[str, Any]:
    """
    Ручной триггер начисления kWh за конкретную дату.
    Возвращает результат accrue_daily_energy.
    """
    return await run_daily_energy_accrual(accrual_date=accrual_date)

async def manual_lottery_check() -> Dict[str, Any]:
    """
    Ручная проверка лотерей (закрытие и выбор победителя).
    Возвращает статистику draw_lotteries().
    """
    return await run_lottery_draw()

async def manual_vip_sync() -> Dict[str, Any]:
    """
    Ручная синхронизация VIP для всех пользователей с кошельками (ежечасно/по кнопке).
    """
    return await sync_all_vip_from_nft()
