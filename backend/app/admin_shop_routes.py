# 📂 backend/app/admin_shop_routes.py — Админ-операции для Магазина (Shop) и заказов
# -----------------------------------------------------------------------------------------------------
# Что делает:
#   • Эндпоинты админ-панели для просмотра и управления заказами:
#       - GET   /api/admin/shop/orders          — получить заказы с фильтрами (status/method/currency/user)
#       - POST  /api/admin/shop/orders/{id}/status   — изменить статус заказа (paid/completed/canceled/...)
#       - POST  /api/admin/shop/orders/{id}/deliver_vip — маркирует VIP заказ как доставленный (completed)
#
# Безопасность:
#   • Авторизация админа через Telegram WebApp initData + проверка whitelisting (таблица: efhc_core.admin_nft_whitelist).
#   • В dev-режиме можно использовать X-Telegram-Id (небезопасно; отключить на проде).
#
# Статусы заказов Shop:
#   • awaiting_payment — заказ ожидает поступления средств (TON/USDT)
#   • paid             — средства зачислены (узнаем через ton_integration.py watcher)
#   • completed        — заказ выполнен (для efhc_pack_* это начисление EFHC; для VIP — после вручной отправки админом)
#   • pending_nft_delivery — для VIP NFT после оплаты, ожидает доставки NFT админом
#   • canceled         — отменён
# -----------------------------------------------------------------------------------------------------

from __future__ import annotations

import json
import hmac
import hashlib
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status, Query, Path, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select

from .database import get_session
from .config import get_settings

router = APIRouter(prefix="/admin/shop", tags=["admin-shop"])
settings = get_settings()

# ----------------------------------------------------------
# Проверка Telegram WebApp initData && право администратора
# ----------------------------------------------------------

def _compute_init_data_hash(data: Dict[str, Any], bot_token: str) -> str:
    parts = []
    for k in sorted(data.keys()):
        if k == "hash":
            continue
        parts.append(f"{k}={data[k]}")
    check_string = "\n".join(parts)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, check_string.encode(), hashlib.sha256)
    return h.hexdigest()


def verify_init_data(init_data: str, bot_token: str) -> Optional[Dict[str, Any]]:
    try:
        pairs = [kv for kv in init_data.split("&") if kv.strip()]
        data: Dict[str, Any] = {}
        for p in pairs:
            k, _, v = p.partition("=")
            if k:
                data[k] = v
        got_hash = data.get("hash")
        if not got_hash:
            return None
        calc = _compute_init_data_hash(data, bot_token)
        if hmac.compare_digest(got_hash, calc):
            return data
        return None
    except Exception:
        return None


async def extract_telegram_id_from_headers(
    x_tg_init: Optional[str],
    x_tg_id: Optional[str],
) -> int:
    """Извлекает и проверяет Telegram ID (подлинность initData)."""
    if x_tg_init:
        d = verify_init_data(x_tg_init, settings.TELEGRAM_BOT_TOKEN)
        if not d:
            raise HTTPException(status_code=401, detail="Invalid Telegram initData")
        user_raw = d.get("user")
        if not user_raw:
            raise HTTPException(status_code=400, detail="No user in initData")
        # user_raw — это JSON в urlencoded виде
        try:
            import json
            u = json.loads(user_raw)
            tid = int(u.get("id"))
            return tid
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user json in initData")

    if x_tg_id and x_tg_id.isdigit():
        # DEV ONLY
        return int(x_tg_id)

    raise HTTPException(status_code=401, detail="No Telegram auth provided")


async def require_admin(
    db: AsyncSession = Depends(get_session),
    x_tg_init: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Init-Data"),
    x_tg_id: Optional[str] = Header(None, convert_underscores=False, alias="X-Telegram-Id"),
) -> int:
    """
    Проверяем право администратора по Telegram ID (whitelist).
    В этой реализации — таблица efhc_core.admin_nft_whitelist(telegram_id).
    """
    tid = await extract_telegram_id_from_headers(x_tg_init, x_tg_id)
    # Проверяем whitelist
    q = await db.execute(text("""
        SELECT 1
          FROM efhc_core.admin_nft_whitelist
         WHERE telegram_id = :tg
        LIMIT 1
    """), {"tg": tid})
    if not q.scalar():
        raise HTTPException(status_code=403, detail="Admin rights required")
    return tid


