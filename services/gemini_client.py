"""Shared Gemini client with free-tier rate limiting."""
import asyncio
import logging
from time import monotonic
from typing import Any

from google import genai

from config import settings

logger = logging.getLogger(__name__)

client = genai.Client(api_key=settings.GEMINI_API_KEY) if settings.GEMINI_API_KEY else None
_request_lock = asyncio.Lock()
_last_request_started = 0.0


def _is_rate_limit_error(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 429 or "429 RESOURCE_EXHAUSTED" in str(exc)


async def generate_content(*, contents: str, config: Any):
    """Generate content while keeping all calls inside the configured free-tier pace."""
    global _last_request_started

    if client is None:
        raise RuntimeError("GEMINI_API_KEY не настроен")

    async with _request_lock:
        for attempt in range(settings.LLM_MAX_RATE_RETRIES + 1):
            elapsed = monotonic() - _last_request_started
            wait_seconds = settings.LLM_MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            _last_request_started = monotonic()
            try:
                return await client.aio.models.generate_content(
                    model=settings.LLM_MODEL,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if not _is_rate_limit_error(exc) or attempt >= settings.LLM_MAX_RATE_RETRIES:
                    raise
                logger.warning(
                    "Gemini free-tier rate limit reached; retrying in %.1f seconds",
                    settings.LLM_MIN_REQUEST_INTERVAL_SECONDS,
                )
                await asyncio.sleep(settings.LLM_MIN_REQUEST_INTERVAL_SECONDS)

    raise RuntimeError("Gemini request retry loop ended unexpectedly")
