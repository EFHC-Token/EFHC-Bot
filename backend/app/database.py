# 📂 backend/app/database.py — подключение к БД, пул, сессии, создание схем
# -----------------------------------------------------------------------------
# Что делает файл:
#   • Создаёт async-движок SQLAlchemy (PostgreSQL/Neon) с драйвером asyncpg.
#   • Инициализирует нужные схемы (efhc_core, efhc_admin, efhc_referrals, efhc_lottery, efhc_tasks).
#   • Отдаёт зависимость get_session() для FastAPI-роутов/сервисов.
#   • Содержит утилиты: проверка соединения, мягкая интеграция с Alembic (заглушка).
#
# Как связано с другими файлами:
#   • config.py — источник всех переменных окружения и имён схем.
#   • models.py — декларативные модели с __table_args__={"schema": "<...>"} (используют эти схемы).
#   • main.py — вызывает on_startup_init_db() при запуске сервера/функции Vercel.
#
# Как менять:
#   • Если DATABASE_URL пустой — упадём на DATABASE_URL_LOCAL из config.py.
#   • Чтобы включить лог SQL — установите DEBUG=true в .env (и читайте его в config.py при желании).
#   • Для Alembic: подключите реальное выполнение миграций в run_alembic_migrations().
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

# Глобальные синглтоны (создаются один раз на процесс/воркер)
_engine: Optional[AsyncEngine] = None
_SessionFactory: Optional[sessionmaker] = None


def _build_database_url() -> str:
    """
    Возвращает корректный async URL для SQLAlchemy с драйвером asyncpg.
    - Если DATABASE_URL в окружении пустой → берём локальный DATABASE_URL_LOCAL.
    - Если URL начинается с 'postgres://' → заменяем на 'postgresql+asyncpg://'
      (частый случай с Neon/Vercel).
    """
    settings = get_settings()
    url = settings.DATABASE_URL or settings.DATABASE_URL_LOCAL

    # Приводим к async-формату для SQLAlchemy
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        # тоже ок, но нам нужен именно драйвер asyncpg
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def _get_pool_sizes() -> tuple[int, int]:
    """
    Читает размеры пула из настроек. На Vercel serverless больших пулов не надо,
    но для локалки/Render числа могут быть выше.
    """
    s = get_settings()
    return (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW)


def get_engine() -> AsyncEngine:
    """
    Ленивая инициализация AsyncEngine. Используйте этот метод везде,
    где нужен engine (инициализация схем, health-check, миграции и т.д.).
    """
    global _engine, _SessionFactory

    if _engine is not None:
        return _engine

    settings = get_settings()
    db_url = _build_database_url()
    pool_size, max_overflow = _get_pool_sizes()

    # echo=False — чтобы не засорять логами. Если хотите дебаг SQL, включайте echo=True.
    # pool_pre_ping=True — полезно при долгих простоях соединений.
    _engine = create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        future=True,
    )

    # Фабрика сессий (autoflush=False: контроль ручной; expire_on_commit=False: объекты живы после commit)
    _SessionFactory = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    return _engine


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    Асинхронный контекстный менеджер «сессия как транзакция».
    Пример:
        async with session_scope() as db:
            ... работа с db ...
    """
    if _SessionFactory is None:
        get_engine()  # инициализируем engine и фабрику, если ещё не

    assert _SessionFactory is not None, "Session factory not initialized"
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
    """
    if _SessionFactory is None:
        get_engine()

    assert _SessionFactory is not None, "Session factory not initialized"
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
    Создаёт необходимые схемы, если их нет.
    Список схем берётся из config.py (DB_SCHEMA_*).
    Важно запускать до создания/использования таблиц.
    """
    settings = get_settings()
    engine = engine or get_engine()

    schemas = [
        settings.DB_SCHEMA_CORE,
        settings.DB_SCHEMA_ADMIN,
        settings.DB_SCHEMA_REFERRAL,
        settings.DB_SCHEMA_LOTTERY,
        settings.DB_SCHEMA_TASKS,
    ]

    create_sql = "; ".join([f'CREATE SCHEMA IF NOT EXISTS "{sch}"' for sch in schemas])

    async with engine.begin() as conn:
        await conn.execute(text(create_sql))


async def set_default_search_path(engine: Optional[AsyncEngine] = None) -> None:
    """
    (Необязательный шаг) Устанавливает search_path для упрощения SQL-скриптов.
    Модели у нас и так используют schema=..., но полезно при голых SQL.
    """
    settings = get_settings()
    engine = engine or get_engine()

    # Порядок важен — сначала core, затем остальные, в конце public
    search_path = ",".join(
        [
            settings.DB_SCHEMA_CORE,
            settings.DB_SCHEMA_ADMIN,
            settings.DB_SCHEMA_REFERRAL,
            settings.DB_SCHEMA_LOTTERY,
            settings.DB_SCHEMA_TASKS,
            "public",
        ]
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'SET search_path TO {search_path}'))


async def check_db_connection(engine: Optional[AsyncEngine] = None) -> bool:
    """
    Простой health-check (SELECT 1). Возвращает True, если соединение установлено.
    """
    engine = engine or get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        # Можно залогировать e, но здесь сознательно не шумим
        return False


async def run_alembic_migrations() -> None:
    """
    Заглушка под реальные миграции Alembic.
    На проде/CI вы обычно запускаете миграции отдельной командой:
        alembic upgrade head
    Если хотите запускать миграции из приложения — интегрируйте здесь вызов Alembic.
    """
    # Пример (псевдокод):
    # from alembic import command
    # from alembic.config import Config as AlembicConfig
    # cfg = AlembicConfig("alembic.ini")
    # command.upgrade(cfg, "head")
    pass


async def on_startup_init_db() -> None:
    """
    Вызывается из main.py при старте приложения/функции:
        app.add_event_handler("startup", on_startup_init_db)
    Делает:
      1) ленивую инициализацию движка/сессии,
      2) создание схем (idempotent),
      3) (опционально) установку search_path,
      4) (опционально) запуск миграций,
      5) health-check.
    """
    engine = get_engine()
    await ensure_schemas(engine)
    # Не обязательно, но удобно:
    await set_default_search_path(engine)
    # Подключение Alembic по желанию:
    # await run_alembic_migrations()
    ok = await check_db_connection(engine)
    if not ok:
        # Явно бросаем исключение, чтобы платформа перезапустила инстанс/функцию
        raise RuntimeError("Database connection failed during startup.")


async def on_shutdown_dispose() -> None:
    """
    Корректное закрытие движка при остановке приложения.
    """
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


# ------------------------------------------------------------
# Локальный тест: python -m backend.app.database
# (пригодится, если хотите быстро проверить коннект и создание схем)
# ------------------------------------------------------------
if __name__ == "__main__":
    async def _selftest():
        print("[EFHC][DB] Initializing...")
        await on_startup_init_db()
        ok = await check_db_connection()
        print(f"[EFHC][DB] Health check: {'OK' if ok else 'FAIL'}")

    asyncio.run(_selftest())
