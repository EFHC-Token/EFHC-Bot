# 📂 backend/app/user_routes.py — пользовательские эндпоинты EFHC (WebApp + безопасность + бизнес-логика)
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Реализует API FastAPI для пользовательских функций:
#       - /user/register               — регистрация пользователя
#       - /user/balance                — баланс EFHC / kWh / bonus
#       - /user/exchange               — обмен EFHC ⇄ kWh (1 EFHC = 1 kWh)
#       - /user/panels                 — просмотр текущих панелей
#       - /user/panels/buy             — покупка панелей (цена 100 EFHC, лимит 1000, генерация 0.598 kWh)
#       - /user/referrals              — рефералка (создать код, статистика)
#       - /user/tasks, /user/tasks/complete — список задач и выполнение (начисление bonus EFHC)
#       - /user/lotteries, /user/lottery/buy — просмотр и покупка билета лотереи
#       - /user/withdraw               — заявка на вывод EFHC (заготовка, фиксирует запрос)
#   • Безопасность:
#       - Подтверждает легитимность каждого запроса от Telegram WebApp:
#           * Требуем заголовок `X-Telegram-Init-Data` (полная строка initData).
#           * Проверяем подпись HMAC-SHA256 согласно документации Telegram.
#           * Разбираем user.id → telegram_id пользователя.
#           * Берём также username (опционально).
#       - Это защищает от подделки заголовка `X-Telegram-Id` и ручных запросов.
#   • Панели:
#       - Панели у нас единого типа (нет 12 уровней).
#       - Стоимость панели: 100 EFHC (настраивается в config.PANEL_PRICE_EFHC).
#       - Генерация: 0.598 kWh/сутки (настраивается в config.PANEL_DAILY_KWH).
#       - Максимум: 1000 панелей на пользователя.
#       - Сохраняются в таблице efhc_core.user_panels одной записью level=1 (или виртуально один уровень).
#   • Обменник:
#       - 1 EFHC = 1 kWh, в обе стороны, с округлением до 3 знаков.
#   • Рефералы:
#       - Привязка пользователя к рефереру (referral_code), если не привязан ранее.
#       - Статистика: количество рефералов, суммарные покупки панелей.
#       - (Начисление бонусов на первое пополнение можно расширить в scheduler/ton_integration — по согласованию.)
#   • Задания:
#       - /user/tasks — активные задачи из efhc_tasks.tasks.
#       - /user/tasks/complete — отметить выполнение и начислить bonus EFHC (reward_bonus_efhc).
#   • Лотереи:
#       - /user/lotteries — список активных.
#       - /user/lottery/buy — покупка билета (можно использовать EFHC с баланса).
#       - Закрытие и выбор победителя делает scheduler.draw_lotteries().
#
# Связь с другими файлами:
#   • config.py           — все настройки окружения, включая PANEL_PRICE_EFHC=100, PANEL_DAILY_KWH=0.598.
#   • database.py         — get_session() для асинхронной работы с БД.
#   • models.py           — декларативные ORM модели (если учтены), здесь используем SQL (text).
#   • scheduler.py        — daily accrual (kWh) по панелям, VIP +7% (если есть user_vip).
#   • nft_checker.py      — проверка NFT для VIP (используется в другом контексте).
#   • admin_routes.py     — админ-панель (списки, правки, whitelist NFT).
#
# Таблицы/схемы:
#   Схемы: efhc_core (балансы, панели, пользователи, vip), efhc_tasks, efhc_lottery, efhc_referrals.
#   Таблицы: 
#       - efhc_core.users (telegram_id, username, created_at, wallet_address? [опционально]),
#       - efhc_core.balances (efhc, bonus, kwh),
#       - efhc_core.user_panels (telegram_id, level=1, count),
#       - efhc_core.user_vip (telegram_id),
#       - efhc_tasks.tasks (id, title, url, reward_bonus_efhc, active),
#       - efhc_tasks.user_tasks (id, telegram_id, task_id, completed_at),
#       - efhc_lottery.lotteries (code, title, target_participants, active, ...),
#       - efhc_lottery.lottery_tickets (lottery_code, telegram_id, purchased_at),
#       - efhc_referrals.referral_links (telegram_id -> referral_code UNIQUE),
#       - efhc_referrals.referrals (telegram_id -> referred_by, при регистрации по коду).
#
# ПРЕДУПРЕЖДЕНИЕ:
#   Если любая из таблиц отсутствует — модуль создаст их idempotent (минимальный набор).
#   Для продакшн рекомендуется миграция Alembic.
# -----------------------------------------------------------------------------

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_session

# -----------------------------------------------------------------------------
# Настройки и константы
# -----------------------------------------------------------------------------
settings = get_settings()

