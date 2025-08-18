# 📂 backend/app/main.py — основной файл запуска FastAPI + Telegram Webhook

import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .user_routes import router as user_router
from .admin_routes import router as admin_router
from .bot import telegram_webhook_handler

# -----------------------------------------------------------------------------
# Настройки логирования
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("efhc")

# -----------------------------------------------------------------------------
# Получаем настройки
# -----------------------------------------------------------------------------
settings = get_settings()

# -----------------------------------------------------------------------------
# Создаём FastAPI приложение
# -----------------------------------------------------------------------------
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="EFHC Bot Backend (FastAPI + Telegram Webhook + Admin API)"
)

# -----------------------------------------------------------------------------
# CORS — разрешаем фронтенду (Vercel) обращаться к API
# -----------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # для продакшена лучше ограничить на домен Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Подключаем роуты (API для пользователей и админов)
# -----------------------------------------------------------------------------
app.include_router(user_router, prefix=f"{settings.API_V1_STR}/users", tags=["users"])
app.include_router(admin_router, prefix=f"{settings.API_V1_STR}/admin", tags=["admin"])

# -----------------------------------------------------------------------------
# Telegram webhook
# -----------------------------------------------------------------------------
@app.post("/tg/webhook")
async def telegram_webhook(request: Request):
    """
    Эндпоинт для получения апдейтов от Telegram.
    В settings.TELEGRAM_BOT_TOKEN уже указан токен, проверка идёт в bot.py.
    """
    try:
        data = await request.json()
        logger.info(f"[Telegram] Update: {data}")
        await telegram_webhook_handler(data)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)})

# -----------------------------------------------------------------------------
# Корневая страница (healthcheck)
# -----------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"status": "EFHC Bot Backend is running"}
