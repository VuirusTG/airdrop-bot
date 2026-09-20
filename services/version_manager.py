"""Version snapshot and Undo/Redo manager for DraftContent."""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import desc, select

from db.database import get_session
from db.models import Draft, DraftSnapshot, Project
from services.draft_content import DraftContent, draft_to_content, sync_content_to_draft
from services.social_card import render_social_card_from_content

logger = logging.getLogger(__name__)


class VersionManager:
    """Manages version snapshots and rollback for drafts."""

    @staticmethod
    async def save_snapshot(
        session: Any,
        draft_id: int,
        project_id: int,
        action: str,
        user_command: str | None,
        edit_plan_json: str | None,
        content: DraftContent,
    ) -> DraftSnapshot:
        """Create an immutable snapshot of the draft state."""
        # Find next version number for this draft
        stmt = (
            select(DraftSnapshot.version)
            .where(DraftSnapshot.draft_id == draft_id)
            .order_by(desc(DraftSnapshot.version))
            .limit(1)
        )
        res = await session.execute(stmt)
        last_version = res.scalar_one_or_none() or 0
        new_version = last_version + 1

        safe_action = (action or "Редактирование").strip()
        if len(safe_action) > 250:
            safe_action = safe_action[:247] + "..."

        snapshot = DraftSnapshot(
            draft_id=draft_id,
            project_id=project_id,
            version=new_version,
            action=safe_action,
            user_command=user_command,
            edit_plan_json=edit_plan_json,
            content_json=content.to_json(),
        )
        session.add(snapshot)
        try:
            await session.commit()
        except Exception as exc:
            # Safe fallback if database column is still VARCHAR(64) before migration
            if "character varying" in str(exc) or "StringDataRightTruncationError" in type(exc).__name__:
                await session.rollback()
                snapshot.action = safe_action[:60] + "..." if len(safe_action) > 64 else safe_action[:64]
                session.add(snapshot)
                await session.commit()
            else:
                raise

        logger.info("Saved snapshot v%s for draft #%s (action: %s)", new_version, draft_id, snapshot.action)
        return snapshot

    @staticmethod
    async def undo(session: Any, draft_id: int) -> tuple[DraftContent | None, str | None]:
        """Roll back to the previous snapshot state.

        Returns:
            (restored_content, status_message)
        """
        stmt = (
            select(DraftSnapshot)
            .where(DraftSnapshot.draft_id == draft_id)
            .order_by(desc(DraftSnapshot.version))
            .limit(2)
        )
        res = await session.execute(stmt)
        snapshots = list(res.scalars().all())

        if len(snapshots) < 2:
            return None, "Нет предыдущих версий для отмены (это исходная версия)."

        current_snap = snapshots[0]
        prev_snap = snapshots[1]

        # Load draft and project
        draft_stmt = select(Draft).where(Draft.id == draft_id)
        d_res = await session.execute(draft_stmt)
        draft = d_res.scalar_one_or_none()
        if not draft:
            return None, "Черновик не найден."

        proj_stmt = select(Project).where(Project.id == draft.project_id)
        p_res = await session.execute(proj_stmt)
        project = p_res.scalar_one_or_none()

        # Restore previous content
        restored_content = DraftContent.from_json(prev_snap.content_json)
        sync_content_to_draft(restored_content, draft)

        # Rerender social card if needed
        card = await render_social_card_from_content(restored_content)
        if card:
            draft.image_path = card.path
            restored_content.artwork.path = card.path

        # Delete the undone snapshot so subsequent undo walks backwards
        await session.delete(current_snap)
        await session.commit()

        msg = f"↩️ Отменено: {current_snap.action}. Восстановлена версия v{prev_snap.version}."
        logger.info("Undone draft #%s to v%s", draft_id, prev_snap.version)
        return restored_content, msg

    @staticmethod
    async def can_undo(session: Any, draft_id: int) -> bool:
        stmt = (
            select(DraftSnapshot.id)
            .where(DraftSnapshot.draft_id == draft_id)
            .limit(2)
        )
        res = await session.execute(stmt)
        return len(list(res.scalars().all())) >= 2
