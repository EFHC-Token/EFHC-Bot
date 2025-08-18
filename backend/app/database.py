# 📂 backend/app/database.py — подключение к БД (Neon/PostgreSQL, async)
# -----------------------------------------------------------------------------
# Что делает файл:
#   - Создаёт async SQLAlchemy Engine и SessionMaker.
#   - Даёт зависимость FastAPI get_session().
#   - Подготавливает search_path = наши схемы (core/referrals/admin/lottery/tasks).
#
# Как связано:
#   - Используется во всех роутингах (Depends(get_session)).
#   - models.py использует Base, но созданный движок здесь.
# -----------------------------------------------------------------------------

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from .config import get_settings

settings = get_settings()

# Создаём движок
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    echo=False,  # включите True для дебага SQL
)

# Фабрика сессий
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

async def _set_search_path(session: AsyncSession) -> None:
    # Установим schema search_path, чтобы можно было обращаться к таблицам без схемы
    schemas = ",".join([
        settings.DB_SCHEMA_CORE,
        settings.DB_SCHEMA_REFERRAL,
        settings.DB_SCHEMA_ADMIN,
        settings.DB_SCHEMA_LOTTERY,
        settings.DB_SCHEMA_TASKS,
        "public",
    ])
    await session.execute(text(f"SET search_path TO {schemas}"))

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        await _set_search_path(session)
        yield session
