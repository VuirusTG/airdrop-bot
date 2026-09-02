"""Generate Telegram, X/Twitter, and image-prompt drafts."""
import json
import re
from dataclasses import dataclass

from google.genai import types

from services.gemini_client import generate_content


SYSTEM_PROMPT = """You prepare publication-ready crypto opportunity drafts.
Write every public-facing field strictly in natural English, even when the source
material is in another language. Be factual and concise. Never invent funding,
deadlines, rewards, token confirmations, task steps, or URLs. If the source is vague,
say what is unconfirmed and tell the reader to verify the official page. Never ask
for a seed phrase or private key.

Telegram requirements:
- Clear title, 2-3 sentence summary, and specific numbered actions.
- Separate potential reward and risk fields.
- Do not include the private source URL. It is shown separately to the admin.
- Never put any URL in title, summary, instructions, potential_reward, or risk_note.
  The app adds the verified project URL separately as a Start here line.
- Keep the combined Telegram copy under 950 characters so it fits in a photo caption.

X/Twitter requirements:
- One ready-to-publish post, maximum 280 characters. Aim for 220-275 characters.
- Line 1: a scroll-stopping hook built from the strongest specific verified fact.
- Then explain why the opportunity may be worth watching and give one compact action.
- State reward uncertainty or the main risk honestly.
- End with a short natural conversation prompt when space allows, such as
  "Worth farming?" or "Would you test it?", followed by 1-2 specific hashtags.
- Use at most one relevant emoji. Use short lines for mobile readability.
- Include the supplied public project URL exactly once in twitter_text.
- Never include the private source URL, generic hype ("Huge opportunity", "Don't
  miss out", "Next 100x"), fake urgency, engagement begging, or more than 2 hashtags.
- Example style (do not copy facts):
  "A new testnet just put early users on the map.\n\nZK Atlas is tracking swaps and liquidity tasks. Rewards are unconfirmed, so use a test wallet and watch the costs.\n\nWorth testing?\n#testnet #airdrop"

Image prompt requirements:
- Produce a polished 16:9 editorial crypto visual prompt in English.
- Describe only the desired colors, lighting, atmosphere, and abstract environment.
- Never request the project name, logos, coins, tokens, currency symbols, signs,
  screens, readable text, fake UI, financial promises, or partner branding.

Respond with ONLY JSON:
{
  "title": "<short title with project name>",
  "summary": "<2-3 factual sentences>",
  "instructions": "<numbered plain-text steps separated by \\n>",
  "potential_reward": "<realistic statement or null>",
  "risk_note": "<one honest sentence or null>",
  "twitter_text": "<complete post, <=280 characters>",
  "image_prompt": "<English generation prompt>"
}"""


@dataclass
class DraftResult:
    title: str
    summary: str
    instructions: str
    potential_reward: str | None
    risk_note: str | None
    twitter_text: str | None
    image_prompt: str | None


def _parse_draft(response_text: str) -> DraftResult:
    data = json.loads(response_text)
    return DraftResult(
        title=data["title"],
        summary=data["summary"],
        instructions=data["instructions"],
        potential_reward=data.get("potential_reward"),
        risk_note=data.get("risk_note"),
        twitter_text=data.get("twitter_text", "").strip() or None,
        image_prompt=data.get("image_prompt"),
    )


def _needs_repair(draft: DraftResult, required_project_url: str | None = None) -> bool:
    public_fields = (
        draft.title,
        draft.summary,
        draft.instructions,
        draft.potential_reward,
        draft.risk_note,
        draft.twitter_text,
        draft.image_prompt,
    )
    has_cyrillic = any(re.search(r"[А-Яа-яЁё]", value or "") for value in public_fields)
    invalid_x_length = not draft.twitter_text or len(draft.twitter_text) > 280
    missing_project_url = bool(
        required_project_url
        and required_project_url not in (draft.twitter_text or "")
    )
    telegram_fields = (
        draft.title,
        draft.summary,
        draft.instructions,
        draft.potential_reward,
        draft.risk_note,
    )
    url_in_telegram = any(re.search(r"https?://|www\.", value or "", re.IGNORECASE) for value in telegram_fields)
    return has_cyrillic or invalid_x_length or missing_project_url or url_in_telegram


async def _request_draft(user_content: str) -> DraftResult:
    response = await generate_content(
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.5,
        ),
    )
    return _parse_draft(response.text)


async def _generate(user_content: str, required_project_url: str | None = None) -> DraftResult:
    draft = await _request_draft(user_content)
    if _needs_repair(draft, required_project_url):
        repair_content = (
            f"{user_content}\n\nThe previous response violated the output rules. Rewrite all "
            "public fields in English only, keep twitter_text at 280 characters or fewer, "
            f"and include this exact project URL once in twitter_text: {required_project_url or 'none'}."
        )
        draft = await _request_draft(repair_content)
    if _needs_repair(draft, required_project_url):
        raise ValueError("Gemini returned a non-English or oversized public draft twice")
    return draft


async def generate_draft(
    name: str,
    raw_text: str,
    chain: str | None,
    source_url: str | None,
    project_url: str | None,
) -> DraftResult:
    return await _generate(
        "Public draft language: English only.\n"
        f"Project: {name}\n"
        f"Chain: {chain or 'unknown'}\n"
        "The source URL is private admin metadata and must not appear in public copy.\n"
        f"Verified public project/action URL: {project_url or 'not found'}\n"
        "Include that project URL exactly once in twitter_text when provided.\n\n"
        f"Raw source content:\n{raw_text[:7000]}\n\nWrite the drafts now."
    , project_url)


async def rework_draft(
    name: str,
    raw_text: str,
    chain: str | None,
    source_url: str | None,
    project_url: str | None,
    previous: DraftResult,
    feedback: str,
) -> DraftResult:
    return await _generate(
        "Public draft language: English only.\n"
        f"Project: {name}\nChain: {chain or 'unknown'}\n"
        "The source URL is private admin metadata. Do not include it in either draft.\n"
        f"Verified public project/action URL: {project_url or 'not found'}\n"
        "Include that project URL exactly once in twitter_text when provided.\n\n"
        f"Raw source content:\n{raw_text[:7000]}\n\n"
        f"Previous Telegram draft:\n{previous.title}\n{previous.summary}\n"
        f"{previous.instructions}\n\nPrevious X copy:\n{previous.twitter_text or 'N/A'}\n\n"
        f"Reviewer feedback:\n{feedback}\n\nWrite the improved drafts now."
    , project_url)
