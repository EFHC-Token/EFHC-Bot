# 📂 backend/app/database.py — подключение к БД, пул, сессии, создание схем
# -----------------------------------------------------------------------------
# Этот модуль отвечает за:
#   • Создание асинхронного движка SQLAlchemy (PostgreSQL/Neon) с драйвером asyncpg.
#   • Настройку пула соединений (pool_size, max_overflow, pre_ping).
#   • Инициализацию необходимых схем БД (efhc_core, efhc_admin, efhc_referrals, efhc_lottery, efhc_tasks).
#   • Предоставление фабрики сессий и зависимостей для FastAPI:
#       - AsyncSessionLocal()   — фабрика сессий, "as is" (совместимость с вашим кодом).
#       - get_session()         — Depends для роутов/сервисов.
#       - session_scope()       — контекстный менеджер транзакции.
#   • Утилиты запуска: ensure_schemas, set_default_search_path, health-check, заглушка Alembic.
#   • Startup/Shutdown hooks: on_startup_init_db(), on_shutdown_dispose().
#
# Взаимосвязи:
#   • config.py — источник: DATABASE_URL, *SCHEMA*, пул, DEBUG и т.д.
#   • models.py — декларативные модели, обычно с __table_args__={"schema": "<...>"}.
#   • main.py — рекомендуется вызывать on_startup_init_db() при старте приложения.
#   • Другие модули (watcher/тон интеграция) могут использовать AsyncSessionLocal() напрямую.
#
# ПРИМЕЧАНИЯ:
#   • Если в окружении DATABASE_URL пустой — используем DATABASE_URL_LOCAL из config.py.
#   • Поддерживаем "postgres://..." → "postgresql+asyncpg://..." (часто в Vercel/Neon).
#   • Для serverless окружений осторожно с pool_size (меньше — лучше).
#   • Миграции Alembic обычно запускаются отдельной командой CI/CD; здесь оставлена заглушка.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from .config import get_settings

# -----------------------------------------------------------------------------
# Глобальные синглтоны (создаются один раз на процесс/воркер)
# -----------------------------------------------------------------------------
_engine: Optional[AsyncEngine] = None
_SessionFactory: Optional[sessionmaker] = None

# Совместимость с ранним кодом: иногда импортировали AsyncSessionLocal напрямую
# (например, "from .database import AsyncSessionLocal" и потом "async with AsyncSessionLocal() as db:")
AsyncSessionLocal: Optional[sessionmaker] = None


def _build_database_url() -> str:
    """
    Возвращает корректный async URL для SQLAlchemy с драйвером asyncpg.
    Логика:
      1) Берём DATABASE_URL из окружения (config.py). Если пустой → DATABASE_URL_LOCAL.
      2) Приводим к формату для asyncpg:
           - postgres://            → postgresql+asyncpg://
           - postgresql://          → postgresql+asyncpg://
         (на некоторых платформах база отдаёт именно postgres://)
    """
    s = get_settings()
    url = s.DATABASE_URL or getattr(s, "DATABASE_URL_LOCAL", "")

    if not url:
        # Явная ошибка: не заданы ни основная, ни локальная строка.
        # Лучше упасть раньше, чем ловить странные ошибки позже.
        raise RuntimeError(
            "DATABASE_URL is empty and DATABASE_URL_LOCAL is not provided in settings."
        )

    # Приводим к формату asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def _get_pool_sizes() -> tuple[int, int]:
    """
    Возвращает (pool_size, max_overflow) из настроек.
    Примечание:
      • На Vercel serverless больших пулов не нужно (даже 1-2).
      • На Render/VPS/локально — можно больше.
    """
    s = get_settings()
    return (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW)


def get_engine() -> AsyncEngine:
    """
    Ленивая инициализация AsyncEngine + фабрики сессий.
    Используйте этот метод везде, где нужен engine (инициализация схем, health-check, миграции и т.д.).
    """
    global _engine, _SessionFactory, AsyncSessionLocal

    if _engine is not None:
        return _engine

    s = get_settings()
    db_url = _build_database_url()
    pool_size, max_overflow = _get_pool_sizes()

    # echo = s.DEBUG для удобной отладки SQL (в проде выключаем).
    _engine = create_async_engine(
        db_url,
        echo=bool(getattr(s, "DEBUG", False)),
        pool_pre_ping=True,   # полезно при долгих простоях; оживляет соединения
        pool_size=pool_size,
        max_overflow=max_overflow,
        future=True,
    )

    # Фабрика сессий (autoflush=False — ручной контроль flush; expire_on_commit=False — объекты живы после commit)
    _SessionFactory = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    # Экспортируем в глобальную переменную для совместимости со старым кодом
    AsyncSessionLocal = _SessionFactory

    return _engine


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    Асинхронный контекстный менеджер «сессия как транзакция»:
        async with session_scope() as db:
            ... работа с db ...
    Автоматически выполняет commit/rollback/close.
    Подходит для фоновых задач, скриптов или сервисных операций.
    """
    if _SessionFactory is None:
        get_engine()  # инициализируем engine и фабрику, если ещё нет

    assert _SessionFactory is not None, "Session factory is not initialized"
    session: AsyncSession = _SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI-зависимость для инъекции сессии в эндпоинты/сервисы.
    Пример:
        @router.get("/users")
        async def list_users(db: AsyncSession = Depends(get_session)):
            ...
    На каждый запрос создаёт новую сессию и корректно её завершает (commit/rollback/close).
    """
    if _SessionFactory is None:
        get_engine()

    assert _SessionFactory is not None, "Session factory is not initialized"
    session: AsyncSession = _SessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ensure_schemas(engine: Optional[AsyncEngine] = None) -> None:
    """
    Создаёт необходимые **схемы** в Postgres, если их ещё нет.
    Список схем берётся из config.py (DB_SCHEMA_*).
    Важно запускать до создания/использования таблиц.
    """
    s = get_settings()
    engine = engine or get_engine()

    schemas = [
        s.DB_SCHEMA_CORE,
        s.DB_SCHEMA_ADMIN,
        s.DB_SCHEMA_REFERRAL,
        s.DB_SCHEMA_LOTTERY,
        s.DB_SCHEMA_TASKS,
    ]

    # Формируем единый SQL для создания схем (idempotent)
    create_sql = "; ".join([f'CREATE SCHEMA IF NOT EXISTS "{sch}"' for sch in schemas])

    async with engine.begin() as conn:
        await conn.execute(text(create_sql))


