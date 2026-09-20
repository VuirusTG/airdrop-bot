import logging
from dataclasses import dataclass

from aiogram import Bot

from config import settings
from db.database import get_session
from db.models import Draft, Project, PublishedPost
from publishing.instagram import InstagramNotReady, publish_to_instagram
from publishing.x import XPublishError, publish_to_x
from services.media import ensure_draft_image, telegram_photo

logger = logging.getLogger(__name__)


TELEGRAM_CAPTION_LIMIT = 1024


@dataclass
class PublishResult:
    platform: str
    success: bool
    url: str | None = None
    platform_post_id: str | None = None
    error: str | None = None


def _telegram_post_url(message_id: int) -> str | None:
    channel = settings.PUBLISH_CHANNEL_ID.strip()
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    return None


def _telegram_photo_caption(draft: Draft) -> str:
    """Keep a photo post inside Telegram's 1024-character caption limit without artificial ellipsis."""
    full_text = draft.rendered_text()
    if len(full_text) <= TELEGRAM_CAPTION_LIMIT:
        return full_text
    # Safely fit within 1024 characters without appending dots
    return full_text[:TELEGRAM_CAPTION_LIMIT].rstrip()


async def _publish_telegram(bot: Bot, project: Project, draft: Draft) -> PublishResult:
    text = draft.rendered_text()
    try:
        # Ensure image exists or regenerate it if missing from ephemeral container storage
        if draft.image_path or settings.ENABLE_SOCIAL_CARD_GENERATION:
            try:
                await ensure_draft_image(project, draft)
            except Exception as exc:
                logger.warning("Could not ensure draft image for %s: %s", project.name, exc)

        photo_input = telegram_photo(draft.image_path) if draft.image_path else None

        if photo_input is not None:
            try:
                message = await bot.send_photo(
                    chat_id=settings.PUBLISH_CHANNEL_ID,
                    photo=photo_input,
                    caption=_telegram_photo_caption(draft),
                )
            except Exception as exc:
                logger.warning("Telegram send_photo failed for %s (%s). Falling back to text post. Error: %s", project.name, draft.image_path, exc)
                try:
                    message = await bot.send_message(
                        chat_id=settings.PUBLISH_CHANNEL_ID,
                        text=text,
                    )
                except Exception as text_exc:
                    return PublishResult(
                        platform="telegram",
                        success=False,
                        error=f"Ошибка публикации в Telegram: {exc} | fallback: {text_exc}",
                    )
        else:
            # Clean formatted text post if no photo is available or resolvable
            message = await bot.send_message(
                chat_id=settings.PUBLISH_CHANNEL_ID,
                text=text,
            )

        return PublishResult(
            platform="telegram",
            success=True,
            url=_telegram_post_url(message.message_id),
            platform_post_id=str(message.message_id),
        )
    except Exception as exc:
        return PublishResult(platform="telegram", success=False, error=str(exc))


async def _publish_x(draft: Draft) -> PublishResult:
    if not draft.twitter_text:
        return PublishResult(
            platform="x",
            success=False,
            error="Текст для X/Twitter не был сгенерирован.",
        )
    try:
        post_id, url = await publish_to_x(draft.twitter_text, draft.image_path)
        return PublishResult(
            platform="x",
            success=True,
            url=url,
            platform_post_id=post_id,
        )
    except XPublishError as exc:
        return PublishResult(platform="x", success=False, error=str(exc))
    except Exception as exc:
        return PublishResult(platform="x", success=False, error=f"Неожиданная ошибка X: {exc}")


async def _publish_instagram(draft: Draft) -> PublishResult:
    try:
        result = await publish_to_instagram(caption=draft.rendered_text(), image_url=draft.image_path)
        return PublishResult(
            platform="instagram", success=True, platform_post_id=result.get("id")
        )
    except InstagramNotReady as exc:
        return PublishResult(platform="instagram", success=False, error=str(exc))
    except Exception as exc:
        return PublishResult(platform="instagram", success=False, error=str(exc))


async def publish_project(bot: Bot, project: Project, draft: Draft) -> list[PublishResult]:
    results = [await _publish_telegram(bot, project, draft), await _publish_x(draft)]
    if settings.INSTAGRAM_ACCESS_TOKEN and settings.INSTAGRAM_BUSINESS_ACCOUNT_ID:
        results.append(await _publish_instagram(draft))

    async with get_session() as session:
        for result in results:
            session.add(
                PublishedPost(
                    project_id=project.id,
                    draft_id=draft.id,
                    platform=result.platform,
                    platform_post_id=result.platform_post_id,
                    url=result.url,
                    success=result.success,
                    error=result.error,
                )
            )
        await session.commit()
    return results
