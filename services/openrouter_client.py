"""OpenRouter API client for free and low-cost LLMs (Llama 3.3, Gemini, Qwen).

Uses OpenAI-compatible endpoint with automatic retries and structured JSON support.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

OPENROUTER_DEFAULT_TIMEOUT = 45.0


def _extract_json_text(text: str) -> str:
    """Extract raw JSON text even if wrapped in markdown codeblocks."""
    cleaned = text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    return cleaned


async def generate_chat_completion(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
    response_format: dict[str, Any] | None = None,
    timeout: float = OPENROUTER_DEFAULT_TIMEOUT,
) -> str:
    """Send a chat completion request to OpenRouter API."""
    if not settings.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    target_model = model or settings.OPENROUTER_MODEL
    url = f"{settings.OPENROUTER_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://github.com/VuirusTG/airdrop-bot",
        "X-Title": "AirdropBot",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, 4):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    wait_sec = attempt * 2.5
                    logger.warning("OpenRouter rate limited (429), backing off for %.1fs (attempt %d/3)", wait_sec, attempt)
                    await asyncio.sleep(wait_sec)
                    continue

                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise ValueError(f"OpenRouter returned empty choices: {data}")

                content = choices[0]["message"]["content"]
                return (content or "").strip()

            except httpx.HTTPStatusError as exc:
                logger.warning("OpenRouter HTTP error %s: %s", exc.response.status_code, exc.response.text[:300])
                if attempt == 3:
                    raise
                await asyncio.sleep(attempt * 1.5)
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                logger.warning("OpenRouter network error on attempt %d: %s", attempt, exc)
                if attempt == 3:
                    raise
                await asyncio.sleep(attempt * 1.5)

    raise RuntimeError("OpenRouter request failed after 3 attempts")


async def generate_json(
    system_instruction: str,
    contents: str,
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    """Generate structured JSON via OpenRouter."""
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": contents},
    ]
    raw_response = await generate_chat_completion(
        messages=messages,
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    return _extract_json_text(raw_response)
