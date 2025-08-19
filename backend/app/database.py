# 📂 backend/app/database.py — подключение к БД, пул, сессии, создание схем
# -----------------------------------------------------------------------------
# ПОЛНАЯ версия (ничего не удаляем, только дополняем)
# -----------------------------------------------------------------------------
# Что делает файл:
#   • Создаёт async-движок SQLAlchemy (PostgreSQL/Neon) с драйвером asyncpg.
#   • Инициализирует нужные схемы (efhc_core, efhc_admin, efhc_referrals, efhc_lottery, efhc_tasks).
#   • Отдаёт зависимость get_session() для FastAPI-роутов/сервисов.
#   • Содержит утилиты: проверка соединения, мягкая интеграция с Alembic (заглушка),
#     установка search_path (опционально), session_scope (контекст).
#
# Как связано с другими файлами:
#   • config.py — источник всех переменных окружения и имён схем.
#   • models.py — декларативные модели с __table_args__={"schema": "<...>"} (используют эти схемы).
#   • main.py — вызывает on_startup_init_db() при запуске сервера/функции Vercel.
#
# Примечания:
#   • Строго следуем async-паттернам SQLAlchemy 2.x.
#   • Ничего не удалено: и session_scope, и get_session, и ensure_schemas, и search_path — всё есть.
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
    Логика:
      1) Берём settings.DATABASE_URL (ожидаем SSL с Neon).
      2) Если пусто — пытаемся собрать из EFHC_DB_* (Vercel/Neon).
      3) Если всё ещё пусто — берём локальный settings.DATABASE_URL_LOCAL.
      4) Любой URL вида postgres:// или postgresql:// → преобразуем к postgresql+asyncpg://
    """
    s = get_settings()

    url = s.DATABASE_URL or ""
    if not url:
        # Пытаемся восстановить из EFHC_DB_* (переменные, которые Vercel подтягивает от Neon)
        # EFHC_DB_POSTGRES_URL_NO_SSL зачастую выглядит как "postgres://user:pass@host/db"
        if s.EFHC_DB_POSTGRES_URL_NO_SSL:
            url = s.EFHC_DB_POSTGRES_URL_NO_SSL
        else:
            url = s.DATABASE_URL_LOCAL

    # Приводим к async-формату для SQLAlchemy (asyncpg)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "asyncpg" not in url:
        # Переносим на asyncpg-драйвер
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def _get_pool_sizes() -> tuple[int, int]:
    """
    Читает размеры пула из настроек. На Vercel serverless больших пулов не надо,
    но для локалки/Render числа могут быть выше. Настройки берём из config.py.
    """
    s = get_settings()
    return (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW)


def get_engine() -> AsyncEngine:
    """
    Ленивая инициализация AsyncEngine. Используйте этот метод везде,
    где нужен engine (инициализация схем, health-check, миграции и т.д.).
    НИЧЕГО не удаляем — полностью совместим со старым кодом.
    """
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    db_url = _build_database_url()
    pool_size, max_overflow = _get_pool_sizes()

    # echo=False — чтобы не засорять логами. Для дебага SQL можно поставить True.
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
    Мы НЕ удаляем эту абстракцию, потому что многие сервисы могут её использовать.
    """
    if _SessionFactory is None:
        get_engine()  # инициализируем engine и фабрику, если ещё нет

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
    Эту функцию мы тоже НЕ удаляем.
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
    Создаёт необходимые схемы, если их нет (idempotent).
    Список схем берётся из config.py (DB_SCHEMA_*).
    Важно запускать до создания/использования таблиц (обычно — в startup).
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

    create_sql = "; ".join([f'CREATE SCHEMA IF NOT EXISTS "{sch}"' for sch in schemas])

    async with engine.begin() as conn:
        await conn.execute(text(create_sql))


async def set_default_search_path(engine: Optional[AsyncEngine] = None) -> None:
    """
    (Необязательный шаг) Устанавливает search_path для упрощения SQL-скриптов.
    Модели у нас и так используют schema=..., но это удобно при голых SQL.
    Мы НЕ удаляем эту утилиту — кому-то из сервисов/миграций она может пригодиться.
    """
    s = get_settings()
    engine = engine or get_engine()

    # Порядок важен — сначала core, затем остальные, в конце public
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
        await conn.execute(text(f'SET search_path TO {search_path}'))


async def check_db_connection(engine: Optional[AsyncEngine] = None) -> bool:
    """
    Простой health-check (SELECT 1). Возвращает True, если соединение установлено.
    Тоже оставляем — используется в старом коде и полезно для диагностики.
    """
    engine = engine or get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def run_alembic_migrations() -> None:
    """
    Заглушка под реальные миграции Alembic.
    На проде/CI вы обычно запускаете миграции отдельной командой:
        alembic upgrade head
    Если хотите запускать миграции из приложения — интегрируйте здесь вызов Alembic.
    Мы НЕ удаляем эту функцию — оставляем, как в старом коде.
    """
    # Пример (псевдокод):
    # from alembic import command
    # from alembic.config import Config as AlembicConfig
    # cfg = AlembicConfig("backend/alembic.ini")
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
    НИЧЕГО не удаляем — полный стартовый пайплайн как и раньше.
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
    Тоже оставляем — важно для аккуратного завершения.
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
