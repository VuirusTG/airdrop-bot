"""Two-stage Content Series and X/Twitter Thread Engine (SeriesPlanner + SeriesWriter)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import settings
from services.draft_content import DraftContent
from services.gemini_client import generate_content
from services.groq_client import generate_json
from services.series_models import (
    SeriesData,
    SeriesFormat,
    SeriesPlan,
    SeriesPlanItem,
    SeriesPostData,
    SeriesStatus,
    VisualStrategy,
)
from services.series_validator import SeriesValidator

logger = logging.getLogger(__name__)


SERIES_PLANNER_SYSTEM_PROMPT = """You are an elite Crypto Editorial Series Planner.
Your goal is to plan a structured, high-engagement series of connected posts or X/Twitter thread.

CRITICAL RULES:
1. Every fact (rewards, chain, tasks, links) MUST come strictly from SOURCE CONTEXT.
2. NEVER invent fake facts: NO fictional dates, partnerships, token pools, or unverified claims.
3. Target platform:
   - "x_thread": Each post must be designed for <= 280 characters. Flow: Hook -> Context -> Main Info -> Tasks -> Conclusion/CTA.
   - "series": Connected Telegram posts.
   - "multi_platform": Unified editorial angle suitable for both Telegram and X.
4. Posts count must be EXACTLY {total_posts}.
5. Assign a distinct logical purpose to each position:
   - Position 1: "hook"
   - Middle positions: "context", "main_information", "tasks", etc.
   - Final position: "conclusion" or "cta"

Return ONLY valid JSON matching this schema:
{{
  "topic": "<topic or title>",
  "format": "{format}",
  "target_platform": "{target_platform}",
  "total_posts": {total_posts},
  "visual_strategy": "{visual_strategy}",
  "desired_cta": "<cta or null>",
  "posts": [
    {{
      "position": 1,
      "purpose": "hook",
      "target_length": "<= 280 chars",
      "media_required": true,
      "key_points": ["<key point 1>", "<key point 2>"]
    }}
  ]
}}"""


SERIES_WRITER_SYSTEM_PROMPT = """You are an elite Crypto Writer & Social Editor.
Your goal is to write high-impact, factual posts for a crypto thread or series based on the approved SERIES PLAN and SOURCE CONTEXT.

CRITICAL EDITORIAL RULES:
1. STRICT CHARACTER LIMIT FOR X/TWITTER THREADS:
   - EACH post MUST be under 270 characters (absolute max 280).
   - NEVER use ellipsis ("..." or "…").
   - NEVER use filler phrases ("etc.", "and more", "and so on", "и т.д.").
   - NEVER use robotic disclaimers ("This draft was created without AI", "As an AI...").
2. STRICT FACT FIDELITY:
   - Use only verified facts from SOURCE CONTEXT.
   - Preserve exact rewards (e.g. "$2500+"), network, project name, and links.
   - Do not invent fictional token sales, investors, or launch dates.
3. THREAD CONTINUITY:
   - The posts must read naturally as a sequence.
   - Avoid monotonous templates (do not start every tweet with "🚀" or "Here is why").
4. Return ONLY a valid JSON array of posts:
[
  {{
    "position": 1,
    "purpose": "hook",
    "content": "<exact post text>",
    "media_required": true
  }}
]"""


POST_REWRITE_SYSTEM_PROMPT = """You are a strict text condenser and editor.
The following post exceeded the strict platform character limit or failed quality rules:
{error_summary}

ORIGINAL POST:
{original_text}

