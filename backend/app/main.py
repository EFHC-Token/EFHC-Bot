# 📂 backend/app/main.py — запуск FastAPI + Telegram webhook + TON watcher + Scheduler
# -----------------------------------------------------------------------------
# Что делает:
#   1) Создаёт и конфигурирует FastAPI-приложение.
#   2) Подключает CORS (для WebApp фронтенда).
#   3) Регистрирует API-роуты: user_routes, admin_routes (с префиксом /api).
#   4) Поднимает Telegram webhook эндпоинт (путь из settings.TELEGRAM_WEBHOOK_PATH)
#      + проверяет секрет (settings.TELEGRAM_WEBHOOK_SECRET), если задан.
#   5) На старте:
#       - инициализирует БД (ensure схемы, health check),
#       - настраивает Scheduler (ежедневные начисления энергии, проверка NFT, лотереи),
#       - запускает фоновый цикл опроса TON API (process_incoming_payments каждый N секунд),
#       - устанавливает webhook у Telegram-бота (если задан BASE_PUBLIC_URL).
#   6) Имеет информационные эндпоинты: GET / (root), GET /healthz.
#
# Зачем нужно:
#   Это центральная точка, которая стыкует все подсистемы: API, бот, планировщик,
#   интеграция TonAPI (автозачисления EFHC), CORS, жизненный цикл приложения.
#
# Где используется:
#   - Запускается uvicorn'ом (локально) или как приложение на Render/VPS.
#   - Webhook Telegram подключается на публичный URL вида: https://<host>/tg/webhook
#   - Фоновый воркер TON отслеживает поступления и записывает в БД.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .database import (
    on_startup_init_db,
    on_shutdown_dispose,
    session_scope,          # контекст менеджер сессии (атомарная транзакция)
)
from .user_routes import router as user_router
from .admin_routes import router as admin_router
from .scheduler import init_scheduler  # планировщик: энергия, VIP, лотереи
from .ton_integration import process_incoming_payments  # обработчик входящих TON событий
from . import bot as bot_module  # наш aiogram-бот (обработчики, меню, и т.д.)

settings = get_settings()

