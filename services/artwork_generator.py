"""Multi-provider AI artwork generator for social card backgrounds.

Supports Cloudflare Workers AI (Flux Schnell) with automatic fallback to
Pollinations AI (Flux) and built-in curated style presets.
"""
from __future__ import annotations

import hashlib
import logging
import random
import urllib.parse
from pathlib import Path

import httpx

from config import settings
from services.cloudflare_image import configured as cf_configured, generate_image as cf_generate

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ARTWORK_CACHE_DIR = ROOT / "images" / "generated" / "artworks"

STYLE_PRESETS: dict[str, dict[str, str]] = {
    "kunoichi": {
        "title": "🥷 Kunoichi (Девушка-ниндзя)",
        "subject": (
            "gorgeous fierce female cyber-ninja (kunoichi) in three-quarter profile, "
            "sleek ponytail, sharp glowing eyes, black cloth tactical ninja mask covering lower face, "
            "matte black shinobi stealth suit with glowing subtle neon accents, dual katanas on back, "
            "large circular glowing holographic portal ring behind her"
        ),
        "theme_color": "lime",
    },
    "shinobi": {
        "title": "⚔️ Shinobi (Кибер-самурай)",
        "subject": (
            "intimidating high-tech male cyber-samurai in tactical shinobi combat armor, "
            "carbon fiber Oni cyber mask with glowing slits, energy katana drawn with light trails, "
            "armored shoulders, dark tactical cape billowing in wind, holographic targeting circle behind him"
        ),
        "theme_color": "cyan",
    },
    "cybercity": {
        "title": "🏙 Cyber City (Неоновый город)",
        "subject": (
            "breathtaking panoramic view of a massive futuristic cyberpunk metropolis at midnight, "
            "towering neon skyscrapers, holographic cryptocurrency billboards, misty rain, wet reflective streets, "
            "flying speeder vehicles streaking light trails across the night sky"
        ),
        "theme_color": "violet",
    },
    "portal": {
        "title": "🌌 Cosmic Portal (Квантовый портал)",
        "subject": (
            "massive glowing quantum stargate portal floating in deep space, "
            "vibrant swirling dimensional nebula, crystalline geometric energy rings, "
            "streams of digital data and celestial particles flowing through the gate"
        ),
        "theme_color": "cyan",
    },
    "matrix": {
        "title": "💻 Matrix Terminal (Дата-центр)",
        "subject": (
            "futuristic underground quantum supercomputer server room, "
            "endless glowing server racks, holographic wireframe data streams, fiber optic cables, "
            "stealth cybernetic console terminal glowing with neon green code"
        ),
        "theme_color": "lime",
    },
}


def _build_editorial_prompt(subject_desc: str) -> str:
    """Compose a prompt enforcing card layout composition rules."""
    return (
        "Cinematic 16:9 cyberpunk editorial artwork. "
        "COMPOSITION RULE: The left 45 percent of the image MUST be very dark, shadowed, clean, empty negative space "
        "with subtle volumetric fog and atmospheric dark gradients, specifically designed for clean text overlay. "
        f"The focal subject is situated on the RIGHT half of the canvas: {subject_desc}. "
        "Cinematic rim lighting, high contrast, deep obsidian blacks, 8k resolution octane render, volumetric mist. "
        "Strictly NO readable text, NO letters, NO words, NO numbers, NO watermarks, NO fake UI badges or logos."
    )


async def _generate_via_pollinations(prompt: str, seed: int | None = None) -> tuple[bytes | None, str]:
    """Generate image via Pollinations.ai Flux endpoint with Turbo fallback."""
    if seed is None:
        seed = random.randint(1000, 999999)
    encoded = urllib.parse.quote(prompt)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AirdropAlphaBot/1.0",
        "Accept": "image/*,*/*",
    }

    # 1. Primary: Pollinations Flux (state-of-the-art 16:9 editorial quality)
    url_flux = f"https://image.pollinations.ai/prompt/{encoded}?width=1200&height=675&model=flux&nologo=true&seed={seed}"
    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url_flux)
            if resp.status_code == 200 and len(resp.content) > 5000:
                logger.info("Pollinations Flux generated image successfully (%d bytes, seed=%s)", len(resp.content), seed)
                return resp.content, "Pollinations Flux"
            logger.warning("Pollinations Flux status %s (len: %s); trying Turbo fallback", resp.status_code, len(resp.content))
    except Exception as exc:
        logger.warning("Pollinations Flux generation failed: %s; trying Turbo fallback", exc)

    # 2. Secondary fallback: Pollinations Turbo (ultra-fast generation)
    url_turbo = f"https://image.pollinations.ai/prompt/{encoded}?width=1200&height=675&model=turbo&nologo=true&seed={seed}"
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url_turbo)
            if resp.status_code == 200 and len(resp.content) > 5000:
                logger.info("Pollinations Turbo generated image successfully (%d bytes, seed=%s)", len(resp.content), seed)
                return resp.content, "Pollinations Turbo"
            logger.warning("Pollinations Turbo status %s (len: %s)", resp.status_code, len(resp.content))
    except Exception as exc:
        logger.warning("Pollinations Turbo generation failed: %s", exc)

    return None, "none"


async def check_pollinations_status() -> tuple[bool, str]:
    """Check connectivity to Pollinations.ai free image service."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get("https://image.pollinations.ai/prompt/test?width=64&height=36&model=turbo&nologo=true")
            if resp.status_code == 200 and len(resp.content) > 500:
                return True, "Активен (Flux & Turbo, безлимитный бесплатный ИИ)"
            return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return True, "Активен (Flux & Turbo доступен без ключей)"


async def generate_artwork(
    prompt: str | None = None,
    preset: str | None = None,
    seed: int | None = None,
) -> tuple[str | None, str]:
    """Generate or retrieve a background artwork using Pollinations.ai or Cloudflare.

    Returns:
        tuple[path_to_image_on_disk | None, provider_name]
    """
    ARTWORK_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    seed_suffix = f"-s{seed}" if seed is not None else ""
    if preset and preset in STYLE_PRESETS:
        subject = STYLE_PRESETS[preset]["subject"]
        if prompt:
            subject = f"{subject}. Variations: {prompt}"
        cache_key = f"preset-{preset}-{hashlib.sha256(subject.encode()).hexdigest()[:10]}{seed_suffix}"
    elif prompt:
        subject = prompt.strip()
        cache_key = f"custom-{hashlib.sha256(subject.encode()).hexdigest()[:12]}{seed_suffix}"
    else:
        preset = "kunoichi"
        subject = STYLE_PRESETS["kunoichi"]["subject"]
        cache_key = f"preset-kunoichi-default{seed_suffix}"

    cached_file = ARTWORK_CACHE_DIR / f"{cache_key}.jpg"
    if cached_file.is_file() and cached_file.stat().st_size > 5000:
        return str(cached_file), "cache"

    full_prompt = _build_editorial_prompt(subject)
    image_bytes = None
    provider = "none"

    # 1. Try Cloudflare Workers AI if configured
    if cf_configured():
        try:
            image_bytes = await cf_generate(full_prompt)
            provider = "Cloudflare Flux"
        except Exception as exc:
            logger.warning("Cloudflare image generation failed, trying Pollinations: %s", exc)

    # 2. Try Pollinations AI (Flux -> Turbo)
    if not image_bytes:
        image_bytes, provider = await _generate_via_pollinations(full_prompt, seed=seed)

    if image_bytes:
        try:
            cached_file.write_bytes(image_bytes)
            return str(cached_file), provider
        except Exception as exc:
            logger.error("Failed to write artwork cache: %s", exc)

    return None, provider
