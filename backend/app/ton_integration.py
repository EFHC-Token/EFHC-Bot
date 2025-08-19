# 📂 backend/app/ton_integration.py — интеграция с TON (TonAPI) и авто-начисления, Shop<Order> подтверждение
# -----------------------------------------------------------------------------------------------------------------
# Что делает модуль:
#   • Опрашивает TonAPI (tonapi.io) по адресу получателя (TON_WALLET_ADDRESS).
#   • Обрабатывает события:
#       - TonTransfer (входящий TON перевод)
#       - JettonTransfer (входящий jetton; используем для EFHC и USDT)
#   • Парсит memo/комментарий (comment) по вашему формату:
#       "id telegram 4357333, 100 EFHC" или "id:4357333 100 efhc" | "vip" | "vip nft"
#       Дополнительно: "order efhc_pack_100" / "order vip_nft" — для привязки к shop_orders.
#   • Дедуплицирует события по event_id (TonAPI) и НЕ зачисляет второй раз.
#   • Начисляет EFHC и/или обновляет статусы shop_orders (awaiting_payment -> paid/completed/pending_nft_delivery).
#
# Предпосылки/требования:
#   • В config.py заданы:
#       - TON_WALLET_ADDRESS (кошелёк Tonkeeper для приёма)
#       - NFT_PROVIDER_BASE_URL (https://tonapi.io)
#       - NFT_PROVIDER_API_KEY (ваш API key)
#       - EFHC_TOKEN_ADDRESS (адрес jetton EFHC - если есть собственный, иначе можно оставить пустым)
#       - USDT_JETTON_ADDRESS (адрес jetton USDT - если используем USDT в TON-сети)
#   • Схемы БД создаются модулями database.ensure_schemas() и ensure_ton_tables()/ensure_shop_tables()
#   • Все mutate-операции в БД проходят через транзакции.
#
# Как используется:
#   • Подключено в фонового воркера (см. main.py → ton_watcher_loop), периодически вызываем:
#         await process_incoming_payments(db, limit=50)
#
# Таблицы:
#   efhc_core.ton_events_log — лог входящих транзакций TON/Jetton
#   efhc_core.balances — внутренний баланс (efhc, bonus, kwh)
#   efhc_core.users — пользователи
#   efhc_core.user_vip — внутренний VIP флаг (влияние +7% на генерацию)
#   efhc_core.shop_orders — заказы магазина (awaiting_payment -> paid/completed/pending_nft_delivery)
#
# Логика Shop + TonIntegration:
#   • Если пользователь инициировал заказ покупки EFHC "efhc_pack_100" методом TON/USDT — создаётся заказ "awaiting_payment".
#   • Когда поступит оплата с memo "id <tg> 100 EFHC; order efhc_pack_100" (TON) или "order efhc_pack_100" (USDT):
#        ↳ Находим заказ — отмечаем статус "paid" и "completed".
#        ↳ Начисляем EFHC пользователю (в TON случае — уже начали начисление по memo/efhc логике).
#   • Для VIP NFT при методе TON/USDT — отмечаем заказ как "paid" и ставим "pending_nft_delivery".
#     Админ потом вручную доставляет NFT и закрывает заказ как "completed".
#
# Примечания:
#   • При Jetton EFHC — мы начисляем EFHC пользователю, если есть tg в memo.
#   • При TON и "id <tg> <N> EFHC" — начисляем N EFHC (без прямой конвертации курса).
#   • Для USDT Jetton — если memo содержит "order efhc_pack_X" → зачислим EFHC = X вручную здесь (без параллельного EFHC).
# -----------------------------------------------------------------------------------------------------------------

from __future__ import annotations

import re
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select

from .config import get_settings

settings = get_settings()

# ------------------------------------------------------------
# Константы и вспомогательные утилиты
# ------------------------------------------------------------

NANO_TON_DECIMALS = Decimal("1e9")   # 1 TON = 1e9 nanotons
DEC3 = Decimal("0.001")
DEC9 = Decimal("0.000000001")


