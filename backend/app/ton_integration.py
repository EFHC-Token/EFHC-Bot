# 📂 backend/app/ton_integration.py — интеграция с TON (TonAPI) и авто-начисления
# -----------------------------------------------------------------------------
# Что делает модуль:
#   • Опрашивает TonAPI (tonapi.io) по адресу получателя (TON_WALLET_ADDRESS).
#   • Обрабатывает события:
#       - TonTransfer (входящий TON перевод)
#       - JettonTransfer (входящий jetton; используем для EFHC и при желании USDT)
#   • Парсит memo/комментарий (comment) по вашему формату:
#       "id telegram 4357333, 100 EFHC" или "id:4357333 100 efhc", "vip", "vip nft" и т.п.
#   • Дедуплицирует события по event_id (TonAPI) и НЕ зачисляет второй раз.
#   • Начисляет внутренние EFHC пользователю (efhc_core.balances) или ставит VIP-флаг.
#
# Предпосылки/требования:
#   • В config.py заданы:
#       - TON_WALLET_ADDRESS (кошелёк Tonkeeper для приёма)
#       - NFT_PROVIDER_BASE_URL (https://tonapi.io)
#       - NFT_PROVIDER_API_KEY (ваш API key)
#       - EFHC_TOKEN_ADDRESS (адрес jetton EFHC)
#       - (опционально) USDT_JETTON_ADDRESS (адрес jetton USDT, если понадобится)
#   • Схемы БД создаются модулем database.ensure_schemas()
#   • Для безопасности/устойчивости — все mutate-операции в БД проходят через транзакции.
#
# Как использовать:
#   • Включено в фоновый воркер (см. main.py → ton_watcher_loop), который периодически вызывает:
#         await process_incoming_payments(db, limit=50)
#
# Таблицы:
#   efhc_core.ton_events_log (
#       event_id TEXT PRIMARY KEY, ts TIMESTAMP WITH TIME ZONE DEFAULT now(),
#       action_type TEXT, asset TEXT, amount NUMERIC(30, 9), decimals INT,
#       from_addr TEXT, to_addr TEXT, memo TEXT, telegram_id BIGINT NULL,
#       parsed_amount_efhc NUMERIC(30, 3) NULL, vip_requested BOOLEAN DEFAULT FALSE,
#       processed BOOLEAN DEFAULT TRUE, processed_at TIMESTAMPTZ DEFAULT now()
#   )
#
#   efhc_core.balances (
#       telegram_id BIGINT PRIMARY KEY,
#       efhc NUMERIC(30, 3) DEFAULT 0,
#       bonus NUMERIC(30, 3) DEFAULT 0,
#       kwh  NUMERIC(30, 3) DEFAULT 0
#   )
#
#   efhc_core.users (
#       telegram_id BIGINT PRIMARY KEY,
#       username TEXT NULL,
#       created_at TIMESTAMPTZ DEFAULT now()
#   )
#
#   efhc_core.user_vip (
#       telegram_id BIGINT PRIMARY KEY,
#       since TIMESTAMPTZ DEFAULT now()
#   )
#
# Примечания:
#   • При Jetton EFHC мы начисляем ровно количество jetton → во внутренний баланс EFHC.
#   • При TON переводе и memo вида "... 100 EFHC" — зачисляем 100 EFHC (без валидации курса).
#     (Если хотите валидацию — можно добавить прайсы и проверку суммы TON по продукту.)
#   • При "VIP", "VIP NFT" — помечаем user как VIP (внутренний VIP; не админ).
# -----------------------------------------------------------------------------

from __future__ import annotations

import re
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .config import get_settings

settings = get_settings()

# ------------------------------------------------------------
# Константы и вспомогательные утилиты
# ------------------------------------------------------------

NANO_TON_DECIMALS = Decimal("1e9")   # 1 TON = 1e9 nanotons
DEC3 = Decimal("0.001")
DEC9 = Decimal("0.000000001")


