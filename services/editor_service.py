"""Editor V2: Structured Natural Language Editor Service.

Parses natural language user commands into typed EditPlans, validates changes,
executes deterministic updates on DraftContent, and routes image operations
(Level 1: Rerender Card, Level 2: Local Edit, Level 3: New Artwork).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from config import settings
from services.draft_content import DraftContent
from services.gemini_client import generate_content
from services.groq_client import generate_json
from services.image_rework import detect_preset, detect_theme_color, requests_image_rework
from services.task_validator import sanitize_task, validate_single_task, validate_tasks

logger = logging.getLogger(__name__)

SUPPORTED_TARGETS = {
    "project_title",
    "category",
    "description",
    "tasks",
    "task_1",
    "task_2",
    "task_3",
    "task_4",
    "task_5",
    "potential_reward",
    "network",
    "footer",
    "early_users",
    "project_link",
    "twitter",
    "telegram",
    "artwork",
    "image_background",
    "character",
    "layout",
    "full_draft",
    "post",
    "draft",
}

SUPPORTED_OPERATIONS = {
    "replace",
    "replace_list",
    "rewrite",
    "add",
    "remove",
    "reorder",
    "shorten",
    "regenerate",
    "restyle",
}


@dataclass
class EditPlan:
    target: str
    operation: str
    old_value: Any = None
    new_value: Any = None
    confidence: float = 1.0
    requires_confirmation: bool = False
    affected_components: list[str] = field(default_factory=lambda: ["draft_data", "telegram_post", "social_card"])
    image_operation: str = "rerender_text"  # none | rerender_text | local_edit | new_artwork
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditPlan:
        return cls(
            target=data.get("target", "tasks"),
            operation=data.get("operation", "replace"),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
            confidence=float(data.get("confidence", 1.0)),
            requires_confirmation=bool(data.get("requires_confirmation", False)),
            affected_components=list(data.get("affected_components") or ["draft_data"]),
            image_operation=data.get("image_operation", "rerender_text"),
            explanation=data.get("explanation", ""),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


INTENT_PARSER_SYSTEM_PROMPT = """You are an ultra-precise Draft Editor Intent Parser for a crypto editorial application.
Your job is to read the CURRENT draft data and the USER COMMAND, and output a strict JSON EditPlan.

RULES:
1. Identify the EXACT target being edited:
   - "project_title" (title)
   - "description" (summary / description text)
   - "category" (AIRDROP, TESTNET, QUEST, etc.)
   - "potential_reward" (reward amount or status)
   - "network" (chain name: Base, Arbitrum, Solana, etc.)
   - "project_link" (URL to project)
   - "tasks" (the whole list of tasks)
   - "task_1", "task_2", "task_3", "task_4", "task_5" (a specific task item)
   - "image_background" or "artwork" (visual style, background art, scene)
   - "full_draft" (if user asks to rewrite, summarize, or rework the whole post/draft, or provides general feedback about post quality/content)

2. Determine the operation:
   - "replace", "replace_list", "rewrite", "add", "remove", "shorten", "regenerate", "restyle"

3. CRITICAL TASK RULES when editing tasks:
   - Each task MUST be a concise action statement <= 120 characters.
   - NEVER use ellipsis ("..." or "…").
   - NEVER use filler words ("etc.", "and more", "and so on").
   - NO embedded numbering ("1.", "Step 1:").
   - NO markdown formatting or emojis inside task strings.
   - NEVER invent new fictional steps not implied by the user or source.
   - If user asks to "shorten" or "сделай короче", preserve factual meaning while removing fluff.

4. Image operation routing:
   - If changing reward, tasks, title, network, category -> image_operation MUST BE "rerender_text" (NO new AI artwork).
   - If changing background, character, art style -> image_operation MUST BE "new_artwork".
   - If changing only twitter text or private links -> image_operation MUST BE "none".

