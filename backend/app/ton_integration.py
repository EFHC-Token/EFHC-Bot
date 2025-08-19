# 📂 backend/app/ton_integration.py — интеграция с TON (TonAPI) и авто-начисления
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Опрашивает TonAPI (tonapi.io) по адресу получателя (TON_WALLET_ADDRESS) и
#     читает последние события/транзакции.
#   • Обрабатывает события:
#       - TonTransfer (входящий TON перевод)
#       - JettonTransfer (входящий jetton; используем для EFHC и при желании USDT)
#   • Парсит memo/комментарий (comment) по нескольким форматам:
#       1) Ваш формат: "id telegram 4357333, 100 EFHC", "id:4357333 100 efhc", "tg 4357333 vip", "4357333 vip nft"
#       2) Формат Shop-кодов: "362746228 EFHC_100_TON", "tg=362746228 code=VIP_USDT"
#   • Дедуплицирует события по event_id (TonAPI) — НЕ зачисляет второй раз.
#   • Начисляет внутренние EFHC пользователю (efhc_core.balances) или ставит VIP-флаг.
#   • Пишет лог `efhc_core.ton_events_log` с подробностями (для админки и аудита).
#   • Содержит фоновую задачу `ton_watcher_loop()` (вызывается из main.py), а также
#     утилиту `manual_process_once()` — для ручного запуска без лупа.
#
# Предпосылки/требования:
#   • В config.py заданы:
#       - TON_WALLET_ADDRESS (кошелёк Tonkeeper для приёма)
#       - NFT_PROVIDER_BASE_URL (https://tonapi.io)
#       - NFT_PROVIDER_API_KEY (ваш API key)
#       - EFHC_TOKEN_ADDRESS (адрес jetton EFHC)
#       - EFHC_DECIMALS (количество знаков EFHC, у нас 3)
#       - (опционально) USDT_JETTON_ADDRESS (если захотите учитывать USDT Jetton)
#   • Схемы БД создаются модулем database.ensure_schemas(), таблицы ниже создаются здесь (idempotent).
#   • Для безопасности/устойчивости — все mutate-операции в БД идут в транзакциях.
#
# Как используется:
#   • В main.py добавляем фоновый воркер TON:
#         asyncio.create_task(ton_watcher_loop(poll_interval=30))
#   • Также можно вызвать единоразово:
#         await manual_process_once(limit=50)
#   • А в admin_routes.py есть просмотр логов /admin/ton/logs.
#
# Таблицы:
#   efhc_core.ton_events_log (
#       event_id TEXT PRIMARY KEY,
#       ts TIMESTAMPTZ DEFAULT now(),
#       action_type TEXT,
#       asset TEXT,
#       amount NUMERIC(30, 9),
#       decimals INT,
#       from_addr TEXT,
#       to_addr TEXT,
#       memo TEXT,
#       telegram_id BIGINT NULL,
#       parsed_amount_efhc NUMERIC(30, 3) NULL,
#       vip_requested BOOLEAN DEFAULT FALSE,
#       processed BOOLEAN DEFAULT TRUE,
#       processed_at TIMESTAMPTZ DEFAULT now()
#   )
#
#   efhc_core.users (
#       telegram_id BIGINT PRIMARY KEY,
#       username TEXT NULL,
#       created_at TIMESTAMPTZ DEFAULT now()
#   )
#
#   efhc_core.balances (
#       telegram_id BIGINT PRIMARY KEY,
#       efhc NUMERIC(30, 3) DEFAULT 0,
#       bonus NUMERIC(30, 3) DEFAULT 0,
#       kwh  NUMERIC(30, 3) DEFAULT 0
#   )
#
#   efhc_core.user_vip (
#       telegram_id BIGINT PRIMARY KEY,
#       since TIMESTAMPTZ DEFAULT now()
#   )
#
# Бизнес-логика:
#   • EFHC = 1 кВт — это используется в обменнике (другой модуль).
#   • VIP = +7% генерации — учитывается в scheduler.py при начислениях энергии (не здесь).
#   • Ограничение панелей 1000 — на уровне /user/panels/buy.
#   • Shop-коды:
#       EFHC_10_TON, EFHC_100_TON, EFHC_1000_TON → начисление EFHC;
#       VIP_TON, VIP_USDT → установка VIP.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .config import get_settings
from .database import get_session