# ----------------------------------------------------------
# Схемы ответов/запросов
# ----------------------------------------------------------

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    id: int
    telegram_id: int
    item_id: str
    method: str
    status: str
    amount: str
    currency: str
    memo: Optional[str]
    extra_data: Optional[dict]


class OrdersListResponse(BaseModel):
    items: List[OrderItem]


class OrderStatusUpdateRequest(BaseModel):
    status: str = Field(..., description="one of 'paid', 'completed', 'canceled', 'pending_nft_delivery'")


class SimpleResult(BaseModel):
    success: bool
    message: str


# ----------------------------------------------------------
# Эндпоинты
# ----------------------------------------------------------

@router.get("/orders", response_model=OrdersListResponse)
async def list_orders(
    status: Optional[str] = Query(None, description="Фильтр по статусу заказа"),
    method: Optional[str] = Query(None, description="Фильтр по методу оплаты: efhc|ton|usdt"),
    currency: Optional[str] = Query(None, description="Фильтр по валюте: EFHC|TON|USDT"),
    telegram_id: Optional[int] = Query(None, description="Фильтр по пользователю"),
    limit: int = Query(100, ge=1, le=1000),
    admin_tid: int = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    """
    Список заказов с фильтрами. Видим в админ-панели.
    """
    # Базовый SQL
    sql = """
      SELECT id, telegram_id, item_id, method, status, amount, currency, memo, extra_data
        FROM efhc_core.shop_orders
    """
    conds = []
    params = {}
    if status:
        conds.append("status = :status")
        params["status"] = status
    if method:
        conds.append("method = :method")
        params["method"] = method
    if currency:
        conds.append("currency = :currency")
        params["currency"] = currency
    if telegram_id:
        conds.append("telegram_id = :tg")
        params["tg"] = telegram_id

    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC LIMIT :lim"
    params["lim"] = limit

    q = await db.execute(text(sql), params)
    rows = q.fetchall()

    items: List[OrderItem] = []
    for r in rows:
        items.append(OrderItem(
            id=r[0],
            telegram_id=r[1],
            item_id=r[2],
            method=r[3],
            status=r[4],
            amount=str(r[5]),
            currency=r[6],
            memo=r[7],
            extra_data=r[8] if r[8] else None,
        ))
    return OrdersListResponse(items=items)


@router.post("/orders/{order_id}/status", response_model=SimpleResult)
async def update_order_status(
    order_id: int = Path(..., ge=1),
    req: OrderStatusUpdateRequest = Body(...),
    admin_tid: int = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    """
    Ручная смена статуса заказа админом. Допускаются статусы:
      - paid
      - completed
      - canceled
      - pending_nft_delivery
    """
    new_status = req.status.strip().lower()
    if new_status not in ("paid", "completed", "canceled", "pending_nft_delivery"):
        raise HTTPException(status_code=400, detail="Unsupported target status")

    # Проверяем, что заказ существует
    q = await db.execute(text("""
        SELECT id, status
          FROM efhc_core.shop_orders
         WHERE id = :oid
         LIMIT 1
    """), {"oid": order_id})
    row = q.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    await db.execute(text("""
        UPDATE efhc_core.shop_orders
           SET status = :st
         WHERE id = :oid
    """), {"st": new_status, "oid": order_id})
    await db.commit()

    return SimpleResult(success=True, message=f"Order {order_id} -> {new_status}")


@router.post("/orders/{order_id}/deliver_vip", response_model=SimpleResult)
async def deliver_vip(
    order_id: int = Path(..., ge=1),
    admin_tid: int = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    """
    Вручную отметить VIP NFT заказ как доставленный.
    Из 'pending_nft_delivery' -> 'completed'.
    """
    q = await db.execute(text("""
        SELECT id, status, item_id
          FROM efhc_core.shop_orders
         WHERE id = :oid
         LIMIT 1
    """), {"oid": order_id})
    row = q.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")

    cur_status = row[1]
    item_id = row[2]
    if item_id != "vip_nft":
        raise HTTPException(status_code=400, detail="Order is not VIP NFT")

    if cur_status not in ("paid", "pending_nft_delivery"):
        raise HTTPException(status_code=400, detail="Order status is not suitable for VIP delivery")

    await db.execute(text("""
        UPDATE efhc_core.shop_orders
           SET status = 'completed'
         WHERE id = :oid
    """), {"oid": order_id})
    await db.commit()
    return SimpleResult(success=True, message="VIP NFT delivery confirmed, order completed.")