Respond ONLY with valid JSON:
{
  "target": "<target>",
  "operation": "<operation>",
  "old_value": <current value or null>,
  "new_value": <new value or list of new values>,
  "confidence": <float 0.0-1.0>,
  "requires_confirmation": <true if major rewrite or removing tasks, else false>,
  "affected_components": ["draft_data", "telegram_post", "social_card"],
  "image_operation": "rerender_text" | "new_artwork" | "none",
  "explanation": "<short human explanation in Russian>"
}"""


def _fast_deterministic_parse(command: str, current: DraftContent) -> EditPlan | None:
    """Fast regex-based intent parsing for unambiguous commands without LLM latency."""
    cmd = command.strip()

    # 1. Change potential reward: "Измени Potential Rewards с $2500+ на $1000+" / "Поменяй награду на $500"
    m_rew = re.search(
        r"(?:измени|поменяй|смени|поставь|change|update|set)\s+(?:potential\s+rewards?|наград\w*|reward)\s+(?:с\s+[^\s]+\s+)?на\s+([$€£]?\d+[\w,+]*|\$?[A-Za-z0-9+]+)",
        cmd,
        re.IGNORECASE,
    )
    if m_rew:
        new_val = m_rew.group(1).strip()
        if not new_val.startswith("$") and new_val[0].isdigit():
            new_val = f"${new_val}"
        return EditPlan(
            target="potential_reward",
            operation="replace",
            old_value=current.potential_reward,
            new_value=new_val,
            confidence=0.99,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Изменение награды на {new_val}",
        )

    # 2. Change network: "Смени сеть на Solana" / "Поменяй network на Base"
    m_net = re.search(
        r"(?:измени|поменяй|смени|поставь|change|set)\s+(?:сеть|network|chain)\s+(?:с\s+[^\s]+\s+)?на\s+([A-Za-z0-9\s_-]+)",
        cmd,
        re.IGNORECASE,
    )
    if m_net:
        new_net = m_net.group(1).strip()
        return EditPlan(
            target="network",
            operation="replace",
            old_value=current.network,
            new_value=new_net,
            confidence=0.98,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Изменение сети на {new_net}",
        )

    # 3. Replace single task: "Замени второй пункт на: Claim all available Chips points."
    m_task_idx = re.search(
        r"(?:замени|поменяй|измени|replace)\s+(первый|второй|третий|четвертый|пятый|\d+)\s*(?:-?[ыи]й)?\s*(?:пункт|шаг|задач\w*|task)\s*на\s*[:\s]*(.+)",
        cmd,
        re.IGNORECASE,
    )
    if m_task_idx:
        idx_word = m_task_idx.group(1).lower()
        word_map = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
        idx_num = word_map.get(idx_word, 1)
        new_task_str = sanitize_task(m_task_idx.group(2))
        old_val = current.tasks[idx_num - 1] if len(current.tasks) >= idx_num else None
        return EditPlan(
            target=f"task_{idx_num}",
            operation="replace",
            old_value=old_val,
            new_value=new_task_str,
            confidence=0.98,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Замена пункта #{idx_num}",
        )

    # 4. Remove single task: "Удали третий пункт" / "Удали задачу 2"
    m_del_task = re.search(
        r"(?:удали|убери|delete|remove)\s+(первый|второй|третий|четвертый|пятый|\d+)\s*(?:-?[ыи]й)?\s*(?:пункт|шаг|задач\w*|task)",
        cmd,
        re.IGNORECASE,
    )
    if m_del_task:
        idx_word = m_del_task.group(1).lower()
        word_map = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
        idx_num = word_map.get(idx_word, 1)
        old_val = current.tasks[idx_num - 1] if len(current.tasks) >= idx_num else None
        return EditPlan(
            target=f"task_{idx_num}",
            operation="remove",
            old_value=old_val,
            new_value=None,
            confidence=0.98,
            requires_confirmation=True,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Удаление пункта #{idx_num}",
        )

    # 5. Add task: "Добавь задачу: Mint supporter NFT" / "Добавь пункт ..."
    m_add_task = re.search(
        r"(?:добавь|допиши|add)\s+(?:задачу|пункт|шаг|task)\s*[:\s]*(.+)",
        cmd,
        re.IGNORECASE,
    )
    if m_add_task:
        new_task_str = sanitize_task(m_add_task.group(1))
        return EditPlan(
            target="tasks",
            operation="add",
            old_value=None,
            new_value=new_task_str,
            confidence=0.98,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation="Добавление новой задачи",
        )

    # 6. Change title: "Измени заголовок на: Base Airdrop Season 2"
    m_title = re.search(
        r"(?:измени|поменяй|смени|set|change)\s+(?:заголовок|название|title)\s*на\s*[:\s]*(.+)",
        cmd,
        re.IGNORECASE,
    )
    if m_title:
        new_title = m_title.group(1).strip()
        return EditPlan(
            target="project_title",
            operation="replace",
            old_value=current.title,
            new_value=new_title,
            confidence=0.98,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Изменение заголовка на '{new_title}'",
        )

    # 7. Image & artwork edits: "поменяй фон", "сделай фон синим", "измени картинку на киберпанк", "поменяй фото", etc.
    if requests_image_rework(cmd):
        theme = detect_theme_color(cmd)
        preset = detect_preset(cmd)
        if theme and not preset and not re.search(r"полностью|сгенерир|flux|ai\b|нов(?:ый|ую)\s+арт", cmd, re.IGNORECASE):
            return EditPlan(
                target="artwork",
                operation="restyle",
                old_value=current.artwork.theme_color,
                new_value=theme,
                confidence=0.98,
                requires_confirmation=False,
                affected_components=["social_card"],
                image_operation="local_edit",
                explanation=f"Смена цвета темы карточки на '{theme}'",
            )
        else:
            return EditPlan(
                target="image_background",
                operation="regenerate",
                old_value=current.artwork.prompt,
                new_value=cmd,
                confidence=0.96,
                requires_confirmation=False,
                affected_components=["draft_data", "social_card"],
                image_operation="new_artwork",
                explanation=f"Обновление фона карточки: {cmd}",
            )

    # 8. Full draft / text rework / rewrite: "Сделай текст поста лаконичным и завлекающим", "Улучши текст", "Сократи пост", etc.
    if re.search(
        r"(?:перепиши|переделай|переработай|улучши|обнови|напиши|сделай|сократи|исправь|измени|подправь|перефразируй|rework|rewrite|shorten)\s+.*(?:пост|текст|черновик|описан|шаг|задач|draft|post|content)",
        cmd,
        re.IGNORECASE,
    ) or re.search(
        r"^(?:сделай|перепиши|переделай|улучши|сократи|подправь)\s+.*(?:лаконичн|завлекающ|читабельн|короче|лучше|красив|понятн|нормальн)",
        cmd,
        re.IGNORECASE,
    ) or re.search(
        r"^(?:нормальный\s+текст|перепиши|переделай|сделай\s+шаги|сделай\s+нормальный\s+текст.*|улучши\s+текст.*|лаконичный\s+текст.*)$",
        cmd,
        re.IGNORECASE,
    ):
        return EditPlan(
            target="full_draft",
            operation="rewrite",
            old_value=None,
            new_value=cmd,
            confidence=0.96,
            requires_confirmation=False,
            affected_components=["draft_data", "telegram_post", "social_card"],
            image_operation="rerender_text",
            explanation=f"Переработка текста черновика: {cmd}",
        )

    return None


async def parse_intent_with_llm(command: str, current: DraftContent) -> EditPlan:
    """Parse user command using Groq with Gemini fallback."""
    user_context = (
        f"CURRENT DRAFT STATE:\n"
        f"Title: {current.title}\n"
        f"Category: {current.category}\n"
        f"Description: {current.description}\n"
        f"Tasks:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(current.tasks, 1)) + "\n"
        f"Potential Reward: {current.potential_reward}\n"
        f"Network: {current.network}\n"
        f"Project Link: {current.project_link}\n"
        f"Artwork Theme Color: {current.artwork.theme_color}\n\n"
        f"USER COMMAND:\n{command}\n\n"
        "Return the structured JSON EditPlan:"
    )

    # Try Groq first
    if settings.GROQ_API_KEY:
        try:
            response_json = await generate_json(
                system_instruction=INTENT_PARSER_SYSTEM_PROMPT,
                contents=user_context,
                temperature=0.1,
                schema_name="draft_edit_plan",
            )
            data = json.loads(response_json)
            return EditPlan.from_dict(data)
        except Exception as exc:
            logger.warning("Groq intent parse failed: %s; trying Gemini fallback", exc)

    # Try Gemini fallback
    if settings.GEMINI_API_KEY:
        try:
            response = await generate_content(
                prompt=f"{INTENT_PARSER_SYSTEM_PROMPT}\n\n{user_context}",
                temperature=0.1,
            )
            text = (response.text or "").strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            data = json.loads(text)
            return EditPlan.from_dict(data)
        except Exception as exc:
            logger.warning("Gemini intent parse failed: %s", exc)

    # Fallback to general rework if LLM unavailable
    return EditPlan(
        target="full_draft",
        operation="rewrite",
        old_value=None,
        new_value=command,
        confidence=0.6,
        requires_confirmation=False,
        affected_components=["draft_data", "telegram_post", "social_card"],
        image_operation="rerender_text",
        explanation=f"Переработка черновика по запросу: {command}",
    )


async def compress_or_correct_tasks(
    invalid_tasks: list[str],
    issues_summary: str,
) -> list[str]:
    """Second AI correction pass strictly targeting invalid tasks.

    Enforces <= 120 characters, removes ellipsis/filler without losing factual meaning.
    """
    correction_prompt = (
        "You are a strict task corrector. The following crypto qualification tasks failed validation:\n"
        f"{issues_summary}\n\n"
        "ORIGINAL TASKS:\n" + "\n".join(f"- {t}" for t in invalid_tasks) + "\n\n"
        "TASK RULES:\n"
        "1. Max 120 characters per task.\n"
        "2. Single clear action statement in natural English.\n"
        "3. NO ellipsis ('...' or '…').\n"
        "4. NO filler words ('etc.', 'and more', 'and so on').\n"
        "5. NO embedded numbering or bullets ('1.', '-').\n"
        "6. NO markdown syntax or emojis.\n"
        "7. Preserve exact factual actions (bridge, swap, mint, stake, etc.).\n\n"
        "Return ONLY a JSON list of corrected string tasks, e.g. [\"Action one\", \"Action two\"]:"
    )

    if settings.GROQ_API_KEY:
        try:
            resp = await generate_json(
                system_instruction="Return ONLY a JSON array of concise task strings <= 120 chars without ellipsis.",
                contents=correction_prompt,
                temperature=0.1,
            )
            parsed = json.loads(resp)
            if isinstance(parsed, list):
                return [sanitize_task(str(x)) for x in parsed if str(x).strip()]
        except Exception as exc:
            logger.warning("Groq task correction pass failed: %s", exc)

    if settings.GEMINI_API_KEY:
        try:
            resp = await generate_content(prompt=correction_prompt, temperature=0.1)
            text = (resp.text or "").strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [sanitize_task(str(x)) for x in parsed if str(x).strip()]
        except Exception as exc:
            logger.warning("Gemini task correction pass failed: %s", exc)

    # If both Groq and Gemini failed, return sanitized tasks without silent slicing
    return [sanitize_task(t) for t in invalid_tasks]


REWRITE_DRAFT_SYSTEM_PROMPT = """You are an elite crypto researcher and Telegram post editor for an exclusive airdrop channel.
Your goal is to rework and elevate a draft into a sharp, professional, compelling publication-ready post based on the current draft context and the user's instructions.

