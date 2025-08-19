# 📂 backend/app/main.py
# -----------------------------------------------------------------------------
# EFHC Bot — основной вход в backend.
# -----------------------------------------------------------------------------
# Что делает:
#   • Создаёт приложение FastAPI с префиксом /api (или /).
#   • Подключает CORS (чтобы работало с фронтендом на Vercel).
#   • Интегрирует Telegram webhook (POST /tg/webhook).
#   • Подключает все маршруты: user_routes, admin_routes.
#   • При старте: инициализация базы (ensure_schemas, search_path, health-check),
#     запуск планировщика scheduler, запуск TON watcher.
#   • При остановке: корректное закрытие базы и планировщика.
#
# ВАЖНО:
#   • Webhook Telegram настроен строго с SECRET из config.py (для безопасности).
#   • Локально можно запускать через uvicorn и использовать polling вместо webhook.
# -----------------------------------------------------------------------------

import asyncio
import logging
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, types

from .config import get_settings
from .database import on_startup_init_db, on_shutdown_dispose, get_session
from . import user_routes, admin_routes
from . import scheduler
from . import ton_integration

settings = get_settings()

# -----------------------------------------------------------------------------
# Настройка логирования
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("efhc-main")

# -----------------------------------------------------------------------------
# Инициализация Telegram Bot (Aiogram)
# -----------------------------------------------------------------------------
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# -----------------------------------------------------------------------------
# Инициализация FastAPI
# -----------------------------------------------------------------------------
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# -----------------------------------------------------------------------------
# CORS: разрешаем фронтенд (Vercel) + "*" (если указано)
# -----------------------------------------------------------------------------
if settings.BACKEND_CORS_ORIGINS or True:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS] or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# -----------------------------------------------------------------------------
# Подключаем роутеры (пользовательские и админские API)
# -----------------------------------------------------------------------------
app.include_router(user_routes.router, prefix=settings.API_V1_STR, tags=["user"])
app.include_router(admin_routes.router, prefix=settings.API_V1_STR, tags=["admin"])

# -----------------------------------------------------------------------------
# Telegram Webhook endpoint
# -----------------------------------------------------------------------------
@app.post(settings.TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """
    Endpoint для Telegram webhook:
      • Проверяет секрет (header 'X-Telegram-Bot-Api-Secret-Token').
      • Передаёт апдейт в диспетчер aiogram.
    """
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret")

    try:
        data = await request.json()
        update = types.Update(**data)
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid update payload")

    # Обработка апдейта aiogram
    await dp.feed_update(bot, update)
    return JSONResponse(content={"ok": True})


# -----------------------------------------------------------------------------
# Startup event — инициализация приложения
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    logger.info("[EFHC] Startup init...")

    # 1. Подключение и проверка базы
    await on_startup_init_db()

    # 2. Запуск планировщика (apscheduler или custom loop)
    scheduler.init_scheduler(app)

    # 3. Запуск TON watcher loop (отслеживание incoming tx по TON_WALLET)
    loop = asyncio.get_event_loop()
    loop.create_task(ton_integration.ton_watcher_loop())

    # 4. Устанавливаем webhook в Telegram (если задан BASE_PUBLIC_URL)
    if settings.BASE_PUBLIC_URL and settings.TELEGRAM_WEBHOOK_PATH:
        webhook_url = f"{settings.BASE_PUBLIC_URL.rstrip('/')}{settings.TELEGRAM_WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            drop_pending_updates=True
        )
        logger.info(f"[EFHC] Telegram webhook set: {webhook_url}")
    else:
        logger.warning("[EFHC] BASE_PUBLIC_URL не задан — webhook не установлен.")


# -----------------------------------------------------------------------------
# Shutdown event — корректное завершение
# -----------------------------------------------------------------------------
@app.on_event("shutdown")
async def on_shutdown():
    logger.info("[EFHC] Shutdown...")
    await on_shutdown_dispose()
    await bot.session.close()


# -----------------------------------------------------------------------------
# Локальный запуск через uvicorn
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
