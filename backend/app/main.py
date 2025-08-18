# 📂 backend/app/main.py
# -----------------------------------------------------------------------------
# Главный файл запуска проекта EFHC Bot
# -----------------------------------------------------------------------------
# Что делает:
# 1. Создаёт FastAPI приложение (API backend).
# 2. Подключает все роуты (пользовательские и админские).
# 3. Инициализирует соединение с базой данных (Neon PostgreSQL).
# 4. Настраивает CORS (чтобы фронтенд на Vercel мог общаться с API).
# 5. Запускает планировщик (ежедневные задачи: начисления кВт, проверка VIP NFT).
# 6. Поднимает Telegram-бота (либо webhook, либо polling).
#
# -----------------------------------------------------------------------------
# Связанные файлы:
# - config.py        → конфиги и переменные окружения
# - database.py      → подключение к PostgreSQL (async SQLAlchemy)
# - models.py        → все таблицы и ORM-модели
# - user_routes.py   → эндпоинты для пользователей
# - admin_routes.py  → эндпоинты админ-панели
# - scheduler.py     → cron-задачи (ежедневные начисления, NFT-чекер)
# - bot.py           → Telegram бот (меню, обработка команд)
# -----------------------------------------------------------------------------

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import init_db
from .user_routes import router as user_router
from .admin_routes import router as admin_router
from .scheduler import init_scheduler
from .bot import start_bot

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

    # -------------------
    # CORS
    # -------------------
    # Чтобы фронтенд на Vercel мог общаться с нашим API
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------
    # Роуты
    # -------------------
    app.include_router(user_router, prefix=settings.API_V1_STR, tags=["user"])
    app.include_router(admin_router, prefix=settings.API_V1_STR, tags=["admin"])

    return app


app = create_app()


# -----------------------------------------------------------------------------
# Startup / Shutdown events
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    # Подключаем БД
    await init_db()

    # Запускаем планировщик задач (cron)
    init_scheduler(app)

    # Запускаем Telegram-бота
    await start_bot()


@app.on_event("shutdown")
async def on_shutdown():
    print("[EFHC] Shutdown complete")


# -----------------------------------------------------------------------------
# Локальный запуск (uvicorn)
# -----------------------------------------------------------------------------
# В production (Vercel/Render) используется WSGI/ASGI-адаптер,
# а локально можно запускать напрямую: python -m backend.app.main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)