SCHEMA_CORE = settings.DB_SCHEMA_CORE or "efhc_core"
SCHEMA_TASKS = settings.DB_SCHEMA_TASKS or "efhc_tasks"
SCHEMA_LOTTERY = settings.DB_SCHEMA_LOTTERY or "efhc_lottery"
SCHEMA_REFERRAL = settings.DB_SCHEMA_REFERRAL or "efhc_referrals"
SCHEMA_ADMIN = settings.DB_SCHEMA_ADMIN or "efhc_admin"

# Единый тип панелей:
PANEL_PRICE_EFHC = Decimal(str(settings.PANEL_PRICE_EFHC or "100"))       # цена за 1 панель в EFHC
PANEL_DAILY_KWH = Decimal(str(settings.PANEL_DAILY_KWH or "0.598"))       # генерация kWh/сутки на 1 панель (для scheduler)
PANEL_MAX_COUNT = int(settings.PANEL_MAX_COUNT or 1000)                   # строгое ограничение на покупку

# Курс EFHC <-> kWh
EXCHANGE_RATE_EFHC_TO_KWH = Decimal("1.000")  # 1 EFHC = 1 kWh
EXCHANGE_RATE_KWH_TO_EFHC = Decimal("1.000")  # обратное преобразование

# Округления
DEC3 = Decimal("0.001")
def d3(x: Decimal) -> Decimal:
    return x.quantize(DEC3, rounding=ROUND_DOWN)

# -----------------------------------------------------------------------------
# Telegram WebApp initData — проверка подписи (безопасность)
# -----------------------------------------------------------------------------
def _compute_telegram_webapp_hash(init_data: str, bot_token: str) -> str:
    """
    Считает проверочный хэш данных initData согласно Telegram WebApp:
      1) secret_key = SHA256(bot_token)
      2) data_check_string = объединение пар key=value (отсортированных по ключам) через \n (без hash)
      3) hash = HMAC_SHA256(data_check_string, secret_key), hex-строка
    Возвращает hex строку.
    """
    # init_data — это строка вида "query_id=...&user=...&auth_date=...&hash=..."
    # Нам нужно собрать словарь и вычислить хэш по полям кроме hash.
    try:
        pairs = init_data.split("&")
        data_map: Dict[str, str] = {}
        for p in pairs:
            if "=" in p:
                k, v = p.split("=", 1)
                data_map[k] = v
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid initData format")

    # Извлечём hash
    recv_hash = data_map.get("hash")
    if not recv_hash:
        raise HTTPException(status_code=400, detail="Missing 'hash' in initData")

    # data_check_string: собрать key=value для всех ключей, кроме 'hash', отсортировать по key
    check_kv = []
    for k in sorted([k for k in data_map.keys() if k != "hash"]):
        check_kv.append(f"{k}={data_map[k]}")
    data_check_string = "\n".join(check_kv)

    # Считаем HMAC: секрет = SHA256(bot_token)
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    mac = hmac.new(secret_key, msg=data_check_string.encode("utf-8"), digestmod=hashlib.sha256)
    calc_hash = mac.hexdigest()

    return calc_hash, recv_hash

def _parse_user_from_init_data(init_data: str) -> Dict[str, Any]:
    """
    Парсит поле 'user=<json>' из initData и возвращает словарь пользователя:
      {id: <telegram_id>, username: <...>, ...}
    """
    # init_data: например "query_id=...&user=%7B%22id%22%3A12345%2C...%7D&auth_date=...&hash=..."
    # user передаётся url-encoded; FastAPI может не декодировать автоматически, так что декодируем вручную.
    try:
        pairs = init_data.split("&")
        kv: Dict[str, str] = {}
        for p in pairs:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k] = v

        user_raw = kv.get("user")
        if not user_raw:
            raise HTTPException(status_code=400, detail="Missing 'user' in initData")
        # user_raw url-encoded JSON:
        user_json = base64.urlsafe_b64decode(_to_base64url_clean(user_raw)).decode("utf-8")
        # user_json теперь — строка JSON, преобразуем в dict
        # Но чаще Telegram передаёт user как URL-кодированную строку с %7B ... Если так — используем urllib.parse.unquote_plus.
    except Exception:
        # Фоллбэк: парсим через unquote_plus
        try:
            from urllib.parse import unquote_plus
            user_json = unquote_plus(user_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="InitData 'user' parse error")

    try:
        user_dict = json.loads(user_json)
    except Exception:
        # Бывает, что user_json уже dict-like; fallback
        raise HTTPException(status_code=400, detail="InitData user JSON parse error")

    if "id" not in user_dict:
        raise HTTPException(status_code=400, detail="InitData user missing id")

    return user_dict

