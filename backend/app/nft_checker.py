# backend/app/nft_checker.py
import httpx
import logging
from sqlalchemy.orm import Session
from . import models

logger = logging.getLogger(__name__)

# Коллекция EFHC NFT (VIP)
VIP_COLLECTION = "EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0"


async def check_user_nft(wallet_address: str) -> bool:
    """
    Проверка наличия NFT у конкретного пользователя.
    Возвращает True, если NFT есть, иначе False.
    """
    try:
        url = f"https://api.getgems.io/v2/accounts/{wallet_address}/nfts"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            nfts = resp.json()

            for nft in nfts:
                if nft.get("collection_address") == VIP_COLLECTION:
                    return True
            return False
    except Exception as e:
        logger.error(f"Ошибка при проверке NFT для {wallet_address}: {e}")
        return False


async def update_all_users_vip(db: Session):
    """
    Ежедневная проверка всех пользователей:
    - Если NFT найден → vip_status = True
    - Если нет → vip_status = False
    """
    users = db.query(models.User).all()
    for user in users:
        if not user.wallet_address:
            continue

        has_vip = await check_user_nft(user.wallet_address)
        user.vip_status = has_vip
        db.add(user)

    db.commit()
    logger.info("Ежедневная проверка VIP NFT завершена.")

