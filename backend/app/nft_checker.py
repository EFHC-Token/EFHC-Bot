# 📂 backend/app/nft_checker.py
# -----------------------------------------------------------------------------
# Проверка VIP NFT (через TON API / GetGems API)
# -----------------------------------------------------------------------------
# Здесь мы реализуем функцию check_user_vip(wallet_ton: str) -> bool
# Она должна:
# 1. Запросить список NFT для указанного TON-кошелька.
# 2. Проверить, есть ли среди них VIP NFT из нашей коллекции.
# 3. Вернуть True/False.
#
# ⚠️ Сейчас это заглушка: используем requests к публичному API TON.
# В будущем можно интегрировать TonCenter, TonAPI, GetGems.
# -----------------------------------------------------------------------------

import aiohttp
from .config import get_settings

settings = get_settings()


async def check_user_vip(wallet_ton: str) -> bool:
    """
    Проверка VIP NFT у конкретного пользователя.

    Аргументы:
    wallet_ton (str) — TON адрес кошелька пользователя.

    Возвращает:
    True — если у пользователя есть хотя бы один NFT из коллекции VIP.
    False — если нет.
    """

    if not wallet_ton:
        return False

    # URL API для проверки NFT (здесь пример для TonAPI)
    # Можно заменить на другой источник.
    api_url = f"{settings.GETGEMS_API_BASE}/v2/accounts/{wallet_ton}/nfts"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                if resp.status != 200:
                    print(f"[EFHC][NFT] Ошибка API ({resp.status}) для {wallet_ton}")
                    return False

                data = await resp.json()

                # -----------------------------------------
                # Вариант API ответа (пример):
                # {
                #   "nft_items": [
                #       {"collection_address": "...", "address": "...", "name": "VIP Pass"}
                #   ]
                # }
                # -----------------------------------------

                nft_items = data.get("nft_items", [])
                for nft in nft_items:
                    collection = nft.get("collection_address", "")
                    if settings.ADMIN_NFT_COLLECTION_URL in collection:
                        print(f"[EFHC][NFT] Найден VIP NFT у {wallet_ton}")
                        return True

    except Exception as e:
        print(f"[EFHC][NFT] Ошибка при проверке NFT: {e}")
        return False

    return False
