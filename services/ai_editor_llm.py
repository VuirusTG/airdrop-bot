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

def _sanitize_description(desc: str) -> str:
    """Enforce brevity on description: strictly 1-2 punchy sentences, <= 32 words, strip corporate PR buzzwords."""
    if not desc:
        return ""
    # Strip known boilerplate
    desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", desc, flags=re.IGNORECASE).strip()
    desc = re.sub(r"The source reports:\s*", "", desc, flags=re.IGNORECASE).strip()
    # Strip marketing buzzwords
    desc = re.sub(r"\b(?:innovative|revolutionary|cutting-edge|redefines|seamlessly)\s+", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"\ban\s+Web3\b", "a Web3", desc, flags=re.IGNORECASE)

    # If LLM generated multiple paragraphs, take only the first paragraph
    paragraphs = [p.strip() for p in desc.split("\n") if p.strip()]
    first_p = paragraphs[0] if paragraphs else desc

    # Split sentences
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", first_p) if s.strip()]
    if not sentences:
        return first_p.strip()

    s0 = sentences[0]
    if len(sentences) == 1:
        words = s0.split()
        if len(words) > 32:
            s0 = " ".join(words[:28]).rstrip(",:;-") + "."
        return s0.strip()

    s1 = sentences[1]
    s0_words = len(s0.split())
    s1_words = len(s1.split())

    if s0_words + s1_words <= 32:
        return f"{s0} {s1}".strip()
    elif s0_words >= 14:
        return s0.strip()
    else:
        avail = 30 - s0_words
        s1_part = " ".join(s1.split()[:avail]).rstrip(",:;-") + "."
        return f"{s0} {s1_part}".strip()


