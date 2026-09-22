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
    """Send a chat completion request to OpenRouter API with native dual-model fallback."""
    if not settings.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    target_model = model or settings.OPENROUTER_MODEL
    fallback_model = getattr(settings, "OPENROUTER_FALLBACK_MODEL", "qwen/qwen-2.5-72b-instruct:free")
    url = f"{settings.OPENROUTER_BASE_URL}/chat/completions"

    api_key = settings.OPENROUTER_API_KEY.strip().strip("'\"")
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/VuirusTG/airdrop-bot",
        "X-Title": "AirdropBot",
        "Content-Type": "application/json",
    }

    # OpenRouter native model-routing fallback: tries target_model, then fallback_model
    models_list = [target_model]
    if fallback_model and fallback_model != target_model:
        models_list.append(fallback_model)

    payload: dict[str, Any] = {
        "model": target_model,
        "models": models_list,
        "route": "fallback",
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        payload["response_format"] = response_format

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, 4):
            # On final retry attempt, explicitly switch the primary target to the fallback model
            if attempt == 3 and fallback_model and fallback_model != target_model:
                payload["model"] = fallback_model

            try:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    wait_sec = attempt * 2.0
                    logger.warning(
                        "OpenRouter rate limited (429) for %s, backing off for %.1fs (attempt %d/3)",
                        payload["model"],
                        wait_sec,
                        attempt,
                    )
                    await asyncio.sleep(wait_sec)
                    continue

                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise ValueError(f"OpenRouter returned empty choices: {data}")

                content = choices[0]["message"]["content"]
                used_model = data.get("model", payload["model"])
                logger.info("OpenRouter response generated successfully via %s", used_model)
                return (content or "").strip()

            except httpx.HTTPStatusError as exc:
                logger.warning("OpenRouter HTTP error %s for %s: %s", exc.response.status_code, payload["model"], exc.response.text[:300])
                if attempt == 3:
                    raise
                await asyncio.sleep(attempt * 1.5)
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                logger.warning("OpenRouter network error on attempt %d for %s: %s", attempt, payload["model"], exc)
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


async def check_connection() -> tuple[bool, str]:
    """Check OpenRouter API key validity, active models, and connectivity."""
    if not settings.OPENROUTER_API_KEY:
        return False, "API-ключ OPENROUTER_API_KEY не задан в переменных окружения Render"
    api_key = settings.OPENROUTER_API_KEY.strip().strip("'\"")
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/VuirusTG/airdrop-bot",
        "X-Title": "AirdropBot",
    }
    url = f"{settings.OPENROUTER_BASE_URL}/auth/key"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                label = data.get("label") or "active"
                limit = data.get("limit")
                limit_info = f", лимит: ${limit}" if limit is not None else ""
                fallback_model = getattr(settings, "OPENROUTER_FALLBACK_MODEL", "qwen/qwen-2.5-72b-instruct:free")
                return True, (
                    f"Подключен ({label}{limit_info})\n"
                    f"   • Основная модель: {settings.OPENROUTER_MODEL}\n"
                    f"   • Резервная модель: {fallback_model}\n"
                    f"   • Dual-model failover: активен"
                )
            if resp.status_code == 401:
                return False, "Ошибка 401 (Unauthorized): неверный API-ключ. Проверьте переменную OPENROUTER_API_KEY в панели Render."
            if resp.status_code == 402:
                return False, "Ошибка 402 (Payment Required): закончился баланс или исчерпана квота на OpenRouter."
            if resp.status_code == 429:
                return False, "Ошибка 429 (Rate Limit): превышен лимит запросов в минуту. Бот автоматически задействует резервную модель Qwen 2.5."
            if resp.status_code in (502, 503, 504):
                return False, f"Ошибка {resp.status_code}: серверы OpenRouter временно недоступны. Сработает Groq/Gemini fallback."
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
    except httpx.TimeoutException:
        return False, "Таймаут соединения с сервером openrouter.ai (сработает автоповтор через Groq/Gemini)"
    except Exception as exc:
        return False, f"Сетевая ошибка при проверке OpenRouter: {str(exc)[:160]}"