CRITICAL EDITORIAL RULES:
1. NEVER output robotic disclaimers or boilerplate warnings such as:
   - "This draft was created without AI..."
   - "Confirm all details on the official page before publishing."
   - "Review any active tasks or qualification criteria."
   These are strictly forbidden.
2. Title: Clean, punchy, exciting project title with category (e.g. "Lighter: Perpetual DEX Airdrop on Robinhood Chain").
3. Description: 2-3 engaging, factual sentences explaining the project value, backers/ecosystem, and what makes this opportunity noteworthy.
4. Tasks: Exactly 2 to 4 concrete, actionable qualification steps based on the project context.
   - Each task MUST be <= 120 characters.
   - Write clear, active verb actions (e.g. "Trade on the perpetual DEX to build onchain volume", "Bridge assets to Robinhood Chain via the official portal").
   - NEVER use ellipsis ("..." or "…").
   - NEVER use filler phrases ("etc.", "and more", "and so on").
   - NO embedded numbering ("1.", "Step 1:").
   - NO markdown formatting or emojis inside task strings.
5. Potential Reward: Keep existing or extract if mentioned (e.g. "11M $LIT tokens ($30M pool)", "$1,000+", "Unconfirmed").
6. Network: Ecosystem chain (e.g. "Robinhood Chain", "Base", "Solana", "Arbitrum", "Ethereum").
7. Category: AIRDROP, TESTNET, or QUEST.

