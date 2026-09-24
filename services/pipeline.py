"""Run one raw signal through deduplication, filtering, drafting, and review."""
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards import review_keyboard
from config import settings
from db.database import get_session
from db.models import Draft, Project, ProjectStatus
from services.archive import compute_dedup_hash, is_archived
from services.fallback_content import fallback_generate_draft, fallback_score_project
from services.groq_provider import generate_draft as generate_groq_draft
from services.groq_provider import score_project as score_groq_project
from services.llm_draft import generate_draft
from services.llm_filter import score_project
from services.media import ensure_draft_image, telegram_photo
from services.project_image import discover_project_image
from services.project_link import discover_project_link
from services.social_card import generate_social_card

logger = logging.getLogger(__name__)
AI_RETRY_COOLDOWN_SECONDS = 300
_gemini_unavailable_until = 0.0
_groq_unavailable_until = 0.0


@dataclass(frozen=True)
class PipelineResult:
    outcome: Literal["review", "filtered", "duplicate"]
    project: Project | None = None
    provider: Literal["gemini", "groq", "local", "none"] = "none"

    @property
    def used_fallback(self) -> bool:
        return self.provider == "local"

    @property
    def used_groq(self) -> bool:
        return self.provider == "groq"


def _gemini_available_for_scan() -> bool:
    return bool(settings.GEMINI_API_KEY) and monotonic() >= _gemini_unavailable_until


def _groq_available_for_scan() -> bool:
    return bool(settings.GROQ_API_KEY) and monotonic() >= _groq_unavailable_until


def _cloud_ai_available_for_scan() -> bool:
    return _gemini_available_for_scan() or _groq_available_for_scan()


def _pause_gemini_for_scan(exc: Exception) -> None:
    global _gemini_unavailable_until
    _gemini_unavailable_until = monotonic() + AI_RETRY_COOLDOWN_SECONDS
    logger.warning(
        "Backup Gemini unavailable for %.0f seconds: %s",
        AI_RETRY_COOLDOWN_SECONDS,
        exc,
    )


def _pause_groq_for_scan(exc: Exception) -> None:
    global _groq_unavailable_until
    _groq_unavailable_until = monotonic() + AI_RETRY_COOLDOWN_SECONDS
    logger.warning(
        "Primary Groq unavailable for %.0f seconds; trying backup providers: %s",
        AI_RETRY_COOLDOWN_SECONDS,
        exc,
    )


async def _score_with_fallbacks(name: str, raw_text: str, source_url: str | None):
    if _groq_available_for_scan():
        try:
            return await score_groq_project(name, raw_text, source_url), "groq"
        except Exception as exc:
            _pause_groq_for_scan(exc)

    if _gemini_available_for_scan():
        try:
            return await score_project(name, raw_text, source_url), "gemini"
        except Exception as exc:
            _pause_gemini_for_scan(exc)

    return fallback_score_project(name, raw_text), "local"


async def _draft_with_fallbacks(
    provider: str,
    name: str,
    raw_text: str,
    chain: str | None,
    category: str,
    source_url: str | None,
    project_url: str | None,
):
    if settings.OPENROUTER_API_KEY:
        try:
            from services.ai_editor_llm import ai_generate_initial_draft
            content, draft_res, prov = await ai_generate_initial_draft(
                name=name,
                raw_text=raw_text,
                chain=chain,
                category=category,
                source_url=source_url,
                project_url=project_url,
            )
            return draft_res, prov, content
        except Exception as exc:
            logger.warning("OpenRouter initial draft failed: %s; falling back", exc)

    if provider == "groq":
        try:
            draft_res = await generate_groq_draft(
                name, raw_text, chain, source_url, project_url
            )
            from services.draft_content import draft_to_content
            content = draft_to_content(draft_res, None)
            return draft_res, "groq", content
        except Exception as exc:
            _pause_groq_for_scan(exc)

    if provider in {"gemini", "groq"} and _gemini_available_for_scan():
        try:
            draft_res = await generate_draft(name, raw_text, chain, source_url, project_url)
            from services.draft_content import draft_to_content
            content = draft_to_content(draft_res, None)
            return draft_res, "gemini", content
        except Exception as exc:
            _pause_gemini_for_scan(exc)

    fb_res = fallback_generate_draft(name, raw_text, chain, category, project_url)
    from services.draft_content import draft_to_content
    content = draft_to_content(fb_res, None)
    return fb_res, "local", content


