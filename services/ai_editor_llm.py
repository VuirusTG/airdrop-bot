"""AI Conversational Draft Editor & Generator.

Uses OpenRouter as the primary intelligent LLM with cascade fallback to Groq and Gemini.
Allows users to edit drafts using natural human language without rigid regex templates.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from config import settings
from services.draft_content import DraftContent
from services.task_validator import sanitize_task, validate_tasks

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_INITIAL_DRAFT = """You are a top-tier crypto researcher and social media director.
Analyze the raw crypto opportunity signal and generate a publication-ready editorial draft for Telegram and Twitter.

TELEGRAM REQUIREMENTS (Russian):
- Title: Clear, scroll-stopping title with project name, e.g. "🔥 Lighter DEX: $30M LIT Airdrop & Testnet".
- Description: 2-3 engaging, concise sentences in natural, crisp Russian explaining what the project is, why it matters, and the value proposition. Never use robotic boilerplate like "This draft was created without AI".
- Tasks: 3-5 concrete actionable numbered steps in Russian (each step <= 120 chars, starting with an action verb, no ellipsis, no filler).
- Potential Reward: Clear realistic reward statement, e.g. "$1000+", "11M $LIT Pool", "Points & TGE Token Allocation".
- Network: Chain name (e.g. "Arbitrum", "Base", "Solana", "Ethereum", "EVM").

