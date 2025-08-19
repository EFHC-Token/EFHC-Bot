# 📂 backend/app/main.py
# -----------------------------------------------------------------------------
# Главный файл запуска EFHC Bot
# -----------------------------------------------------------------------------
# Что делает:
# 1. Создаёт FastAPI приложение (API backend)
# 2. Подключает все роуты (user + admin)
# 3. Поднимает Telegram-бота (webhook или polling)
# 4. Запускает планировщик (ежедневные начисления, проверка NFT)
# 5. Запускает фоновый воркер TON (отслеживание платежей)
# -----------------------------------------------------------------------------

import asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .database import init_db, AsyncSessionLocal
from .user_routes import router as user_router
from .admin_routes import router as admin_router
from .scheduler import init_scheduler
from .bot import handle_update, start_bot
from .ton_integration import process_incoming_payments

settings = get_settings()

# -----------------------------------------------------------------------------
# Создание FastAPI приложения
# -----------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        description="EFHC Bot Backend API (FastAPI + Neon + Telegram Bot)"
    )

    # ------------------- CORS -------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------- Роуты -------------------
    app.include_router(user_router, prefix=settings.API_V1_STR, tags=["user"])
    app.include_router(admin_router, prefix=settings.API_V1_STR, tags=["admin"])

    # ------------------- Telegram webhook endpoint -------------------
    @app.post(settings.TELEGRAM_WEBHOOK_PATH or "/tg/webhook")
    async def telegram_webhook(req: Request):
        """
        Telegram присылает сюда все сообщения.
        handle_update — твоя функция, которая обрабатывает update.
        """
        update = await req.json()
        await handle_update(update)
        return JSONResponse({"ok": True})

    return app


# -----------------------------------------------------------------------------
# Создаём приложение
# -----------------------------------------------------------------------------
app = create_app()


# -----------------------------------------------------------------------------
# Фоновый воркер TON
# -----------------------------------------------------------------------------
async def ton_watcher_loop():
    """
    Фоновый цикл, который опрашивает TonAPI и обрабатывает входящие платежи.
    Интервал 20 секунд. На проде можно заменить на APScheduler/CRON.
    """
    await asyncio.sleep(3)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                handled = await process_incoming_payments(db, limit=50)
                if handled:
                    print(f"[TON] processed {handled} new tx")
        except Exception as e:
            print(f"[TON] watcher error: {e}")
        await asyncio.sleep(20)


# -----------------------------------------------------------------------------
# Startup / Shutdown events
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    # Подключаем БД
    await init_db()

    # Запускаем планировщик задач (ежедневные начисления, проверка NFT)
    init_scheduler(app)

    # Запускаем фонового воркера TON
    asyncio.create_task(ton_watcher_loop())

    # Запускаем Telegram-бота (polling, если нужно)
    if not settings.WEBHOOK_ENABLED:
        await start_bot()

    print("[EFHC] Startup complete")


@app.on_event("shutdown")
async def on_shutdown():
    print("[EFHC] Shutdown complete")


# -----------------------------------------------------------------------------
# Локальный запуск через uvicorn (для отладки)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)