def _to_base64url_clean(s: str) -> bytes:
    """
    Преобразует строку s к base64url корректной длины.
    Telegram может прислать user=... в необычном виде. Эта функция попытка декодировать.
    """
    try:
        # Если это "%7B%22id...%7D" — это urlencoded. Используем unquote_plus
        from urllib.parse import unquote_plus
        s2 = unquote_plus(s)
        # Превратим в base64url:
        b = s2.encode("utf-8")
        return base64.urlsafe_b64encode(b)
    except Exception:
        return s.encode("utf-8")

async def _verify_webapp_request(
    x_telegram_init_data: Optional[str] = None,
    settings_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Проверяет заголовок X-Telegram-Init-Data и возвращает словарь:
      {
        "telegram_id": int,
        "username": Optional[str],
        "raw_user": dict
      }
    Если подпись неверна — поднимает HTTP 403.
    """
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")

    bot_token = settings_token or settings.TELEGRAM_BOT_TOKEN
    if not bot_token:
        raise HTTPException(status_code=500, detail="Server misconfigured: no TELEGRAM_BOT_TOKEN")

    calc_hash, recv_hash = _compute_telegram_webapp_hash(x_telegram_init_data, bot_token)
    if not hmac.compare_digest(calc_hash, recv_hash):
        raise HTTPException(status_code=403, detail="Invalid Telegram initData signature")

    # Если подпись ок — извлекаем пользователя
    user = _parse_user_from_init_data(x_telegram_init_data)
    telegram_id = int(user.get("id"))
    username = user.get("username") or None

    return {"telegram_id": telegram_id, "username": username, "raw_user": user}

# -----------------------------------------------------------------------------
# Создание базовых таблиц (idempotent) — если их нет
# -----------------------------------------------------------------------------
CREATE_CORE_TABLES_SQL = f"""
-- Пользователи EFHC
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT NULL,
    wallet_address TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Балансы EFHC/bonus/kWh
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.balances (
    telegram_id BIGINT PRIMARY KEY REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    efhc NUMERIC(30, 3) DEFAULT 0,
    bonus NUMERIC(30, 3) DEFAULT 0,
    kwh  NUMERIC(30, 3) DEFAULT 0
);

-- Панели у пользователя: один единственный тип "уровня" (level=1)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_panels (
    telegram_id BIGINT NOT NULL REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    level INT NOT NULL DEFAULT 1,
    count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (telegram_id, level)
);

-- Внутренний VIP флаг (для повышенной генерации по панелям)
CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.user_vip (
    telegram_id BIGINT PRIMARY KEY REFERENCES {SCHEMA_CORE}.users(telegram_id) ON DELETE CASCADE,
    since TIMESTAMPTZ DEFAULT now()
);
"""

CREATE_TASKS_TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_TASKS};

-- Задания (админом создаются)
CREATE TABLE IF NOT EXISTS {SCHEMA_TASKS}.tasks (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NULL,
    reward_bonus_efhc NUMERIC(30, 3) NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Лог выполненных заданий
CREATE TABLE IF NOT EXISTS {SCHEMA_TASKS}.user_tasks (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    task_id INT NOT NULL REFERENCES {SCHEMA_TASKS}.tasks(id) ON DELETE CASCADE,
    completed_at TIMESTAMPTZ DEFAULT now()
);
"""

CREATE_LOTTERY_TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_LOTTERY};

CREATE TABLE IF NOT EXISTS {SCHEMA_LOTTERY}.lotteries (
    id SERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    prize_type TEXT NOT NULL,
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

CREATE_REFERRAL_TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_REFERRAL};

-- Ссылки-приглашения: каждому пользователю выдаём уникальный referral_code
CREATE TABLE IF NOT EXISTS {SCHEMA_REFERRAL}.referral_links (
    telegram_id BIGINT PRIMARY KEY,
    referral_code TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Факт регистрации рефералов: кто кого пригласил
CREATE TABLE IF NOT EXISTS {SCHEMA_REFERRAL}.referrals (
    telegram_id BIGINT PRIMARY KEY,
    referred_by BIGINT NULL,  -- какой telegram_id пригласил этого пользователя
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_user_routes_tables(db: AsyncSession) -> None:
    """
    Создаёт необходимые таблицы EFHC Core/Tasks/Lottery/Referral, если ещё не созданы.
    """
    await db.execute(text(CREATE_CORE_TABLES_SQL))
    await db.execute(text(CREATE_TASKS_TABLES_SQL))
    await db.execute(text(CREATE_LOTTERY_TABLES_SQL))
    await db.execute(text(CREATE_REFERRAL_TABLES_SQL))
    await db.commit()

# -----------------------------------------------------------------------------
# Вспомогательные утилиты БД
# -----------------------------------------------------------------------------
async def _ensure_user_exists(db: AsyncSession, telegram_id: int, username: Optional[str] = None) -> None:
    """
    Обеспечивает наличие записи пользователя в efhc_core.users и efhc_core.balances.
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.users (telegram_id, username)
            VALUES (:tg, :un)
            ON CONFLICT (telegram_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, {SCHEMA_CORE}.users.username)
        """),
        {"tg": telegram_id, "un": username},
    )
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )

