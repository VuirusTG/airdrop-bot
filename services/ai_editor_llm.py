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
ALL public-facing copy (both Telegram and Twitter) MUST be written in natural, fluent English.

TELEGRAM REQUIREMENTS (English):
- Title: Clear, scroll-stopping title in English with project name, e.g. "🔥 Lighter DEX: $30M LIT Airdrop & Testnet".
- Description: 2-3 engaging, concise sentences in natural, crisp English explaining what the project is, why it matters, and the value proposition. Never use robotic boilerplate like "This draft was created without AI".
- Tasks: 3-5 concrete actionable numbered steps in English (each step <= 120 chars, starting with an action verb, no ellipsis, no filler).
- Potential Reward: Clear realistic reward statement, e.g. "$1000+", "11M $LIT Pool", "Points & TGE Token Allocation".
- Network: Chain name (e.g. "Arbitrum", "Base", "Solana", "Ethereum", "EVM").

TWITTER / X REQUIREMENTS (English):
- Single ready-to-post tweet, STRICTLY <= 280 characters in English.
- Scroll-stopping hook on line 1.
- Brief context + 1 compact action.
- Include verified project URL exactly once.
- End with short conversational call to action ("Worth testing?", "Farming this?") + 1-2 hashtags (#airdrop, #testnet).

IMAGE METADATA:
- theme_color: "lime" (default), "cyan", "violet", "gold", "red", or "orange".
- image_prompt: 16:9 English prompt for background atmosphere if generated.

Respond ONLY with valid JSON:
{
  "title": "<engaging English title>",
  "category": "AIRDROP" | "TESTNET" | "QUEST" | "POINTS",
  "description": "<2-3 concise English sentences>",
  "tasks": ["<step 1 in English>", "<step 2 in English>", "<step 3 in English>"],
  "potential_reward": "<e.g. $1000+ or null>",
  "network": "<network name or null>",
  "twitter_text": "<ready tweet in English <= 280 chars>",
  "theme_color": "lime" | "cyan" | "violet" | "gold" | "red" | "orange",
  "image_prompt": "<English visual background prompt>"
}"""

SYSTEM_PROMPT_CONVERSATIONAL_EDITOR = """You are an intelligent Conversational Crypto Draft Editor.
Your job is to interpret the user's natural language instruction and update the current draft accordingly.

CRITICAL PRINCIPLE — INCREMENTAL / CUMULATIVE EDITING:
The user is editing this draft across multiple sequential turns.
YOU MUST PRESERVE all existing values from CURRENT DRAFT STATE unless the user explicitly requested to modify that specific field:
- If user asks to edit text / description: KEEP the exact existing `potential_reward`, `twitter_text`, `theme_color`, `tasks`, and `network` intact.
- If user asks to edit reward: ONLY modify `potential_reward`. Do NOT rewrite description or tasks.
- If user asks to edit Twitter: ONLY modify `twitter_text`.
- If user asks to remove or edit risk: ONLY modify `risk_note` (set to null if removing, e.g. "убери раздел Risk").
- If user asks to edit color or image: ONLY modify `theme_color` or `art_prompt` or `image_operation`. Do NOT rewrite text or steps.
- In your JSON response, return `modified_fields`: ["<field_name>", ...] containing ONLY the fields that you actually modified.

EXAMPLES OF USER INTENT:
1. "измени Potential rewards на фото на $1000+" or "поменяй награду на $500":
   - Update `potential_reward` to "$1000+".
   - Keep tasks, description, theme_color, and twitter intact.
   - `modified_fields`: ["potential_reward"]
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Изменена награда на $1000+ на карточке и в посте"

2. "сделай текст поста лаконичным и завлекающим" or "сделай короче и понятнее":
   - Rewrite `description` to be punchy, engaging, and concise (2-3 sentences).
   - Refine `tasks` to be sharp and actionable.
   - Keep existing `potential_reward`, `theme_color`, and `twitter_text` intact!
   - `modified_fields`: ["description", "tasks"]
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Текст поста переписан лаконично и завлекающе"

3. "перепиши твит" or "сделай твит бодрее":
   - Rewrite `twitter_text` (strictly <= 280 chars, strong hook, includes project link).
   - Keep all other fields untouched.
   - `modified_fields`: ["twitter_text"]
   - Set `image_operation` to "none".
   - `explanation`: "Обновлен текст для Twitter"

4. "удали 2 пункт и добавь: Сделай депозит от 20 USDC":
   - Modify the `tasks` array accordingly.
   - `modified_fields`: ["tasks"]
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Обновлены шаги действий"

5. "смени тему на синюю" or "сделай фиолетовый фон":
   - Set `theme_color` to "cyan" / "violet" / "lime" / "gold" / "red" / "orange".
   - Keep description, tasks, reward, twitter intact!
   - `modified_fields`: ["theme_color"]
   - Set `image_operation` to "local_edit".
   - `explanation`: "Сменен цвет темы карточки"

6. "сделай фон в стиле киберпанк" or "поменяй картинку/арт":
   - Set `image_operation` to "new_artwork".
   - Set `art_prompt` to English prompt for background art.
   - `modified_fields`: ["image_operation", "art_prompt"]
   - `explanation`: "Запрошена генерация нового фона карточки"

7. "убери раздел Risk с поста для телеграмма" or "удали риск":
   - Set `risk_note` to null.
   - Keep all other fields untouched!
   - `modified_fields`: ["risk_note"]
   - Set `image_operation` to "none".
   - `explanation`: "Удален раздел предупреждения о риске (Risk)"

RULES:
- Maintain factual integrity: do not invent false URLs or seed phrase requests.
- Tasks: max 5 steps, <= 120 chars each, no ellipsis ("..."), clear actionable verbs in English.
- Twitter: <= 280 characters in English.
- ALL public-facing text (title, description, tasks, potential reward, twitter_text) MUST be in natural, fluent English.

Respond ONLY with valid JSON:
{
  "modified_fields": ["<field1>", "<field2>"],
  "title": "<current or updated title>",
  "category": "<current or updated category>",
  "description": "<current or updated description>",
  "tasks": ["<step 1>", "<step 2>", ...],
  "potential_reward": "<updated or current reward>",
  "network": "<updated or current network>",
  "risk_note": "<updated risk note or null>",
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
        f"Risk Note: {current.risk_note or 'None'}\n"
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
        raw_mod = data.get("modified_fields") or []
        mod_fields = set(str(f).lower().strip() for f in raw_mod)
        is_all = not mod_fields or "all" in mod_fields

        # Title
        if (is_all or "title" in mod_fields or "project_title" in mod_fields) and data.get("title"):
            updated.title = str(data["title"]).strip()

        # Category
        if (is_all or "category" in mod_fields) and data.get("category"):
            updated.category = str(data["category"]).strip().upper()

        # Description
        if (is_all or "description" in mod_fields or "summary" in mod_fields) and data.get("description"):
            desc = str(data["description"]).strip()
            desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", desc, flags=re.IGNORECASE).strip()
            updated.description = desc

        # Potential reward
        if (is_all or "potential_reward" in mod_fields or "reward" in mod_fields) and "potential_reward" in data:
            rew = data.get("potential_reward")
            if rew:
                updated.potential_reward = str(rew).strip()

        # Network
        if (is_all or "network" in mod_fields or "chain" in mod_fields) and data.get("network"):
            net = data.get("network")
            if net:
                updated.network = str(net).strip()

        # Twitter text
        if (is_all or "twitter_text" in mod_fields or "twitter" in mod_fields or "tweet" in mod_fields) and data.get("twitter_text"):
            tw = str(data["twitter_text"]).strip()
            if len(tw) <= 300:
                updated.twitter_text = tw

        # Risk note
        if (is_all or "risk_note" in mod_fields or "risk" in mod_fields) and "risk_note" in data:
            rn = data.get("risk_note")
            if rn is None or (isinstance(rn, str) and not rn.strip()) or str(rn).lower().strip() in ("none", "null", "false"):
                updated.risk_note = None
            else:
                updated.risk_note = str(rn).strip()
        elif any(w in instruction.lower() for w in ["убери риск", "удали риск", "remove risk", "delete risk", "без риска", "раздел risk", "раздел риск"]):
            updated.risk_note = None

        # Theme color: only update if user instruction explicitly requests color change or model marked it
        from services.image_rework import detect_theme_color
        cmd_color = detect_theme_color(instruction)
        if cmd_color:
            updated.artwork.theme_color = cmd_color
        elif ("theme_color" in mod_fields or "color" in mod_fields or "artwork" in mod_fields) and data.get("theme_color"):
            color = str(data["theme_color"]).lower().strip()
            if color in {"lime", "cyan", "violet", "gold", "red", "orange"}:
                updated.artwork.theme_color = color

        # Tasks
        if is_all or "tasks" in mod_fields or "instructions" in mod_fields:
            raw_tasks = data.get("tasks")
            if isinstance(raw_tasks, list) and raw_tasks:
                sanitized = [sanitize_task(str(t)) for t in raw_tasks if str(t).strip()]
                val_res = validate_tasks(sanitized)
                if val_res.is_valid:
                    updated.tasks = sanitized[:5]
                else:
                    cleaned_tasks = [t.rstrip(".,;…").strip() for t in sanitized if len(t) <= 120]
                    if cleaned_tasks:
                        updated.tasks = cleaned_tasks[:5]

        # Always preserve custom artwork path & preset across conversational edits
        if current.artwork.custom_artwork_path and not updated.artwork.custom_artwork_path:
            updated.artwork.custom_artwork_path = current.artwork.custom_artwork_path
        if current.artwork.preset and not updated.artwork.preset:
            updated.artwork.preset = current.artwork.preset

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


async def ai_generate_initial_draft(
    name: str,
    raw_text: str,
    chain: str | None = None,
    category: str = "AIRDROP",
    source_url: str | None = None,
    project_url: str | None = None,
):
    """Generate initial draft via OpenRouter (Llama 3.3 70B primary, Qwen 2.5 72B fallback).

    Produces publication-ready content:
    - Telegram in crisp, engaging English with 3-4 numbered actionable tasks
    - Twitter in punchy English (<= 280 chars with project link)
    - Social card artwork theme color and image prompt
    """
    from services.draft_content import ArtworkMetadata, LayoutMetadata
    from services.llm_draft import DraftResult

    user_prompt = (
        f"Project Name: {name}\n"
        f"Detected Category: {category}\n"
        f"Ecosystem Chain / Network: {chain or 'Unknown'}\n"
        f"Verified Public Project Link: {project_url or 'None'}\n"
        f"Private Source URL: {source_url or 'None'}\n\n"
        f"RAW SOURCE DATA / ANNOUNCEMENT:\n{raw_text[:7000]}\n\n"
        "Generate the publication-ready JSON draft now. All public fields (title, description, tasks, potential_reward, twitter_text) MUST be written strictly in natural, fluent English."
    )

    data, provider = await _call_llm_json(SYSTEM_PROMPT_INITIAL_DRAFT, user_prompt)
    if isinstance(data, dict):
        title = str(data.get("title") or name).strip()
        cat = str(data.get("category") or category).strip().upper()
        desc = str(data.get("description") or "").strip()
        desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", desc, flags=re.IGNORECASE).strip()

        raw_tasks = data.get("tasks") or []
        sanitized_tasks = [sanitize_task(str(t)) for t in raw_tasks if str(t).strip()]
        val_res = validate_tasks(sanitized_tasks)
        if not val_res.is_valid:
            sanitized_tasks = [t.rstrip(".,;…").strip() for t in sanitized_tasks if len(t) <= 120]
        if not sanitized_tasks:
            sanitized_tasks = [f"Visit official {name} portal", "Complete verification or testnet tasks"]

        potential_reward = str(data.get("potential_reward") or "").strip() or None
        network = str(data.get("network") or chain or "").strip() or None
        twitter_text = str(data.get("twitter_text") or "").strip() or None
        if twitter_text and len(twitter_text) > 280:
            twitter_text = twitter_text[:279].rsplit(" ", 1)[0] + "…"

        theme_color = str(data.get("theme_color") or "lime").lower().strip()
        if theme_color not in {"lime", "cyan", "violet", "gold", "red", "orange"}:
            theme_color = "lime"

        image_prompt = str(data.get("image_prompt") or "").strip() or None

        content = DraftContent(
            title=title,
            category=cat,
            description=desc,
            tasks=sanitized_tasks[:5],
            potential_reward=potential_reward,
            network=network,
            project_link=project_url,
            links=[project_url] if project_url else [],
            twitter_text=twitter_text,
            source_url=source_url,
            artwork=ArtworkMetadata(
                theme_color=theme_color,
                prompt=image_prompt,
            ),
            layout=LayoutMetadata(),
        )

        draft_result = DraftResult(
            title=title,
            summary=desc,
            instructions=content.render_instructions_text(),
            potential_reward=potential_reward,
            risk_note=None,
            twitter_text=twitter_text,
            image_prompt=image_prompt,
        )

        return content, draft_result, provider

    # Fallback to local heuristic
    from services.fallback_content import fallback_generate_draft
    from services.draft_content import draft_to_content

    fb_draft = fallback_generate_draft(name, raw_text, chain, category, project_url)
    content = draft_to_content(fb_draft, None)
    content.project_link = project_url
    content.source_url = source_url
    return content, fb_draft, "local"

