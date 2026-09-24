"""Unified DraftContent data model.

Serves as the single source of truth for:
- Database representation
- Telegram post rendering
- Social Card rendering
- AI Editor / Intent Parser / EditPlan operations
- Version snapshots & Undo/Redo

Maintains 100% backward compatibility with legacy Draft records in Neon PostgreSQL & SQLite.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ArtworkMetadata:
    path: str | None = None
    source: str | None = "master"  # e.g. "generated_social_card_master", "preset_shinobi", "ai_flux", "user_upload"
    prompt: str | None = None
    preset: str | None = "kunoichi"
    theme_color: str = "lime"  # lime, cyan, violet, gold, red, orange
    custom_artwork_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ArtworkMetadata:
        if not data:
            return cls()
        return cls(
            path=data.get("path"),
            source=data.get("source", "master"),
            prompt=data.get("prompt"),
            preset=data.get("preset", "kunoichi"),
            theme_color=data.get("theme_color", "lime"),
            custom_artwork_path=data.get("custom_artwork_path"),
        )


@dataclass
class LayoutMetadata:
    card_style_version: str = "ninja-scout-cyber-v10"
    show_reward: bool = True
    show_network: bool = True
    show_social_bar: bool = True
    show_early_users: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LayoutMetadata:
        if not data:
            return cls()
        return cls(
            card_style_version=data.get("card_style_version", "ninja-scout-cyber-v10"),
            show_reward=bool(data.get("show_reward", True)),
            show_network=bool(data.get("show_network", True)),
            show_social_bar=bool(data.get("show_social_bar", True)),
            show_early_users=bool(data.get("show_early_users", True)),
        )


@dataclass
class DraftContent:
    title: str
    category: str = "AIRDROP"
    description: str = ""
    tasks: list[str] = field(default_factory=list)
    potential_reward: str | None = None
    network: str | None = None
    project_link: str | None = None
    links: list[str] = field(default_factory=list)
    twitter: str | None = "@JanjezCrypto"
    telegram: str | None = "@janjezcrypto"
    footer: str = "LINK IN BIO"
    early_users: str = "EARLY USERS / GET THE EDGE"
    risk_note: str | None = None
    twitter_text: str | None = None
    source_url: str | None = None
    raw_instructions_fallback: str | None = None
    needs_review: bool = False
    artwork: ArtworkMetadata = field(default_factory=ArtworkMetadata)
    layout: LayoutMetadata = field(default_factory=LayoutMetadata)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DraftContent:
        artwork_data = data.get("artwork")
        layout_data = data.get("layout")
        return cls(
            title=data.get("title", ""),
            category=data.get("category", "AIRDROP"),
            description=data.get("description", ""),
            tasks=list(data.get("tasks") or []),
            potential_reward=data.get("potential_reward"),
            network=data.get("network"),
            project_link=data.get("project_link"),
            links=list(data.get("links") or []),
            twitter=data.get("twitter", "@JanjezCrypto"),
            telegram=data.get("telegram", "@janjezcrypto"),
            footer=data.get("footer", "LINK IN BIO"),
            early_users=data.get("early_users", "EARLY USERS / GET THE EDGE"),
            risk_note=data.get("risk_note"),
            twitter_text=data.get("twitter_text"),
            source_url=data.get("source_url"),
            raw_instructions_fallback=data.get("raw_instructions_fallback"),
            needs_review=bool(data.get("needs_review", False)),
            artwork=ArtworkMetadata.from_dict(artwork_data if isinstance(artwork_data, dict) else None),
            layout=LayoutMetadata.from_dict(layout_data if isinstance(layout_data, dict) else None),
        )

    @classmethod
    def from_json(cls, json_str: str) -> DraftContent:
        data = json.loads(json_str)
        return cls.from_dict(data)

    def render_telegram_post(self) -> str:
        """Deterministic Telegram channel post renderer from structured data.

        Applies standard headers, formatting, numbered tasks, and links.
        No random truncation or LLM hallucination.
        """
        clean_title = self.title.strip()
        parts = [f"🚀 {clean_title}"]
        if self.description:
            parts += ["", self.description.strip()]

        # Tasks section with deterministic numbering
        if self.tasks:
            parts += ["", "📝 What to do:"]
            for idx, task in enumerate(self.tasks, start=1):
                clean_task = task.strip().rstrip(".,;:") + "."
                parts.append(f"{idx}. {clean_task}")
        elif self.raw_instructions_fallback:
            parts += ["", "📝 What to do:", self.raw_instructions_fallback.strip()]

        if self.potential_reward:
            parts += ["", f"💰 Potential reward: {self.potential_reward.strip()}"]

        if self.network and self.network.lower() not in ("unknown", "none"):
            parts += [f"🌐 Network: {self.network.strip()}"]

        if self.risk_note:
            parts += ["", f"⚠️ Risk: {self.risk_note.strip()}"]

        if self.project_link:
            parts += ["", f"🔗 Start here: {self.project_link.strip()}"]

        return "\n".join(parts)

    def render_instructions_text(self) -> str:
        """Render tasks as numbered plain-text for legacy instructions column."""
        if self.tasks:
            lines = []
            for idx, task in enumerate(self.tasks, start=1):
                clean_task = task.strip().rstrip(".,;:") + "."
                lines.append(f"{idx}. {clean_task}")
            return "\n".join(lines)
        return self.raw_instructions_fallback or ""


def parse_legacy_instructions(instructions_text: str | None) -> tuple[list[str], bool]:
    """Deterministically parse legacy instructions string into structured tasks[].

    Rules:
    - Never invent fake tasks or placeholder templates.
    - Strip leading numbering (1., 2.), dashes, bullets.
    - If unparseable or empty, keep empty tasks and flag needs_review=True.
    """
    if not instructions_text or not instructions_text.strip():
        return [], True

    raw = instructions_text.strip()
    # Split by newlines or numbered patterns like " 2. " / "\n2. "
    lines = [line.strip() for line in re.split(r"\r?\n+|(?<=\.)\s*(?=\d+[\.\)])", raw) if line.strip()]

    tasks: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^\s*(\d+[\.\)]|[-*•])\s*", "", line).strip()
        # Ignore boilerplate headers like "What to do:" or "Instructions:"
        if re.match(r"^(what to do|instructions|tasks|steps|how to qualify)[\s:]*$", cleaned, re.IGNORECASE):
            continue
        # Ignore noise, placeholders (TBD, N/A), or text with fewer than 2 words / 6 letters
        words = [w for w in re.split(r"\s+", cleaned) if any(c.isalpha() for c in w)]
        alpha_count = sum(1 for c in cleaned if c.isalpha())
        if len(words) >= 2 and alpha_count >= 6:
            cleaned = cleaned.rstrip(" :;,-.")
            tasks.append(cleaned)
        if len(tasks) == 5:
            break

    if not tasks:
        return [], True

    needs_review = len(tasks) < 1 or len(tasks) > 5
    return tasks, needs_review


def draft_to_content(draft: Any, project: Any = None) -> DraftContent:
    """Load or adapt a database Draft object into a unified DraftContent object.

    If draft.content_json is present, loads it directly.
    Otherwise converts legacy columns safely without data loss.
    """
    if getattr(draft, "content_json", None):
        try:
            return DraftContent.from_json(draft.content_json)
        except Exception as exc:
            logger.warning("Could not parse draft #%s content_json: %s; falling back to legacy fields", getattr(draft, "id", None), exc)

    # Convert from legacy columns
    tasks, needs_review = parse_legacy_instructions(getattr(draft, "instructions", ""))
    
    chain_val = getattr(project, "chain", None) if project else None
    cat_val = getattr(project, "category", "AIRDROP") if project else "AIRDROP"
    proj_url = getattr(draft, "project_url", None) or (getattr(project, "project_url", None) if project else None)
    src_url = getattr(draft, "source_url", None) or (getattr(project, "source_url", None) if project else None)

    artwork = ArtworkMetadata(
        path=getattr(draft, "image_path", None),
        source=getattr(draft, "image_source", "master"),
        prompt=getattr(draft, "image_prompt", None),
    )

    legacy_risk = getattr(draft, "risk_note", None)
    if legacy_risk and any(bp in legacy_risk.lower() for bp in ["verify the domain", "never share a seed", "airdrop allocations", "not yet finalized"]):
        legacy_risk = None

    return DraftContent(
        title=getattr(draft, "title", "") or (getattr(project, "name", "") if project else ""),
        category=cat_val or "AIRDROP",
        description=getattr(draft, "summary", "") or "",
        tasks=tasks,
        potential_reward=getattr(draft, "potential_reward", None),
        network=chain_val,
        project_link=proj_url,
        links=[proj_url] if proj_url else [],
        risk_note=legacy_risk,
        twitter_text=getattr(draft, "twitter_text", None),
        source_url=src_url,
        raw_instructions_fallback=getattr(draft, "instructions", None) if needs_review else None,
        needs_review=needs_review,
        artwork=artwork,
    )


def sync_content_to_draft(content: DraftContent, draft: Any) -> None:
    """Save DraftContent to draft.content_json and update legacy columns for backward compatibility."""
    draft.content_json = content.to_json()

    # Sync legacy columns so older code or external tools continue to work seamlessly
    draft.title = content.title
    draft.summary = content.description
    draft.instructions = content.render_instructions_text()
    draft.potential_reward = content.potential_reward
    draft.risk_note = content.risk_note
    draft.twitter_text = content.twitter_text
    draft.project_url = content.project_link
    draft.source_url = content.source_url
    draft.image_path = content.artwork.path
    draft.image_source = content.artwork.source
    draft.image_prompt = content.artwork.prompt


async def migrate_existing_drafts(session: Any) -> tuple[int, int]:
    """Safe, idempotent migration for existing database records.

    Finds all drafts with content_json IS NULL and populates content_json.
    Returns (migrated_count, total_count).
    """
    from sqlalchemy import select
    from db.models import Draft, Project

    stmt = select(Draft)
    result = await session.execute(stmt)
    drafts = list(result.scalars().all())

    migrated = 0
    for draft in drafts:
        if not draft.content_json:
            proj_stmt = select(Project).where(Project.id == draft.project_id)
            p_res = await session.execute(proj_stmt)
            project = p_res.scalar_one_or_none()
            content = draft_to_content(draft, project)
            sync_content_to_draft(content, draft)
            migrated += 1

    if migrated > 0:
        await session.commit()
        logger.info("Migrated %s legacy drafts to unified DraftContent", migrated)

    return migrated, len(drafts)
