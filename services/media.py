"""Convert stored local paths or remote URLs into aiogram photo inputs with on-demand recovery."""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def resolve_image_path(value: str | None) -> Path | None:
    if not value or value.startswith(("http://", "https://")):
        return None
    path = Path(value)
    if path.is_file():
        return path
    # Check in standard images/generated location by filename
    alt = ROOT / "images" / "generated" / path.name
    if alt.is_file():
        return alt
    # Check relative to project root
    if not path.is_absolute():
        direct = ROOT / path
        if direct.is_file():
            return direct
    return None


def telegram_photo(value: str | None) -> FSInputFile | str | None:
    if not value:
        return None
    if value.startswith(("https://", "http://")):
        return value
    resolved = resolve_image_path(value)
    if resolved and resolved.is_file():
        return FSInputFile(resolved)
    path = Path(value)
    if path.is_file():
        return FSInputFile(path)
    return None


async def ensure_draft_image(project, draft) -> str | None:
    """Ensure that the draft image exists on disk or as a URL, regenerating if missing."""
    if not draft or not project:
        return None
    if draft.image_path and draft.image_path.startswith(("http://", "https://")):
        return draft.image_path

    if draft.image_path:
        resolved = resolve_image_path(draft.image_path)
        if resolved and resolved.is_file():
            return str(resolved)

    # Missing on disk (e.g. after container restart or generated in GitHub Actions)
    # Regenerate deterministic social card on demand.
    try:
        from services.social_card import generate_social_card

        card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=draft.instructions,
            official_image_url=None,
            image_prompt=getattr(draft, "image_prompt", None),
            project_url=getattr(draft, "project_url", None) or getattr(project, "project_url", None),
            generation_key=f"project-{project.id}-v{getattr(draft, 'version', 1)}" if getattr(project, "id", None) else "initial",
            potential_reward=getattr(draft, "potential_reward", None),
        )
        if card and card.path and Path(card.path).is_file():
            draft.image_path = card.path
            draft.image_source = card.source
            return card.path
    except Exception as exc:
        logger.warning("Could not regenerate missing draft image for %s: %s", getattr(project, "name", "unknown"), exc)

    return None
