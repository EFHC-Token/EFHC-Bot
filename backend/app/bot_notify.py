# backend/app/bot_notify.py

import asyncio
from typing import List

from .config import get_settings

settings = get_settings()

async def notify_admins(message: str) -> None:
    """
    Отправляет уведомление администраторам (список admin_ids в settings.ADMINS_WHITELIST).
    Используйте ваш Telegram Bot (aiogram/httpx).
    """
    # Пример через HTTP-запрос к Telegram Bot API:
    # for admin_id in settings.ADMINS_WHITELIST:
    #     await httpx.post(f"https://api.telegram.org/bot{settings.BOT_TOKEN}/sendMessage",
    #                      json={"chat_id": admin_id, "text": message})
    pass
