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

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args=_connect_args(),
    pool_pre_ping=True,
    pool_recycle=300,
)
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
            "content_json": "TEXT",
            "edit_plan_json": "TEXT",
        },
    )
    await _ensure_columns(
        conn,
        "projects",
        {"filter_version": "INTEGER DEFAULT 1", "project_url": "TEXT"},
    )
    def check_draft_snapshots_action(sync_conn):
        insp = inspect(sync_conn)
        if not insp.has_table("draft_snapshots"):
            return False
        for col in insp.get_columns("draft_snapshots"):
            if col["name"] == "action":
                col_type = str(col.get("type", "")).upper()
                if "255" in col_type or "TEXT" in col_type:
                    return False
                return True
        return False

    needs_widen = await conn.run_sync(check_draft_snapshots_action)
    if needs_widen and conn.dialect.name == "postgresql":
        async with conn.begin_nested():
            try:
                await conn.execute(text("ALTER TABLE draft_snapshots ALTER COLUMN action TYPE VARCHAR(255)"))
            except Exception:
                pass


async def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    def get_existing_columns(sync_conn):
        insp = inspect(sync_conn)
        if not insp.has_table(table):
            return set()
        return {column["name"] for column in insp.get_columns(table)}

    existing = await conn.run_sync(get_existing_columns)
    if not existing:
        return
    for name, sql_type in columns.items():
        if name not in existing:
            async with conn.begin_nested():
                try:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                except Exception:
                    pass


@asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        yield session