def _d3(x: Decimal) -> Decimal:
    """Округление до 3х знаков (для EFHC, kWh, бонусов)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


def _d9(x: Decimal) -> Decimal:
    """Округление до 9 знаков (для TON)."""
    return x.quantize(DEC9, rounding=ROUND_DOWN)


# ------------------------------------------------------------
# Подготовка таблиц (idempotent)
# ------------------------------------------------------------

CREATE_TABLES_SQL = """
-- Лог входящих событий TonAPI: только фиксируем факт и что мы сделали
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

-- Пользователи (минимально; реальный проект может иметь свою версию этой таблицы)
CREATE TABLE IF NOT EXISTS efhc_core.users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Балансы (внутренний кошелёк EFHC/bonus/kwh)
CREATE TABLE IF NOT EXISTS efhc_core.balances (
    telegram_id BIGINT PRIMARY KEY REFERENCES efhc_core.users(telegram_id) ON DELETE CASCADE,
    efhc NUMERIC(30, 3) DEFAULT 0,
    bonus NUMERIC(30, 3) DEFAULT 0,
    kwh  NUMERIC(30, 3) DEFAULT 0
);

-- Внутренний VIP-флаг (не админ; админ контролируется через NFT whitelist)
CREATE TABLE IF NOT EXISTS efhc_core.user_vip (
    telegram_id BIGINT PRIMARY KEY REFERENCES efhc_core.users(telegram_id) ON DELETE CASCADE,
    since TIMESTAMPTZ DEFAULT now()
);
"""


async def ensure_ton_tables(db: AsyncSession) -> None:
    """Создаёт необходимые таблицы для логирования/начисления, если они отсутствуют."""
    await db.execute(text(CREATE_TABLES_SQL))
    await db.commit()


# ------------------------------------------------------------
# Парсер MEMO/комментария
# ------------------------------------------------------------

MEMO_RE = re.compile(
    r"""
    (?:
        id[\s:_-]*telegram   # 'id telegram' (возможны : _ -)
        |id                  # или просто 'id'
        |tg
        |telegram
    )?
    [\s:_-]*                 # разделитель
    (?P<tg>\d{5,15})?        # сам Telegram ID (опционально)
    [^\dA-Za-z]+             # разделитель
    (?P<amount>\d+(?:[.,]\d{1,9})?)? # сумма (в т.ч. десятичная)
    \s*
    (?P<asset>efhc|vip|nft|vip\s*nft)? # актив/тип ('EFHC', 'VIP', 'VIP NFT')
    """,
    re.IGNORECASE | re.VERBOSE,
)

VIP_RE = re.compile(r"\b(vip(?:\s*nft)?)\b", re.IGNORECASE)
EFHC_RE = re.compile(r"\befhc\b", re.IGNORECASE)


def parse_memo_for_payment(memo: str) -> Tuple[Optional[int], Optional[Decimal], Optional[str], bool]:
    """
    Разбирает memo/комментарий.
    Возвращает: (telegram_id, amount, asset, vip_flag)
      - telegram_id: int | None
      - amount: Decimal | None (в EFHC)
      - asset: 'EFHC' | None
      - vip_flag: True, если указано VIP / VIP NFT (приоритетнее суммы)
    Поддерживает разные стили:
      "id telegram 4357333, 100 EFHC"
      "id:4357333 100 efhc"
      "tg 4357333 vip"
      "4357333 vip nft"
    """
    if not memo:
        return (None, None, None, False)

    memo_norm = memo.strip()
    # Быстрая проверка на VIP (вне зависимости от общей регулярки)
    vip_flag = bool(VIP_RE.search(memo_norm))

    # Ищем по основной регулярке
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

    # Если в тексте есть 'efhc', а asset ещё пуст — подставим EFHC
    if asset is None and EFHC_RE.search(memo_norm):
        asset = "EFHC"

    # Если VIP флаг — количество EFHC не обязательно
    return (tg_id, amount, asset, vip_flag)


# ------------------------------------------------------------
# Работа с балансами и VIP
# ------------------------------------------------------------

async def _ensure_user_exists(db: AsyncSession, telegram_id: int) -> None:
    """
    Убедиться, что пользователь существует в efhc_core.users, и есть запись в balances.
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
    await db.commit()