async def _get_balance(db: AsyncSession, telegram_id: int) -> Dict[str, str]:
    """
    Возвращает текущий баланс {efhc, bonus, kwh} как строки с 3 знаками.
    """
    q = await db.execute(
        text(f"""
            SELECT efhc, bonus, kwh
              FROM {SCHEMA_CORE}.balances
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id},
    )
    row = q.fetchone()
    if not row:
        return {"efhc": "0.000", "bonus": "0.000", "kwh": "0.000"}
    efhc, bonus, kwh = (Decimal(row[0] or 0), Decimal(row[1] or 0), Decimal(row[2] or 0))
    return {"efhc": f"{d3(efhc):.3f}", "bonus": f"{d3(bonus):.3f}", "kwh": f"{d3(kwh):.3f}"}

async def _update_balance(db: AsyncSession, telegram_id: int, efhc_delta: Decimal = Decimal("0"), bonus_delta: Decimal = Decimal("0"), kwh_delta: Decimal = Decimal("0")) -> None:
    """
    Применяет изменения к балансу EFHC/bonus/kWh у пользователя.
    """
    await db.execute(
        text(f"""
            UPDATE {SCHEMA_CORE}.balances
               SET efhc = COALESCE(efhc, 0) + :d_e,
                   bonus = COALESCE(bonus, 0) + :d_b,
                   kwh = COALESCE(kwh, 0) + :d_k
             WHERE telegram_id = :tg
        """),
        {"tg": telegram_id, "d_e": str(d3(efhc_delta)), "d_b": str(d3(bonus_delta)), "d_k": str(d3(kwh_delta))},
    )

async def _get_panels_count(db: AsyncSession, telegram_id: int) -> int:
    """
    Возвращает количество панелей (level=1) у пользователя.
    """
    q = await db.execute(
        text(f"""
            SELECT count
              FROM {SCHEMA_CORE}.user_panels
             WHERE telegram_id = :tg AND level = 1
        """),
        {"tg": telegram_id},
    )
    row = q.fetchone()
    return int(row[0]) if row else 0

async def _set_panels_count(db: AsyncSession, telegram_id: int, new_count: int) -> None:
    """
    Устанавливает количество панелей (level=1) у пользователя.
    """
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.user_panels (telegram_id, level, count)
            VALUES (:tg, 1, :c)
            ON CONFLICT (telegram_id, level) DO UPDATE SET count = :c, updated_at = now()
        """),
        {"tg": telegram_id, "c": new_count},
    )

# -----------------------------------------------------------------------------
# Pydantic-схемы запросов/ответов
# -----------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    referral_code: Optional[str] = Field(None, description="Реферальный код (если есть)")

class BalanceResponse(BaseModel):
    efhc: str
    bonus: str
    kwh: str
    panels: int
    vip: bool

class ExchangeRequest(BaseModel):
    direction: str = Field(..., description="Направление: 'efhc_to_kwh' или 'kwh_to_efhc'")
    amount: Decimal = Field(..., ge=Decimal("0.001"), description="Количество для обмена (строго >0)")

class ExchangeResponse(BaseModel):
    ok: bool
    efhc: str
    kwh: str
    bonus: Optional[str] = None

class BuyPanelsRequest(BaseModel):
    count: int = Field(..., ge=1, le=PANEL_MAX_COUNT, description="Сколько панелей купить (1..1000)")

class BuyPanelsResponse(BaseModel):
    ok: bool
    panels: int
    efhc: str

class ReferralsResponse(BaseModel):
    code: str
    total_referrals: int
    total_panels_by_refs: int

class TasksResponse(BaseModel):
    items: List[Dict[str, Any]]

class TaskCompleteRequest(BaseModel):
    task_id: int

class TaskCompleteResponse(BaseModel):
    ok: bool
    reward_bonus_efhc: str

class LotteriesResponse(BaseModel):
    items: List[Dict[str, Any]]

class LotteryBuyRequest(BaseModel):
    code: str
    pay_with: str = Field("EFHC", description="чем платим: в текущей версии EFHC")

class LotteryBuyResponse(BaseModel):
    ok: bool
    tickets_sold: int
    efhc: Optional[str] = None

class WithdrawRequest(BaseModel):
    amount_efhc: Decimal = Field(..., gt=Decimal("0.000"))
    to_wallet: str = Field(..., description="TON адрес получателя")
    memo: Optional[str] = None

class WithdrawResponse(BaseModel):
    ok: bool
    request_id: int

