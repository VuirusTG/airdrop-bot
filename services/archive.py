"""
Dedup + archive logic: a project is "archived" simply by having a terminal
status (FILTERED_OUT, DELETED, or PUBLISHED). Before ingesting anything new,
we compute its dedup_hash and check whether that hash already exists with a
terminal status — if so, we skip it, so the bot never re-offers a project
you've already decided on (or that already got auto-filtered).
"""
import hashlib
import re

from sqlalchemy import select

from db.models import Project, ProjectStatus

TERMINAL_STATUSES = {
    ProjectStatus.DELETED,
    ProjectStatus.PUBLISHED,
}


def compute_dedup_hash(name: str, chain: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    normalized += (chain or "").lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


async def is_archived(session, dedup_hash: str) -> bool:
    result = await session.execute(
        select(Project).where(
            Project.dedup_hash == dedup_hash,
            Project.status.in_(TERMINAL_STATUSES),
        )
    )
    return result.scalar_one_or_none() is not None