async def credit_efhc(db: AsyncSession, telegram_id: int, amount_efhc: Decimal) -> None:
    """
    Начислить внутренние EFHC пользователю.
    amount_efhc — уже в единицах EFHC (с округлением до 3 знаков).
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
    await db.commit()


async def set_user_vip(db: AsyncSession, telegram_id: int) -> None:
    """
    Пометить пользователя как VIP (внутренний флаг). Это НЕ админ-доступ.
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
    await db.commit()


# ------------------------------------------------------------
# Вызовы TonAPI (tonapi.io)
# ------------------------------------------------------------

def _tonapi_headers() -> Dict[str, str]:
    """
    Заголовки запроса к TonAPI. API key передаём в X-API-Key.
    """
    hdrs = {
        "Accept": "application/json",
    }
    if settings.NFT_PROVIDER_API_KEY:
        hdrs["X-API-Key"] = settings.NFT_PROVIDER_API_KEY
    return hdrs


async def fetch_address_events(
    address: str,
    limit: int = 50,
    before_lt: Optional[int] = None
) -> Dict[str, Any]:
    """
    Получить события адреса (account events) с TonAPI v2.
    Документация (сводно): /v2/accounts/{account_id}/events
    Параметры:
      - address: наш TON_WALLET_ADDRESS
      - limit: количество событий
      - before_lt: пагинация назад по lt (опционально)
    Возвращает JSON dict.
    """
    base = settings.NFT_PROVIDER_BASE_URL.rstrip("/")
    url = f"{base}/v2/accounts/{address}/events?limit={limit}"
    if before_lt is not None:
        url += f"&before_lt={before_lt}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_tonapi_headers())
        r.raise_for_status()
        return r.json()


# ------------------------------------------------------------
# Основная логика обработки событий
# ------------------------------------------------------------