# -----------------------------------------------------------------------------
# Создание FastAPI приложения
# -----------------------------------------------------------------------------
def create_app() -> FastAPI:
    """
    Создаёт и конфигурирует FastAPI приложение.

    - Инициализирует CORS (для фронтенда WebApp на Vercel/другом домене).
    - Подключает роутеры пользователей/админов с префиксом из settings.API_V1_STR.
    - Определяет Telegram webhook endpoint (POST {TELEGRAM_WEBHOOK_PATH}).
    - Добавляет корневые и healthcheck эндпоинты.
    """
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        description="EFHC Bot Backend API (FastAPI + PostgreSQL + Aiogram + TON)",
    )

    # -------------------
    # CORS
    # -------------------
    # Разрешаем запросы с фронтенда. Если BACKEND_CORS_ORIGINS пусто — разрешаем всем для простоты.
    allow_origins = settings.BACKEND_CORS_ORIGINS or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------
    # Роуты API
    # -------------------
    app.include_router(user_router, prefix=settings.API_V1_STR, tags=["user"])
    app.include_router(admin_router, prefix=settings.API_V1_STR, tags=["admin"])

    # -------------------
    # Telegram webhook endpoint
    # -------------------
    @app.post(settings.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(
        req: Request,
        x_telegram_webhook_secret: Optional[str] = Header(
            None, convert_underscores=False, alias="X-Telegram-Webhook-Secret"
        ),
    ):
        """
        Эндпоинт, куда Telegram присылает все обновления (updates) в режиме webhook.

        Защита:
          - Если settings.TELEGRAM_WEBHOOK_SECRET задан, проверяем заголовок X-Telegram-Webhook-Secret.
          - Иначе (секрет не задан) принимаем без проверки — подходит для локала/test.

        Обработка:
          - Тело запроса — JSON update (dict), мы проксируем его в aiogram.Dispatcher.
          - Обработчики команд/кнопок/меню определены в backend/app/bot.py.

        Важно:
          - Данный путь должен совпадать с settings.TELEGRAM_WEBHOOK_PATH.
          - У бота должен быть установлен webhook на {BASE_PUBLIC_URL}{TELEGRAM_WEBHOOK_PATH}.
        """
        if settings.TELEGRAM_WEBHOOK_SECRET:
            if not x_telegram_webhook_secret or x_telegram_webhook_secret != settings.TELEGRAM_WEBHOOK_SECRET:
                # Отвечаем 403, если секретный заголовок отсутствует или не совпадает
                raise HTTPException(status_code=403, detail="Invalid webhook secret")

        try:
            update = await req.json()
        except Exception:
            # Невалидный JSON
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        # Передаём update в aiogram Dispatcher
        # Получаем dispatcher из нашего модуля бота
        dp = bot_module.get_dispatcher()
        # Глобальный объект Bot (aiogram)
        tg_bot = bot_module.bot

        # aiogram v3: метод feed_raw_update (или feed_update) для программной подачі апдейта
        try:
            # Возможен вариант: await dp.feed_update(bot=tg_bot, update=update)
            # Используем feed_raw_update для максимально "сырой" подачи JSON
            await dp.feed_raw_update(tg_bot, update)
        except Exception as e:
            print(f"[EFHC][BOT][WEBHOOK] Update handling error: {e}", file=sys.stderr)

        return JSONResponse({"ok": True})

    # -------------------
    # Инфо и Healthcheck
    # -------------------
    @app.get("/")
    async def root():
        """
        Простой диагностический эндпоинт — отдаём базовую информацию по приложению.
        Используется для быстрой проверки: задеплоилось ли приложение.
        """
        return {
            "name": settings.PROJECT_NAME,
            "env": settings.ENV,
            "api_prefix": settings.API_V1_STR,
            "ton_wallet": settings.TON_WALLET_ADDRESS,
            "efhc_jetton": settings.EFHC_TOKEN_ADDRESS,
            "webhook_path": settings.TELEGRAM_WEBHOOK_PATH,
            "base_public_url": settings.BASE_PUBLIC_URL,
        }

    @app.get("/healthz")
    async def healthz():
        """
        Healthcheck эндпоинт для оркестраторов/балансировщиков.
        Можно расширить: пинг БД, пинг TonAPI и т.д.
        """
        return {"status": "ok"}

    return app


# -----------------------------------------------------------------------------
# Фоновая задача: опрос TonAPI и авто-начисления EFHC
# -----------------------------------------------------------------------------
async def ton_watcher_loop():
    """
    Фоновый цикл, который:
      - Каждые N секунд (20с) опрашивает TonAPI (через ton_integration.process_incoming_payments)
      - Распознаёт новые входящие транзакции по TON_WALLET_ADDRESS.
      - Парсит memo (формат: "id telegram 4357333, 100 EFHC" или "id telegram 4357333, VIP").
      - Начисляет EFHC/выдаёт VIP/зачисляет в магазин по правилам.
      - Сохраняет логи в efhc_core.ton_events_log.

    Безопасность:
      - Все начисления происходят как внутренние переводы в БД (не из внешнего кошелька).
      - Внешний TON-кошелёк только источник факта оплаты (вне нашего контроля).

    Где используется:
      - Запускается на старте приложения (startup event) как asyncio.create_task().
    """
    await asyncio.sleep(3.0)  # небольшая пауза, чтобы сервис успел подняться
    while True:
        try:
            # Работаем с сессией в транзакционном контексте
            async with session_scope() as db:
                handled = await process_incoming_payments(db, limit=50)
                if handled:
                    print(f"[EFHC][TON] processed {handled} new tx")
        except Exception as e:
            # Логируем и продолжаем (чтобы цикл не умирал)
            print(f"[EFHC][TON] watcher error: {e}", file=sys.stderr)

        await asyncio.sleep(20.0)  # интервал опроса (секунды)


# -----------------------------------------------------------------------------
# Инициализация приложения (startup/shutdown)
# -----------------------------------------------------------------------------
app = create_app()

@app.on_event("startup")
async def on_startup():
    """
    Событие запуска приложения:
      1) Инициализируем БД (создаём схемы, выставляем search_path, health-check).
      2) Инициализируем планировщик задач (ежедневная энергия, проверка NFT, лотереи).
      3) Запускаем фонового воркера TON (опрашиваем входящие платежи).
      4) Устанавливаем webhook боту (если указан BASE_PUBLIC_URL).
    """
    print("[EFHC] Starting up...")

    # --- 1) Инициализация БД: схемы, search_path, health-check
    await on_startup_init_db()
    print("[EFHC][DB] Initialized")

    # --- 2) Планировщик (APScheduler / asyncio) — начисления, VIP, лотереи
    init_scheduler(app)
    print("[EFHC][SCHEDULER] Initialized")

    # --- 3) Запуск фонового воркера TON
    asyncio.create_task(ton_watcher_loop())
    print("[EFHC][TON] Watcher started")

    # --- 4) Установка webhook у Telegram-бота (если есть публичный URL)
    try:
        # Используем логику из bot_module.setup_webhook(), она сама проверит BASE_PUBLIC_URL/BOT_WEBHOOK_URL
        await bot_module.setup_webhook()
        # Если нужно дополнительно убедиться, что путь совпадает с TELEGRAM_WEBHOOK_PATH — можно переустановить:
        # Но не дублируем вызов без необходимости. Функция setup_webhook уже учитывает настройки.
        print("[EFHC][BOT] Webhook setup completed (if BASE_PUBLIC_URL was provided).")
    except Exception as e:
        print(f"[EFHC][BOT] Webhook setup skipped/failed: {e}", file=sys.stderr)
        # Это не критично: локально можно работать в режиме polling (см. bot_module.start_bot()).
        # При желании можно запустить polling здесь для локальной отладки:
        # asyncio.create_task(bot_module.start_bot())


@app.on_event("shutdown")
async def on_shutdown():
    """
    Корректное завершение работы:
      - Закрываем движок БД.
      - Можно добавить остановку планировщика (если используется отдельный планировщик).
    """
    print("[EFHC] Shutting down...")
    await on_shutdown_dispose()
    print("[EFHC] Shutdown complete")


# -----------------------------------------------------------------------------
# Локальный запуск через uvicorn (для отладки)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Пример: python -m backend.app.main
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000, reload=True)
