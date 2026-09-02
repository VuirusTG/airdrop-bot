"""Small Groq client with free-tier pacing and JSON responses."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any

import httpx

from config import settings


logger = logging.getLogger(__name__)
CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS_URL = "https://api.groq.com/openai/v1/models"
_request_lock = asyncio.Lock()
_last_request_started = 0.0


class GroqError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        return str(error.get("message") or error or payload)
    except ValueError:
        return response.text


def _is_failed_generation(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        error = response.json().get("error", {})
    except ValueError:
        return False
    message = str(error.get("message", "")).lower()
    return bool(error.get("failed_generation")) or "failed to generate json" in message


async def generate_json(
    *,
    system_instruction: str,
    contents: str,
    temperature: float,
    schema_name: str,
    response_schema: dict[str, Any],
) -> str:
    global _last_request_started

    if not settings.GROQ_API_KEY:
        raise GroqError("GROQ_API_KEY не настроен")

    payload = {
        "model": settings.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": contents},
        ],
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    async with _request_lock:
        async with httpx.AsyncClient(timeout=45.0) as client:
            for attempt in range(settings.GROQ_MAX_RATE_RETRIES + 1):
                elapsed = monotonic() - _last_request_started
                wait_seconds = settings.GROQ_MIN_REQUEST_INTERVAL_SECONDS - elapsed
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

                _last_request_started = monotonic()
                try:
                    response = await client.post(CHAT_URL, headers=_headers(), json=payload)
                except httpx.HTTPError as exc:
                    raise GroqError(f"Не удалось подключиться к Groq: {exc}") from exc

                if response.status_code == 429 and attempt < settings.GROQ_MAX_RATE_RETRIES:
                    retry_after = float(response.headers.get("retry-after", "2.1") or 2.1)
                    logger.warning("Groq rate limit reached; retrying in %.1f seconds", retry_after)
                    await asyncio.sleep(max(2.1, retry_after))
                    continue
                if _is_failed_generation(response) and attempt < settings.GROQ_MAX_RATE_RETRIES:
                    logger.warning(
                        "Groq structured generation failed; retrying (%d/%d)",
                        attempt + 1,
                        settings.GROQ_MAX_RATE_RETRIES,
                    )
                    await asyncio.sleep(1.0)
                    continue
                if response.status_code != 200:
                    raise GroqError(
                        f"Groq API вернул HTTP {response.status_code}: {_error_detail(response)[:300]}"
                    )

                try:
                    return response.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise GroqError("Groq вернул ответ без текста результата") from exc

    raise GroqError("Groq request retry loop ended unexpectedly")


async def check_connection() -> tuple[bool, str]:
    if not settings.GROQ_API_KEY:
        return False, "API-ключ не настроен"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(MODELS_URL, headers=_headers())
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}: {_error_detail(response)[:160]}"
        model_ids = {item.get("id") for item in response.json().get("data", [])}
        if settings.GROQ_MODEL not in model_ids:
            return False, f"модель {settings.GROQ_MODEL} недоступна"
        return True, f"модель доступна: {settings.GROQ_MODEL}"
    except Exception as exc:
        return False, str(exc)[:200]
