"""Series and Thread Editor integrating with Editor V2 (scopes: SERIES, POST, FIELD)."""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from config import settings
from services.editor_service import EditPlan
from services.gemini_client import generate_content
from services.groq_client import generate_json
from services.series_engine import SeriesWriter
from services.series_models import (
    SeriesData,
    SeriesFormat,
    SeriesPostData,
)
from services.series_validator import SeriesValidator

logger = logging.getLogger(__name__)

ORDINAL_NUMBERS_MAP = {
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четверт": 4,
    "пят": 5,
    "шест": 6,
    "седьм": 7,
    "восьм": 8,
    "девят": 9,
    "десят": 10,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}

SERIES_INTENT_PARSER_SYSTEM_PROMPT = """You are an ultra-precise Intent Parser for editing a Multi-Post Content Series or X/Twitter Thread.
Your job is to read the CURRENT SERIES STATE and USER COMMAND, and output a strict JSON EditPlan.

SCOPES:
- "post": Modifying, shortening, replacing, regenerating, removing, or inserting a single post by position.
- "series": Modifying the entire series or multiple posts at once.
- "field": Modifying series title or topic metadata.

OPERATIONS:
- "shorten", "replace", "rewrite", "regenerate", "remove", "add"

CRITICAL RULES:
1. If the user targets a specific post (e.g. "Сделай третий пост короче", "Замени второй пост на: ...", "Удали 4 пост"), set:
   - "scope": "post"
   - "position": <1-indexed integer>
2. If the user targets the whole series (e.g. "Сделай весь тред более информативным"), set:
   - "scope": "series"
   - "position": null
3. If user says "этот пост" or "текущий твит", use current_position if provided.

Respond ONLY with valid JSON:
{
  "scope": "post" | "series" | "field",
  "position": <integer or null>,
  "target": "content" | "post" | "title",
  "operation": "shorten" | "replace" | "rewrite" | "regenerate" | "remove" | "add",
  "new_value": <string or null>,
  "confidence": <float 0.0-1.0>,
  "explanation": "<short Russian explanation>"
}"""


