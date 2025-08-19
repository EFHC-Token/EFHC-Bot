# 📂 backend/app/shop_routes.py — Магазин (Shop): конфиг и покупки товаров (EFHC/TON/USDT, VIP NFT)
# -------------------------------------------------------------------------------------------------
# Что содержит:
#   • GET /api/shop/config
#       - Возвращает конфигурацию магазина для конкретного пользователя:
#           • TON/USDT кошельки проекта для входящих платежей,
#           • Ссылка на NFT-маркет (GetGems),
#           • Привязанный TON-кошелёк пользователя (если есть),
#           • Уникальный memo (включает Telegram ID) — нужно указывать в транзакции,
#           • Список товаров (динамический), включая VIP NFT (250 EFHC / 20 TON / 50 USDT),
#             EFHC пакеты (10/100/1000 EFHC), а также произвольные бустеры/скины.
#   • POST /api/shop/buy
#       - Оформляет покупку:
#           • method "efhc"  — списание EFHC с внутреннего баланса и создание заказа,
#           • method "ton"   — создание заказа в ожидании платежа через TON; возвращаем ton://transfer
#           • method "usdt"  — аналогично, метод с внешними данными (в рамках EFHC — логируем и ждём подтверждения)
#           • Для VIP NFT:
#               - efhc — списываем EFHC, создаём pending заказ (вручную доставляется админом),
#               - ton/usdt — ждём поступления средств; отправка NFT — вручную админом.
#   • Таблица shop_orders: все покупки фиксируются.
#   • Проверка подлинности пользователя:
#       - Предпочтительно: через заголовок X-Telegram-Init-Data (initData из Telegram WebApp), HMAC SHA-256.
#       - В dev-режиме возможно указание X-Telegram-Id (небезопасно в прод).
#
# Важные зависимости:
#   • models.py — User, Balance
#   • database.py — get_session
#   • config.py — get_settings (TON_WALLET_ADDRESS, USDT_WALLET_ADDRESS, NFT_MARKET_URL, EFHC_TOKEN_ADDRESS и др.)
#   • ton_integration.py — фоновая обработка транзакций (подтверждает TON/USDT покупки через TonAPI и зачисляет EFHC).
#
# Логика:
#   1) Пользователь в Shop видит список товаров и возможные способы покупки.
#   2) При выборе "EFHC" — внутренний перевод: списываем EFHC и создаём заказ.
#   3) При выборе "TON"/"USDT" — создаётся заказ-ожидание, формируется memo.
#      Пользователь оплатит через Tonkeeper/MytonWallet/Wallet, мы найдём транзакцию по memo+сумме.
#   4) VIP NFT: при method=efhc — списываем 250 EFHC, создаём заказ "pending_nft_delivery".
#      При method in {ton, usdt} — ждём оплаты; админ вручную отправляет NFT на кошелёк пользователя.
#
# Цены и настройки:
#   • VIP NFT: 250 EFHC / 20 TON / 50 USDT
#   • EFHC Packs: 10 EFHC за 0.8 TON / 3 USDT; 100 EFHC за 8 TON / 30 USDT; 1000 EFHC за 80 TON / 300 USDT
#
# Безопасность:
#   • Проверка Telegram WebApp InitData — обязательна в прод, здесь поддерживается.
#   • Все операции изменения баланса — в транзакциях.
#
# Дальнейшее расширение:
#   • Добавить админ-роуты управления товарами (каталог), скидки/акции.
#   • Добавить webhook-подтверждение платежей.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

import hmac
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, status, Body, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, update

from .database import get_session
from .config import get_settings
from .models import User, Balance

router = APIRouter()
settings = get_settings()

# --------------------------------------------------------------------------------------
# Округление
# --------------------------------------------------------------------------------------
DEC3 = Decimal("0.001")

def d3(x: Decimal) -> Decimal:
    """Округление decimal до 3 знаков после запятой для EFHC/kWh."""
    return x.quantize(DEC3, rounding=ROUND_DOWN)


