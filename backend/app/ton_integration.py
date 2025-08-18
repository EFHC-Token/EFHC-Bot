# 📂 backend/app/ton_integration.py
# -----------------------------------------------------------------------------
# Модуль интеграции с TON/EFHC/USDT:
#  - проверка транзакций через TonAPI (и tonconsole как резерв)
#  - парсинг memo: "telegram_id, 100 EFHC" или "telegram_id, VIP NFT"
#  - начисление пользователю EFHC/билетов/заявки VIP
#  - логирование для админ-панели
# -----------------------------------------------------------------------------

import httpx
from typing import Optional, Dict, Any
from decimal import Decimal
from fastapi import Depends

from .config import get_settings
from .database import get_session
from .models import User, Transaction, VipNftRequest, Balance

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, insert

settings = get_settings()

# ---------------------------
# Вспомогательные функции
# ---------------------------
def parse_memo(memo: str) -> Optional[Dict[str, Any]]:
    """
    Парсим memo вида:
        "4357333, 100 EFHC"
        "4357333, VIP NFT"
    На выходе словарь:
        {"telegram_id": 4357333, "amount": 100, "asset": "EFHC"}
        {"telegram_id": 4357333, "asset": "VIP_NFT"}
    """
    try:
        parts = [p.strip() for p in memo.split(",")]
        if len(parts) < 2:
            return None
        tg_id = int(parts[0])
        payload = parts[1]

        # Если цифра + asset
        if "EFHC" in payload.upper():
            amount = int(payload.split()[0])
            return {"telegram_id": tg_id, "amount": amount, "asset": "EFHC"}

        if "VIP" in payload.upper():
            return {"telegram_id": tg_id, "asset": "VIP_NFT"}

        return {"telegram_id": tg_id, "raw": payload}
    except Exception:
        return None


# ---------------------------
# TON API / tonconsole
# ---------------------------
async def fetch_transactions(limit: int = 20) -> list[Dict[str, Any]]:
    """
    Загружаем транзакции с TonAPI.
    Fallback: tonconsole.
    """
    url = f"https://tonapi.io/v2/blockchain/accounts/{settings.TON_WALLET_ADDRESS}/transactions?limit={limit}"
    headers = {"Authorization": f"Bearer {settings.TON_API_KEY}"}

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            return data.get("transactions", [])
        except Exception as e:
            print(f"[TON] TonAPI failed, fallback tonconsole: {e}")

            # fallback: tonconsole
            url2 = f"https://tonconsole.com/api/v2/getTransactions?account={settings.TON_WALLET_ADDRESS}&limit={limit}"
            headers2 = {"X-API-Key": settings.TONCONSOLE_API_KEY}
            try:
                r2 = await client.get(url2, headers=headers2)
                r2.raise_for_status()
                data2 = r2.json()
                return data2.get("transactions", [])
            except Exception as e2:
                print(f"[TON] Tonconsole also failed: {e2}")
                return []


# ---------------------------
# Основная обработка транзакций
# ---------------------------
async def process_transactions(db: AsyncSession):
    """
    Проверка транзакций и начисление EFHC пользователям.
    """
    txs = await fetch_transactions(limit=50)
    for tx in txs:
        tx_hash = tx.get("hash")
        if not tx_hash:
            continue

        # Проверим, есть ли уже в базе
        res = await db.execute(select(Transaction).where(Transaction.tx_hash == tx_hash))
        if res.scalar_one_or_none():
            continue  # уже обработали

        # Парсим memo (комментарий)
        memo = tx.get("in_msg", {}).get("message", "")
        parsed = parse_memo(memo)
        if not parsed:
            continue

        tg_id = parsed["telegram_id"]
        user_res = await db.execute(select(User).where(User.telegram_id == tg_id))
        user = user_res.scalar_one_or_none()
        if not user:
            print(f"[TON] Unknown telegram_id {tg_id} in memo")
            continue

        # Начисляем
        if parsed.get("asset") == "EFHC":
            amount = Decimal(parsed["amount"])
            # Обновим баланс
            res_bal = await db.execute(select(Balance).where(Balance.user_id == user.id))
            bal = res_bal.scalar_one_or_none()
            if bal:
                bal.efhc_main += amount
            else:
                db.add(Balance(user_id=user.id, efhc_main=amount))
            db.add(Transaction(user_id=user.id, tx_hash=tx_hash, asset="EFHC", amount=amount, memo=memo))
            print(f"[TON] Credited {amount} EFHC to {tg_id}")

        elif parsed.get("asset") == "VIP_NFT":
            # Создаём заявку на VIP
            db.add(VipNftRequest(user_id=user.id, pay_asset="TON", status="pending", history=[{"tx": tx_hash}]))
            db.add(Transaction(user_id=user.id, tx_hash=tx_hash, asset="VIP_NFT", amount=1, memo=memo))
            print(f"[TON] VIP NFT request for {tg_id}")

        await db.commit()
