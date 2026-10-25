"""Version snapshot and Undo/Redo manager for ContentSeries."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, desc, select

from db.models import ContentSeries, SeriesSnapshot
from services.series_models import SeriesData, sync_data_to_series_db

logger = logging.getLogger(__name__)


class SeriesVersionManager:
    """Manages version snapshots, Undo, and Redo for content series and threads."""

    @staticmethod
    async def save_snapshot(
        session: Any,
        series_id: int,
        action: str,
        user_command: str | None,
        series_data: SeriesData,
    ) -> SeriesSnapshot:
        """Create a version snapshot.

        Clears any forward redo history when a new edit is performed.
        """
        stmt = select(ContentSeries).where(ContentSeries.id == series_id)
        res = await session.execute(stmt)
        series = res.scalar_one_or_none()
        current_v = series.current_version if series and series.current_version else 0

        # Delete any redo snapshots with version > current_v
        if current_v > 0:
            del_stmt = delete(SeriesSnapshot).where(
                SeriesSnapshot.series_id == series_id,
                SeriesSnapshot.version > current_v,
            )
            await session.execute(del_stmt)

        new_version = current_v + 1
        if series:
            series.current_version = new_version

        safe_action = (action or "Редактирование серии").strip()
        if len(safe_action) > 250:
            safe_action = safe_action[:247] + "..."

        snapshot = SeriesSnapshot(
            series_id=series_id,
            version=new_version,
            action=safe_action,
            user_command=user_command,
            data_json=series_data.to_json(),
        )
        session.add(snapshot)
        await session.commit()
        logger.info("Saved SeriesSnapshot v%s for series #%s (%s)", new_version, series_id, safe_action)
        return snapshot

    @staticmethod
    async def undo(session: Any, series_id: int) -> tuple[SeriesData | None, str | None]:
        """Roll back to the previous version snapshot without deleting the undone state (enabling Redo)."""
        stmt = select(ContentSeries).where(ContentSeries.id == series_id)
        res = await session.execute(stmt)
        series = res.scalar_one_or_none()
        if not series:
            return None, "Серия не найдена."

        current_v = series.current_version or 1
        target_v = current_v - 1

        if target_v < 1:
            return None, "Нет предыдущих версий для отмены (это исходная версия)."

        target_stmt = select(SeriesSnapshot).where(
            SeriesSnapshot.series_id == series_id,
            SeriesSnapshot.version == target_v,
        )
        t_res = await session.execute(target_stmt)
        target_snap = t_res.scalar_one_or_none()

        if not target_snap:
            return None, "Предыдущая версия не найдена в истории."

        restored_data = SeriesData.from_json(target_snap.data_json)
        sync_data_to_series_db(restored_data, series)
        series.current_version = target_v
        await session.commit()

        msg = f"↩️ Отменено: {target_snap.action}. Восстановлена версия v{target_v}."
        logger.info("Undone series #%s from v%s to v%s", series_id, current_v, target_v)
        return restored_data, msg

    @staticmethod
    async def redo(session: Any, series_id: int) -> tuple[SeriesData | None, str | None]:
        """Roll forward to the next version snapshot after an Undo."""
        stmt = select(ContentSeries).where(ContentSeries.id == series_id)
        res = await session.execute(stmt)
        series = res.scalar_one_or_none()
        if not series:
            return None, "Серия не найдена."

        current_v = series.current_version or 1
        target_v = current_v + 1

        target_stmt = select(SeriesSnapshot).where(
            SeriesSnapshot.series_id == series_id,
            SeriesSnapshot.version == target_v,
        )
        t_res = await session.execute(target_stmt)
        target_snap = t_res.scalar_one_or_none()

        if not target_snap:
            return None, "Нет доступных версий для повтора (Redo)."

        restored_data = SeriesData.from_json(target_snap.data_json)
        sync_data_to_series_db(restored_data, series)
        series.current_version = target_v
        await session.commit()

        msg = f"↪️ Возвращено: {target_snap.action}. Восстановлена версия v{target_v}."
        logger.info("Redone series #%s from v%s to v%s", series_id, current_v, target_v)
        return restored_data, msg

    @staticmethod
    async def can_undo(session: Any, series_id: int) -> bool:
        stmt = select(ContentSeries.current_version).where(ContentSeries.id == series_id)
        res = await session.execute(stmt)
        curr_v = res.scalar_one_or_none() or 1
        if curr_v <= 1:
            return False
        # Check if snapshot exists
        s_stmt = select(SeriesSnapshot.id).where(
            SeriesSnapshot.series_id == series_id,
            SeriesSnapshot.version == curr_v - 1,
        ).limit(1)
        s_res = await session.execute(s_stmt)
        return s_res.scalar_one_or_none() is not None

    @staticmethod
    async def can_redo(session: Any, series_id: int) -> bool:
        stmt = select(ContentSeries.current_version).where(ContentSeries.id == series_id)
        res = await session.execute(stmt)
        curr_v = res.scalar_one_or_none() or 1
        s_stmt = select(SeriesSnapshot.id).where(
            SeriesSnapshot.series_id == series_id,
            SeriesSnapshot.version == curr_v + 1,
        ).limit(1)
        s_res = await session.execute(s_stmt)
        return s_res.scalar_one_or_none() is not None