def _fast_deterministic_series_parse(
    command: str,
    series: SeriesData,
    current_position: int | None = None,
) -> EditPlan | None:
    cmd = command.strip().lower()

    # 1. Resolve target position if mentioned in command
    target_pos = None

    # Check "последний"
    if "последн" in cmd or "last" in cmd:
        target_pos = series.total_posts

    # Check "этот" / "текущий"
    elif ("этот" in cmd or "текущ" in cmd or "this" in cmd) and current_position:
        target_pos = current_position

    # Check word ordinals (третий, второй, etc.)
    if not target_pos:
        for prefix, num in ORDINAL_NUMBERS_MAP.items():
            if re.search(rf"\b{prefix}\w*\b", cmd):
                target_pos = num
                break

    # Check digits ("пост 3", "пост #3", "3 пост", "твит 2")
    if not target_pos:
        m_num = re.search(r"(?:пост\w*|твит\w*|шаг\w*|post|tweet)\s*#?\s*(\d+)", cmd)
        if m_num:
            target_pos = int(m_num.group(1))
        else:
            m_num2 = re.search(r"(\d+)\s*(?:-?[ыий]й)?\s*(?:пост\w*|твит\w*|post|tweet)", cmd)
            if m_num2:
                target_pos = int(m_num2.group(1))

    # --- OPERATION MATCHING ---

    # A) Remove / Delete single post: "удали третий пост", "удалить последний пост"
    if any(w in cmd for w in ["удали", "удалить", "стереть", "delete", "remove"]):
        if target_pos:
            return EditPlan(
                scope="post",
                position=target_pos,
                target="post",
                operation="remove",
                new_value=None,
                confidence=0.98,
                explanation=f"Удаление поста #{target_pos}",
            )

    # B) Shorten single post: "сделай третий пост короче", "сократи 2 пост"
    if any(w in cmd for w in ["короче", "сократи", "кратко", "лаконичн", "shorten"]):
        if target_pos:
            return EditPlan(
                scope="post",
                position=target_pos,
                target="content",
                operation="shorten",
                new_value="shorten",
                confidence=0.98,
                explanation=f"Сокращение поста #{target_pos}",
            )

    # C) Replace single post: "замени второй пост на: ...", "поменяй 3 пост на: ..."
    m_replace = re.search(r"(?:замени|поменяй|измени|replace)\s+.*?\s+(?:на|to)\s*[:\s]*(.+)", command, re.IGNORECASE)
    if m_replace and target_pos:
        new_text = m_replace.group(1).strip()
        return EditPlan(
            scope="post",
            position=target_pos,
            target="content",
            operation="replace",
            new_value=new_text,
            confidence=0.97,
            explanation=f"Замена текста поста #{target_pos}",
        )

    # D) Add / Insert post:
    # Handles:
    # - "добавь новый пост перед третьим", "вставь пост перед 3"
    # - "добавь пост о распределении токенов"
    # - "добавь 4 пост: ..."
    if any(w in cmd for w in ["добавь", "вставь", "add", "insert"]) and any(w in cmd for w in ["пост", "твит", "post", "tweet"]):
        m_before = re.search(r"(?:перед|before)\s*#?\s*(\d+|[а-яa-z]+)", cmd)
        ins_pos = None
        if m_before:
            pos_raw = m_before.group(1)
            if pos_raw.isdigit():
                ins_pos = int(pos_raw)
            else:
                for pfx, num in ORDINAL_NUMBERS_MAP.items():
                    if pfx in pos_raw:
                        ins_pos = num
                        break
        elif target_pos:
            ins_pos = target_pos
        else:
            # Default to inserting before the last post (CTA/conclusion) or at the end
            ins_pos = max(1, series.total_posts)

        # Extract content
        content_part = re.sub(
            r"^.*?(?:добавь|вставь|add|insert)\s+(?:новый\s+)?(?:пост\w*|твит\w*)\s*(?:#?\d+)?\s*(?:перед\s+\S+\s*)?[:\s]*(?:о|про|на тему|about)?\s*",
            "",
            command,
            flags=re.IGNORECASE,
        ).strip()
        if not content_part:
            content_part = command.strip()

        return EditPlan(
            scope="post",
            position=ins_pos,
            target="post",
            operation="add",
            new_value=content_part,
            confidence=0.96,
            explanation=f"Добавление поста на позицию #{ins_pos}: {content_part[:30]}",
        )

    # E) Regenerate single post: "перегенерируй второй пост", "обнови 3 пост"
    if any(w in cmd for w in ["перегенерир", "регенерир", "обнови", "regenerate"]):
        if target_pos:
            return EditPlan(
                scope="post",
                position=target_pos,
                target="post",
                operation="regenerate",
                new_value=None,
                confidence=0.98,
                explanation=f"Перегенерация поста #{target_pos}",
            )

    # F) Specific post rewrite / addition: "добавь в четвертый пост информацию про задания"
    if target_pos:
        return EditPlan(
            scope="post",
            position=target_pos,
            target="content",
            operation="rewrite",
            new_value=command,
            confidence=0.92,
            explanation=f"Правка поста #{target_pos}: {command[:50]}",
        )

    # G) Title edit: "поменяй заголовок серии на: ..."
    m_title = re.search(r"(?:заголовок|название|title|topic).*?\s+(?:на|to)\s*[:\s]*(.+)", command, re.IGNORECASE)
    if m_title and any(w in cmd for w in ["сери", "тред", "thread", "series"]):
        return EditPlan(
            scope="field",
            target="title",
            operation="replace",
            new_value=m_title.group(1).strip().strip('"\''),
            confidence=0.98,
            explanation=f"Изменение заголовка серии",
        )

    # H) Whole series rewrite: "сделай весь тред более информативным"
    if any(w in cmd for w in ["весь тред", "всю серию", "полностью", "entire", "whole thread"]):
        return EditPlan(
            scope="series",
            position=None,
            target="series",
            operation="rewrite",
            new_value=command,
            confidence=0.95,
            explanation="Переработка всей серии",
        )

    return None


