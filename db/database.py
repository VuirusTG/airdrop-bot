"""
Async engine + session factory. Call init_db() once on startup.
"""
from contextlib import asynccontextmanager

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings


def _connect_args() -> dict:
    """Use TLS for Neon/asyncpg without passing libpq-only URL parameters."""
    if settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        return {"ssl": "require"}
    return {}
from db.models import Base

engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args=_connect_args())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_optional_columns(conn)


async def _ensure_optional_columns(conn) -> None:
    """Small forward-compatible migrations for local SQLite databases."""
    await _ensure_columns(
        conn,
        "drafts",
        {
            "twitter_text": "TEXT",
            "image_prompt": "TEXT",
            "image_source": "VARCHAR(64)",
            "source_url": "TEXT",
            "project_url": "TEXT",
        },
    )
    await _ensure_columns(
        conn,
        "projects",
        {"filter_version": "INTEGER DEFAULT 1", "project_url": "TEXT"},
    )


async def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    def get_existing_columns(sync_conn):
        return {column["name"] for column in inspect(sync_conn).get_columns(table)}

    existing = await conn.run_sync(get_existing_columns)
    for name, sql_type in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


@asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        yield session
