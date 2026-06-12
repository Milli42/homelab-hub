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
    """Create all tables on first run, then apply lightweight column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # WAL for better concurrent read behavior
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await _migrate(conn)


# Additive column migrations: (table, column, DDL type/clause). SQLite ADD COLUMN
# is a no-op-safe way to evolve the schema without Alembic. Each runs only if the
# column is missing, so this is idempotent.
_COLUMN_MIGRATIONS = [
    ("note_groups", "pin_hash", "VARCHAR(160)"),
]


async def _migrate(conn):
    for table, column, ddl in _COLUMN_MIGRATIONS:
        rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {r[1] for r in rows.fetchall()}
        if column not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