async def process_raw_signal(
    bot: Bot,
    name: str,
    raw_text: str,
    source: str,
    source_url: str | None = None,
) -> PipelineResult:
    dedup_hash = compute_dedup_hash(name, chain=None)

    async with get_session() as session:
        if await is_archived(session, dedup_hash):
            return PipelineResult("duplicate")

        result = await session.execute(
            select(Project)
            .options(selectinload(Project.drafts))
            .where(Project.dedup_hash == dedup_hash)
        )
        project = result.scalar_one_or_none()
        if project:
            filter_version = project.filter_version or 0
            needs_new_filter_version = (
                project.status == ProjectStatus.FILTERED_OUT
                and 0 < filter_version < settings.FILTER_VERSION
            )
            needs_cloud_recheck = (
                project.status == ProjectStatus.FILTERED_OUT
                and filter_version == 0
                and _cloud_ai_available_for_scan()
            )
            if not needs_new_filter_version and not needs_cloud_recheck:
                return PipelineResult("duplicate")

        filter_result, provider = await _score_with_fallbacks(name, raw_text, source_url)
        if project is None:
            project = Project(dedup_hash=dedup_hash, name=name, source=source)

        project.name = name
        project.chain = filter_result.chain
        project.category = filter_result.category
        project.source = source
        project.source_url = source_url
        project.raw_data = raw_text
        project.legitimacy_score = filter_result.score
        provider_label = "Groq" if provider == "groq" else "Gemini" if provider == "gemini" else None
        project.score_reasoning = (
            f"[{provider_label}] {filter_result.reasoning}"
            if provider_label
            else filter_result.reasoning
        )
        project.filter_version = settings.FILTER_VERSION

        if not filter_result.passes:
            # Version 0 keeps a local rejection eligible for a future cloud AI recheck.
            project.filter_version = 0 if provider == "local" else settings.FILTER_VERSION
            project.status = ProjectStatus.FILTERED_OUT
            session.add(project)
            await session.commit()
            return PipelineResult("filtered", project, provider)

        project_url = await discover_project_link(source_url, raw_text, name)
        project.project_url = project_url
        draft_result, provider, content = await _draft_with_fallbacks(
            provider,
            name,
            raw_text,
            filter_result.chain,
            filter_result.category,
            source_url,
            project_url,
        )
        official_image = await discover_project_image(source_url)
        social_card = await generate_social_card(
            name=name,
            category=filter_result.category,
            chain=filter_result.chain,
            instructions=draft_result.instructions,
            official_image_url=official_image.url if official_image else None,
            image_prompt=draft_result.image_prompt,
            project_url=project_url,
            potential_reward=draft_result.potential_reward,
            theme_color=content.artwork.theme_color if content and content.artwork else None,
            custom_steps=content.tasks if content and content.tasks else None,
        )
        image_path = social_card.path if social_card else official_image.url if official_image else None
        image_source = social_card.source if social_card else official_image.source if official_image else None
        draft = Draft(
            version=(project.latest_draft().version + 1) if project.latest_draft() else 1,
            title=draft_result.title,
            summary=draft_result.summary,
            instructions=draft_result.instructions,
            potential_reward=draft_result.potential_reward,
            risk_note=draft_result.risk_note,
            twitter_text=draft_result.twitter_text,
            image_path=image_path,
            image_source=image_source,
            image_prompt=draft_result.image_prompt,
            source_url=source_url,
            project_url=project_url,
            content_json=content.to_json() if content else None,
        )
        project.drafts.append(draft)
        project.status = ProjectStatus.PENDING_REVIEW
        session.add(project)
        await session.flush()

        # One candidate = one Telegram review message. This is important for
        # webhook delivery and makes Previous/Next edit a single stable message.
        if draft.image_path:
            try:
                await ensure_draft_image(project, draft)
            except Exception:
                pass

        photo_input = telegram_photo(draft.image_path) if draft.image_path else None
        caption = _review_caption(project, draft)
        twitter_in_caption = bool(draft.twitter_text and draft.twitter_text.strip() in caption)

        if photo_input is not None:
            try:
                message = await bot.send_photo(
                    chat_id=settings.ADMIN_USER_ID,
                    photo=photo_input,
                    caption=caption,
                    reply_markup=review_keyboard(project.id),
                )
            except Exception as exc:
                logger.warning("Could not attach candidate image for %s: %s", project.name, exc)
                message = await bot.send_message(
                    chat_id=settings.ADMIN_USER_ID,
                    text=_review_text(project, draft),
                    reply_markup=review_keyboard(project.id),
                )
        else:
            message = await bot.send_message(
                chat_id=settings.ADMIN_USER_ID,
                text=_review_text(project, draft),
                reply_markup=review_keyboard(project.id),
            )
        project.review_chat_id = message.chat.id
        project.review_message_id = message.message_id
        await session.commit()

        # If Twitter text was not included in the photo caption (e.g. caption reached 1024 chars),
        # send it as an explicit companion message so it is NEVER hidden!
        if draft.twitter_text and not twitter_in_caption and photo_input is not None:
            try:
                await bot.send_message(
                    chat_id=settings.ADMIN_USER_ID,
                    text=f"🐦 <b>2. Черновик для Twitter (X) [#{project.id}]:</b>\n\n{draft.twitter_text.strip()}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        return PipelineResult("review", project, provider)


def _review_caption(project: Project, draft: Draft) -> str:
    score = f"{project.legitimacy_score:.1f}/10" if project.legitimacy_score is not None else "n/a"
    lines = [
        f"🔎 REVIEW • #{project.id} • {score}",
        f"🚀 {draft.title}",
        "",
        draft.summary.strip(),
        "",
        "📝 What to do:",
        draft.instructions.strip(),
    ]
    if draft.potential_reward:
        lines += ["", f"💰 {draft.potential_reward.strip()}"]
    if draft.risk_note and not any(bp in draft.risk_note.lower() for bp in ["verify the domain", "never share a seed", "airdrop allocations", "not yet finalized"]):
        lines += ["", f"⚠️ {draft.risk_note.strip()}"]
    if draft.project_url:
        lines += ["", f"🔗 {draft.project_url}"]
    if draft.twitter_text:
        lines += ["", "🐦 Twitter (X):", draft.twitter_text.strip()]
    text = "\n".join(lines).strip()
    return text if len(text) <= 1024 else text[:1019].rsplit(" ", 1)[0] + "…"


def _review_text(project: Project, draft: Draft) -> str:
    confidence_note = "сомнительный кандидат — проверьте источник" if project.legitimacy_score < 6 else "кандидат прошел фильтр"
    header = (
        f"Новый кандидат: {project.legitimacy_score:.1f}/10\n"
        f"Статус: {confidence_note}\n"
        f"Почему: {project.score_reasoning}\n\n"
    )
    text = header + draft.rendered_review_text()
    return text if len(text) <= 4096 else text[:4090] + "\n..."
