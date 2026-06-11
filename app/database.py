"""
SQLite async database setup for Homelab Hub.
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/homelab_hub.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


async def init_db():
    """Create all tables on first run."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # WAL for better concurrent read behavior
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