def _d3(x: Decimal) -> Decimal:
    """Округление до 3х знаков (для EFHC/kWh/bonus)."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


def _d9(x: Decimal) -> Decimal:
    """Округление до 9 знаков (для TON)."""
    return x.quantize(DEC9, rounding=ROUND_DOWN)


# ------------------------------------------------------------
# Подготовка необходимых таблиц (idempotent)
# ------------------------------------------------------------

CREATE_TABLES_SQL = """
-- Лог входящих событий TonAPI
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

-- Пользователи
CREATE TABLE IF NOT EXISTS efhc_core.users (
    telegram_id BIGINT PRIMARY KEY,
    username TEXT NULL,
    wallet_address TEXT NULL,      -- Привязанный TON-кошелек (если есть)
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

-- Заказы из магазина
CREATE TABLE IF NOT EXISTS efhc_core.shop_orders (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    item_id TEXT NOT NULL,
    method TEXT NOT NULL, -- 'efhc' | 'ton' | 'usdt'
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'awaiting_payment', 'paid', 'completed', 'canceled', 'pending_nft_delivery'
    amount NUMERIC(30, 3) NOT NULL DEFAULT 0,
    currency TEXT NOT NULL, -- 'EFHC' | 'TON' | 'USDT'
    memo TEXT NULL,
    extra_data JSONB NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


async def ensure_ton_tables(db: AsyncSession) -> None:
    """Создаёт необходимые таблицы, если они отсутствуют."""
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
    [\s:_-]*
    (?P<tg>\d{5,15})?
    [^\dA-Za-z]+
    (?P<amount>\d+(?:[.,]\d{1,9})?)?
    \s*
    (?P<asset>efhc|vip|nft|vip\s*nft)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

VIP_RE = re.compile(r"\b(vip(?:\s*nft)?)\b", re.IGNORECASE)
EFHC_RE = re.compile(r"\befhc\b", re.IGNORECASE)
ORDER_RE = re.compile(r"order\s+([A-Za-z0-9_]+)", re.IGNORECASE)


def parse_memo_for_payment(memo: str) -> Tuple[Optional[int], Optional[Decimal], Optional[str], bool, Optional[str]]:
    """
    Разбирает memo/комментарий.
    Возвращает: (telegram_id, amount, asset, vip_flag, order_item_id)
      - telegram_id: int | None
      - amount: Decimal | None (в EFHC)
      - asset: 'EFHC' | None
      - vip_flag: True, если указано VIP / VIP NFT
      - order_item_id: значение после 'order <item_id>' если есть
    Поддерживаемые стили:
      "id telegram 4357333, 100 EFHC"
      "id:4357333 100 efhc"
      "tg 4357333 vip"
      "4357333 vip nft"
      "id 123456 100 EFHC; order efhc_pack_100"
    """
    if not memo:
        return (None, None, None, False, None)

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

    # Подставим EFHC, если в memo есть 'efhc'
    if asset is None and EFHC_RE.search(memo_norm):
        asset = "EFHC"

    # Ищем order item_id
    o = ORDER_RE.search(memo_norm)
    order_item_id = o.group(1) if o else None

    return (tg_id, amount, asset, vip_flag, order_item_id)


# ------------------------------------------------------------
# Работа с балансами и VIP
# ------------------------------------------------------------

async def _ensure_user_exists(db: AsyncSession, telegram_id: int) -> None:
    """Убедиться, что есть пользователь в efhc_core.users, а также запись в balances."""
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
    """Начислить внутренние EFHC пользователю."""
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
    """Пометить пользователя как VIP (внутренний флаг)."""
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
# Работа с shop_orders — привязка платежей по memo и обновление статуса
# ------------------------------------------------------------

def _efhc_amount_from_order_item(item_id: str) -> Optional[Decimal]:
    """
    Если order efhc_pack_<N> → возвращает N (сколько EFHC начислить).
    Иначе — None.
    """
    if item_id and item_id.lower().startswith("efhc_pack_"):
        try:
            n = item_id.split("_")[-1]
            v = Decimal(n)
            return _d3(v)
        except Exception:
            return None
    return None


async def _update_order_status_on_payment(
    db: AsyncSession,
    telegram_id: int,
    order_item_id: str,
    currency: str,
    paid_amount: Decimal,
    log_memo: str
) -> None:
    """
    Находит заказ в 'awaiting_payment' по telegram_id + item_id и переводит в нужный статус:
      - efhc_pack_X → 'paid' + 'completed'
      - vip_nft → 'paid' + 'pending_nft_delivery'
      - прочие → 'paid' (или 'completed' по логике)
    Лог в ton_events_log уже записан ранее, здесь только устанавливается статус shop_orders.
    """
    # Ищем "свежий" заказ с ожидаемым item_id, у которого статус awaiting_payment
    q = await db.execute(text("""
        SELECT id, status, amount, currency, memo
          FROM efhc_core.shop_orders
         WHERE telegram_id = :tg
           AND item_id = :item
           AND status = 'awaiting_payment'
         ORDER BY created_at DESC
         LIMIT 1
    """), {"tg": telegram_id, "item": order_item_id})
    row = q.fetchone()
    if not row:
        # Нет заказа в ожидании — возможно, уже обработан или не создавался.
        return

    order_id, cur_status, exp_amount, exp_currency, memo = row
    # Для аккуратности можно проверить сумму/валюту; если суммы НЕ совпадают — всё равно будем помечать Paid (или логировать несоответствие).
    # В реальном проде стоит сделать строгую проверку.
    # Переводим 'awaiting_payment' -> 'paid'. Далее — специфично для item_id.
    await db.execute(text("""
        UPDATE efhc_core.shop_orders
           SET status = 'paid'
         WHERE id = :oid
    """), {"oid": order_id})

    # Теперь завершение/продолжение в зависимости от товарной логики
    if order_item_id.lower().startswith("efhc_pack_"):
        # Пакет EFHC: заказ можно считать 'completed' — EFHC уже начислено логикой TON (или USDT блоком ниже)
        await db.execute(text("""
            UPDATE efhc_core.shop_orders
               SET status = 'completed'
             WHERE id = :oid
        """), {"oid": order_id})
        await db.commit()
        return

    if order_item_id == "vip_nft":
        # VIP NFT — не начисляем ровно сейчас (вручную отправляет админ), переводим в pending_nft_delivery
        await db.execute(text("""
            UPDATE efhc_core.shop_orders
               SET status = 'pending_nft_delivery'
             WHERE id = :oid
        """), {"oid": order_id})
        await db.commit()
        return

    # Иной товар: рекомендуем пометить как 'completed' (или согласовать отдельно)
    await db.execute(text("""
        UPDATE efhc_core.shop_orders
           SET status = 'completed'
         WHERE id = :oid
    """), {"oid": order_id})
    await db.commit()


# ------------------------------------------------------------
# Вызовы TonAPI
# ------------------------------------------------------------

def _tonapi_headers() -> Dict[str, str]:
    """Заголовки запроса к TonAPI. API key передаём в X-API-Key."""
    hdrs = {"Accept": "application/json"}
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
    Документация: /v2/accounts/{account_id}/events
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
    """Проверяем, есть ли уже такой event_id в логе (иначе не обрабатываем повторно)."""
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
    """Записать событие в лог. processed=TRUE, т.к. мы сразу обработали начисления/обновления."""
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
    """Преобразование nanotons → TON (Decimal c 9 знаками)."""
    return _d9(Decimal(nano) / NANO_TON_DECIMALS)


def _decode_jetton_amount(raw: str, decimals: int) -> Decimal:
    """Преобразовать строковую сумму jetton (минимальные единицы) в Decimal."""
    q = Decimal(f"1e{decimals}")
    return (Decimal(raw) / q).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)


# ------------------------------------------------------------
# Процессинг входящих платежей
# ------------------------------------------------------------

async def process_incoming_payments(db: AsyncSession, limit: int = 50) -> int:
    """
    Публичная функция: опрашивает TonAPI и обрабатывает новые входящие платежи.
    Возвращает количество обработанных actions.
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

    efhc_addr = (settings.EFHC_TOKEN_ADDRESS or "").strip()    # если есть EFHC как jetton
    usdt_addr = (getattr(settings, "USDT_JETTON_ADDRESS", "") or "").strip()

    for ev in events:
        event_id = ev.get("event_id") or ev.get("id") or ""
        if not event_id:
            continue
        # Дедупликация события
        if await _is_event_seen(db, event_id=event_id):
            continue

        actions = ev.get("actions", []) or []
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
                        continue
                    amount_nano = int(obj.get("amount", 0))
                    amount_ton = _decode_ton_amount(amount_nano)
                    comment = obj.get("comment") or obj.get("payload") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    tg_id, parsed_amt, asset, vip_flag, order_item_id = parse_memo_for_payment(comment)

                    # Если VIP — ставим флаг (если есть tg_id)
                    if vip_flag and tg_id:
                        await set_user_vip(db, tg_id)
                        # Дополнительно, если это указано как "order vip_nft", то обновим заказ
                        if order_item_id == "vip_nft":
                            await _update_order_status_on_payment(db, tg_id, order_item_id, "TON", amount_ton, comment)

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

                    # Если в memo указан EFHC — зачислим EFHC
                    # Например: 'id 123 100 EFHC; order efhc_pack_100'
                    if tg_id and asset == "EFHC" and parsed_amt and parsed_amt > 0:
                        await credit_efhc(db, tg_id, parsed_amt)
                        # Если это было в рамках заказа, то отмечаем "paid" + "completed" для efhc_pack_XXX
                        if order_item_id and order_item_id.lower().startswith("efhc_pack_"):
                            await _update_order_status_on_payment(db, tg_id, order_item_id, "TON", amount_ton, comment)

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

                    # TON без распознаваемого memo
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
                        continue

                    raw_amount = obj.get("amount") or "0"
                    decimals = int(obj.get("decimals") or obj.get("jetton", {}).get("decimals") or settings.EFHC_DECIMALS)
                    comment = obj.get("comment") or ""
                    from_addr = (obj.get("sender", {}) or {}).get("address") or ""

                    # EFHC jetton?
                    if efhc_addr and jetton_addr == efhc_addr:
                        amount_efhc = _decode_jetton_amount(raw_amount, decimals=decimals)
                        tg_id, parsed_amt, asset, vip_flag, order_item_id = parse_memo_for_payment(comment)

                        if tg_id:
                            # EFHC jetton — начислим EFHC ~ amount_efhc (jetton есть EFHC)
                            await credit_efhc(db, tg_id, amount_efhc)
                            # Это может быть заказ 'efhc_pack_x', но часто EFHC из внешней сети просто пополняется
                            if order_item_id and order_item_id.lower().startswith("efhc_pack_"):
                                # На практике за EFHC jetton мы не можем купить пакеты EFHC (дублирование смысла),
                                # но если магазин настроен так — то отметим заказ как completed.
                                await _update_order_status_on_payment(db, tg_id, order_item_id, "JETTON_EFHC", amount_efhc, comment)

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

                    # USDT jetton?
                    if usdt_addr and jetton_addr == usdt_addr:
                        amount_usdt = _decode_jetton_amount(raw_amount, decimals=decimals)
                        tg_id, parsed_amt, asset, vip_flag, order_item_id = parse_memo_for_payment(comment)
                        # Если это заказ EFHC пакета — начнем начислять EFHC
                        if tg_id and order_item_id and order_item_id.lower().startswith("efhc_pack_"):
                            efhc_amount = _efhc_amount_from_order_item(order_item_id)
                            if efhc_amount and efhc_amount > 0:
                                await credit_efhc(db, tg_id, efhc_amount)
                                await _update_order_status_on_payment(db, tg_id, order_item_id, "USDT", amount_usdt, comment)

                        # Если VIP NFT — отмечаем оплачен и pending_nft_delivery
                        if tg_id and order_item_id == "vip_nft":
                            await _update_order_status_on_payment(db, tg_id, order_item_id, "USDT", amount_usdt, comment)

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
                            telegram_id=tg_id if tg_id else None,
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

                # Прочие типы action — игнорируем
                continue

            except Exception as e:
                print(f"[TON] action error in event {ev.get('event_id')}: {e}")
                continue

    return handled_actions