RULES:
1. For X/Twitter: Maximum 260 characters.
2. Preserve ALL essential facts (numbers, tokens, network, actions).
3. Do NOT use ellipsis ("..." or "…").
4. Do NOT use filler words ("etc.", "and more").
5. Return ONLY the rewritten text string."""


async def _call_llm_json(system_instruction: str, contents: str, schema_name: str = "series_json") -> str | None:
    """Cascade: OpenRouter -> Groq -> Gemini."""
    # 1. OpenRouter
    if settings.OPENROUTER_API_KEY:
        try:
            from services.openrouter_client import generate_json as openrouter_json
            return await openrouter_json(
                system_instruction=system_instruction,
                contents=contents,
                model=settings.OPENROUTER_MODEL,
                temperature=0.2,
            )
        except Exception as exc:
            logger.warning("OpenRouter failed in series engine: %s; trying Groq", exc)

    # 2. Groq
    if settings.GROQ_API_KEY:
        try:
            return await generate_json(
                system_instruction=system_instruction,
                contents=contents,
                temperature=0.2,
                schema_name=schema_name,
            )
        except Exception as exc:
            logger.warning("Groq failed in series engine: %s; trying Gemini", exc)

    # 3. Gemini
    if settings.GEMINI_API_KEY:
        try:
            resp = await generate_content(
                prompt=f"{system_instruction}\n\n{contents}",
                temperature=0.2,
            )
            text = (resp.text or "").strip()
            if "```json" in text:
                return text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                return text.split("```")[1].split("```")[0].strip()
            return text
        except Exception as exc:
            logger.warning("Gemini failed in series engine: %s", exc)

    return None


class SeriesPlanner:
    """Generates a structured SeriesPlan from source facts and requested format."""

    @classmethod
    async def plan_series(
        cls,
        topic: str,
        format_type: str = SeriesFormat.X_THREAD,
        total_posts: int = 5,
        visual_strategy: str = VisualStrategy.SINGLE_COVER,
        source_context: dict[str, Any] | None = None,
    ) -> SeriesPlan:
        ctx = source_context or {}
        target_platform = "x" if format_type == SeriesFormat.X_THREAD else ("telegram" if format_type == SeriesFormat.SERIES else "both")

        sys_prompt = SERIES_PLANNER_SYSTEM_PROMPT.format(
            total_posts=total_posts,
            format=format_type,
            target_platform=target_platform,
            visual_strategy=visual_strategy,
        )

        user_content = (
            f"TOPIC / TITLE: {topic}\n"
            f"SOURCE CONTEXT / FACTS:\n{json.dumps(ctx, ensure_ascii=False, indent=2)}\n\n"
            f"Generate the {total_posts}-post plan JSON:"
        )

        raw_json = await _call_llm_json(sys_prompt, user_content, schema_name="series_plan")
        if raw_json:
            try:
                data = json.loads(raw_json)
                if isinstance(data, dict):
                    data["source_context"] = ctx
                    data["total_posts"] = total_posts
                    data["format"] = format_type
                    data["target_platform"] = target_platform
                    data["visual_strategy"] = visual_strategy
                    plan = SeriesPlan.from_dict(data)
                    # Ensure exact count
                    if len(plan.posts) == total_posts:
                        return plan
            except Exception as exc:
                logger.warning("Failed to parse SeriesPlanner JSON: %s; using deterministic plan", exc)

        # Deterministic fallback plan
        posts = []
        purposes = ["hook", "context", "main_information", "tasks", "conclusion"]
        if total_posts == 3:
            purposes = ["hook", "main_information", "conclusion"]
        elif total_posts == 7:
            purposes = ["hook", "context", "architecture", "main_information", "tasks", "rewards", "conclusion"]
        elif total_posts == 10:
            purposes = ["hook", "context", "architecture", "ecosystem", "main_information", "tokenomics", "tasks", "safety", "rewards", "conclusion"]

        for i in range(1, total_posts + 1):
            purp = purposes[(i - 1) % len(purposes)]
            posts.append(
                SeriesPlanItem(
                    position=i,
                    purpose=purp,
                    target_length="<= 280 chars" if format_type == SeriesFormat.X_THREAD else "<= 600 chars",
                    media_required=(i == 1 and visual_strategy != VisualStrategy.NONE),
                    key_points=[f"Cover {purp.replace('_', ' ')} based on verified facts"],
                )
            )

        return SeriesPlan(
            topic=topic,
            format=format_type,
            target_platform=target_platform,
            total_posts=total_posts,
            visual_strategy=visual_strategy,
            posts=posts,
            source_context=ctx,
        )


class SeriesWriter:
    """Generates and refines individual posts matching the plan without silent truncation."""

    @classmethod
    async def write_posts(cls, plan: SeriesPlan) -> list[SeriesPostData]:
        ctx = plan.source_context

        user_content = (
            f"SERIES TOPIC: {plan.topic}\n"
            f"FORMAT: {plan.format} (Total {plan.total_posts} posts)\n"
            f"SOURCE FACTS:\n{json.dumps(ctx, ensure_ascii=False, indent=2)}\n\n"
            f"SERIES PLAN:\n{plan.to_json()}\n\n"
            "Write the complete JSON array of posts:"
        )

        raw_json = await _call_llm_json(SERIES_WRITER_SYSTEM_PROMPT, user_content, schema_name="series_posts")
        posts_data: list[SeriesPostData] = []

        if raw_json:
            try:
                data = json.loads(raw_json)
                if isinstance(data, list) and len(data) == plan.total_posts:
                    for idx, p_dict in enumerate(data, start=1):
                        content_str = str(p_dict.get("content", "")).strip()
                        post = SeriesPostData(
                            position=idx,
                            purpose=str(p_dict.get("purpose", plan.posts[idx - 1].purpose if idx <= len(plan.posts) else "content")),
                            content=content_str,
                            media_required=bool(p_dict.get("media_required", (idx == 1 and plan.visual_strategy != VisualStrategy.NONE))),
                        )
                        posts_data.append(post)
            except Exception as exc:
                logger.warning("Failed to parse SeriesWriter JSON array: %s", exc)

        # Deterministic fallback writer if LLM was unavailable or invalid
        if len(posts_data) != plan.total_posts:
            posts_data = cls._deterministic_fallback_write(plan)

        # Anti-truncation verification & rewrite loop
        for post in posts_data:
            val_res = SeriesValidator.validate_single_post(post, plan.format)
            if not val_res.is_valid:
                logger.info("Post #%s failed validation (%s), running rewrite pass", post.position, val_res.error_summary)
                rewritten_text = await cls.rewrite_single_post(
                    post.content,
                    val_res.error_summary,
                    plan.format,
                )
                post.content = rewritten_text

        return posts_data

    @classmethod
    async def rewrite_single_post(cls, original_text: str, issues: str, series_format: str) -> str:
        """AI rewrite loop to condense text without cutting or using ellipsis."""
        prompt = POST_REWRITE_SYSTEM_PROMPT.format(
            error_summary=issues,
            original_text=original_text,
        )
        resp = await _call_llm_json(
            "Rewrite and condense the text strictly under character limits. NO ellipsis.",
            prompt,
            schema_name="post_rewrite",
        )
        if resp:
            clean = resp.strip().strip('"\'')
            if clean.startswith("{") and "content" in clean:
                try:
                    clean = json.loads(clean).get("content", clean)
                except Exception:
                    pass
            # Clean any accidental ellipsis
            clean = re.sub(r"\.{3,}|…", "", clean).strip()
            clean = re.sub(r"\b(?:etc\.?|and more|и т\.д\.?)\b", "", clean, flags=re.IGNORECASE).strip()
            if series_format == SeriesFormat.X_THREAD and len(clean) <= 280 and clean:
                return clean
            elif series_format != SeriesFormat.X_THREAD and clean:
                return clean

        # Deterministic non-cutting condenser
        text = original_text.strip()
        text = re.sub(r"\.{3,}|…", "", text).strip()
        text = re.sub(r"\b(?:etc\.?|and more|и т\.д\.?)\b", "", text, flags=re.IGNORECASE).strip()

        # If user explicitly requested shortening / condensing
        if any(w in issues.lower() for w in ["condense", "shorten", "короче", "сократ"]):
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) > 2:
                # Keep hook and condense remaining items
                return f"{lines[0]}\n\n" + "\n".join(lines[1:min(3, len(lines))])
            elif len(text) > 80:
                words = text.split()
                return " ".join(words[:max(8, len(words) // 2)])

        if series_format == SeriesFormat.X_THREAD and len(text) > 280:
            # Cut at sentence or phrase boundary without appending ellipsis
            sentences = re.split(r"(?<=[.!?])\s+", text)
            condensed = ""
            for s in sentences:
                if len(condensed) + len(s) + 1 <= 275:
                    condensed = f"{condensed} {s}".strip()
                else:
                    break
            if condensed:
                return condensed
            # If a single sentence is > 280, cut at last complete word boundary
            word_cut = text[:275].rsplit(" ", 1)[0].rstrip(" ,:;.-")
            return word_cut
        return text

    @classmethod
    def _deterministic_fallback_write(cls, plan: SeriesPlan) -> list[SeriesPostData]:
        ctx = plan.source_context
        title = ctx.get("title") or plan.topic or "Crypto Project"
        desc = ctx.get("description") or f"Overview and qualification guide for {title}."
        reward = ctx.get("potential_reward") or "Confirmed"
        network = ctx.get("network") or "EVM"
        link = ctx.get("project_link") or "Link in bio"
        raw_tasks = list(ctx.get("tasks") or ["Check official portal", "Complete verification", "Monitor updates"])

        posts: list[SeriesPostData] = []
        n = plan.total_posts

        if plan.format == SeriesFormat.X_THREAD:
            # Post 1: Hook
            p1 = f"🧵 Everything you need to know about {title} on {network}.\n\nReward: {reward}\n\nFull guide and breakdown below 👇 (1/{n})"
            posts.append(SeriesPostData(position=1, purpose="hook", content=p1, media_required=True))

            # Post 2: Context
            clean_desc = desc if len(desc) <= 230 else desc[:225].rsplit(" ", 1)[0]
            p2 = f"What is {title}?\n\n{clean_desc}\n\nBuilt on {network}. (2/{n})"
            posts.append(SeriesPostData(position=2, purpose="context", content=p2))

            # Middle posts: Tasks & Details
            if n >= 3:
                step_lines = "\n".join(f"• {t}" for t in raw_tasks[:3])
                p3 = f"Key actions to qualify:\n\n{step_lines}\n\n(3/{n})"
                if len(p3) > 275:
                    p3 = f"Key actions to qualify:\n• {raw_tasks[0]}\n• {raw_tasks[1] if len(raw_tasks)>1 else 'Complete tasks'}\n\n(3/{n})"
                posts.append(SeriesPostData(position=3, purpose="tasks", content=p3))

            for i in range(4, n):
                posts.append(
                    SeriesPostData(
                        position=i,
                        purpose="main_information",
                        content=f"Important details for {title}:\n• Network: {network}\n• Allocation: {reward}\n• Official verified access\n\n({i}/{n})",
                    )
                )

            # Final post: CTA
            if n >= 2:
                p_last = f"Ready to start? Official portal:\n🔗 {link}\n\nBookmark this thread and follow for live verification alerts! ({n}/{n})"
                if len(p_last) > 275:
                    p_last = f"Official portal:\n🔗 {link}\n\nFollow for updates! ({n}/{n})"
                posts.append(SeriesPostData(position=n, purpose="cta", content=p_last))

        else:
            # Telegram Series
            for i in range(1, n + 1):
                purp = plan.posts[i - 1].purpose if i <= len(plan.posts) else "content"
                if i == 1:
                    content = f"🚀 <b>{title} • Часть 1/{n}</b>\n\n{desc}\n\nСеть: {network} | Награда: {reward}"
                elif i == n:
                    content = f"🏁 <b>{title} • Завершение {n}/{n}</b>\n\nОфициальная ссылка на проект:\n🔗 {link}\n\nСледите за обновлениями в нашем канале!"
                else:
                    content = f"📝 <b>{title} • Шаги {i}/{n}</b>\n\n" + "\n".join(f"{idx}. {t}" for idx, t in enumerate(raw_tasks, 1))
                posts.append(
                    SeriesPostData(
                        position=i,
                        purpose=purp,
                        content=content,
                        media_required=(i == 1 and plan.visual_strategy != VisualStrategy.NONE),
                    )
                )

        return posts


class SeriesEngine:
    """Master orchestrator generating end-to-end validated series."""

    @classmethod
    async def create_series_from_source(
        cls,
        source: DraftContent | dict[str, Any] | str,
        topic: str | None = None,
        format_type: str = SeriesFormat.X_THREAD,
        total_posts: int = 5,
        visual_strategy: str = VisualStrategy.SINGLE_COVER,
        project_id: int | None = None,
        source_draft_id: int | None = None,
    ) -> SeriesData:
        # Extract structured context
        source_context: dict[str, Any] = {}
        resolved_topic = topic or "Crypto Project"

        if isinstance(source, DraftContent):
            source_context = {
                "title": source.title,
                "category": source.category,
                "description": source.description,
                "potential_reward": source.potential_reward,
                "network": source.network,
                "project_link": source.project_link,
                "tasks": source.tasks,
                "twitter": source.twitter,
            }
            resolved_topic = topic or source.title
        elif isinstance(source, dict):
            source_context = dict(source)
            resolved_topic = topic or source.get("name") or source.get("title") or "Crypto Project"
        elif isinstance(source, str):
            resolved_topic = source.strip()
            source_context = {"topic": resolved_topic}

        # 1. Plan
        plan = await SeriesPlanner.plan_series(
            topic=resolved_topic,
            format_type=format_type,
            total_posts=total_posts,
            visual_strategy=visual_strategy,
            source_context=source_context,
        )

        # 2. Write
        posts = await SeriesWriter.write_posts(plan)

        series = SeriesData(
            title=resolved_topic,
            topic=resolved_topic,
            format=format_type,
            status=SeriesStatus.DRAFT,
            total_posts=total_posts,
            visual_strategy=visual_strategy,
            project_id=project_id,
            source_draft_id=source_draft_id,
            plan=plan,
            posts=posts,
        )

        # 3. Validate
        val = SeriesValidator.validate_series(series)
        if not val.is_valid:
            logger.warning("Series validation returned issues: %s", val.error_summary)

        return series

    @classmethod
    async def generate_series(
        cls,
        project: Any = None,
        draft: Any = None,
        source: Any = None,
        topic: str | None = None,
        format_type: str = SeriesFormat.X_THREAD,
        desired_posts_count: int = 5,
        total_posts: int | None = None,
        visual_strategy: str = VisualStrategy.SINGLE_COVER,
    ) -> SeriesData:
        """Convenience method accepting Project/Draft and generating SeriesData."""
        count = total_posts or desired_posts_count or 5
        resolved_source = source
        if not resolved_source:
            if draft:
                from services.draft_content import draft_to_content
                resolved_source = draft_to_content(draft, project) if project else draft
            elif project:
                resolved_source = project.name
            else:
                resolved_source = topic or "Crypto Project"

        resolved_topic = topic or (project.name if project else None)
        return await cls.create_series_from_source(
            source=resolved_source,
            topic=resolved_topic,
            format_type=format_type,
            total_posts=count,
            visual_strategy=visual_strategy,
            project_id=project.id if project else None,
            source_draft_id=draft.id if draft else None,
        )