SYSTEM_PROMPT_INITIAL_DRAFT = """You are a top-tier crypto researcher and social media director for a premier crypto airdrop & alpha channel.
Analyze the raw crypto opportunity signal and generate an ultra-concise, high-converting editorial draft for Telegram and Twitter.
ALL public-facing copy MUST be written in natural, fluent English.

CRITICAL TELEGRAM CHANNEL STYLE GUIDELINES (Inspired by top alpha channels like DropsTab, DeFi Airdrops, CryptoRank):
- Telegram users scan posts in 3-5 seconds on mobile. Long descriptions and PR essays get skipped!
- Title: Catchy headline starting with an emoji, e.g. "🔥 Lighter DEX — $30M Airdrop & Testnet" or "🔥 Savior of Health — Heal Points & NFT Airdrop".

- Description: STRICTLY 1 to 2 SHORT, PUNCHY SENTENCES (20 to 35 words MAXIMUM).
  * Sentence 1: What the project is + network/ecosystem (e.g., "Savior of Health is a Web3 wellness platform on BNB Chain.").
  * Sentence 2: The value hook / reward opportunity (e.g., "Daily health streaks and wellness surveys earn Heal Points towards the upcoming token airdrop.").
  * CRITICAL RULES:
    1. NEVER write multiple paragraphs or long walls of text!
    2. NEVER describe step-by-step instructions in the description (the steps are already listed below under 'What to do')!
    3. BAN corporate marketing fluff and buzzwords: "innovative", "revolutionary", "redefines", "seamless", "cutting-edge", "introducing a live campaign where", "by combining X, Y, and Z mechanics", "empowers users to".
    4. Write like a crypto alpha insider, NOT a corporate press release.

- Tasks: 3-4 concrete actionable numbered steps in English (each step <= 65 chars, starting with an active imperative verb: Connect, Complete, Mint, Stake, Deposit, Farm, Claim).
  * CRITICAL: NEVER write generic advice like "Open the official website", "Verify campaign", "Do your own research", or "Use a burner wallet".
  * NEVER include raw URLs in task items (all links belong exclusively in the public link field).
  * Every task MUST be a specific, verifiable project activity.

- Potential Reward: Short token/reward statement (e.g. "$MBK Token Airdrop (Confirmed for TGE)", "11M $LIT Pool", "Heal Points & Soulbound NFTs").
  * NEVER state "No reward confirmed" if the context mentions tokens, airdrops, XP, points, or rewards!

- Network: Chain name (e.g. "BNB Chain", "Arbitrum", "Base", "Solana", "Ethereum").

TWITTER / X REQUIREMENTS:
- Ready-to-post tweet, STRICTLY <= 280 characters in English with high-converting structure:
  * Line 1: Hook with emoji (e.g., "🪂 [Project] Airdrop is live on #[Chain]!")
  * Line 2: Value & reward (e.g., "💰 [Reward] confirmed ahead of TGE.")
  * Line 3: 2 bullet tasks (e.g., "1⃣ Complete daily check-in\\n2⃣ Mint soulbound NFT")
  * Line 4: Verified project URL + #airdrop #crypto
  * Total length MUST be <= 280 characters.

IMAGE METADATA:
- theme_color: "lime" (default), "cyan", "violet", "gold", "red", or "orange".
- image_prompt: 16:9 English prompt for background atmosphere if generated.

FEW-SHOT EXAMPLES OF DESIRED STYLE:

Example 1:
Input: Savior of Health project on BNB Chain with Healdrop, surveys, streaks, soulbound NFTs.
Output:
{
  "title": "🔥 Savior of Health — Heal Points & NFT Airdrop",
  "category": "AIRDROP",
  "description": "Savior of Health is a Web3 wellness platform on BNB Chain. Users earn Heal Points and soulbound NFTs by completing daily health streaks and wellness surveys.",
  "tasks": [
    "Connect your wallet to the portal.",
    "Complete daily wellness check-ins & surveys.",
    "Mint your soulbound NFT on BNB Chain.",
    "Farm Heal Points to rank up on the leaderboard."
  ],
  "potential_reward": "Heal Points & Soulbound NFT Airdrop",
  "network": "BNB Chain",
  "twitter_text": "🪂 Savior of Health Airdrop is live on #BNBChain!\\n\\n💰 Heal Points & NFTs\\n\\n1⃣ Complete daily wellness check-ins\\n2⃣ Mint soulbound NFT\\n\\n🔗 Farm here: https://saviorofhealth.app/ #airdrop #crypto",
  "theme_color": "lime",
  "image_prompt": "Futuristic bio-digital cyber wellness interface with glowing green vital telemetry and sleek dark glass"
}

Example 2:
Input: Lighter DEX on Arbitrum, 30M LIT token pool, orderbook trading.
Output:
{
  "title": "🔥 Lighter DEX — $30M LIT Airdrop & Testnet",
  "category": "TESTNET",
  "description": "Lighter is an institutional-grade orderbook DEX on Arbitrum. The team has launched an incentivized testnet with a confirmed 30M $LIT token reward pool.",
  "tasks": [
    "Connect your wallet to the Arbitrum testnet.",
    "Claim faucet tokens and deposit test collateral.",
    "Execute limit and market trades on the orderbook.",
    "Track your trading volume and reward tier."
  ],
  "potential_reward": "30M $LIT Token Pool",
  "network": "Arbitrum",
  "twitter_text": "🪂 Lighter DEX Testnet is live on #Arbitrum!\\n\\n💰 30M $LIT Token Pool\\n\\n1⃣ Connect wallet to testnet\\n2⃣ Execute trades on orderbook\\n\\n🔗 Join here: https://lighter.xyz #airdrop #DeFi",
  "theme_color": "cyan",
  "image_prompt": "High-frequency cyber financial exchange holographic chart displays in electric cyan and deep indigo"
}

Respond ONLY with valid JSON:
{
  "title": "<concise English title starting with emoji>",
  "category": "AIRDROP" | "TESTNET" | "QUEST" | "POINTS",
  "description": "<STRICTLY 1-2 sentences, max 35 words>",
  "tasks": ["<step 1>", "<step 2>", "<step 3>", "<step 4>"],
  "potential_reward": "<specific reward or token allocation>",
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

2. "сделай текст поста лаконичным и завлекающим" or "сделай короче и понятнее" or "слишком много текста":
   - Rewrite `description` to be ULTRA-CONCISE (strictly 1-2 short sentences, max 30 words, explaining what the project is and why it matters).
   - NEVER repeat the tasks in the description!
   - Refine `tasks` to be sharp and actionable (under 65 chars each).
   - Keep existing `potential_reward`, `theme_color`, and `twitter_text` intact!
   - `modified_fields`: ["description", "tasks"]
   - Set `image_operation` to "rerender_text".
   - `explanation`: "Текст описания сокращен до 1-2 емких предложений без лишней воды"

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
- Tasks: max 4 steps, <= 65 chars each, no ellipsis ("..."), clear actionable verbs in English.
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
                temperature=0.35,
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
            desc = _sanitize_description(str(data["description"]))
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
        elif re.search(r"(?:удали|убери|сотри|очисти|вырежи|исключи|скипни|delete|remove|clear|drop|omit|skip|hide|скрой)\b[^\n]*?\b(?:risk\w*|риск\w*)\b|\b(?:без|no|without)\s+(?:risk\w*|риск\w*)\b", instruction, re.IGNORECASE):
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
        desc = _sanitize_description(str(data.get("description") or ""))

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