# -----------------------------------------------------------------------------
# Роутер
# -----------------------------------------------------------------------------
router = APIRouter()

# -----------------------------------------------------------------------------
# Эндпоинт: /user/register — регистрация пользователя, привязка реферала (если передан referral_code)
# -----------------------------------------------------------------------------
@router.post("/user/register")
async def user_register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data"),
):
    """
    Регистрирует пользователя на основании Telegram WebApp initData:
      - Проверяет подпись initData (Telegram) — защита от подделки.
      - Создаёт запись в efhc_core.users, efhc_core.balances.
      - Если был referral_code, привязывает в efhc_referrals.referrals (если это первая регистрация).
      - Возвращает текущий баланс и состояние панелей/VIP.
    """
    # Проверка initData и извлечение telegram_id
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    # Реферальная привязка
    if payload.referral_code:
        code = payload.referral_code.strip()
        # Найдём кто владелец этого кода
        q = await db.execute(
            text(f"""
                SELECT telegram_id
                  FROM {SCHEMA_REFERRAL}.referral_links
                 WHERE referral_code = :code
            """),
            {"code": code},
        )
        row = q.fetchone()
        if row:
            referrer = int(row[0])
            # Привяжем если не привязан ранее (и не сам себя)
            if referrer != telegram_id:
                await db.execute(
                    text(f"""
                        INSERT INTO {SCHEMA_REFERRAL}.referrals (telegram_id, referred_by)
                        VALUES (:tg, :ref)
                        ON CONFLICT (telegram_id) DO NOTHING
                    """),
                    {"tg": telegram_id, "ref": referrer},
                )

    # Баланс
    bal = await _get_balance(db, telegram_id)
    panels = await _get_panels_count(db, telegram_id)

    # VIP флаг
    q_v = await db.execute(
        text(f"SELECT 1 FROM {SCHEMA_CORE}.user_vip WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    vip = q_v.scalar() is not None

    await db.commit()
    return {
        "ok": True,
        "telegram_id": telegram_id,
        "balance": bal,
        "panels": panels,
        "vip": vip,
        "panel_price_efhc": f"{PANEL_PRICE_EFHC:.3f}",
        "panel_daily_kwh": f"{PANEL_DAILY_KWH:.3f}",
        "panel_limit": PANEL_MAX_COUNT,
    }

# -----------------------------------------------------------------------------
# Эндпоинт: /user/balance — получить текущее состояние баланса и панелей
# -----------------------------------------------------------------------------
@router.get("/user/balance", response_model=BalanceResponse)
async def user_balance(
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Возвращает баланс EFHC/bonus/kWh, количество панелей и флаг VIP для текущего пользователя.
    Идентификация происходит через проверку Telegram WebApp initData.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    bal = await _get_balance(db, telegram_id)
    panels = await _get_panels_count(db, telegram_id)

    q_v = await db.execute(
        text(f"SELECT 1 FROM {SCHEMA_CORE}.user_vip WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    vip = q_v.scalar() is not None

    await db.commit()
    return BalanceResponse(
        efhc=bal["efhc"],
        bonus=bal["bonus"],
        kwh=bal["kwh"],
        panels=panels,
        vip=vip
    )

# -----------------------------------------------------------------------------
# Эндпоинт: /user/exchange — обмен EFHC ⇄ kWh
# -----------------------------------------------------------------------------
@router.post("/user/exchange", response_model=ExchangeResponse)
async def user_exchange(
    payload: ExchangeRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Выполняет обмен EFHC ⇄ kWh по курсу 1:1. 
    Проверяем баланс, округление до 3 знаков.
      - direction = 'efhc_to_kwh': уменьшаем EFHC, увеличиваем kWh.
      - direction = 'kwh_to_efhc': уменьшаем kWh, увеличиваем EFHC.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    if payload.direction not in ("efhc_to_kwh", "kwh_to_efhc"):
        raise HTTPException(status_code=400, detail="Invalid direction")

    amount = d3(Decimal(payload.amount))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be > 0")

    # Получим текущие значения
    q = await db.execute(
        text(f"SELECT efhc, kwh FROM {SCHEMA_CORE}.balances WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    row = q.fetchone()
    efhc_cur = Decimal(row[0] or 0) if row else Decimal("0.000")
    kwh_cur = Decimal(row[1] or 0) if row else Decimal("0.000")

    if payload.direction == "efhc_to_kwh":
        # Проверка: хватает ли EFHC
        if efhc_cur < amount:
            raise HTTPException(status_code=400, detail="Insufficient EFHC balance")
        # 1 EFHC -> 1 kWh
        await _update_balance(db, telegram_id, efhc_delta=-amount, kwh_delta=amount)
    else:
        # kwh_to_efhc
        if kwh_cur < amount:
            raise HTTPException(status_code=400, detail="Insufficient kWh balance")
        await _update_balance(db, telegram_id, efhc_delta=amount, kwh_delta=-amount)

    bal = await _get_balance(db, telegram_id)
    await db.commit()
    return ExchangeResponse(ok=True, efhc=bal["efhc"], kwh=bal["kwh"], bonus=bal["bonus"])

# -----------------------------------------------------------------------------
# Эндпоинт: /user/panels — список/количество панелей
# -----------------------------------------------------------------------------
@router.get("/user/panels")
async def user_panels(
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Возвращает текущее количество панелей у пользователя и параметры панели (цена, генерация).
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    panels = await _get_panels_count(db, telegram_id)
    bal = await _get_balance(db, telegram_id)

    return {
        "panels": panels,
        "panel_price_efhc": f"{PANEL_PRICE_EFHC:.3f}",
        "panel_daily_kwh": f"{PANEL_DAILY_KWH:.3f}",
        "panel_limit": PANEL_MAX_COUNT,
        "balance": bal
    }

# -----------------------------------------------------------------------------
# Эндпоинт: /user/panels/buy — покупка панелей
# -----------------------------------------------------------------------------
@router.post("/user/panels/buy", response_model=BuyPanelsResponse)
async def user_panels_buy(
    payload: BuyPanelsRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Покупка N панелей. Ограничение на общее количество: 1000.
    Стоимость: N * 100 EFHC (PANEL_PRICE_EFHC).
    Списываем EFHC и увеличиваем количество панелей (level=1).
    Этот эндпоинт не начисляет энергию — она начисляется ежедневным scheduler'ом.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    buy_count = int(payload.count)
    if buy_count <= 0:
        raise HTTPException(status_code=400, detail="Invalid count")

    cur_panels = await _get_panels_count(db, telegram_id)
    if cur_panels + buy_count > PANEL_MAX_COUNT:
        raise HTTPException(status_code=400, detail=f"Cannot exceed panel limit ({PANEL_MAX_COUNT})")

    total_price = d3(PANEL_PRICE_EFHC * Decimal(buy_count))

    # Проверяем баланс EFHC
    q = await db.execute(text(f"""
        SELECT efhc FROM {SCHEMA_CORE}.balances WHERE telegram_id = :tg
    """), {"tg": telegram_id})
    row = q.fetchone()
    efhc_cur = Decimal(row[0] or 0) if row else Decimal("0.000")
    if efhc_cur < total_price:
        raise HTTPException(status_code=400, detail="Insufficient EFHC for purchase")

    # Списать EFHC
    await _update_balance(db, telegram_id, efhc_delta=-total_price)
    # Увеличить количество панелей
    new_count = cur_panels + buy_count
    await _set_panels_count(db, telegram_id, new_count)

    bal = await _get_balance(db, telegram_id)
    await db.commit()
    return BuyPanelsResponse(ok=True, panels=new_count, efhc=bal["efhc"])

# -----------------------------------------------------------------------------
# Эндпоинт: /user/referrals — показать/создать ref-код, статистика
# -----------------------------------------------------------------------------
@router.get("/user/referrals", response_model=ReferralsResponse)
async def user_referrals(
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Возвращает referral_code для текущего пользователя (создаёт, если нет),
    и статистику рефералов: общее число, суммарные покупки панелей.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    # Убедимся, что есть referral_link
    q = await db.execute(
        text(f"SELECT referral_code FROM {SCHEMA_REFERRAL}.referral_links WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    row = q.fetchone()
    if row:
        code = row[0]
    else:
        # Сгенерируем код как 'EFHC{telegram_id}' или по иной логике
        code = f"EFHC{telegram_id}"
        # В случае коллизии — можно добавить suffix.
        try:
            await db.execute(
                text(f"""
                    INSERT INTO {SCHEMA_REFERRAL}.referral_links (telegram_id, referral_code)
                    VALUES (:tg, :code)
                """),
                {"tg": telegram_id, "code": code},
            )
        except Exception:
            # fallback при коллизии
            code = f"EFHC{telegram_id}X"
            await db.execute(
                text(f"""
                    INSERT INTO {SCHEMA_REFERRAL}.referral_links (telegram_id, referral_code)
                    VALUES (:tg, :code)
                    ON CONFLICT (telegram_id) DO UPDATE SET referral_code = EXCLUDED.referral_code
                """),
                {"tg": telegram_id, "code": code},
            )

    # Статистика: количество рефералов
    q2 = await db.execute(
        text(f"""
            SELECT COUNT(*) 
              FROM {SCHEMA_REFERRAL}.referrals
             WHERE referred_by = :tg
        """),
        {"tg": telegram_id},
    )
    total_refs = int(q2.scalar() or 0)

    # Суммарные покупки панелей рефералами: простая метрика по текущему count в user_panels
    # При желании, можно учитывать историю/лог покупок.
    q3 = await db.execute(
        text(f"""
            SELECT COALESCE(SUM(up.count), 0)
              FROM {SCHEMA_CORE}.user_panels up
             WHERE up.telegram_id IN (
                SELECT r.telegram_id FROM {SCHEMA_REFERRAL}.referrals r WHERE r.referred_by = :tg
             )
        """),
        {"tg": telegram_id},
    )
    total_panels_by_refs = int(q3.scalar() or 0)

    await db.commit()
    return ReferralsResponse(code=code, total_referrals=total_refs, total_panels_by_refs=total_panels_by_refs)

# -----------------------------------------------------------------------------
# Эндпоинт: /user/tasks — список активных задач
# -----------------------------------------------------------------------------
@router.get("/user/tasks", response_model=TasksResponse)
async def user_tasks_list(
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Возвращает список активных задач для выполнения:
      [{id, title, url, reward_bonus_efhc}, ...]
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    q = await db.execute(
        text(f"""
            SELECT id, title, url, reward_bonus_efhc
              FROM {SCHEMA_TASKS}.tasks
             WHERE active = TRUE
             ORDER BY id ASC
        """)
    )
    items = []
    for r in q.all() or []:
        items.append({
            "id": int(r[0]),
            "title": r[1],
            "url": r[2],
            "reward_bonus_efhc": f"{d3(Decimal(r[3] or 0)):.3f}"
        })
    await db.commit()
    return TasksResponse(items=items)

# -----------------------------------------------------------------------------
# Эндпоинт: /user/tasks/complete — отметить выполнение задачи
# -----------------------------------------------------------------------------
@router.post("/user/tasks/complete", response_model=TaskCompleteResponse)
async def user_task_complete(
    payload: TaskCompleteRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Помечает задачу как выполненную и начисляет bonus EFHC пользователю (одноразово).
    Для предотвращения повторного выполнения можно запретить повтор.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    # Найдём награду
    q = await db.execute(
        text(f"SELECT reward_bonus_efhc FROM {SCHEMA_TASKS}.tasks WHERE id = :tid AND active = TRUE"),
        {"tid": payload.task_id},
    )
    row = q.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found or not active")

    reward = d3(Decimal(row[0] or 0))
    if reward <= 0:
        raise HTTPException(status_code=400, detail="Task reward is zero")

    # Запретить повторное выполнение — проверим наличие записи
    q2 = await db.execute(
        text(f"""
            SELECT 1 FROM {SCHEMA_TASKS}.user_tasks
             WHERE telegram_id = :tg AND task_id = :tid
        """),
        {"tg": telegram_id, "tid": payload.task_id},
    )
    if q2.scalar():
        raise HTTPException(status_code=400, detail="Task already completed")

    # Начисляем bonus EFHC
    await _update_balance(db, telegram_id, bonus_delta=reward)

    # Фиксируем выполнение
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_TASKS}.user_tasks (telegram_id, task_id)
            VALUES (:tg, :tid)
        """),
        {"tg": telegram_id, "tid": payload.task_id},
    )

    await db.commit()
    return TaskCompleteResponse(ok=True, reward_bonus_efhc=f"{reward:.3f}")

# -----------------------------------------------------------------------------
# Эндпоинт: /user/lotteries — активные лотереи
# -----------------------------------------------------------------------------
@router.get("/user/lotteries", response_model=LotteriesResponse)
async def user_lotteries(
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Возвращает список активных лотерей:
      [{code, title, prize_type, target_participants, tickets_sold}, ...]
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    q = await db.execute(
        text(f"""
            SELECT code, title, prize_type, target_participants, tickets_sold
              FROM {SCHEMA_LOTTERY}.lotteries
             WHERE active = TRUE
             ORDER BY created_at ASC
        """)
    )
    items = []
    for r in q.all() or []:
        items.append({
            "code": r[0],
            "title": r[1],
            "prize_type": r[2],
            "target_participants": int(r[3] or 0),
            "tickets_sold": int(r[4] or 0),
        })
    await db.commit()
    return LotteriesResponse(items=items)

# -----------------------------------------------------------------------------
# Эндпоинт: /user/lottery/buy — покупка билета лотереи
# -----------------------------------------------------------------------------
@router.post("/user/lottery/buy", response_model=LotteryBuyResponse)
async def user_lottery_buy(
    payload: LotteryBuyRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Покупка 1 билета лотереи code.
    В текущей версии оплата EFHC; можно расширить на bonus EFHC при желании.
    ВАЖНО: цену билета лучше хранить в lotteries (отдельное поле), но сейчас используем 1 EFHC для примера.
    **Уточните цену билета лотереи.** Здесь поставим 1 EFHC как дефолт.
    """
    TICKET_PRICE_EFHC = Decimal("1.000")  # TODO: вынести в конфиг или поле таблицы lotteries

    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    # Проверим, что лотерея активна
    q = await db.execute(
        text(f"""
            SELECT active, tickets_sold, target_participants
              FROM {SCHEMA_LOTTERY}.lotteries
             WHERE code = :code
        """),
        {"code": payload.code}
    )
    row = q.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Lottery not found")
    active, tickets_sold, target_participants = bool(row[0]), int(row[1] or 0), int(row[2] or 0)
    if not active:
        raise HTTPException(status_code=400, detail="Lottery is not active")

    # Проверим баланс EFHC
    q2 = await db.execute(
        text(f"SELECT efhc FROM {SCHEMA_CORE}.balances WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    row2 = q2.fetchone()
    efhc_cur = Decimal(row2[0] or 0) if row2 else Decimal("0.000")
    if efhc_cur < TICKET_PRICE_EFHC:
        raise HTTPException(status_code=400, detail="Insufficient EFHC")

    # Списываем EFHC и создаём билет
    await _update_balance(db, telegram_id, efhc_delta=-TICKET_PRICE_EFHC)
    await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_LOTTERY}.lottery_tickets (lottery_code, telegram_id)
            VALUES (:code, :tg)
        """),
        {"code": payload.code, "tg": telegram_id},
    )
    # Обновляем счётчик tickets_sold
    await db.execute(
        text(f"""
            UPDATE {SCHEMA_LOTTERY}.lotteries
               SET tickets_sold = tickets_sold + 1
             WHERE code = :code
        """),
        {"code": payload.code},
    )

    # Возвращаем текущее число билетов
    q3 = await db.execute(
        text(f"SELECT tickets_sold FROM {SCHEMA_LOTTERY}.lotteries WHERE code = :code"),
        {"code": payload.code},
    )
    ts = int(q3.scalar() or 0)

    bal = await _get_balance(db, telegram_id)
    await db.commit()
    return LotteryBuyResponse(ok=True, tickets_sold=ts, efhc=bal["efhc"])

# -----------------------------------------------------------------------------
# Эндпоинт: /user/withdraw — заявка на вывод EFHC (через TON/Jetton)
# -----------------------------------------------------------------------------
@router.post("/user/withdraw", response_model=WithdrawResponse)
async def user_withdraw(
    payload: WithdrawRequest,
    db: AsyncSession = Depends(get_session),
    x_tg_init_data: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data")
):
    """
    Создаёт заявку на вывод EFHC. В текущей реализации:
      - Проверяем баланс EFHC, блокируем количество на вывод (списываем сразу).
      - Создаём запись в efhc_core.withdraw_requests (нужна таблица).
      - Обработчик/админ затем исполняет заявку (см. admin_routes/тон-логика).
    В реальной жизни тут потребуется автоматизированная интеграция с TonSmartContract или TonAPI.
    """
    auth = await _verify_webapp_request(x_tg_init_data, settings_token=settings.TELEGRAM_BOT_TOKEN)
    telegram_id = auth["telegram_id"]
    username = auth["username"]

    await ensure_user_routes_tables(db)
    await _ensure_user_exists(db, telegram_id, username)

    amount = d3(Decimal(payload.amount_efhc))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    # Проверяем баланс EFHC
    q = await db.execute(
        text(f"SELECT efhc FROM {SCHEMA_CORE}.balances WHERE telegram_id = :tg"),
        {"tg": telegram_id},
    )
    row = q.fetchone()
    efhc_cur = Decimal(row[0] or 0) if row else Decimal("0.000")
    if efhc_cur < amount:
        raise HTTPException(status_code=400, detail="Insufficient EFHC")

    # Списываем EFHC (блокируем)
    await _update_balance(db, telegram_id, efhc_delta=-amount)

    # Убедимся, что таблица заявок существует:
    await db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_CORE}.withdraw_requests (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            amount_efhc NUMERIC(30, 3) NOT NULL,
            to_wallet TEXT NOT NULL,
            memo TEXT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ DEFAULT now(),
            processed_at TIMESTAMPTZ NULL
        );
    """))

    # Создаём заявку
    res = await db.execute(
        text(f"""
            INSERT INTO {SCHEMA_CORE}.withdraw_requests (telegram_id, amount_efhc, to_wallet, memo)
            VALUES (:tg, :amt, :dst, :memo)
            RETURNING id
        """),
        {"tg": telegram_id, "amt": str(amount), "dst": payload.to_wallet, "memo": payload.memo or None},
    )
    req_id = int(res.fetchone()[0])

    await db.commit()
    return WithdrawResponse(ok=True, request_id=req_id)
