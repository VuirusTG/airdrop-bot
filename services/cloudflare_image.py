"""Cloudflare Workers AI image generation for Ninja Scout social cards."""
from __future__ import annotations

import base64
import httpx

from config import settings


API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareImageError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _error_detail(payload: dict) -> str:
    errors = payload.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or errors[0])
    return str(payload)[:300]


async def generate_image(prompt: str) -> bytes:
    """Generate the artwork layer used by the branded 16:9 card.

    The model creates the cinematic right-side artwork (usually a cyber ninja)
    while the application itself renders all readable typography and facts.
    This prevents AI-generated text/logos from becoming distorted.
    """
    if not configured():
        raise CloudflareImageError("Cloudflare API token или Account ID не настроен")

    safe_prompt = (
        "Create a premium 16:9 cyberpunk crypto editorial artwork for a branded social-media card. "
        "The final application will place readable typography and information panels over the LEFT side, "
        "so keep the left 40 percent very dark, clean, low-detail and uncluttered. "
        "The RIGHT side must contain the main visual: a stylish masked cyber-ninja / shinobi character, "
        "dynamic three-quarter pose, black tactical clothing, subtle neon-lime accents, high-detail anime-realistic illustration, "
        "cinematic rim lighting, dramatic fog, glowing futuristic gateway or abstract project energy, holographic grid, "
        "deep blacks, neon green/lime highlights, premium game-poster quality, strong depth and sharp foreground subject. "
        f"Project visual brief: {prompt[:1800]}. "
        "Do not render any readable text, words, letters, numbers, captions, UI labels, watermarks, prices, "
        "logos, brand names, fake interfaces, coins or token symbols. "
        "A simple abstract geometric emblem in the environment is allowed, but it must not contain letters. "
        "No extra people. No collage. No split-screen. No white border. Full-bleed 16:9 artwork."
    )
    endpoint = (
        f"{API_BASE}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/"
        f"{settings.CLOUDFLARE_IMAGE_MODEL}"
    )
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                endpoint,
                headers=_headers(),
                json={
                    "prompt": safe_prompt,
                    "steps": settings.CLOUDFLARE_IMAGE_STEPS,
                },
            )
    except httpx.HTTPError as exc:
        raise CloudflareImageError(f"Не удалось подключиться к Cloudflare Workers AI: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudflareImageError(f"Cloudflare вернул HTTP {response.status_code} без JSON") from exc
    if response.status_code != 200 or not payload.get("success"):
        raise CloudflareImageError(
            f"Cloudflare Workers AI вернул HTTP {response.status_code}: {_error_detail(payload)}"
        )

    encoded = (payload.get("result") or {}).get("image")
    if not encoded:
        raise CloudflareImageError("Cloudflare не вернул изображение")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise CloudflareImageError("Cloudflare вернул повреждённое Base64-изображение") from exc


async def check_connection() -> tuple[bool, str]:
    if not configured():
        missing = []
        if not settings.CLOUDFLARE_API_TOKEN:
            missing.append("CLOUDFLARE_API_TOKEN")
        if not settings.CLOUDFLARE_ACCOUNT_ID:
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        return False, "не заполнены: " + ", ".join(missing)

    endpoint = f"{API_BASE}/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/ai/models/search"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                endpoint,
                headers=_headers(),
                params={"search": settings.CLOUDFLARE_IMAGE_MODEL.split("/")[-1], "per_page": 10},
            )
        payload = response.json()
        if response.status_code != 200 or not payload.get("success"):
            return False, f"HTTP {response.status_code}: {_error_detail(payload)}"
        return True, f"Workers AI доступен; модель: {settings.CLOUDFLARE_IMAGE_MODEL}"
    except Exception as exc:
        return False, str(exc)[:200]