async def set_default_search_path(engine: Optional[AsyncEngine] = None) -> None:
    """
    (Необязательный шаг) Устанавливает search_path для упрощения голых SQL-скриптов.
    Модели у нас и так используют schema=..., но search_path бывает удобен (например в raw SQL).
    Важно: порядок схем в search_path влияет на поиск таблиц/функций.
    """
    s = get_settings()
    engine = engine or get_engine()

    # Порядок: сначала core, затем остальные, в конце public
    search_path = ",".join(
        [
            s.DB_SCHEMA_CORE,
            s.DB_SCHEMA_ADMIN,
            s.DB_SCHEMA_REFERRAL,
            s.DB_SCHEMA_LOTTERY,
            s.DB_SCHEMA_TASKS,
            "public",
        ]
    )
    async with engine.begin() as conn:
        await conn.execute(text(f"SET search_path TO {search_path}"))


async def check_db_connection(engine: Optional[AsyncEngine] = None) -> bool:
    """
    Простой health-check (SELECT 1). Возвращает True, если соединение установлено.
    Удобно дергать в on_startup, readiness probes и при локальной отладке.
    """
    engine = engine or get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        # Здесь сознательно не логируем stacktrace, чтобы не засорять логи старта.
        # При необходимости можно добавить логирование.
        return False


async def run_alembic_migrations() -> None:
    """
    Заглушка под реальные миграции Alembic.
    На проде/CI вы обычно запускаете миграции отдельной командой:
        alembic upgrade head
    Если хотите запускать миграции внутри приложения — интегрируйте здесь Alembic.
    Пример (псевдокод):
        from alembic import command
        from alembic.config import Config as AlembicConfig
        cfg = AlembicConfig("alembic.ini")
        command.upgrade(cfg, "head")
    """
    pass


async def on_startup_init_db() -> None:
    """
    Вызывается из main.py при старте приложения/функции:
        app.add_event_handler("startup", on_startup_init_db)
    Делает:
      1) Ленивую инициализацию движка/фабрики сессий (get_engine()).
      2) Создание необходимых схем (ensure_schemas()) — idempotent.
      3) (Опционально) Устанавливает search_path (set_default_search_path()).
      4) (Опционально) Запускает миграции Alembic (run_alembic_migrations()).
      5) Проверяет соединение (check_db_connection()) — бросает исключение при неуспехе.
    """
    engine = get_engine()
    await ensure_schemas(engine)
    await set_default_search_path(engine)
    # По желанию можно включить миграции отсюда:
    # await run_alembic_migrations()
    ok = await check_db_connection(engine)
    if not ok:
        # Явно бросаем исключение, чтобы платформа перезапустила инстанс/функцию
        raise RuntimeError("Database connection failed during startup.")


async def on_shutdown_dispose() -> None:
    """
    Корректное закрытие движка при остановке приложения.
    Важно вызывать в событии shutdown, чтобы аккуратно закрыть соединения.
    """
    global _engine, _SessionFactory, AsyncSessionLocal
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _SessionFactory = None
    AsyncSessionLocal = None


# -----------------------------------------------------------------------------
# Локальный самотест: python -m backend.app.database
# Проверяет инициализацию, создание схем и health-check.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    async def _selftest():
        print("[EFHC][DB] Initializing...")
        await on_startup_init_db()
        ok = await check_db_connection()
        print(f"[EFHC][DB] Health check: {'OK' if ok else 'FAIL'}")

        # Демонстрация manual session (как делает ваш watcher):
        if AsyncSessionLocal is not None:
            async with AsyncSessionLocal() as db:
                # Любой простейший запрос
                await db.execute(text("SELECT 1"))
                await db.commit()
                print("[EFHC][DB] Manual session: OK")

    asyncio.run(_selftest())