# -----------------------------------------------------------------------------
# Настройки из config.py (все переменные ENV / конфигурация проекта)
# -----------------------------------------------------------------------------
settings = get_settings()

# Адрес кошелька TON проекта (Tonkeeper)
TON_WALLET_ADDRESS = (settings.TON_WALLET_ADDRESS or "").strip()

# Базовый URL TonAPI (провайдер NFT/TON) и API ключ
TON_API_BASE = (settings.NFT_PROVIDER_BASE_URL or "https://tonapi.io").rstrip("/")
TON_API_KEY = settings.NFT_PROVIDER_API_KEY

# EFHC jetton адрес (для входящих JettonTransfer, если приходит EFHC как токен)
EFHC_JETTON_ADDRESS = (settings.EFHC_TOKEN_ADDRESS or "").strip()
EFHC_DECIMALS = int(getattr(settings, "EFHC_DECIMALS", 3) or 3)

# Опционально: USDT Jetton адрес, если нужно учитывать USDT в будущем (не обязательно)
USDT_JETTON_ADDRESS = (getattr(settings, "USDT_JETTON_ADDRESS", "") or "").strip()

# -----------------------------------------------------------------------------
# Константы округления и единицы
# -----------------------------------------------------------------------------
NANO_TON_DECIMALS = Decimal("1e9")   # 1 TON = 1e9 nanoton
DEC3 = Decimal("0.001")              # 3 знака после запятой (EFHC/kWh/bonus)
DEC9 = Decimal("0.000000001")        # 9 знаков (TON, raw jetton amounts возможно)

def _d3(x: Decimal) -> Decimal:
    """
    Округление до 3 знаков (EFHC/kWh/bonus). ROUND_DOWN — избегаем расширенной записи,
    более безопасно для балансов.
    """
    return x.quantize(DEC3, rounding=ROUND_DOWN)

