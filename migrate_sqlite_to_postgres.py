"""One-time migration of the bundled SQLite DB into Neon/PostgreSQL.

Run from the project root after installing requirements:

  PowerShell:
    $env:DATABASE_URL="postgresql+asyncpg://..."
    python migrate_sqlite_to_postgres.py

The source SQLite file defaults to ./airdrop_bot.db.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

from db.models import Base, Draft, Project, PublishedPost, WebSettings

MODELS = [Project, Draft, PublishedPost, WebSettings]


def normalize_url(url: str) -> tuple[str, dict]:
    if url.startswith("postgresql+asyncpg://"):
        normalized = url
    elif url.startswith("postgresql://"):
        normalized = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        normalized = "postgresql+asyncpg://" + url[len("postgres://") :]
    else:
        return url, {}

    # Neon gives libpq-style query parameters such as sslmode/channel_binding.
    # asyncpg does not accept these as keyword arguments, so remove them from
    # the URL and pass SSL explicitly through SQLAlchemy's connect_args.
    parts = urlsplit(normalized)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    connect_args = {"ssl": "require"} if sslmode in (None, "require", "verify-ca", "verify-full") else {}
    return cleaned, connect_args


async def main() -> None:
    source = Path(os.getenv("SQLITE_PATH", "airdrop_bot.db")).resolve()
    target, connect_args = normalize_url(os.environ["DATABASE_URL"])
    if not source.exists():
        raise SystemExit(f"SQLite source not found: {source}")
    if not target.startswith("postgresql+asyncpg://"):
        raise SystemExit("DATABASE_URL must be a PostgreSQL URL")

    sqlite_engine = create_engine(f"sqlite:///{source}")
    with Session(sqlite_engine) as src:
        rows = {model: src.execute(select(model)).scalars().all() for model in MODELS}

    pg_engine = create_async_engine(target, connect_args=connect_args)
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Fresh Neon database is expected. Clear tables in FK-safe order.
        for model in reversed(MODELS):
            await conn.execute(text(f'DELETE FROM "{model.__tablename__}"'))
        for model in MODELS:
            for row in rows[model]:
                data = {column.name: getattr(row, column.name) for column in model.__table__.columns}
                await conn.execute(model.__table__.insert().values(**data))

        # Explicit SQLite IDs need PostgreSQL sequences advanced past the copied rows.
        for model in MODELS:
            pk = list(model.__table__.primary_key.columns)[0].name
            table = model.__tablename__
            sequence = await conn.scalar(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table, "column_name": pk},
            )
            if sequence:
                max_id = await conn.scalar(text(f'SELECT MAX("{pk}") FROM "{table}"'))
                if max_id is not None:
                    await conn.execute(text("SELECT setval(:sequence_name, :value, true)"), {"sequence_name": sequence, "value": int(max_id)})

    await pg_engine.dispose()
    sqlite_engine.dispose()
    print("Migration completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