class SeriesEditor:
    """Editor V2 engine for multi-post series and X threads."""

    @classmethod
    async def create_series_edit_plan(
        cls,
        command: str,
        series: SeriesData,
        current_position: int | None = None,
    ) -> EditPlan:
        """Parse user command using LLM first with deterministic fallback."""
        # Fast deterministic parse
        deterministic_plan = _fast_deterministic_series_parse(command, series, current_position)
        if deterministic_plan:
            return deterministic_plan

        # LLM Intent Parser
        if settings.OPENROUTER_API_KEY or settings.GROQ_API_KEY or settings.GEMINI_API_KEY:
            user_context = (
                f"CURRENT SERIES STATE:\n"
                f"Title: {series.title}\n"
                f"Format: {series.format} ({series.total_posts} posts)\n"
                f"Current Viewed Position: {current_position or 'None'}\n"
                f"Posts Summary:\n"
                + "\n".join(f"[{p.position}] ({p.purpose}): {p.content[:80]}..." for p in series.posts)
                + f"\n\nUSER COMMAND: {command}\n\n"
                "Return the JSON EditPlan:"
            )

            raw = None
            if settings.OPENROUTER_API_KEY:
                try:
                    from services.openrouter_client import generate_json as openrouter_json
                    raw = await openrouter_json(
                        system_instruction=SERIES_INTENT_PARSER_SYSTEM_PROMPT,
                        contents=user_context,
                        model=settings.OPENROUTER_MODEL,
                        temperature=0.1,
                    )
                except Exception:
                    pass

            if not raw and settings.GROQ_API_KEY:
                try:
                    raw = await generate_json(
                        system_instruction=SERIES_INTENT_PARSER_SYSTEM_PROMPT,
                        contents=user_context,
                        temperature=0.1,
                        schema_name="series_edit_plan",
                    )
                except Exception:
                    pass

            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return EditPlan.from_dict(data)
                except Exception:
                    pass

        # Fallback to series-wide rewrite
        return EditPlan(
            scope="series",
            position=None,
            target="series",
            operation="rewrite",
            new_value=command,
            confidence=0.7,
            explanation=f"Переработка серии: {command[:60]}",
        )

    @classmethod
    async def apply_series_edit_plan(
        cls,
        plan: EditPlan,
        series: SeriesData,
    ) -> tuple[SeriesData, bool, str | None]:
        """Apply an EditPlan to SeriesData strictly adhering to scope boundaries."""
        updated = copy.deepcopy(series)
        scope = (plan.scope or "post").lower()
        op = plan.operation.lower()
        pos = plan.position

        # --- SCOPE: FIELD (Title / Topic) ---
        if scope == "field" or plan.target == "title":
            if plan.new_value:
                updated.title = str(plan.new_value).strip()
            return updated, True, None

        # --- SCOPE: POST (Single post edit) ---
        if scope == "post":
            if not pos or pos < 1 or (pos > len(updated.posts) and op != "add"):
                return series, False, f"Указанный пост #{pos} не существует в серии из {len(updated.posts)} постов."

            # A) Shorten single post
            if op == "shorten":
                post = updated.get_post(pos)
                if post:
                    shortened = await SeriesWriter.rewrite_single_post(
                        post.content,
                        "Condense text to be significantly shorter while keeping key actions and facts.",
                        updated.format,
                    )
                    post.content = shortened
                return updated, True, None

            # B) Replace single post content
            elif op == "replace":
                post = updated.get_post(pos)
                if post:
                    new_val_str = str(plan.new_value or "").strip()
                    val_res = SeriesValidator.validate_single_post(
                        SeriesPostData(position=pos, content=new_val_str),
                        updated.format,
                    )
                    if not val_res.is_valid:
                        new_val_str = await SeriesWriter.rewrite_single_post(
                            new_val_str,
                            val_res.error_summary,
                            updated.format,
                        )
                    post.content = new_val_str
                return updated, True, None

            # C) Rewrite single post with instruction
            elif op == "rewrite":
                post = updated.get_post(pos)
                if post:
                    prompt = f"Original post: {post.content}\nInstruction: {plan.new_value}"
                    rewritten = await SeriesWriter.rewrite_single_post(
                        prompt,
                        "Rewrite post based on user instruction while strictly respecting length limits.",
                        updated.format,
                    )
                    post.content = rewritten
                return updated, True, None

            # D) Regenerate single post
            elif op == "regenerate":
                post = updated.get_post(pos)
                if post:
                    src_ctx = updated.plan.source_context if updated.plan else {}
                    prompt = (
                        f"Regenerate post #{pos} for {updated.title} with purpose '{post.purpose}'.\n"
                        f"Verified Facts: {json.dumps(src_ctx, ensure_ascii=False)}\n"
                        f"Previous content was: {post.content}"
                    )
                    rewritten = await SeriesWriter.rewrite_single_post(
                        prompt,
                        "Write fresh compelling content strictly under character limits without ellipsis.",
                        updated.format,
                    )
                    post.content = rewritten
                return updated, True, None

            # E) Remove post
            elif op in ("remove", "delete"):
                if len(updated.posts) <= 1:
                    return series, False, "Нельзя удалить единственный оставшийся пост в серии."
                updated.posts = [p for p in updated.posts if p.position != pos]
                updated.renumber_posts()
                return updated, True, None

            # F) Add / Insert post
            elif op in ("add", "insert"):
                new_post = SeriesPostData(
                    position=pos,
                    purpose="content",
                    content=str(plan.new_value or "Новый пункт").strip(),
                )
                insert_idx = min(max(pos - 1, 0), len(updated.posts))
                updated.posts.insert(insert_idx, new_post)
                updated.renumber_posts()
                return updated, True, None

        # --- SCOPE: SERIES (Entire series rework) ---
        elif scope == "series":
            # Rewrite all posts adhering to overall instruction
            if plan.new_value:
                for p in updated.posts:
                    prompt = f"Original post #{p.position} ({p.purpose}): {p.content}\nSeries instruction: {plan.new_value}"
                    p.content = await SeriesWriter.rewrite_single_post(
                        prompt,
                        "Elevate post quality while strictly observing length limits without ellipsis.",
                        updated.format,
                    )
            return updated, True, None

        return updated, True, None