async def _is_event_seen(db: AsyncSession, event_id: str) -> bool:
    """
    Проверяем, есть ли уже такой event_id в логе (иначе не обрабатываем повторно).
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
    Записать событие в лог. processed=TRUE, т.к. мы делаем авто-начисление сразу.
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
    await db.commit()


def _decode_ton_amount(nano: int) -> Decimal:
    """Преобразование nanotons → TON (Decimal с 9 знаками)."""
    return _d9(Decimal(nano) / NANO_TON_DECIMALS)


def _decode_jetton_amount(raw: str, decimals: int) -> Decimal:
    """
    Преобразует строковую сумму jetton (в "минимальных" единицах) в человекочитаемый Decimal.
    raw: строка вида "123000" и т.п., как возвращает TonAPI
    decimals: количество знаков после запятой у jetton (для EFHC — 3)
    """
    q = Decimal(f"1e{decimals}")
    return (Decimal(raw) / q).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)


async def process_incoming_payments(db: AsyncSession, limit: int = 50) -> int:
    """
    Публичная функция: опрашивает TonAPI и обрабатывает новые входящие платежи.
    Возвращает количество обработанных действий (action), а не событий (event).

    Алгоритм:
      1) ensure_ton_tables() — создаём таблицы, если их нет.
      2) fetch_address_events — получаем события кошелька TON_WALLET_ADDRESS.
      3) Для каждого события:
            - если event_id уже в логе → пропускаем.
            - иначе разбираем actions:
                • TonTransfer (входящий) → парсим memo, ищем 'EFHC 100'/'VIP'.
                  Начисляем EFHC по parsed_amount_efhc (если asset EFHC) или ставим VIP.
                • JettonTransfer (входящий, адрес jetton == EFHC) → начисляем EFHC на сумму jetton.
                • (опционально: USDT_JETTON_ADDRESS → можно подключить аналогично)
            - пишем лог в ton_events_log
      4) Возвращаем число обработанных action.
    """
    await ensure_ton_tables(db)

    wallet = settings.TON_WALLET_ADDRESS
    if not wallet:
        print("[TON][WARN] TON_WALLET_ADDRESS не задан — обработка TON платежей отключена.")
        return 0

    try:
        data = await fetch_address_events(address=wallet, limit=limit)
    except httpx.HTTPError as e:
        print(f"[TON] fetch events error: {e}")
        return 0

    events: List[Dict[str, Any]] = data.get("events", []) or data.get("items", []) or []
    handled_actions = 0

    efhc_addr = (settings.EFHC_TOKEN_ADDRESS or "").strip()
    usdt_addr = getattr(settings, "USDT_JETTON_ADDRESS", "") or ""

    for ev in events:
        event_id = ev.get("event_id") or ev.get("id") or ""
        if not event_id:
            # Если у TonAPI пустой id — пропускаем
            continue

        # Дедупликация события
        if await _is_event_seen(db, event_id=event_id):
            continue

        actions = ev.get("actions", []) or []
        # Пробежимся по action; логика: учитываем ТОЛЬКО входящие (recipient == наш адрес)
        for action in actions:
            try:
                atype = action.get("type") or ""

                # ------------------------------
                # 1) Входящий нативный TON
                # ------------------------------
                if atype == "TonTransfer" and action.get("TonTransfer"):
                    obj = action["TonTransfer"]
                    to_addr = (obj.get("recipient", {}) or {}).get("address") or ""
                    if (to_addr or "").lower() != wallet.lower():
                        continue  # не к нам
                    amount_nano = int(obj.get("amount", 0))
                    amount_ton = _decode_ton_amount(amount_nano)
                    comment = obj.get("comment") or obj.get("payload") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    # Разбираем memo: ожидаем Telegram ID и '100 EFHC' / 'VIP'
                    tg_id, parsed_amt, asset, vip_flag = parse_memo_for_payment(comment)

                    # Если VIP — ставим VIP флаг (если есть tg_id)
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

                    # Если в memo указано EFHC-количество — начнем начислять EFHC
                    if tg_id and asset == "EFHC" and parsed_amt and parsed_amt > 0:
                        await credit_efhc(db, tg_id, parsed_amt)
                        await _log_event(
                            db=db,
                            event_id=event_id,
                            action_type=atype,
                            asset="TON",
                            amount=amount_ton,         # сколько TON пришло фактически
                            decimals=9,
                            from_addr=from_addr,
                            to_addr=wallet,
                            memo=comment,
                            telegram_id=tg_id,
                            parsed_amount_efhc=_d3(parsed_amt),  # сколько EFHC мы начислили
                            vip_requested=False,
                        )
                        handled_actions += 1
                        continue

                    # Иначе — TON без понятного memo → просто залогируем без начислений
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

                # ------------------------------
                # 2) Входящий JettonTransfer (EFHC/USDT/и т.д.)
                # ------------------------------
                if atype == "JettonTransfer" and action.get("JettonTransfer"):
                    obj = action["JettonTransfer"]
                    jetton_addr = ((obj.get("jetton", {}) or {}).get("address") or "").strip()
                    to_addr = (obj.get("recipient", {}) or {}).get("address") or ""
                    if (to_addr or "").lower() != wallet.lower():
                        continue  # не к нам

                    raw_amount = obj.get("amount") or "0"
                    decimals = int(obj.get("decimals") or obj.get("jetton", {}).get("decimals") or settings.EFHC_DECIMALS)
                    comment = obj.get("comment") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    # EFHC jetton?
                    if efhc_addr and jetton_addr == efhc_addr:
                        amount_efhc = _decode_jetton_amount(raw_amount, decimals=decimals)
                        # Можно уточнить из memo tg_id, но начислим даже без memo на конкретного пользователя мы не можем.
                        # Требуется tg_id! Иначе — логируем как неопознанный депозит.
                        tg_id, parsed_amt, asset, vip_flag = parse_memo_for_payment(comment)

                        # Если tg_id существует — начисляем amount_efhc (игнорируем parsed_amt)
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
                        else:
                            # Без tg_id не можем зачислить — логируем только факт поступления
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

                    # USDT jetton? (если захотите — можно аналогично сделать обработку memo)
                    if usdt_addr and jetton_addr == usdt_addr:
                        amount_usdt = _decode_jetton_amount(raw_amount, decimals=decimals)
                        # В текущей версии — логируем, не конвертируем в EFHC (это на ваше усмотрение).
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

                    # Иной jetton — логируем
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

                # Прочие типы action — игнорируем или логируем по желанию
                # Для краткости — игнор.
                continue

            except Exception as e:
                # Не прерываем цикл по action, но пишем в консоль
                print(f"[TON] action error in event {ev.get('event_id')}: {e}")
                continue

    return handled_actions
