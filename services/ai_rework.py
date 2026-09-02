"""Manual rework through Groq with Gemini kept as a cloud fallback."""
from __future__ import annotations

from config import settings
from services.groq_provider import rework_draft as rework_with_groq
from services.llm_draft import DraftResult, rework_draft as rework_with_gemini


class ReworkUnavailableError(RuntimeError):
    pass


async def rework_draft(
    name: str,
    raw_text: str,
    chain: str | None,
    source_url: str | None,
    project_url: str | None,
    previous: DraftResult,
    feedback: str,
) -> tuple[DraftResult, str]:
    errors: list[str] = []

    if settings.GROQ_API_KEY:
        try:
            result = await rework_with_groq(
                name, raw_text, chain, source_url, project_url, previous, feedback
            )
            return result, "Groq"
        except Exception as exc:
            errors.append(f"Groq: {str(exc)[:240]}")
    else:
        errors.append("Groq: API-ключ не настроен")

    if settings.GEMINI_API_KEY:
        try:
            result = await rework_with_gemini(
                name, raw_text, chain, source_url, project_url, previous, feedback
            )
            return result, "Gemini (резерв)"
        except Exception as exc:
            errors.append(f"Gemini: {str(exc)[:240]}")
    else:
        errors.append("Gemini: API-ключ не настроен")

    raise ReworkUnavailableError("; ".join(errors))