# --------------------------------------------------------------------------------------
# Проверка Telegram WebApp InitData (HMAC-SHA256) — рекомендовано Telegram
# Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
# --------------------------------------------------------------------------------------
def _compute_init_data_hash(data: Dict[str, Any], bot_token: str) -> str:
    """
    Считаем HMAC-SHA256 по `data_check_string` и ключу от SHA256(bot_token),
    где data_check_string — строка со строками k=val отсортированными по ключу и склеенными \n.
    """
    check_string_parts = []
    for k in sorted(data.keys()):
        if k == "hash":
            continue
        val = data[k]
        check_string_parts.append(f"{k}={val}")
    data_check_string = "\n".join(check_string_parts)

    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256)
    return h.hexdigest()


def verify_telegram_init_data(init_data: str, bot_token: str) -> Optional[Dict[str, Any]]:
    """
    Разбирает строку init_data из WebApp (формат query string) и проверяет ее подлинность.
    Возвращает словарь полей, если OK, иначе None.
    """
    try:
        # init_data выглядит как query string: "query_id=...&user=...&auth_date=...&hash=..."
        # Преобразуем в dict
        pairs = [kv for kv in init_data.split("&") if kv.strip()]
        data: Dict[str, Any] = {}
        for p in pairs:
            k, _, v = p.partition("=")
            if not k:
                continue
            data[k] = v

        got_hash = data.get("hash")
        if not got_hash:
            return None

        # Считаем контрольную сумму
        calc_hash = _compute_init_data_hash(data, bot_token=bot_token)
        if hmac.compare_digest(got_hash, calc_hash):
            return data
        return None
    except Exception:
        return None


async def extract_telegram_id_from_headers(
    x_tg_init: Optional[str],
    x_tg_id: Optional[str]
) -> int:
    """
    Безопасное извлечение telegram_id пользователя:
      1) Если передан X-Telegram-Init-Data (initData), проверяем подпись.
      2) Иначе, в дев-режиме допускаем X-Telegram-Id (НЕ использовать на проде).
    """
    if x_tg_init:
        data = verify_telegram_init_data(x_tg_init, settings.TELEGRAM_BOT_TOKEN)
        if not data:
            raise HTTPException(status_code=401, detail="Invalid Telegram initData")

        # initData включает "user" (URL-encoded JSON)
        # user={"id":12345,"first_name":"...","username":"..."}
        user_raw = data.get("user")
        if user_raw:
            try:
                user_json = json.loads(user_raw)  # parse JSON
                tid = int(user_json.get("id"))
                return tid
            except Exception:
                pass
        raise HTTPException(status_code=400, detail="No valid user in initData")

    # fallback: X-Telegram-Id — небезопасно для PROD!
    if x_tg_id and x_tg_id.isdigit():
        return int(x_tg_id)

    raise HTTPException(status_code=401, detail="No Telegram authentication provided")


