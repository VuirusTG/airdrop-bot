"""Publisher for Content Series and X/Twitter Threads.

Supports:
- Sequential Telegram posting with message ID tracking.
- Sequential X/Twitter reply chain threading (Post 1 -> Post 2 replies to 1 -> Post 3 replies to 2).
- Partial failure recovery: skips already published posts without duplicate postings.
- Concurrency and idempotency protection against double-clicks and restarts.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import settings
from db.database import get_session
from db.models import ContentSeries, PostStatus, SeriesPost, SeriesStatus
from publishing.dispatcher import _telegram_photo_caption, _telegram_post_url
from publishing.x import XPublishError, publish_to_x
from services.media import ensure_draft_image, telegram_photo

logger = logging.getLogger(__name__)

# In-memory lock set to prevent concurrent duplicate publishing executions
_active_series_locks: set[int] = set()


class SeriesPublishError(RuntimeError):
    pass


@dataclass
class SeriesPublishResult:
    """Publishing outcome object supporting both attribute and dict-like access."""
    success: bool
    series_id: int = 0
    published_count: int = 0
    total_posts: int = 0
    format: str = "x_thread"
    status: str = SeriesStatus.PUBLISHED
    first_x_post_id: str | None = None
    url: str | None = None
    error_message: str | None = None

    def __getitem__(self, item: str) -> Any:
        if item == "error":
            return self.error_message
        return getattr(self, item, None)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item) or (item == "error" and self.error_message is not None)

    def get(self, item: str, default: Any = None) -> Any:
        if item == "error":
            return self.error_message or default
        return getattr(self, item, default)


class SeriesPublisher:
    """Handles publishing and recovery for multi-post series and threads."""

    @classmethod
    async def publish_series(
        cls,
        series_id_or_bot: Any = None,
        series_id: int | None = None,
        bot: Bot | None = None,
        target_platform: str | None = None,
    ) -> SeriesPublishResult:
        """Main entry point for publishing a series to its target platform(s)."""
        actual_bot: Bot | None = None
        actual_series_id: int = 0

        if isinstance(series_id_or_bot, int):
            actual_series_id = series_id_or_bot
            actual_bot = bot
        elif isinstance(series_id, int):
            actual_series_id = series_id
            actual_bot = series_id_or_bot if not isinstance(series_id_or_bot, int) else bot
        elif hasattr(series_id_or_bot, "send_message"):
            actual_bot = series_id_or_bot
            actual_series_id = series_id or 0
        else:
            try:
                actual_series_id = int(series_id_or_bot)
            except (TypeError, ValueError):
                actual_series_id = series_id or 0

        if actual_series_id in _active_series_locks:
            return SeriesPublishResult(
                success=False,
                series_id=actual_series_id,
                error_message="Публикация этой серии уже выполняется. Подождите завершения.",
                status=SeriesStatus.PUBLISHING,
            )

        _active_series_locks.add(actual_series_id)
        try:
            async with get_session() as session:
                stmt = (
                    select(ContentSeries)
                    .options(selectinload(ContentSeries.posts))
                    .where(ContentSeries.id == actual_series_id)
                )
                res = await session.execute(stmt)
                series = res.scalar_one_or_none()
                if not series:
                    return SeriesPublishResult(
                        success=False,
                        series_id=actual_series_id,
                        error_message="Серия не найдена.",
                    )

                # DB-level concurrency guard
                if series.status == SeriesStatus.PUBLISHING:
                    return SeriesPublishResult(
                        success=False,
                        series_id=actual_series_id,
                        error_message="Публикация этой серии уже выполняется другим процессом или задачей.",
                        status=SeriesStatus.PUBLISHING,
                    )

                platform = (target_platform or series.format).lower()

                if "x" in platform or platform == "x_thread":
                    raw_res = await cls._publish_x_thread(session, series)
                else:
                    if not actual_bot:
                        return SeriesPublishResult(
                            success=False,
                            series_id=actual_series_id,
                            error_message="Bot instance не передан для публикации в Telegram.",
                        )
                    raw_res = await cls._publish_telegram_series(actual_bot, session, series)

                first_x_id = None
                for p in sorted(series.posts, key=lambda p: p.position):
                    if p.x_post_id:
                        first_x_id = p.x_post_id
                        break

                return SeriesPublishResult(
                    success=bool(raw_res.get("success", False)),
                    series_id=actual_series_id,
                    published_count=int(raw_res.get("published_count", 0)),
                    total_posts=series.total_posts,
                    format=series.format,
                    status=series.status,
                    first_x_post_id=first_x_id,
                    url=raw_res.get("url") or raw_res.get("first_url"),
                    error_message=raw_res.get("error"),
                )
        finally:
            _active_series_locks.discard(actual_series_id)

    @classmethod
    async def _publish_x_thread(cls, session: Any, series: ContentSeries) -> dict[str, Any]:
        """Publish posts sequentially as an X/Twitter reply thread."""
        logger.info("series_publish_started: series_id=%s, platform=x_thread", series.id)
        series.status = SeriesStatus.PUBLISHING
        await session.commit()

        # Sort posts by position
        sorted_posts = sorted(series.posts, key=lambda p: p.position)
        last_tweet_id: str | None = None
        published_count = 0
        failed_count = 0
        first_tweet_url: str | None = None

        for post in sorted_posts:
            # 1. Idempotency check: Skip already published posts
            if post.status == PostStatus.PUBLISHED and post.x_post_id:
                last_tweet_id = post.x_post_id
                published_count += 1
                if not first_tweet_url:
                    first_tweet_url = f"https://x.com/i/web/status/{post.x_post_id}"
                continue

            # 2. Strict character limit validation (no silent truncation)
            post_text = post.content.strip()
            if len(post_text) > 280:
                failed_count += 1
                post.status = PostStatus.FAILED
                post.error = f"Пост #{post.position} превышает лимит X ({len(post_text)} > 280 символов)"
                series.status = SeriesStatus.PARTIALLY_PUBLISHED
                await session.commit()
                return {
                    "success": False,
                    "series_id": series.id,
                    "status": SeriesStatus.PARTIALLY_PUBLISHED,
                    "published_count": published_count,
                    "failed_at_position": post.position,
                    "error": f"Пост #{post.position} превышает лимит X ({len(post_text)} > 280 символов)",
                    "first_url": first_tweet_url,
                }

            # 3. Publish post in thread
            try:
                img_path = post.media_path if (post.position == 1 and post.media_required) else None
                post_id, url = await publish_to_x(
                    text=post_text,
                    image_path=img_path,
                    reply_to_tweet_id=last_tweet_id,
                )

                post.x_post_id = post_id
                post.reply_to_x_post_id = last_tweet_id
                post.status = PostStatus.PUBLISHED
                post.error = None
                last_tweet_id = post_id
                published_count += 1

                if not first_tweet_url:
                    first_tweet_url = url

                await session.commit()
                logger.info(
                    "series_post_published: series_id=%s, pos=%s, tweet_id=%s, in_reply_to=%s",
                    series.id,
                    post.position,
                    post_id,
                    post.reply_to_x_post_id,
                )

                # Small polite rate-limit gap between tweets in thread
                await asyncio.sleep(1.0)

            except Exception as exc:
                failed_count += 1
                post.status = PostStatus.FAILED
                post.error = str(exc)
                series.status = SeriesStatus.PARTIALLY_PUBLISHED
                await session.commit()
                logger.warning(
                    "series_publish_failed: series_id=%s, pos=%s, error=%s",
                    series.id,
                    post.position,
                    exc,
                )
                return {
                    "success": False,
                    "series_id": series.id,
                    "status": SeriesStatus.PARTIALLY_PUBLISHED,
                    "published_count": published_count,
                    "failed_at_position": post.position,
                    "error": f"Ошибка на посте #{post.position}: {exc}",
                    "first_url": first_tweet_url,
                }

        # All posts succeeded
        series.status = SeriesStatus.PUBLISHED
        series.published_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("series_publish_completed: series_id=%s, total=%s", series.id, published_count)

        return {
            "success": True,
            "series_id": series.id,
            "status": SeriesStatus.PUBLISHED,
            "published_count": published_count,
            "url": first_tweet_url,
        }

    @classmethod
    async def _publish_telegram_series(
        cls,
        bot: Bot,
        session: Any,
        series: ContentSeries,
    ) -> dict[str, Any]:
        """Publish posts sequentially to Telegram channel."""
        logger.info("series_publish_started: series_id=%s, platform=telegram", series.id)
        series.status = SeriesStatus.PUBLISHING
        await session.commit()

        sorted_posts = sorted(series.posts, key=lambda p: p.position)
        published_count = 0
        first_post_url: str | None = None

        for post in sorted_posts:
            # 1. Idempotency check: Skip already published posts
            if post.status == PostStatus.PUBLISHED and post.telegram_message_id:
                published_count += 1
                if not first_post_url:
                    first_post_url = _telegram_post_url(post.telegram_message_id)
                continue

            # 2. Strict character limit validation (Telegram 4096)
            post_text = post.content.strip()
            if len(post_text) > 4096:
                post.status = PostStatus.FAILED
                post.error = f"Пост #{post.position} превышает лимит Telegram ({len(post_text)} > 4096 символов)"
                series.status = SeriesStatus.PARTIALLY_PUBLISHED
                await session.commit()
                return {
                    "success": False,
                    "series_id": series.id,
                    "status": SeriesStatus.PARTIALLY_PUBLISHED,
                    "published_count": published_count,
                    "failed_at_position": post.position,
                    "error": f"Пост #{post.position} превышает лимит Telegram ({len(post_text)} > 4096 символов)",
                    "first_url": first_post_url,
                }

            # 3. Publish post
            try:
                sent_msg = None
                img_path = post.media_path if (post.position == 1 and post.media_required) else None

                if img_path:
                    photo_input = telegram_photo(img_path)
                    if photo_input:
                        try:
                            sent_msg = await bot.send_photo(
                                chat_id=settings.PUBLISH_CHANNEL_ID,
                                photo=photo_input,
                                caption=post.content.strip()[:1024],
                                parse_mode="HTML",
                            )
                        except Exception as p_err:
                            logger.warning("Telegram send_photo failed for series post #%s: %s", post.position, p_err)

                if not sent_msg:
                    sent_msg = await bot.send_message(
                        chat_id=settings.PUBLISH_CHANNEL_ID,
                        text=post.content.strip(),
                        parse_mode="HTML",
                    )

                post.telegram_message_id = sent_msg.message_id
                post.status = PostStatus.PUBLISHED
                post.error = None
                published_count += 1

                if not first_post_url:
                    first_post_url = _telegram_post_url(sent_msg.message_id)

                await session.commit()
                logger.info(
                    "series_post_published: series_id=%s, pos=%s, tg_msg_id=%s",
                    series.id,
                    post.position,
                    sent_msg.message_id,
                )

                # Small delay to keep chronological order clean in channel
                await asyncio.sleep(1.2)

            except Exception as exc:
                post.status = PostStatus.FAILED
                post.error = str(exc)
                series.status = SeriesStatus.PARTIALLY_PUBLISHED
                await session.commit()
                logger.warning(
                    "series_publish_failed: series_id=%s, pos=%s, error=%s",
                    series.id,
                    post.position,
                    exc,
                )
                return {
                    "success": False,
                    "series_id": series.id,
                    "status": SeriesStatus.PARTIALLY_PUBLISHED,
                    "published_count": published_count,
                    "failed_at_position": post.position,
                    "error": f"Ошибка на посте #{post.position}: {exc}",
                    "first_url": first_post_url,
                }

        # All posts succeeded
        series.status = SeriesStatus.PUBLISHED
        series.published_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("series_publish_completed: series_id=%s, total=%s", series.id, published_count)

        return {
            "success": True,
            "series_id": series.id,
            "status": SeriesStatus.PUBLISHED,
            "published_count": published_count,
            "url": first_post_url,
        }
