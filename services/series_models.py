"""Data models, schemas, and serializers for Content Series and X/Twitter Threads."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from db.models import ContentSeries, SeriesPost


class SeriesFormat:
    SERIES = "series"          # Telegram series of connected posts
    X_THREAD = "x_thread"      # Real sequential X/Twitter reply chain thread
    MULTI_PLATFORM = "multi_platform"  # Unified content written distinctly for TG and X


class SeriesStatus:
    DRAFT = "draft"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PostStatus:
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class VisualStrategy:
    NONE = "none"
    SINGLE_COVER = "single_cover"
    SELECTED_POSTS = "selected_posts"
    EVERY_POST = "every_post"


@dataclass
class SeriesPlanItem:
    position: int
    purpose: str = "content"  # hook, context, main_information, tasks, conclusion, cta
    target_length: str = "<= 280 chars"
    media_required: bool = False
    cta: str | None = None
    key_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeriesPlanItem:
        return cls(
            position=int(data.get("position", 1)),
            purpose=str(data.get("purpose", "content")),
            target_length=str(data.get("target_length", "<= 280 chars")),
            media_required=bool(data.get("media_required", False)),
            cta=data.get("cta"),
            key_points=list(data.get("key_points") or []),
        )


@dataclass
class SeriesPlan:
    topic: str
    format: str = SeriesFormat.X_THREAD
    target_platform: str = "x"  # telegram | x | both
    total_posts: int = 5
    visual_strategy: str = VisualStrategy.SINGLE_COVER
    desired_cta: str | None = None
    posts: list[SeriesPlanItem] = field(default_factory=list)
    source_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "format": self.format,
            "target_platform": self.target_platform,
            "total_posts": self.total_posts,
            "visual_strategy": self.visual_strategy,
            "desired_cta": self.desired_cta,
            "posts": [p.to_dict() for p in self.posts],
            "source_context": self.source_context,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeriesPlan:
        raw_posts = data.get("posts") or []
        posts = [SeriesPlanItem.from_dict(p) if isinstance(p, dict) else p for p in raw_posts]
        return cls(
            topic=str(data.get("topic", "")),
            format=str(data.get("format", SeriesFormat.X_THREAD)),
            target_platform=str(data.get("target_platform", "x")),
            total_posts=int(data.get("total_posts", len(posts) or 5)),
            visual_strategy=str(data.get("visual_strategy", VisualStrategy.SINGLE_COVER)),
            desired_cta=data.get("desired_cta"),
            posts=posts,
            source_context=dict(data.get("source_context") or {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> SeriesPlan:
        return cls.from_dict(json.loads(json_str))


@dataclass
class SeriesPostData:
    position: int
    purpose: str = "content"
    content: str = ""
    content_json: str | None = None
    x_text: str | None = None
    telegram_text: str | None = None
    media_required: bool = False
    media_type: str | None = None
    media_path: str | None = None
    status: str = PostStatus.PENDING
    telegram_message_id: int | None = None
    x_post_id: str | None = None
    reply_to_x_post_id: str | None = None
    error: str | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeriesPostData:
        return cls(
            position=int(data.get("position", 1)),
            purpose=str(data.get("purpose", "content")),
            content=str(data.get("content", "")),
            content_json=data.get("content_json"),
            x_text=data.get("x_text"),
            telegram_text=data.get("telegram_text"),
            media_required=bool(data.get("media_required", False)),
            media_type=data.get("media_type"),
            media_path=data.get("media_path"),
            status=str(data.get("status", PostStatus.PENDING)),
            telegram_message_id=data.get("telegram_message_id"),
            x_post_id=data.get("x_post_id"),
            reply_to_x_post_id=data.get("reply_to_x_post_id"),
            error=data.get("error"),
            id=data.get("id"),
        )


@dataclass
class SeriesData:
    id: int | None = None
    title: str = ""
    topic: str = ""
    format: str = SeriesFormat.X_THREAD
    status: str = SeriesStatus.DRAFT
    total_posts: int = 5
    visual_strategy: str = VisualStrategy.SINGLE_COVER
    project_id: int | None = None
    source_draft_id: int | None = None
    plan: SeriesPlan | None = None
    posts: list[SeriesPostData] = field(default_factory=list)
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "topic": self.topic,
            "format": self.format,
            "status": self.status,
            "total_posts": self.total_posts,
            "visual_strategy": self.visual_strategy,
            "project_id": self.project_id,
            "source_draft_id": self.source_draft_id,
            "plan": self.plan.to_dict() if self.plan else None,
            "posts": [p.to_dict() for p in self.posts],
            "published_at": self.published_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeriesData:
        plan_data = data.get("plan")
        plan = SeriesPlan.from_dict(plan_data) if isinstance(plan_data, dict) else None
        raw_posts = data.get("posts") or []
        posts = [SeriesPostData.from_dict(p) if isinstance(p, dict) else p for p in raw_posts]
        return cls(
            id=data.get("id"),
            title=str(data.get("title", "")),
            topic=str(data.get("topic", "")),
            format=str(data.get("format", SeriesFormat.X_THREAD)),
            status=str(data.get("status", SeriesStatus.DRAFT)),
            total_posts=int(data.get("total_posts", len(posts) or 5)),
            visual_strategy=str(data.get("visual_strategy", VisualStrategy.SINGLE_COVER)),
            project_id=data.get("project_id"),
            source_draft_id=data.get("source_draft_id"),
            plan=plan,
            posts=posts,
            published_at=data.get("published_at"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> SeriesData:
        return cls.from_dict(json.loads(json_str))

    def get_post(self, position: int) -> SeriesPostData | None:
        for p in self.posts:
            if p.position == position:
                return p
        return None

    def renumber_posts(self) -> None:
        """Ensure post positions are strictly 1, 2, ..., N in sequence."""
        for idx, post in enumerate(self.posts, start=1):
            post.position = idx
        self.total_posts = len(self.posts)

    def format_single_post_view(self, position: int) -> str:
        """Formatted text for a single post view in the Telegram navigation card."""
        post = self.get_post(position)
        if not post:
            return f"Пост #{position} не найден."

        tag = "🧵 X / TWITTER THREAD" if self.format == SeriesFormat.X_THREAD else "📚 TELEGRAM SERIES"
        status_icon = "✅" if post.status == PostStatus.PUBLISHED else ("❌" if post.status == PostStatus.FAILED else "⏳")
        purpose_label = post.purpose.upper().replace("_", " ")

        lines = [
            f"<b>{tag}</b>",
            f"<b>{self.title or self.topic}</b>",
            f"<i>Пост {post.position} из {self.total_posts} • {purpose_label} [{status_icon}]</i>",
            "",
            post.content.strip(),
        ]

        if post.media_required:
            lines.append("")
            lines.append("<i>📎 К этому посту прикрепляется медиа (обложка/карточка).</i>")

        if post.error:
            lines.append("")
            lines.append(f"⚠️ <b>Ошибка отправки:</b> {post.error}")

        if post.x_post_id:
            lines.append("")
            lines.append(f"🔗 <a href='https://x.com/i/web/status/{post.x_post_id}'>Открыть в X</a>")

        return "\n".join(lines)

    def format_full_preview(self) -> str:
        """Generate the full sequential preview of all posts in order."""
        tag = "🧵 ПОЛНЫЙ ПРЕДПРОСМОТР X-ТРЕДА" if self.format == SeriesFormat.X_THREAD else "📚 ПОЛНЫЙ ПРЕДПРОСМОТР СЕРИИ"
        divider = "\n" + "─" * 28 + "\n"

        parts = [
            f"<b>{tag}</b>",
            f"<b>Тема:</b> {self.title or self.topic}",
            f"<b>Формат:</b> {self.format.upper()} ({self.total_posts} постов)",
            divider,
        ]

        for post in sorted(self.posts, key=lambda p: p.position):
            post_header = f"<b>[{post.position}/{self.total_posts}] ({post.purpose.upper()})</b>"
            parts.append(f"{post_header}\n{post.content.strip()}")
            parts.append(divider)

        return "\n".join(parts).rstrip(divider)


def series_db_to_data(series: ContentSeries) -> SeriesData:
    """Map SQLAlchemy ContentSeries and its posts to SeriesData."""
    plan = None
    if series.plan_json:
        try:
            plan = SeriesPlan.from_json(series.plan_json)
        except Exception:
            pass

    posts_data = []
    for p in series.posts:
        posts_data.append(
            SeriesPostData(
                id=p.id,
                position=p.position,
                purpose=p.purpose,
                content=p.content,
                content_json=p.content_json,
                media_required=p.media_required,
                media_type=p.media_type,
                media_path=p.media_path,
                status=p.status,
                telegram_message_id=p.telegram_message_id,
                x_post_id=p.x_post_id,
                reply_to_x_post_id=p.reply_to_x_post_id,
                error=p.error,
            )
        )
    posts_data.sort(key=lambda x: x.position)

    return SeriesData(
        id=series.id,
        title=series.title,
        topic=series.topic,
        format=series.format,
        status=series.status,
        total_posts=series.total_posts,
        visual_strategy=series.visual_strategy,
        project_id=series.project_id,
        source_draft_id=series.source_draft_id,
        plan=plan,
        posts=posts_data,
        published_at=series.published_at.isoformat() if series.published_at else None,
    )


def sync_data_to_series_db(data: SeriesData, series: ContentSeries, session: Any = None) -> None:
    """Sync SeriesData back to SQLAlchemy ContentSeries and its posts."""
    series.title = data.title
    series.topic = data.topic
    series.format = data.format
    series.status = data.status
    series.total_posts = data.total_posts
    series.visual_strategy = data.visual_strategy
    series.plan_json = data.plan.to_json() if data.plan else None

    # Map existing posts by position safely without triggering async lazy-load
    existing_by_pos = {}
    try:
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(series)
        if "posts" in insp.dict and insp.dict["posts"]:
            for p in series.posts:
                existing_by_pos[p.position] = p
    except Exception:
        existing_by_pos = {}
    new_posts = []

    for post_data in data.posts:
        if post_data.position in existing_by_pos:
            db_p = existing_by_pos[post_data.position]
            db_p.purpose = post_data.purpose
            db_p.content = post_data.content
            db_p.content_json = getattr(post_data, "content_json", None)
            db_p.media_required = post_data.media_required
            db_p.media_type = post_data.media_type
            db_p.media_path = post_data.media_path
            db_p.status = post_data.status
            db_p.telegram_message_id = post_data.telegram_message_id
            db_p.x_post_id = post_data.x_post_id
            db_p.reply_to_x_post_id = post_data.reply_to_x_post_id
            db_p.error = post_data.error
            new_posts.append(db_p)
        else:
            db_p = SeriesPost(
                series_id=series.id,
                position=post_data.position,
                purpose=post_data.purpose,
                content=post_data.content,
                content_json=getattr(post_data, "content_json", None),
                media_required=post_data.media_required,
                media_type=post_data.media_type,
                media_path=post_data.media_path,
                status=post_data.status,
                telegram_message_id=post_data.telegram_message_id,
                x_post_id=post_data.x_post_id,
                reply_to_x_post_id=post_data.reply_to_x_post_id,
                error=post_data.error,
            )
            new_posts.append(db_p)

    series.posts = new_posts