# --------------------------------------------------------------------------------------
# Таблица "shop_orders" — учёт всех заказов из магазина
# --------------------------------------------------------------------------------------
CREATE_SHOP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS efhc_core.shop_orders (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    item_id TEXT NOT NULL,
    method TEXT NOT NULL, -- 'efhc' | 'ton' | 'usdt'
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'awaiting_payment', 'paid', 'completed', 'canceled', 'pending_nft_delivery'
    amount NUMERIC(30, 3) NOT NULL DEFAULT 0, -- сколько EFHC/TON/USDT по товару
    currency TEXT NOT NULL, -- 'EFHC' | 'TON' | 'USDT'
    memo TEXT NULL,         -- memo для платежей TON/USDT
    extra_data JSONB NULL,  -- запас для деталей
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

async def ensure_shop_tables(db: AsyncSession) -> None:
    """Создаёт таблицу shop_orders, если её нет."""
    await db.execute(text(CREATE_SHOP_TABLE_SQL))
    await db.commit()


# --------------------------------------------------------------------------------------
# Конфигурация товаров магазина
# Можно хранить в БД или в settings. Здесь — в коде, но при желании выносится в конфиг.
# --------------------------------------------------------------------------------------
def shop_items() -> List[Dict[str, Any]]:
    """
    Возвращает список товаров, отображаемых в магазине.
    Рекомендуется поддерживать здесь структуру:
      { id, title, desc, price_efhc?, price_ton?, price_usdt? }
    """
    return [
        {
            "id": "vip_nft",
            "title": "VIP NFT",
            "desc": "Дает +7% к генерации энергии (проверяется ежедневно).",
            "price_efhc": 250,   # внутренняя покупка EFHC  => списание EFHC
            "price_ton": 20,
            "price_usdt": 50
        },
        {
            "id": "efhc_pack_10",
            "title": "10 EFHC",
            "desc": "Быстрое пополнение баланса EFHC. Платеж в TON/USDT.",
            "price_ton": Decimal("0.8"),    # соответствие ТЗ: 10 EFHC = 0.8 TON
            "price_usdt": Decimal("3.0"),
        },
        {
            "id": "efhc_pack_100",
            "title": "100 EFHC",
            "desc": "Быстрое пополнение баланса EFHC. Платеж в TON/USDT.",
            "price_ton": Decimal("8.0"),
            "price_usdt": Decimal("30.0"),
        },
        {
            "id": "efhc_pack_1000",
            "title": "1000 EFHC",
            "desc": "Быстрое пополнение баланса EFHC. Платеж в TON/USDT.",
            "price_ton": Decimal("80.0"),
            "price_usdt": Decimal("300.0"),
        },
        # Пример дополнительных товаров (бустеров/скинов)
        {
            "id": "booster_10",
            "title": "Бустер ⚡",
            "desc": "+10% генерации на 24 часа",
            "price_efhc": 50
        },
        {
            "id": "skin_tree",
            "title": "Декор 🌳",
            "desc": "Уникальное дерево на фоне (визуальный элемент)",
            "price_efhc": 100
        }
    ]


def get_item_by_id(iid: str) -> Optional[Dict[str, Any]]:
    """Возвращает словарь товара по item_id из списка shop_items()."""
    for it in shop_items():
        if it.get("id") == iid:
            return it
    return None


# --------------------------------------------------------------------------------------
# Pydantic-схемы для эндпоинтов
# --------------------------------------------------------------------------------------
class ShopConfigResponse(BaseModel):
    ton_wallet: str
    usdt_wallet: str
    nft_market_url: str
    user_wallet: Optional[str]
    memo: str
    items: List[Dict[str, Any]]


class ShopBuyRequest(BaseModel):
    user_id: Optional[int] = Field(None, description="Telegram ID (в проде лучше брать из токена)")
    item_id: str = Field(..., description="ID товара (например, 'vip_nft')")
    method: str = Field(..., description="'efhc' | 'ton' | 'usdt'")


class ShopBuyResponse(BaseModel):
    success: bool
    status: str
    order_id: Optional[int] = None
    payment_url: Optional[str] = None
    payment_address: Optional[str] = None
    memo: Optional[str] = None
    currency: Optional[str] = None
    amount: Optional[str] = None
    message: Optional[str] = None


# --------------------------------------------------------------------------------------
# Вспомогательные операции с балансом
# --------------------------------------------------------------------------------------
async def ensure_user_balance(db: AsyncSession, telegram_id: int) -> None:
    """Убедиться, что есть пользователь и его запись в balances."""
    await db.execute(
        text(f"""
            INSERT INTO efhc_core.users (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """), {"tg": telegram_id}
    )
    await db.execute(
        text(f"""
            INSERT INTO efhc_core.balances (telegram_id)
            VALUES (:tg)
            ON CONFLICT (telegram_id) DO NOTHING
        """), {"tg": telegram_id}
    )
    await db.commit()


async def get_user_balance(db: AsyncSession, telegram_id: int) -> Tuple[Decimal, Decimal, Decimal]:
    """Возвращает (efhc, bonus, kwh) текущий баланс пользователя."""
    q = await db.execute(text("""
        SELECT efhc, bonus, kwh
          FROM efhc_core.balances
         WHERE telegram_id = :tg
    """), {"tg": telegram_id})
    row = q.fetchone()
    if not row:
        return (Decimal("0.000"), Decimal("0.000"), Decimal("0.000"))
    return (Decimal(row[0] or 0), Decimal(row[1] or 0), Decimal(row[2] or 0))


async def update_user_efhc(db: AsyncSession, telegram_id: int, new_efhc: Decimal) -> None:
    """Обновить баланс EFHC пользователя."""
    await db.execute(text("""
        UPDATE efhc_core.balances
           SET efhc = :v
         WHERE telegram_id = :tg
    """), {"v": str(d3(new_efhc)), "tg": telegram_id})
    await db.commit()


# --------------------------------------------------------------------------------------
# Логика построения memo и ссылок ton://transfer для TON
# --------------------------------------------------------------------------------------
def build_payment_memo(telegram_id: int, item_id: str, amount: Decimal, currency: str) -> str:
    """
    Формирует memo (comment) для транзакции, чтобы watcher (ton_integration.py) смог
    сопоставить платеж с пользователем и товаром.
    Рекомендуем включать 'id <telegram_id> <amount> EFHC' — для случаев EFHC-пополнений.
    Пример: "id 123456 100 EFHC; order: efhc_pack_100"
    """
    if currency.upper() == "EFHC":
        # редкий случай — не используется для внешней оплаты, но для полноты...
        efhc_label = f"{amount} EFHC"
    elif currency.upper() == "TON":
        efhc_label = f"order {item_id} via TON"
    else:
        efhc_label = f"order {item_id} via {currency}"
    # Форматируем memo согласно общей логике: включаем "id <tg>"
    # Для EFHC пакетов, чтобы watcher начислял EFHC — good: "id 4357333 10 EFHC".
    if item_id.startswith("efhc_pack_"):
        efhc_amount = item_id.split("_")[-1]
        memo = f"id {telegram_id} {efhc_amount} EFHC; order {item_id}"
        return memo
    # VIP NFT — отдельная метка
    if item_id == "vip_nft":
        memo = f"id {telegram_id} VIP NFT; order {item_id}"
        return memo

    # Произвольный товар
    memo = f"id {telegram_id}; order {item_id}"
    return memo


def build_ton_transfer_url(address: str, amount_ton: Decimal, memo: str) -> str:
    """
    Формирует ссылку тон-кошелька: ton://transfer/<address>?amount=...&text=...
    Не все кошельки понимают text=, но Tonkeeper/MyTonWallet/Telegram Wallet обрабатывают.
    """
    # amount в нанотонах? В некоторых реализациях — требуется нанотоны.
    # Но многие клиенты понимают amount в TON. Для надёжности можно переводить в nano:
    # nano = int(amount_ton * 1e9), но здесь отдадим в TON.
    safe_text = memo.replace(" ", "%20")  # простейшее urlencode
    return f"ton://transfer/{address}?amount={amount_ton}&text={safe_text}"


# --------------------------------------------------------------------------------------
# GET /api/shop/config — основная конфигурация магазина
# --------------------------------------------------------------------------------------
@router.get("/shop/config", response_model=ShopConfigResponse)
async def shop_config(
    user_id: Optional[int] = Query(None, description="Telegram ID (из initData в проде)"),
    db: AsyncSession = Depends(get_session),
    x_tg_init: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data"),
    x_tg_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Возвращает конфигурацию магазина для конкретного пользователя.
    Откуда берётся Telegram ID:
      • предпочтительно — из X-Telegram-Init-Data (initData),
      • в dev-режиме — X-Telegram-Id.
    Результат:
      • ton_wallet: TON-кошелек проекта,
      • usdt_wallet: USDT-кошелек проекта,
      • nft_market_url: ссылка на NFT-маркет (GetGems),
      • user_wallet: TON-кошелек пользователя (если привязан),
      • memo: строка memo (формат: "id <tg> ...") — для внешних платежей,
      • items: список товаров.
    """
    # Идентифицируем пользователя
    tid = await extract_telegram_id_from_headers(x_tg_init, x_tg_id)
    if user_id:
        if user_id != tid:
            raise HTTPException(status_code=403, detail="UserID mismatch with token data")

    # Ищем привязанный кошелёк пользователя
    # В минимальной версии — храним в efhc_core.users поле "wallet_address". Если в models нет — добавьте.
    # В текущей версии предположим, что поле wallet_address есть у User (иначе замените).
    q = await db.execute(select(User).where(User.telegram_id == tid))
    user: Optional[User] = q.scalar_one_or_none()

    user_wallet = getattr(user, "wallet_address", None) if user else None
    # Формируем memo для безопасности — базовый; конкретный зависим от item и суммы — формируем в /buy
    # На этапе config можно отдать общий: "id <tg> EFHC". В /buy для формирования конкретного заказа — уточним.
    base_memo = f"id {tid}"

    resp = ShopConfigResponse(
        ton_wallet=settings.TON_WALLET_ADDRESS or "",
        usdt_wallet=getattr(settings, "USDT_WALLET_ADDRESS", "") or "",
        nft_market_url=getattr(settings, "NFT_MARKET_URL", "") or (settings.VIP_NFT_COLLECTION or ""),
        user_wallet=user_wallet or "",
        memo=base_memo,
        items=shop_items()
    )
    return resp


# --------------------------------------------------------------------------------------
# POST /api/shop/buy — покупка товара
# --------------------------------------------------------------------------------------
@router.post("/shop/buy", response_model=ShopBuyResponse)
async def shop_buy(
    payload: ShopBuyRequest = Body(...),
    db: AsyncSession = Depends(get_session),
    x_tg_init: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data"),
    x_tg_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
):
    """
    Оформляет покупку товара. payload: { user_id, item_id, method }
    Поддерживаемые методы: 'efhc' | 'ton' | 'usdt'
    Логика:
      • method = 'efhc':
         - для тонкостей: Ищем price_efhc в товаре. Списываем EFHC с баланса и создаём заказ со status:
            - для vip_nft: 'pending_nft_delivery' (только админ вручную отправляет NFT)
            - для booster/skin: 'completed' или 'pending', в зависимости от логики (в примере — 'completed')
      • method = 'ton':
         - ищем price_ton,
         - формируем memo (включая id tg + order id + информацию о товаре),
         - создаём order 'awaiting_payment',
         - возвращаем тон-ссылку —
           ton://transfer/<TON_WALLET_ADDRESS>?amount=<price_ton>&text=<memo>
      • method = 'usdt':
         - аналогично, зафиксируем заказ 'awaiting_payment' с currency = 'USDT' и суммой,
         - возвратим 'payment_address' и 'memo' — оплату отслеживаем вне через watcher.
    """
    # Проверка пользователя
    tid_from_header = await extract_telegram_id_from_headers(x_tg_init, x_tg_id)
    if payload.user_id and tid_from_header != payload.user_id:
        raise HTTPException(status_code=403, detail="User mismatch with header token")

    telegram_id = payload.user_id or tid_from_header

    # Убедиться, что таблицы существуют
    await ensure_shop_tables(db)
    await ensure_user_balance(db, telegram_id)

    # Проверка товара
    item = get_item_by_id(payload.item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    method = payload.method.lower().strip()
    if method not in ("efhc", "ton", "usdt"):
        raise HTTPException(status_code=400, detail="Unsupported method")

    # Идём по методам
    if method == "efhc":
        price_efhc = item.get("price_efhc")
        if not price_efhc:
            raise HTTPException(status_code=400, detail="Item is not available for EFHC payment")

        price_efhc = Decimal(str(price_efhc))
        # Проверяем баланс
        cur_e, cur_b, cur_k = await get_user_balance(db, telegram_id)
        if cur_e < price_efhc:
            raise HTTPException(status_code=400, detail="Insufficient EFHC balance")

        # Списываем EFHC и создаём заказ
        new_e = d3(cur_e - price_efhc)
        await update_user_efhc(db, telegram_id, new_e)

        # Особая логика для VIP NFT (списали EFHC — заказ ожидание выдачи NFT вручную)
        if item["id"] == "vip_nft":
            status = "pending_nft_delivery"
            memo = f"id {telegram_id} VIP NFT; internal EFHC method"
        else:
            # Прочие товары (бустеры/скины) — зачисление и завершение можно сделать сразу: 'completed'
            status = "completed"
            memo = f"id {telegram_id}; item {item['id']}; internal EFHC purchase"

        # Записываем заказ
        q = await db.execute(text("""
            INSERT INTO efhc_core.shop_orders (telegram_id, item_id, method, status, amount, currency, memo, extra_data)
            VALUES (:tg, :item, 'efhc', :st, :amt, 'EFHC', :memo, :extra)
            RETURNING id
        """), {
            "tg": telegram_id,
            "item": item["id"],
            "st": status,
            "amt": str(price_efhc),
            "memo": memo,
            "extra": json.dumps({"title": item["title"], "desc": item.get("desc", "")}),
        })
        row = q.fetchone()
        await db.commit()

        order_id = row[0] if row else None

        return ShopBuyResponse(
            success=True,
            status=status,
            order_id=order_id,
            message="EFHC списаны. Заказ зарегистрирован."
        )

    if method == "ton":
        price_ton = item.get("price_ton")
        if not price_ton:
            raise HTTPException(status_code=400, detail="Item is not available for TON payment")

        price_ton = Decimal(str(price_ton))
        # Формируем memo и ссылку ton://transfer
        memo = build_payment_memo(telegram_id, payload.item_id, price_ton, "TON")
        ton_addr = settings.TON_WALLET_ADDRESS or ""
        if not ton_addr:
            raise HTTPException(status_code=500, detail="Project TON wallet is not configured")

        # создаём заказ "awaiting_payment"
        q = await db.execute(text("""
            INSERT INTO efhc_core.shop_orders(telegram_id, item_id, method, status, amount, currency, memo, extra_data)
            VALUES(:tg, :item, 'ton', 'awaiting_payment', :amt, 'TON', :memo, :extra)
            RETURNING id
        """), {
            "tg": telegram_id,
            "item": payload.item_id,
            "amt": str(price_ton),
            "memo": memo,
            "extra": json.dumps({"title": item["title"], "desc": item.get("desc", "")}),
        })
        row = q.fetchone()
        await db.commit()

        order_id = row[0] if row else None
        payment_url = build_ton_transfer_url(ton_addr, price_ton, memo)
        return ShopBuyResponse(
            success=True,
            status="awaiting_payment",
            order_id=order_id,
            payment_url=payment_url,
            payment_address=ton_addr,
            memo=memo,
            currency="TON",
            amount=str(price_ton),
            message="Оплатите через TON-кошелек. После поступления средств заказ будет отмечен как paid/completed"
        )

    if method == "usdt":
        price_usdt = item.get("price_usdt")
        if not price_usdt:
            raise HTTPException(status_code=400, detail="Item is not available for USDT payment")

        price_usdt = Decimal(str(price_usdt))
        # Для USDT — в зависимости от сети: здесь предполагаем, что USDT принимается на адрес settings.USDT_WALLET_ADDRESS
        usdt_addr = getattr(settings, "USDT_WALLET_ADDRESS", "") or ""
        if not usdt_addr:
            raise HTTPException(status_code=500, detail="Project USDT wallet is not configured")

        memo = build_payment_memo(telegram_id, payload.item_id, price_usdt, "USDT")

        # создать заказ 'awaiting_payment'
        q = await db.execute(text("""
            INSERT INTO efhc_core.shop_orders(telegram_id, item_id, method, status, amount, currency, memo, extra_data)
            VALUES(:tg, :item, 'usdt', 'awaiting_payment', :amt, 'USDT', :memo, :extra)
            RETURNING id
        """), {
            "tg": telegram_id,
            "item": payload.item_id,
            "amt": str(price_usdt),
            "memo": memo,
            "extra": json.dumps({"title": item["title"], "desc": item.get("desc", "")}),
        })
        row = q.fetchone()
        await db.commit()

        order_id = row[0] if row else None

        return ShopBuyResponse(
            success=True,
            status="awaiting_payment",
            order_id=order_id,
            payment_address=usdt_addr,
            memo=memo,
            currency="USDT",
            amount=str(price_usdt),
            message="Оплатите в USDT на указанный адрес. После поступления средств заказ будет отмечен как paid/completed"
        )

    # на всякий случай:
    raise HTTPException(status_code=400, detail="Unsupported method (internal logic)")
