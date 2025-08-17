# backend/app/database.py
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# ------------------------------------------------------
# Конфигурация базы данных
# ------------------------------------------------------
# Для Supabase строка подключения берётся из переменных окружения
# Пример: postgresql+asyncpg://user:password@dbhost:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/efhc_db")

# Асинхронный движок
engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # можно True для отладки SQL
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
)

# Фабрика сессий
async_session_factory = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Базовый класс моделей
Base = declarative_base()


# ------------------------------------------------------
# Утилита для получения сессии
# ------------------------------------------------------
async def get_session() -> AsyncSession:
    async with async_session_factory() as session:
        yield session
