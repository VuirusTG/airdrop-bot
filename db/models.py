"""Database models for discovered projects, drafts, and publications."""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStatus(str, enum.Enum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    DRAFTED = "drafted"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DELETED = "deleted"
    PUBLISHED = "published"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("dedup_hash", name="uq_project_dedup_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    chain: Mapped[str] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="airdrop")
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    project_url: Mapped[str] = mapped_column(Text, nullable=True)
    raw_data: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.NEW, index=True
    )
    legitimacy_score: Mapped[float] = mapped_column(Float, nullable=True)
    score_reasoning: Mapped[str] = mapped_column(Text, nullable=True)
    filter_version: Mapped[int] = mapped_column(Integer, default=1)
    review_chat_id: Mapped[int] = mapped_column(Integer, nullable=True)
    review_message_id: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    drafts: Mapped[list["Draft"]] = relationship(back_populates="project", order_by="Draft.version")
    published_posts: Mapped[list["PublishedPost"]] = relationship(back_populates="project")

    def latest_draft(self) -> "Draft | None":
        return self.drafts[-1] if self.drafts else None


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    potential_reward: Mapped[str] = mapped_column(Text, nullable=True)
    risk_note: Mapped[str] = mapped_column(Text, nullable=True)
    twitter_text: Mapped[str] = mapped_column(Text, nullable=True)
    image_path: Mapped[str] = mapped_column(String(512), nullable=True)
    image_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    image_source: Mapped[str] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    project_url: Mapped[str] = mapped_column(Text, nullable=True)
    rework_feedback: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="drafts")

    def rendered_text(self) -> str:
        parts = [f"🚀 {self.title}", "", self.summary, "", "📝 What to do:", self.instructions]
        if self.potential_reward:
            parts += ["", f"💰 Potential reward: {self.potential_reward}"]
        if self.risk_note:
            parts += ["", f"⚠️ Risk: {self.risk_note}"]
        if self.project_url:
            parts += ["", f"🔗 Start here: {self.project_url}"]
        return "\n".join(parts)

    def rendered_review_text(self) -> str:
        parts = [
            "🔒 Источник (виден только администратору, в пост не попадет):",
            self.source_url or "Источник не указан",
            "",
            "Ссылка на проект (попадет в публичные посты):",
            self.project_url or "⚠️ Не найдена — автоматическая публикация заблокирована",
            "",
            "1. Черновик для телеграмм канала",
            "",
            self.rendered_text(),
        ]
        if self.twitter_text:
            parts += ["", "2. Черновик для твиттера", "", self.twitter_text]
        parts += ["", "----------", "", "3. Изображение"]
        if self.image_path:
            if self.image_source == "generated_social_card_cloudflare":
                label = "AI social card (Cloudflare Workers AI + локальный макет)"
            elif self.image_source == "generated_social_card":
                label = "Бесплатно сгенерированная social card"
            else:
                label = "Рекомендуемое изображение со страницы источника"
            parts.append(f"{label}: {self.image_path}")
        else:
            parts.append("Подходящее официальное изображение автоматически не найдено.")
        if self.image_prompt:
            parts += ["", "Промпт для генерации:", self.image_prompt]
        return "\n".join(parts)


class PublishedPost(Base):
    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    platform: Mapped[str] = mapped_column(String(32))
    platform_post_id: Mapped[str] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="published_posts")


class WebSettings(Base):
    """Single-tenant website preferences.

    The MVP deliberately keeps this separate from environment-owned bot settings.
    A public SaaS migration should add a user_id and one row per workspace.
    """

    __tablename__ = "web_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    filter_prompt: Mapped[str] = mapped_column(Text, default="")
    rss_feeds: Mapped[str] = mapped_column(Text, default="[]")
    social_accounts: Mapped[str] = mapped_column(Text, default="[]")
    enabled_platforms: Mapped[str] = mapped_column(Text, default='["telegram", "x"]')
    encrypted_credentials: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