def _d9(x: Decimal) -> Decimal:
    """
    Округление до 9 знаков (TON и нативные токены).
    """
    return x.quantize(DEC9, rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# Подготовка таблиц (idempotent) — создаём только если отсутствуют
# -----------------------------------------------------------------------------
CREATE_TABLES_SQL = """
-- Лог входящих событий из TonAPI: фиксируем факт и наши действия
CREATE TABLE IF NOT EXISTS efhc_core.ton_events_log (
    event_id TEXT PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    action_type TEXT,
    asset TEXT,
    amount NUMERIC(30, 9),
    decimals INT,
    from_addr TEXT,
    to_addr TEXT,
    memo TEXT,
    telegram_id BIGINT NULL,
    parsed_amount_efhc NUMERIC(30, 3) NULL,
    vip_requested BOOLEAN DEFAULT FALSE,
    processed BOOLEAN DEFAULT TRUE,
    processed_at TIMESTAMPTZ DEFAULT now()
);

-- Пользователи EFHC (минимально; проект может расширять поля)
CREATE TABLE IF NOT EXISTS efhc_core.users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Балансы EFHC/bonus/kWh
CREATE TABLE IF NOT EXISTS efhc_core.balances (
    telegram_id BIGINT PRIMARY KEY REFERENCES efhc_core.users(telegram_id) ON DELETE CASCADE,
    efhc NUMERIC(30, 3) DEFAULT 0,
    bonus NUMERIC(30, 3) DEFAULT 0,
    kwh  NUMERIC(30, 3) DEFAULT 0
);

-- Внутренний VIP пользователя (НЕ админ-доступ)
CREATE TABLE IF NOT EXISTS efhc_core.user_vip (
    telegram_id BIGINT PRIMARY KEY REFERENCES efhc_core.users(telegram_id) ON DELETE CASCADE,
    since TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_ton_tables(db: AsyncSession) -> None:
    """
    Создаёт необходимые таблицы для логирования/начисления (если ещё не созданы).
    Идёмпотентно: многократный вызов не сломает схему.
    Используется в начале process_incoming_payments().
    """
    await db.execute(text(CREATE_TABLES_SQL))
    await db.commit()


# -----------------------------------------------------------------------------
# Парсер MEMO/комментария (несколько форматов)
# -----------------------------------------------------------------------------
# Для "старого" формата: "id telegram 4357333, 100 EFHC" / "id:4357333 vip"
MEMO_RE = re.compile(
    r"""
    (?:
        id[\s:_-]*telegram   # 'id telegram' (возможны : _ -)
        |id                  # или просто 'id'
        |tg
        |telegram
    )?
    [\s:_-]*                 # разделитель
    (?P<tg>\d{5,15})?        # Telegram ID (опционально)
    [^\dA-Za-z]+             # разделитель
    (?P<amount>\d+(?:[.,]\d{1,9})?)? # сумма (в т.ч. десятичная) — возможно EFHC число
    \s*
    (?P<asset>efhc|vip|nft|vip\s*nft)? # актив/тип ('EFHC', 'VIP', 'VIP NFT')
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Для дополнительных форматов Shop-кодов: "362746228 EFHC_100_TON", "tg=362746228 code=VIP_USDT"
RE_ID_CODE_SIMPLE = re.compile(r"(?P<tg>\d+)\s*[:=\s]\s*(?P<code>[A-Za-z0-9_]+)")
RE_TG_CODE_KV = re.compile(r"tg\s*[:=\s]\s*(?P<tg>\d+)\s+code\s*[:=\s]\s*(?P<code>[A-Za-z0-9_]+)", re.IGNORECASE)

VIP_RE = re.compile(r"\b(vip(?:\s*nft)?)\b", re.IGNORECASE)
EFHC_RE = re.compile(r"\befhc\b", re.IGNORECASE)

EFHC_PREFIX = "EFHC"   # Префикс Shop-кода для EFHC (EFHC_100_TON)
VIP_PREFIX = "VIP"     # Префикс Shop-кода для VIP (VIP_TON, VIP_USDT)

def parse_memo_for_payment(memo: str) -> Tuple[Optional[int], Optional[Decimal], Optional[str], bool]:
    """
    Разбирает "старый" формат комментариев.
    Возвращает: (telegram_id, amount, asset, vip_flag)
        telegram_id: int|None
        amount: Decimal|None (в EFHC)
        asset: "EFHC" или None
        vip_flag: True, если указано VIP/VIP NFT (приоритетнее суммы)
    Поддерживаемые варианты:
        "id telegram 4357333, 100 EFHC"
        "id:4357333 100 efhc"
        "tg 4357333 vip"
        "4357333 vip nft"
    """
    if not memo:
        return (None, None, None, False)

    memo_norm = memo.strip()
    vip_flag = bool(VIP_RE.search(memo_norm))

    m = MEMO_RE.search(memo_norm)
    tg_id: Optional[int] = None
    amount: Optional[Decimal] = None
    asset: Optional[str] = None

    if m:
        tg_str = m.group("tg")
        amt_str = m.group("amount")
        asset_str = m.group("asset")

        if tg_str:
            try:
                tg_id = int(tg_str)
            except Exception:
                tg_id = None

        if amt_str:
            amt_str = amt_str.replace(",", ".")
            try:
                amount = Decimal(amt_str)
            except Exception:
                amount = None

        if asset_str:
            if "vip" in asset_str.lower():
                asset = "VIP"
                vip_flag = True
            elif "efhc" in asset_str.lower():
                asset = "EFHC"

    if asset is None and EFHC_RE.search(memo_norm):
        asset = "EFHC"

    return (tg_id, amount, asset, vip_flag)


def _parse_shop_comment(comment: str) -> Dict[str, Any]:
    """
    Парсит "shop-формат" комментариев:
        "362746228 EFHC_100_TON"
        "362746228:VIP_USDT"
        "tg=362746228 code=EFHC_1000_TON"
    Возвращает:
      {
        "ok": True/False,
        "telegram_id": int|None,
        "code": str|None (например, EFHC_100_TON, VIP_TON)
      }
    """
    c = (comment or "").strip()
    if not c:
        return {"ok": False, "telegram_id": None, "code": None}

    m = RE_TG_CODE_KV.search(c)
    if m:
        return {"ok": True, "telegram_id": int(m.group("tg")), "code": m.group("code").upper()}

    m2 = RE_ID_CODE_SIMPLE.search(c)
    if m2:
        return {"ok": True, "telegram_id": int(m2.group("tg")), "code": m2.group("code").upper()}

    return {"ok": False, "telegram_id": None, "code": None}


def _guess_shop_item_by_code(code: str) -> Dict[str, Any]:
    """
    Определяем, что за товар по коду: 'EFHC_100_TON', 'VIP_USDT' и т.п.
    Возвращает структуру:
    {
        "action_type": "SHOP_PURCHASE" или "VIP_BUY" или "UNKNOWN",
        "efhc_amount": Decimal(...) или None,
        "vip_requested": bool
    }
    """
    code = (code or "").strip().upper()
    if not code:
        return {"action_type": "UNKNOWN", "efhc_amount": None, "vip_requested": False}

    if code.startswith(EFHC_PREFIX):
        # Ожидаем формат: EFHC_{число}_ASSET
        m = re.search(r"EFHC[_-](\d+)", code)
        if m:
            amt = Decimal(m.group(1))
            return {"action_type": "SHOP_PURCHASE", "efhc_amount": amt, "vip_requested": False}

    if code.startswith(VIP_PREFIX):
        return {"action_type": "VIP_BUY", "efhc_amount": None, "vip_requested": True}

    return {"action_type": "UNKNOWN", "efhc_amount": None, "vip_requested": False}


# -----------------------------------------------------------------------------
# Работа с балансами/пользователями (через SQL)
# -----------------------------------------------------------------------------
async def _ensure_user_exists(db: AsyncSession, telegram_id: int) -> None:
    """
    Убедиться, что пользователь есть в efhc_core.users и efhc_core.balances.
    Если нет — создаём. Без commit — вызывающий решает, когда фиксировать транзакцию.
    """
    await db.execute(
        text("""
            INSERT INTO efhc_core.users (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )
    await db.execute(
        text("""
            INSERT INTO efhc_core.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )

async def credit_efhc(db: AsyncSession, telegram_id: int, amount_efhc: Decimal) -> None:
    """
    Начислить внутренние EFHC пользователю.
    amount_efhc — Decimal (в EFHC), округляем до 3 знаков.
    """
    await _ensure_user_exists(db, telegram_id)
    await db.execute(
        text("""
            UPDATE efhc_core.balances
               SET efhc = COALESCE(efhc, 0) + :amt
             WHERE telegram_id = :tg
        """),
        {"amt": str(_d3(amount_efhc)), "tg": telegram_id},
    )

async def set_user_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Пометить пользователя как VIP (внутренний флаг, влияет на +7% генерации).
    Это НЕ админ-доступ (админ-доступ по NFT whitelist в admin_routes/nft_checker).
    """
    await _ensure_user_exists(db, telegram_id)
    await db.execute(
        text("""
            INSERT INTO efhc_core.user_vip (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """),
        {"tg": telegram_id},
    )


# -----------------------------------------------------------------------------
# Вызовы TonAPI (tonapi.io) — HTTP-клиент и заголовки
# -----------------------------------------------------------------------------
def _tonapi_headers() -> Dict[str, str]:
    """
    Заголовки запроса к TonAPI.
    Варианты провайдера требуют либо 'X-API-Key: <key>', либо 'Authorization: Bearer <token>'.
    Здесь используем X-API-Key, корректно для tonapi.io на текущий момент.
    """
    hdrs = {"Accept": "application/json"}
    if TON_API_KEY:
        hdrs["X-API-Key"] = TON_API_KEY
    return hdrs

async def fetch_address_events(address: str, limit: int = 50, before_lt: Optional[int] = None) -> Dict[str, Any]:
    """
    Получить события адреса (account events) с TonAPI v2.
    Документация: GET /v2/accounts/{address}/events
    Параметры:
      - address: наш TON_WALLET_ADDRESS (куда приходят переводы).
      - limit: количество событий.
      - before_lt: пагинация (идём назад по lt) — опционально.
    Возвращает JSON dict с ключами вида "events" или "items".
    """
    base = TON_API_BASE
    url = f"{base}/v2/accounts/{address}/events?limit={limit}"
    if before_lt is not None:
        url += f"&before_lt={before_lt}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_tonapi_headers())
        r.raise_for_status()
        return r.json()


# -----------------------------------------------------------------------------
# Утилиты: логирование/проверка повторов/декодеры сумм
# -----------------------------------------------------------------------------
async def _is_event_seen(db: AsyncSession, event_id: str) -> bool:
    """
    Проверка: есть ли event_id в логах (чтобы не обработать дважды).
    """
    q = await db.execute(
        text("SELECT 1 FROM efhc_core.ton_events_log WHERE event_id = :eid"),
        {"eid": event_id},
    )
    return q.scalar() is not None

async def _log_event(
    db: AsyncSession,
    event_id: str,
    action_type: str,
    asset: str,
    amount: Decimal,
    decimals: int,
    from_addr: str,
    to_addr: str,
    memo: Optional[str],
    telegram_id: Optional[int],
    parsed_amount_efhc: Optional[Decimal],
    vip_requested: bool,
) -> None:
    """
    Записать событие в лог (processed=TRUE, так как выполняем начисления сразу).
    Используется как финальный этап в обработке каждого action/события.
    """
    await db.execute(
        text("""
            INSERT INTO efhc_core.ton_events_log(
                event_id, action_type, asset, amount, decimals, from_addr, to_addr, memo,
                telegram_id, parsed_amount_efhc, vip_requested, processed
            )
            VALUES (:eid, :atype, :asset, :amt, :dec, :from_addr, :to_addr, :memo,
                    :tg, :pa, :vip, TRUE)
            ON CONFLICT (event_id) DO NOTHING
        """),
        {
            "eid": event_id,
            "atype": action_type,
            "asset": asset,
            "amt": str(amount),
            "dec": decimals,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "memo": memo or "",
            "tg": telegram_id,
            "pa": str(parsed_amount_efhc) if parsed_amount_efhc is not None else None,
            "vip": vip_requested,
        },
    )

def _decode_ton_amount(nano: int) -> Decimal:
    """
    Преобразование количество в nanoton → TON (Decimal с 9 знаками).
    """
    return _d9(Decimal(nano) / NANO_TON_DECIMALS)

def _decode_jetton_amount(raw: str, decimals: int) -> Decimal:
    """
    Преобразует строковую сумму jetton (в "минимальных" единицах) в человекочитаемый Decimal.
    raw: строка вида "123000" (как возвращает TonAPI).
    decimals: знаков после запятой у jetton (для EFHC — 3).
    """
    q = Decimal(f"1e{decimals}")
    return (Decimal(raw) / q).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)


# -----------------------------------------------------------------------------
# Основная "ушаниц" логика: обработать входящие платежи
# -----------------------------------------------------------------------------
async def process_incoming_payments(db: AsyncSession, limit: int = 50) -> int:
    """
    Публичная функция: опрашивает TonAPI и обрабатывает новые входящие платежи.
    Возвращает количество обработанных действий (action), а не событий (event).

    Алгоритм:
      1) ensure_ton_tables() — создаёт таблицы, если их нет.
      2) fetch_address_events — получаем события кошелька TON_WALLET_ADDRESS.
      3) Для каждого события:
            - если event_id уже в логе → пропускаем,
            - иначе разбираем actions (TonTransfer / JettonTransfer):
                • TonTransfer → парсим memo:
                    - по старому формату: "id:4357333 100 EFHC" / "tg 4357333 vip"
                    - по shop-коду: "362746228 EFHC_100_TON" / "tg=362746228 code=VIP_USDT"
                  Если code=EFHC_*_* → начисляем EFHC.
                  Если code=VIP_* → ставим VIP.
                  Если старый формат EFHC N → начисляем N EFHC.
                • JettonTransfer EFHC → начисляем EFHC на величину jetton (лучше с tg_id из memo).
                  Если tg_id нет — логируем, но не зачисляем (иначе не знаем кому).
                • USDT/прочие jetton → логируем (на ваш выбор можно расширить).
            - Логируем ton_events_log для аудита/дедупликаций.
      4) Возвращаем число обработанных action.
    """
    await ensure_ton_tables(db)

    wallet = TON_WALLET_ADDRESS
    if not wallet:
        print("[EFHC][TON] WARN: TON_WALLET_ADDRESS не задан — обработка TON платежей отключена.")
        return 0

    try:
        data = await fetch_address_events(address=wallet, limit=limit)
    except httpx.HTTPError as e:
        print(f"[EFHC][TON] fetch events error: {e}")
        return 0

    events: List[Dict[str, Any]] = data.get("events", []) or data.get("items", []) or []
    handled_actions = 0

    efhc_addr = (EFHC_JETTON_ADDRESS or "").strip()
    usdt_addr = (USDT_JETTON_ADDRESS or "").strip()

    for ev in events:
        event_id = ev.get("event_id") or ev.get("id") or ""
        if not event_id:
            # Если TonAPI вернул пустой id — пропускаем
            continue

        # Дедупликация события (важно!)
        if await _is_event_seen(db, event_id=event_id):
            continue

        actions = ev.get("actions", []) or []
        for action in actions:
            try:
                atype = action.get("type") or ""

                # ---------------------------------------------------------
                # 1) Входящий нативный TON
                # ---------------------------------------------------------
                if atype == "TonTransfer" and action.get("TonTransfer"):
                    obj = action["TonTransfer"]
                    to_addr = (obj.get("recipient", {}) or {}).get("address") or ""
                    if (to_addr or "").lower() != wallet.lower():
                        continue  # не наш перевод

                    amount_nano = int(obj.get("amount", 0))
                    amount_ton = _decode_ton_amount(amount_nano)
                    comment = obj.get("comment") or obj.get("payload") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    # 1) Пробуем shop формат:
                    shop = _parse_shop_comment(comment)
                    if shop["ok"]:
                        tg_id = shop["telegram_id"]
                        item = _guess_shop_item_by_code(shop["code"])
                        if item["action_type"] == "VIP_BUY" and tg_id:
                            # Установим VIP (внутренний флаг)
                            await set_user_vip(db, tg_id)
                            await _log_event(
                                db=db,
                                event_id=event_id,
                                action_type=atype,
                                asset="TON",
                                amount=amount_ton,
                                decimals=9,
                                from_addr=from_addr,
                                to_addr=wallet,
                                memo=comment,
                                telegram_id=tg_id,
                                parsed_amount_efhc=None,  # VIP не EFHC
                                vip_requested=True,
                            )
                            handled_actions += 1
                            continue
                        elif item["action_type"] == "SHOP_PURCHASE" and tg_id and item["efhc_amount"]:
                            # Начисляем EFHC на баланс
                            await credit_efhc(db, tg_id, _d3(Decimal(item["efhc_amount"])))
                            await _log_event(
                                db=db,
                                event_id=event_id,
                                action_type=atype,
                                asset="TON",
                                amount=amount_ton,
                                decimals=9,
                                from_addr=from_addr,
                                to_addr=wallet,
                                memo=comment,
                                telegram_id=tg_id,
                                parsed_amount_efhc=_d3(Decimal(item["efhc_amount"])),
                                vip_requested=False,
                            )
                            handled_actions += 1
                            continue
                        # Если не распознали — идём дальше, пробуем "старый" формат

                    # 2) Старый формат: "id telegram 4357333, 100 EFHC", "tg 4357333 vip"
                    tg_id, parsed_amt, asset, vip_flag = parse_memo_for_payment(comment)

                    # VIP — приоритетно: ставим VIP, если указано и есть tg_id
                    if vip_flag and tg_id:
                        await set_user_vip(db, tg_id)
                        await _log_event(
                            db=db,
                            event_id=event_id,
                            action_type=atype,
                            asset="TON",
                            amount=amount_ton,
                            decimals=9,
                            from_addr=from_addr,
                            to_addr=wallet,
                            memo=comment,
                            telegram_id=tg_id,
                            parsed_amount_efhc=None,
                            vip_requested=True,
                        )
                        handled_actions += 1
                        continue

                    # Если в memo указан EFHC и число → начисляем EFHC
                    if tg_id and asset == "EFHC" and parsed_amt and parsed_amt > 0:
                        await credit_efhc(db, tg_id, _d3(parsed_amt))
                        await _log_event(
                            db=db,
                            event_id=event_id,
                            action_type=atype,
                            asset="TON",
                            amount=amount_ton,
                            decimals=9,
                            from_addr=from_addr,
                            to_addr=wallet,
                            memo=comment,
                            telegram_id=tg_id,
                            parsed_amount_efhc=_d3(parsed_amt),
                            vip_requested=False,
                        )
                        handled_actions += 1
                        continue

                    # Иначе — TON без понятного memo: просто логируем
                    await _log_event(
                        db=db,
                        event_id=event_id,
                        action_type=atype,
                        asset="TON",
                        amount=amount_ton,
                        decimals=9,
                        from_addr=from_addr,
                        to_addr=wallet,
                        memo=comment,
                        telegram_id=tg_id,
                        parsed_amount_efhc=None,
                        vip_requested=False,
                    )
                    handled_actions += 1
                    continue

                # ---------------------------------------------------------
                # 2) Входящий JettonTransfer (EFHC/USDT/прочее)
                # ---------------------------------------------------------
                if atype == "JettonTransfer" and action.get("JettonTransfer"):
                    obj = action["JettonTransfer"]
                    jetton_addr = ((obj.get("jetton", {}) or {}).get("address") or "").strip()
                    to_addr = (obj.get("recipient", {}) or {}).get("address") or ""
                    if (to_addr or "").lower() != wallet.lower():
                        continue  # не к нам

                    raw_amount = obj.get("amount") or "0"
                    decimals = int(obj.get("decimals") or obj.get("jetton", {}).get("decimals") or EFHC_DECIMALS)
                    comment = obj.get("comment") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    # EFHC jetton?
                    if efhc_addr and jetton_addr == efhc_addr:
                        amount_efhc = _decode_jetton_amount(raw_amount, decimals=decimals)

                        # Возможно указали tg_id в memo (или code). Иначе — не знаем кому зачислять.
                        # 1) Shop формат
                        shop = _parse_shop_comment(comment)
                        if shop["ok"] and shop["telegram_id"]:
                            await credit_efhc(db, shop["telegram_id"], amount_efhc)
                            await _log_event(
                                db=db,
                                event_id=event_id,
                                action_type=atype,
                                asset="EFHC",
                                amount=amount_efhc,
                                decimals=decimals,
                                from_addr=from_addr,
                                to_addr=wallet,
                                memo=comment,
                                telegram_id=shop["telegram_id"],
                                parsed_amount_efhc=_d3(amount_efhc),
                                vip_requested=False,
                            )
                            handled_actions += 1
                            continue

                        # 2) Старый формат (вдруг): "id:4357333 100 efhc"
                        tg_id, parsed_amt, asset, vip_flag = parse_memo_for_payment(comment)
                        if tg_id:
                            await credit_efhc(db, tg_id, amount_efhc)
                            await _log_event(
                                db=db,
                                event_id=event_id,
                                action_type=atype,
                                asset="EFHC",
                                amount=amount_efhc,
                                decimals=decimals,
                                from_addr=from_addr,
                                to_addr=wallet,
                                memo=comment,
                                telegram_id=tg_id,
                                parsed_amount_efhc=_d3(amount_efhc),
                                vip_requested=False,
                            )
                            handled_actions += 1
                        else:
                            # Без tg_id — логируем факт, не начисляем
                            await _log_event(
                                db=db,
                                event_id=event_id,
                                action_type=atype,
                                asset="EFHC",
                                amount=amount_efhc,
                                decimals=decimals,
                                from_addr=from_addr,
                                to_addr=wallet,
                                memo=comment,
                                telegram_id=None,
                                parsed_amount_efhc=None,
                                vip_requested=False,
                            )
                            handled_actions += 1
                        continue

                    # USDT jetton (опционально)
                    if usdt_addr and jetton_addr == usdt_addr:
                        amount_usdt = _decode_jetton_amount(raw_amount, decimals=decimals)
                        await _log_event(
                            db=db,
                            event_id=event_id,
                            action_type=atype,
                            asset="USDT",
                            amount=amount_usdt,
                            decimals=decimals,
                            from_addr=from_addr,
                            to_addr=wallet,
                            memo=comment,
                            telegram_id=None,
                            parsed_amount_efhc=None,
                            vip_requested=False,
                        )
                        handled_actions += 1
                        continue

                    # Иной jetton — просто логируем
                    jetton_amount = _decode_jetton_amount(raw_amount, decimals=decimals)
                    await _log_event(
                        db=db,
                        event_id=event_id,
                        action_type=atype,
                        asset=f"JETTON:{jetton_addr}",
                        amount=jetton_amount,
                        decimals=decimals,
                        from_addr=from_addr,
                        to_addr=wallet,
                        memo=comment,
                        telegram_id=None,
                        parsed_amount_efhc=None,
                        vip_requested=False,
                    )
                    handled_actions += 1
                    continue

                # Прочие типы action можно игнорировать или логировать по потребности.
                continue

            except Exception as e:
                # Не прерываем цикл по action, но логируем в консоль
                print(f"[EFHC][TON] action error in event {ev.get('event_id')}: {e}")
                continue

        # Если здесь нужна «фиксация» только после всех action события — commit в вызывающем коде.

    return handled_actions


# -----------------------------------------------------------------------------
# Фоновой цикл (watcher): регулярно опрашивает TonAPI и обрабатывает события
# -----------------------------------------------------------------------------
async def ton_watcher_loop(poll_interval: int = 30) -> None:
    """
    Фоновая задача «watcher»:
        - Каждые poll_interval секунд вызывает process_incoming_payments().
        - Поднимается в main.py через asyncio.create_task(ton_watcher_loop()).
    Безопасен к ошибкам: любые исключения логируются, цикл живёт дальше.
    """
    await asyncio.sleep(3)  # небольшая задержка до полного старта
    print(f"[EFHC][TON] Watcher loop started. poll_interval={poll_interval}s, wallet={TON_WALLET_ADDRESS or 'NOT SET'}")

    while True:
        try:
            async with get_session() as db:
                cnt = await process_incoming_payments(db, limit=50)
                # Все DML выше происходят без commit — commit делаем в конце пачки
                await db.commit()
                if cnt > 0:
                    print(f"[EFHC][TON] Processed actions: {cnt}")
        except Exception as e:
            print(f"[EFHC][TON] ERROR in watcher loop: {e}")
        await asyncio.sleep(poll_interval)


# -----------------------------------------------------------------------------
# Ручной запуск (admin-кейсы или отладка)
# -----------------------------------------------------------------------------
async def manual_process_once(limit: int = 50) -> Dict[str, Any]:
    """
    Ручной триггер (например, из админки или отдельной команды):
        - Один вызов process_incoming_payments()
        - Возвращаем JSON-результат с количеством обработанных действий.
    """
    try:
        async with get_session() as db:
            cnt = await process_incoming_payments(db, limit=limit)
            await db.commit()
        return {"ok": True, "processed_actions": cnt}
    except Exception as e:
        return {"ok": False, "error": str(e)}