Return ONLY a valid JSON object:
{
  "title": "<clean title>",
  "category": "AIRDROP",
  "description": "<2-3 sentence engaging summary>",
  "tasks": [
    "<actionable task 1>",
    "<actionable task 2>",
    "<actionable task 3>"
  ],
  "potential_reward": "<reward or null>",
  "network": "<network or null>"
}"""


async def rewrite_draft_with_llm(current: DraftContent, instruction: str) -> DraftContent:
    """Rewrite and elevate the entire draft using Groq with Gemini fallback."""
    import copy
    updated = copy.deepcopy(current)

    user_context = (
        f"CURRENT DRAFT:\n"
        f"Title: {current.title}\n"
        f"Category: {current.category}\n"
        f"Description: {current.description}\n"
        f"Tasks:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(current.tasks, 1)) + "\n"
        f"Potential Reward: {current.potential_reward}\n"
        f"Network: {current.network}\n"
        f"Project Link: {current.project_link}\n\n"
        f"USER INSTRUCTION / FEEDBACK:\n{instruction}\n\n"
        "Generate the complete rewritten draft JSON:"
    )

    data = None
    # 1. Groq
    if settings.GROQ_API_KEY:
        try:
            resp_json = await generate_json(
                system_instruction=REWRITE_DRAFT_SYSTEM_PROMPT,
                contents=user_context,
                temperature=0.3,
                schema_name="draft_rewrite",
            )
            data = json.loads(resp_json)
        except Exception as exc:
            logger.warning("Groq draft rewrite failed: %s; trying Gemini", exc)

    # 2. Gemini fallback
    if data is None and settings.GEMINI_API_KEY:
        try:
            resp = await generate_content(
                prompt=f"{REWRITE_DRAFT_SYSTEM_PROMPT}\n\n{user_context}",
                temperature=0.3,
            )
            text = (resp.text or "").strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            data = json.loads(text)
        except Exception as exc:
            logger.warning("Gemini draft rewrite failed: %s", exc)

    if isinstance(data, dict):
        if data.get("title"):
            updated.title = str(data["title"]).strip()
        if data.get("category"):
            updated.category = str(data["category"]).strip().upper()
        if data.get("description"):
            desc = str(data["description"]).strip()
            desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", desc, flags=re.IGNORECASE).strip()
            updated.description = desc
        if data.get("potential_reward"):
            updated.potential_reward = str(data["potential_reward"]).strip()
        if data.get("network"):
            updated.network = str(data["network"]).strip()

        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, list) and raw_tasks:
            sanitized = [sanitize_task(str(t)) for t in raw_tasks if str(t).strip()]
            val_res = validate_tasks(sanitized)
            if not val_res.is_valid:
                logger.info("Rewritten tasks failed validation (%s), compressing...", val_res.error_summary)
                corrected = await compress_or_correct_tasks(sanitized, val_res.error_summary)
                val_res2 = validate_tasks(corrected)
                if val_res2.is_valid:
                    sanitized = corrected
            if sanitized:
                updated.tasks = sanitized[:5]

        return updated

    # Fallback if both LLMs unavailable: clean robotic disclaimers deterministically
    clean_desc = re.sub(r"This draft was created without AI[^\.]*\.?", "", updated.description, flags=re.IGNORECASE).strip()
    updated.description = clean_desc
    return updated


class EditorService:
    """Core Editor V2 engine orchestrating parsing, validation, application, and preview."""

    @staticmethod
    async def create_edit_plan(command: str, current: DraftContent) -> EditPlan:
        # 1. Try fast deterministic parser
        fast_plan = _fast_deterministic_parse(command, current)
        if fast_plan:
            return fast_plan

        # 2. Use LLM Intent Parser
        plan = await parse_intent_with_llm(command, current)
        return plan

    @staticmethod
    async def apply_edit_plan(
        plan: EditPlan,
        current: DraftContent,
    ) -> tuple[DraftContent, bool, str | None]:
        """Apply EditPlan to DraftContent, enforcing strict task validation.

        Returns:
            (updated_content, is_valid, error_message_if_any)
        """
        import copy
        new_content = copy.deepcopy(current)

        target = plan.target.lower()
        op = plan.operation.lower()
        new_val = plan.new_value

        # 0. Full Draft Rewrite / Post Rework
        if target in ("full_draft", "post", "post_text", "draft"):
            instruction = str(new_val) if new_val else plan.explanation
            rewritten = await rewrite_draft_with_llm(new_content, instruction)
            return rewritten, True, None

        # 1. Single Task Item (task_1, task_2, etc.)
        if target.startswith("task_"):
            try:
                idx = int(target.split("_")[1]) - 1
            except ValueError:
                idx = 0

            if op == "remove":
                if 0 <= idx < len(new_content.tasks):
                    new_content.tasks.pop(idx)
            else:
                sanitized = sanitize_task(str(new_val or ""))
                # Validate
                val_res = validate_single_task(sanitized, idx)
                if val_res:
                    corrected = await compress_or_correct_tasks([sanitized], val_res[0].reason)
                    if corrected:
                        sanitized = corrected[0]
                if 0 <= idx < len(new_content.tasks):
                    new_content.tasks[idx] = sanitized
                elif idx == len(new_content.tasks) and len(new_content.tasks) < 5:
                    new_content.tasks.append(sanitized)

        # 2. All Tasks (tasks)
        elif target == "tasks":
            if op == "add":
                sanitized = sanitize_task(str(new_val or ""))
                if len(new_content.tasks) < 5:
                    new_content.tasks.append(sanitized)
            elif isinstance(new_val, list):
                raw_list = [sanitize_task(str(x)) for x in new_val if str(x).strip()]
                # Validate whole task list
                val_result = validate_tasks(raw_list)
                if not val_result.is_valid:
                    # Run second AI correction pass
                    logger.info("Tasks failed validation (%s), running correction pass", val_result.error_summary)
                    corrected_list = await compress_or_correct_tasks(raw_list, val_result.error_summary)
                    val_result2 = validate_tasks(corrected_list)
                    if not val_result2.is_valid:
                        return current, False, f"Задачи не прошли валидацию: {val_result2.error_summary}. Уточните список."
                    raw_list = corrected_list
                new_content.tasks = raw_list[:5]
            elif isinstance(new_val, str) and ("\n" in new_val or re.search(r"^\d+[\.\)]", new_val.strip())):
                from services.draft_content import parse_legacy_instructions
                parsed_tasks, _ = parse_legacy_instructions(new_val)
                if parsed_tasks:
                    val_result = validate_tasks(parsed_tasks)
                    if not val_result.is_valid:
                        parsed_tasks = await compress_or_correct_tasks(parsed_tasks, val_result.error_summary)
                    new_content.tasks = parsed_tasks[:5]
            elif isinstance(new_val, str) and new_val.strip():
                rewritten_draft = await rewrite_draft_with_llm(new_content, f"Rewrite tasks only: {new_val}")
                new_content.tasks = rewritten_draft.tasks

        # 3. Potential Reward
        elif target == "potential_reward":
            new_content.potential_reward = str(new_val).strip() if new_val else None

        # 4. Project Title
        elif target in ("project_title", "title"):
            new_content.title = str(new_val).strip()

        # 5. Category
        elif target == "category":
            new_content.category = str(new_val).strip().upper()

        # 6. Network
        elif target in ("network", "chain"):
            new_content.network = str(new_val).strip()

        # 7. Description
        elif target in ("description", "summary"):
            new_content.description = str(new_val).strip()

        # 8. Project Link
        elif target in ("project_link", "link"):
            new_content.project_link = str(new_val).strip()

        # 9. Artwork & Image Background
        elif target in ("artwork", "image_background", "character", "image", "photo", "background", "card", "theme", "style"):
            val_str = str(new_val).strip() if new_val else ""
            color = detect_theme_color(val_str)
            if color or op == "restyle" or plan.image_operation == "local_edit":
                new_content.artwork.theme_color = color or val_str
                plan.image_operation = "local_edit"
            else:
                new_content.artwork.prompt = val_str
                plan.image_operation = "new_artwork"

        # Final check on tasks
        final_val = validate_tasks(new_content.tasks)
        if not final_val.is_valid:
            return current, False, f"Ошибка задач: {final_val.error_summary}"

        return new_content, True, None
