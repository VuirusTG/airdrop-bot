"""Groq implementations of automatic project scoring and draft generation."""
from __future__ import annotations

import json

from services.groq_client import generate_json
from services.llm_draft import DraftResult, SYSTEM_PROMPT as DRAFT_SYSTEM_PROMPT
from services.llm_draft import _needs_repair, _parse_draft
from services.llm_filter import FilterResult, SYSTEM_PROMPT as FILTER_SYSTEM_PROMPT


FILTER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_opportunity": {"type": "boolean"},
        "has_current_action": {"type": "boolean"},
        "score": {"type": "number"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "critical_risk": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["review", "reject"]},
        "reasoning": {"type": "string"},
        "chain": {"type": ["string", "null"]},
        "category": {
            "type": "string",
            "enum": ["airdrop", "testnet", "quest", "points", "waitlist", "other"],
        },
    },
    "required": [
        "is_opportunity",
        "has_current_action",
        "score",
        "confidence",
        "critical_risk",
        "verdict",
        "reasoning",
        "chain",
        "category",
    ],
    "additionalProperties": False,
}

DRAFT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "instructions": {"type": "string"},
        "potential_reward": {"type": ["string", "null"]},
        "risk_note": {"type": ["string", "null"]},
        "twitter_text": {"type": "string"},
        "image_prompt": {"type": "string"},
    },
    "required": [
        "title",
        "summary",
        "instructions",
        "potential_reward",
        "risk_note",
        "twitter_text",
        "image_prompt",
    ],
    "additionalProperties": False,
}


async def score_project(
    name: str,
    raw_text: str,
    source_url: str | None = None,
) -> FilterResult:
    user_content = f"Candidate: {name}\n"
    if source_url:
        user_content += f"Source URL: {source_url}\n"
    user_content += f"\nRaw signal:\n{raw_text[:7000]}"
    response_text = await generate_json(
        system_instruction=FILTER_SYSTEM_PROMPT,
        contents=user_content,
        temperature=0.1,
        schema_name="crypto_opportunity_filter",
        response_schema=FILTER_RESPONSE_SCHEMA,
    )
    data = json.loads(response_text)
    return FilterResult(
        score=float(data["score"]),
        verdict=data.get("verdict", "reject"),
        reasoning=data["reasoning"],
        chain=data.get("chain"),
        category=data.get("category", "other"),
        is_opportunity=bool(data.get("is_opportunity")),
        has_current_action=bool(data.get("has_current_action")),
        confidence=data.get("confidence", "low"),
        critical_risk=bool(data.get("critical_risk")),
    )


async def _request_draft(user_content: str) -> DraftResult:
    response_text = await generate_json(
        system_instruction=DRAFT_SYSTEM_PROMPT,
        contents=user_content,
        temperature=0.5,
        schema_name="crypto_publication_draft",
        response_schema=DRAFT_RESPONSE_SCHEMA,
    )
    return _parse_draft(response_text)


async def _generate(user_content: str, project_url: str | None) -> DraftResult:
    draft = await _request_draft(user_content)
    if _needs_repair(draft, project_url):
        draft = await _request_draft(
            f"{user_content}\n\nRewrite all public fields in English, keep twitter_text at "
            f"280 characters or fewer, and include this exact project URL once: {project_url or 'none'}."
        )
    if _needs_repair(draft, project_url):
        raise ValueError("Groq returned a non-English or oversized public draft twice")
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
        f"Raw source content:\n{raw_text[:7000]}\n\nWrite the drafts now.",
        project_url,
    )


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
        f"Reviewer feedback:\n{feedback}\n\nWrite the improved drafts now.",
        project_url,
    )