TWITTER / X REQUIREMENTS (English):
- Single ready-to-post tweet, STRICTLY <= 280 characters.
- Scroll-stopping hook on line 1.
- Brief context + 1 compact action.
- Include verified project URL exactly once.
- End with short conversational call to action ("Worth testing?", "Farming this?") + 1-2 hashtags (#airdrop, #testnet).

IMAGE METADATA:
- theme_color: "lime" (default), "cyan", "violet", "gold", "red", or "orange".
- image_prompt: 16:9 English prompt for background atmosphere if generated.

Respond ONLY with valid JSON:
{
  "title": "<engaging title>",
  "category": "AIRDROP" | "TESTNET" | "QUEST" | "POINTS",
  "description": "<2-3 concise Russian sentences>",
  "tasks": ["<step 1>", "<step 2>", "<step 3>"],
  "potential_reward": "<e.g. $1000+ or null>",
  "network": "<network name or null>",
  "twitter_text": "<ready tweet <= 280 chars>",
  "theme_color": "lime" | "cyan" | "violet" | "gold" | "red" | "orange",
  "image_prompt": "<English visual background prompt>"
}"""

SYSTEM_PROMPT_CONVERSATIONAL_EDITOR = """You are an intelligent Conversational Crypto Draft Editor.
Your job is to interpret the user's natural language instruction and update the current draft accordingly.

You are NOT a rigid template parser. You understand nuance, intent, and context:

EXAMPLES OF USER INTENT:
1. "измени Potential rewards на фото на $1000+" or "поменяй награду на $500":
   - Update `potential_reward` to "$1000+".
   - Keep tasks and description intact.
   - Set `image_operation` to "rerender_text" (this rerenders the HUD text on the social card without generating new art).
   - `explanation`: "Изменена награда на $1000+ на карточке и в посте"

2. "сделай текст поста лаконичным и завлекающим" or "сделай короче и понятнее":
   - Rewrite `description` to be punchy, engaging, and concise (2-3 sentences).
   - Refine `tasks` to be sharp and actionable.
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Текст поста переписан лаконично и завлекающе"

3. "перепиши твит" or "сделай твит бодрее":
   - Rewrite `twitter_text` (strictly <= 280 chars, strong hook, includes project link).
   - Set `image_operation` to "none".
   - `explanation`: "Обновлен текст для Twitter"

4. "удали 2 пункт и добавь: Сделай депозит от 20 USDC":
   - Modify the `tasks` array accordingly.
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Обновлены шаги действий"

5. "смени тему на синюю" or "сделай фиолетовый фон":
   - Set `theme_color` to "cyan" / "violet" / "lime" / "gold" / "red" / "orange".
   - Set `image_operation` to "local_edit".
   - `explanation`: "Сменен цвет темы карточки"

6. "сделай фон в стиле киберпанк" or "поменяй картинку/арт":
   - Set `image_operation` to "new_artwork".
   - Set `art_prompt` to English prompt for background art.
   - `explanation`: "Запрошена генерация нового фона карточки"

RULES:
- Maintain factual integrity: do not invent false URLs or seed phrase requests.
- Tasks: max 5 steps, <= 120 chars each, no ellipsis ("..."), clear actionable verbs.
- Twitter: <= 280 characters.
- Telegram text in natural Russian, Twitter in natural English.

Respond ONLY with valid JSON:
{
  "title": "<current or updated title>",
  "category": "<current or updated category>",
  "description": "<current or updated description>",
  "tasks": ["<step 1>", "<step 2>", ...],
  "potential_reward": "<updated or current reward>",
  "network": "<updated or current network>",
  "twitter_text": "<updated or current twitter text>",
  "theme_color": "<lime|cyan|violet|gold|red|orange>",
  "image_operation": "rerender_text" | "local_edit" | "new_artwork" | "none",
  "art_prompt": "<string or null>",
  "explanation": "<concise explanation in Russian of what was changed>"
}"""


async def _call_llm_json(system_instruction: str, user_content: str) -> tuple[dict[str, Any] | None, str]:
    """Execute LLM call across OpenRouter -> Groq -> Gemini cascade."""
    # 1. OpenRouter (primary if configured)
    if settings.OPENROUTER_API_KEY:
        try:
            from services.openrouter_client import generate_json as openrouter_generate_json

            resp_str = await openrouter_generate_json(
                system_instruction=system_instruction,
                contents=user_content,
                model=settings.OPENROUTER_MODEL,
                temperature=0.2,
            )
            data = json.loads(resp_str)
            if isinstance(data, dict):
                return data, f"OpenRouter ({settings.OPENROUTER_MODEL})"
        except Exception as exc:
            logger.warning("OpenRouter failed: %s; falling back to Groq", exc)

    # 2. Groq (secondary)
    if settings.GROQ_API_KEY:
        try:
            from services.groq_client import generate_json as groq_generate_json

            resp_str = await groq_generate_json(
                system_instruction=system_instruction,
                contents=user_content,
                temperature=0.2,
            )
            data = json.loads(resp_str)
            if isinstance(data, dict):
                return data, "Groq"
        except Exception as exc:
            logger.warning("Groq failed: %s; falling back to Gemini", exc)

    # 3. Gemini (tertiary)
    if settings.GEMINI_API_KEY:
        try:
            from services.gemini_client import generate_content

            resp = await generate_content(
                prompt=f"{system_instruction}\n\n{user_content}",
                temperature=0.2,
            )
            text = (resp.text or "").strip()
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0].strip()
            data = json.loads(text)
            if isinstance(data, dict):
                return data, "Gemini"
        except Exception as exc:
            logger.warning("Gemini failed: %s", exc)

    return None, "none"


async def ai_edit_draft(
    current: DraftContent,
    instruction: str,
    project_context: str | None = None,
) -> tuple[DraftContent, dict[str, Any]]:
    """Conversational AI draft editor: modifies DraftContent based on human feedback."""
    updated = copy.deepcopy(current)

    current_tasks_str = "\n".join(f"{i}. {t}" for i, t in enumerate(current.tasks, 1))
    user_content = (
        f"CURRENT DRAFT STATE:\n"
        f"Title: {current.title}\n"
        f"Category: {current.category}\n"
        f"Description: {current.description}\n"
        f"Tasks:\n{current_tasks_str or 'None'}\n"
        f"Potential Reward: {current.potential_reward or 'None'}\n"
        f"Network: {current.network or 'None'}\n"
        f"Twitter Draft: {current.twitter_text or 'None'}\n"
        f"Social Card Theme Color: {current.artwork.theme_color}\n"
        f"Project Link: {current.project_link or 'None'}\n"
    )
    if project_context:
        user_content += f"\nPROJECT BACKGROUND CONTEXT:\n{project_context[:1500]}\n"

    user_content += f"\nUSER INSTRUCTION / FEEDBACK:\n{instruction}\n\nApply the changes and output the updated JSON."

    data, provider = await _call_llm_json(SYSTEM_PROMPT_CONVERSATIONAL_EDITOR, user_content)

    meta: dict[str, Any] = {
        "provider": provider,
        "image_operation": "rerender_text",
        "explanation": f"Изменения по запросу: {instruction[:50]}",
        "art_prompt": None,
    }

    if isinstance(data, dict):
        if data.get("title"):
            updated.title = str(data["title"]).strip()
        if data.get("category"):
            updated.category = str(data["category"]).strip().upper()
        if data.get("description"):
            desc = str(data["description"]).strip()
            desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", desc, flags=re.IGNORECASE).strip()
            updated.description = desc
        if "potential_reward" in data:
            rew = data.get("potential_reward")
            updated.potential_reward = str(rew).strip() if rew else None
        if "network" in data:
            net = data.get("network")
            updated.network = str(net).strip() if net else None
        if "twitter_text" in data and data.get("twitter_text"):
            tw = str(data["twitter_text"]).strip()
            if len(tw) <= 300:
                updated.twitter_text = tw
        if data.get("theme_color"):
            color = str(data["theme_color"]).lower().strip()
            if color in {"lime", "cyan", "violet", "gold", "red", "orange"}:
                updated.artwork.theme_color = color

        # Sanitize and validate tasks
        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, list) and raw_tasks:
            sanitized = [sanitize_task(str(t)) for t in raw_tasks if str(t).strip()]
            val_res = validate_tasks(sanitized)
            if val_res.is_valid:
                updated.tasks = sanitized[:5]
            else:
                # If slight task validation failure, keep sanitized tasks up to 5 without ellipsis
                cleaned_tasks = [t.rstrip(".,;…").strip() for t in sanitized if len(t) <= 120]
                if cleaned_tasks:
                    updated.tasks = cleaned_tasks[:5]

        meta["image_operation"] = data.get("image_operation", "rerender_text")
        meta["explanation"] = data.get("explanation", f"Обновлен черновик ({provider})")
        meta["art_prompt"] = data.get("art_prompt")
    else:
        # Fallback if no LLM was reached
        from services.image_rework import detect_theme_color

        color = detect_theme_color(instruction)
        if color:
            updated.artwork.theme_color = color
            meta["image_operation"] = "local_edit"
            meta["explanation"] = f"Смена цвета темы на '{color}'"
        else:
            meta["explanation"] = f"Правка черновика: {instruction[:40]}"

    return updated, meta
