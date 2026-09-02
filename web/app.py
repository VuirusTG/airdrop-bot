from __future__ import annotations

import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import Header
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from config import settings
from db.database import get_session, init_db
from db.models import Draft, Project, ProjectStatus, WebSettings
from bot.handlers import admin_review
from ingestion.scheduler import source_scanner
from publishing.dispatcher import publish_project
from services.ai_rework import rework_draft
from services.fallback_content import fallback_generate_draft
from services.health import collect_system_health
from services.llm_draft import DraftResult


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"


webhook_bot = Bot(settings.BOT_TOKEN)
dp = Dispatcher()
dp.include_router(admin_review.router)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    source_scanner.configure(webhook_bot)
    if settings.WEBHOOK_BASE_URL:
        await webhook_bot.set_webhook(
            url=f"{settings.WEBHOOK_BASE_URL}/telegram/webhook",
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET or None,
            drop_pending_updates=False,
        )
    try:
        yield
    finally:
        # Keep Telegram's webhook registered across Render sleep/restarts.
        # Deleting it here would prevent the next Telegram update from waking a sleeping Free service.
        await webhook_bot.session.close()


app = FastAPI(title="Alpha Radar", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ReworkRequest(BaseModel):
    feedback: str = Field(min_length=3, max_length=2000)


class SettingsRequest(BaseModel):
    filter_prompt: str = Field(default="", max_length=6000)
    rss_feeds: list[str] = Field(default_factory=list, max_length=50)
    social_accounts: list[str] = Field(default_factory=list, max_length=100)
    enabled_platforms: list[str] = Field(default_factory=list)
    credentials: dict[str, str] = Field(default_factory=dict)


def _project_query():
    return select(Project).options(
        selectinload(Project.drafts), selectinload(Project.published_posts)
    )


def _draft_json(draft: Draft | None) -> dict | None:
    if not draft:
        return None
    return {
        "id": draft.id,
        "version": draft.version,
        "title": draft.title,
        "summary": draft.summary,
        "instructions": draft.instructions,
        "potential_reward": draft.potential_reward,
        "risk_note": draft.risk_note,
        "twitter_text": draft.twitter_text,
        "project_url": draft.project_url,
        "source_url": draft.source_url,
        "image_url": f"/api/posts/{draft.project_id}/image" if draft.image_path else None,
        "rework_feedback": draft.rework_feedback,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


def _project_json(project: Project) -> dict:
    draft = project.latest_draft()
    publications = [
        {
            "platform": item.platform,
            "success": item.success,
            "url": item.url,
            "error": item.error,
            "published_at": item.published_at.isoformat() if item.published_at else None,
        }
        for item in project.published_posts
    ]
    return {
        "id": project.id,
        "name": project.name,
        "chain": project.chain,
        "category": project.category,
        "source": project.source,
        "source_url": project.source_url,
        "project_url": project.project_url,
        "status": project.status.value,
        "score": project.legitimacy_score,
        "score_reasoning": project.score_reasoning,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "draft": _draft_json(draft),
        "publications": publications,
    }


async def _load_project(project_id: int) -> Project:
    async with get_session() as session:
        result = await session.execute(_project_query().where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            raise HTTPException(404, "Post not found")
        return project


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "airdrop-bot", "mode": "webhook" if settings.WEBHOOK_BASE_URL else "local"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    update: dict,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    if settings.TELEGRAM_WEBHOOK_SECRET and secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    parsed = Update.model_validate(update)
    await dp.feed_update(webhook_bot, parsed)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/dashboard")
async def dashboard():
    async with get_session() as session:
        result = await session.execute(
            _project_query()
            .where(Project.status.in_([ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED]))
            .order_by(desc(Project.updated_at))
        )
        posts = [_project_json(item) for item in result.scalars().unique().all()]
        archive_count = await session.scalar(
            select(__import__("sqlalchemy").func.count(Project.id)).where(
                Project.status.in_([ProjectStatus.DELETED, ProjectStatus.PUBLISHED])
            )
        )
    return {"posts": posts, "count": len(posts), "archive_count": archive_count or 0}


@app.get("/api/archive")
async def archive():
    async with get_session() as session:
        result = await session.execute(
            _project_query()
            .where(Project.status.in_([ProjectStatus.DELETED, ProjectStatus.PUBLISHED]))
            .order_by(desc(Project.updated_at))
        )
        posts = [_project_json(item) for item in result.scalars().unique().all()]
    return {"posts": posts, "count": len(posts)}


@app.get("/api/posts/{project_id}")
async def post_detail(project_id: int):
    return _project_json(await _load_project(project_id))


@app.get("/api/posts/{project_id}/image")
async def post_image(project_id: int):
    project = await _load_project(project_id)
    draft = project.latest_draft()
    if not draft or not draft.image_path:
        raise HTTPException(404, "Image not found")
    if draft.image_path.startswith(("http://", "https://")):
        return RedirectResponse(draft.image_path)
    path = Path(draft.image_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file() or ROOT.resolve() not in path.parents:
        raise HTTPException(404, "Image not found")
    return FileResponse(path)


@app.post("/api/posts/{project_id}/delete")
async def delete_post(project_id: int):
    async with get_session() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Post not found")
        if project.status == ProjectStatus.PUBLISHED:
            raise HTTPException(409, "Published posts cannot be deleted")
        project.status = ProjectStatus.DELETED
        await session.commit()
    return {"ok": True, "status": "deleted"}


@app.post("/api/posts/{project_id}/restore")
async def restore_post(project_id: int):
    async with get_session() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise HTTPException(404, "Post not found")
        if project.status != ProjectStatus.DELETED:
            raise HTTPException(409, "Only deleted posts can be restored")
        project.status = ProjectStatus.PENDING_REVIEW
        await session.commit()
    return {"ok": True, "status": "pending_review"}


@app.post("/api/posts/{project_id}/rework")
async def rework_post(project_id: int, payload: ReworkRequest):
    async with get_session() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.drafts)).where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if not project or not project.latest_draft():
            raise HTTPException(404, "Post or draft not found")
        old = project.latest_draft()
        previous = DraftResult(
            title=old.title,
            summary=old.summary,
            instructions=old.instructions,
            potential_reward=old.potential_reward,
            risk_note=old.risk_note,
            twitter_text=old.twitter_text,
            image_prompt=old.image_prompt,
        )
        try:
            generated, provider = await rework_draft(
                project.name, project.raw_data or "", project.chain, project.source_url,
                project.project_url, previous, payload.feedback,
            )
        except Exception:
            generated = fallback_generate_draft(
                project.name, project.raw_data or old.summary, project.chain,
                project.category, project.project_url,
            )
            provider = "local fallback"
        new_draft = Draft(
            project_id=project.id,
            version=old.version + 1,
            title=generated.title,
            summary=generated.summary,
            instructions=generated.instructions,
            potential_reward=generated.potential_reward,
            risk_note=generated.risk_note,
            twitter_text=generated.twitter_text,
            image_path=old.image_path,
            image_source=old.image_source,
            image_prompt=generated.image_prompt,
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=payload.feedback,
        )
        project.drafts.append(new_draft)
        project.status = ProjectStatus.PENDING_REVIEW
        await session.commit()
        await session.refresh(new_draft)
    return {"ok": True, "provider": provider, "draft": _draft_json(new_draft)}


@app.post("/api/posts/{project_id}/approve")
async def approve_post(project_id: int):
    bot = Bot(settings.BOT_TOKEN)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(Project).options(selectinload(Project.drafts)).where(Project.id == project_id)
            )
            project = result.scalar_one_or_none()
            if not project or not project.latest_draft():
                raise HTTPException(404, "Post or draft not found")
            if not project.latest_draft().project_url:
                raise HTTPException(409, "A verified project URL is required before publishing")
            if project.status == ProjectStatus.PUBLISHED:
                raise HTTPException(409, "Post is already published")
            project.status = ProjectStatus.APPROVED
            await session.commit()
            results = await publish_project(bot, project, project.latest_draft())
            telegram = next(item for item in results if item.platform == "telegram")
            project.status = ProjectStatus.PUBLISHED if telegram.success else ProjectStatus.PENDING_REVIEW
            await session.commit()
        return {"ok": telegram.success, "results": [item.__dict__ for item in results]}
    finally:
        await bot.session.close()


def _fernet() -> Fernet:
    secret = os.getenv("WEB_SECRET_KEY", "").strip()
    if not secret:
        raise HTTPException(503, "WEB_SECRET_KEY is required to save credentials")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


async def _settings_row(session) -> WebSettings:
    row = await session.get(WebSettings, 1)
    if not row:
        row = WebSettings(
            id=1,
            filter_prompt=(
                "Find actionable airdrops, testnets and quests. Reject price news, "
                "generic market analysis and projects with critical scam signals."
            ),
            rss_feeds=json.dumps(settings.RSS_FEEDS),
            social_accounts=json.dumps(settings.TRUSTED_X_ACCOUNTS),
        )
        session.add(row)
        await session.flush()
    return row


@app.get("/api/settings")
async def get_settings():
    async with get_session() as session:
        row = await _settings_row(session)
        encrypted = json.loads(row.encrypted_credentials or "{}")
        await session.commit()
        return {
            "filter_prompt": row.filter_prompt,
            "rss_feeds": json.loads(row.rss_feeds or "[]"),
            "social_accounts": json.loads(row.social_accounts or "[]"),
            "enabled_platforms": json.loads(row.enabled_platforms or "[]"),
            "connections": {
                name: True for name in encrypted
            } | {
                "groq": bool(settings.GROQ_API_KEY) or "groq" in encrypted,
                "gemini": bool(settings.GEMINI_API_KEY) or "gemini" in encrypted,
                "telegram": bool(settings.BOT_TOKEN) or "telegram" in encrypted,
                "x": bool(settings.X_API_KEY) or "x" in encrypted,
            },
        }


@app.put("/api/settings")
async def save_settings(payload: SettingsRequest):
    async with get_session() as session:
        row = await _settings_row(session)
        row.filter_prompt = payload.filter_prompt.strip()
        row.rss_feeds = json.dumps([item.strip() for item in payload.rss_feeds if item.strip()])
        row.social_accounts = json.dumps(
            [item.strip().lstrip("@") for item in payload.social_accounts if item.strip()]
        )
        row.enabled_platforms = json.dumps(payload.enabled_platforms)
        if payload.credentials:
            cipher = _fernet()
            encrypted = json.loads(row.encrypted_credentials or "{}")
            for name, value in payload.credentials.items():
                clean = value.strip()
                if clean:
                    encrypted[name] = cipher.encrypt(clean.encode()).decode()
            row.encrypted_credentials = json.dumps(encrypted)
        await session.commit()
    return {"ok": True}


@app.get("/api/health")
async def health(live: bool = Query(False)):
    if not live:
        items = [
            {"name": "Database", "working": True, "detail": "SQLite connected"},
            {"name": "Groq", "working": bool(settings.GROQ_API_KEY), "detail": "Configured" if settings.GROQ_API_KEY else "Local fallback enabled"},
            {"name": "Gemini", "working": bool(settings.GEMINI_API_KEY), "detail": "Configured" if settings.GEMINI_API_KEY else "Optional"},
            {"name": "Telegram", "working": bool(settings.BOT_TOKEN), "detail": "Bot token configured"},
            {"name": "X / Twitter", "working": bool(settings.X_API_KEY), "detail": "Automatic publishing" if settings.X_API_KEY else "Open in X fallback"},
        ]
        return {"items": items, "live": False}
    bot = Bot(settings.BOT_TOKEN)
    try:
        status = await collect_system_health(bot)
    finally:
        await bot.session.close()
    items = [item.__dict__ for item in status.sources]
    items += [item.__dict__ for item in (status.telegram, status.x, status.groq, status.gemini, status.cloudflare)]
    return {"items": items, "recommendations": status.recommendations, "live": True}


@app.get("/api/open-in-x/{project_id}")
async def open_in_x(project_id: int):
    project = await _load_project(project_id)
    draft = project.latest_draft()
    if not draft or not draft.twitter_text:
        raise HTTPException(404, "X draft not found")
    return RedirectResponse(f"https://twitter.com/intent/tweet?text={quote(draft.twitter_text)}")
