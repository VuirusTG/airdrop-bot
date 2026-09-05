"""Render branded 16:9 social cards for Telegram and X."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from config import settings
from services.cloudflare_image import configured as cloudflare_configured
from services.cloudflare_image import generate_image as generate_cloudflare_image


logger = logging.getLogger(__name__)
WIDTH = 1200
HEIGHT = 675
CARD_STYLE_VERSION = "ninja-editorial-v4"
URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)
SCENE_BRIEFS = {
    "airdrop": "a luminous gateway opening above a deep futuristic canyon, energy particles and flowing data ribbons",
    "testnet": "a futuristic systems laboratory with interconnected light pathways, modular architecture and visible depth",
    "quest": "a bold digital expedition route through a geometric landscape with illuminated checkpoints",
    "points": "ascending pathways of light through abstract architecture with clear forward motion and layered milestones",
    "waitlist": "a sealed luminous gateway inside minimal futuristic architecture, anticipation and discovery",
}
COLOR_WORDS = (
    "red",
    "cyan",
    "teal",
    "blue",
    "green",
    "lime",
    "yellow",
    "orange",
    "magenta",
    "violet",
    "white",
    "black",
    "silver",
    "gold",
)
ENVIRONMENT_CUES = (
    "city",
    "canyon",
    "forest",
    "desert",
    "space",
    "temple",
    "laboratory",
    "gateway",
    "network",
    "landscape",
    "architecture",
    "ocean",
    "mountains",
)

PALETTES = {
    "airdrop": ((245, 241, 232), (22, 24, 24), (220, 55, 46), (255, 255, 255)),
    "testnet": ((17, 20, 25), (244, 246, 248), (48, 201, 176), (30, 35, 43)),
    "quest": ((225, 247, 70), (18, 25, 31), (235, 62, 52), (245, 247, 239)),
    "points": ((48, 13, 23), (255, 244, 238), (255, 111, 57), (82, 26, 39)),
    "waitlist": ((239, 243, 250), (17, 25, 39), (54, 105, 214), (255, 255, 255)),
}

FONT_BOLD = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
FONT_DISPLAY = (
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
)
FONT_REGULAR = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@dataclass(frozen=True)
class SocialCard:
    path: str
    source: str = "generated_social_card"


def _font(size: int, bold: bool = False):
    for candidate in FONT_BOLD if bold else FONT_REGULAR:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _display_font(size: int):
    for candidate in FONT_DISPLAY:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return _font(size, bold=True)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start: int,
    minimum: int,
    display: bool = False,
):
    for size in range(start, minimum - 1, -2):
        font = _display_font(size) if display else _font(size, bold=True)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _display_font(minimum) if display else _font(minimum, bold=True)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    words = re.sub(r"\s+", " ", text).strip().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines:
        lines[-1] = lines[-1].rstrip(".,;:")
    return lines


def _ascii_display(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip(" \t\r\n-|:;,.!?")


def _project_label(name: str, project_url: str | None = None) -> str:
    clean_name = _ascii_display(URL_RE.sub("", name)).lstrip("$")
    looks_like_headline = (
        len(clean_name) > 34
        or len(clean_name.split()) > 4
        or bool(HEADLINE_WORDS.search(clean_name))
    )
    if project_url and looks_like_headline:
        host = urlparse(project_url).hostname or ""
        labels = [label for label in host.lower().split(".") if label and label != "www"]
        if len(labels) >= 2:
            domain_label = labels[-2]
            if domain_label not in {"twitter", "x", "t", "medium", "telegram", "linktr"}:
                return _ascii_display(domain_label).upper()
    ticker = re.search(r"\$([A-Za-z][A-Za-z0-9]{1,11})", name or "")
    if ticker:
        return ticker.group(1).upper()
    return (clean_name or "NEW OPPORTUNITY").upper()


def _clean_step(step: str) -> str:
    had_url = bool(URL_RE.search(step))
    cleaned = URL_RE.sub("", step)
    cleaned = _ascii_display(cleaned)
    cleaned = re.sub(r"\s+([:;,.])", r"\1", cleaned).rstrip(" :;,-")
    if had_url and re.search(r"\b(?:visit|open|go to)\b", cleaned, re.IGNORECASE):
        return "Open the official project page"
    return cleaned


def _background_brief(category: str, image_prompt: str | None) -> str:
    scene = SCENE_BRIEFS.get(category, "an abstract futuristic gateway with layered light and architectural depth")
    prompt_lower = (image_prompt or "").lower()
    colors = [color for color in COLOR_WORDS if re.search(rf"\b{color}\b", prompt_lower)][:2]
    cues = [cue for cue in ENVIRONMENT_CUES if re.search(rf"\b{cue}\b", prompt_lower)][:2]
    palette = f" Palette accents: {', '.join(colors)}." if colors else ""
    environment = f" Additional environment cues: {', '.join(cues)}." if cues else ""
    return (
        f"Science-fiction editorial environment: {scene}. Dynamic cinematic perspective, premium poster lighting, "
        f"clean focal area for a foreground character.{palette}{environment} Natural and abstract forms only."
    )


def _steps(instructions: str, limit: int = 3) -> list[str]:
    raw_steps = [part.strip() for part in re.split(r"\n+|(?=\d+\.\s)", instructions or "")]
    cleaned: list[str] = []
    for step in raw_steps:
        step = _clean_step(re.sub(r"^\d+[.)]\s*", "", step).strip())
        if step and step not in cleaned:
            cleaned.append(step)
        if len(cleaned) == limit:
            break
    return cleaned or ["Review the official page", "Verify campaign requirements"]


async def _download_image(url: str | None) -> bytes | None:
    if not url or not url.startswith(("https://", "http://")):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            return None
        return response.content
    except Exception as exc:
        logger.warning("Could not download official image for social card: %s", exc)
        return None


def _draw_fallback_art(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    accent,
    ink,
    name: str,
) -> None:
    left, top, right, bottom = box
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    for radius, width in ((185, 4), (135, 3), (85, 2)):
        draw.ellipse(
            (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
            outline=accent,
            width=width,
        )
    initials = "".join(word[0] for word in name.split()[:2]).upper() or "?"
    font = _fit_font(draw, initials, 150, 92, 48, display=True)
    draw.text((center_x, center_y), initials, fill=ink, font=font, anchor="mm")


def _paste_mascot(canvas: Image.Image, mascot_path: str, accent) -> bool:
    path = Path(mascot_path).resolve()
    if not path.is_file():
        logger.warning("Social card mascot is missing: %s", path)
        return False
    try:
        mascot = Image.open(path).convert("RGBA")
        mascot.thumbnail((435, 625), Image.Resampling.LANCZOS)
        x = WIDTH - mascot.width - 18
        y = HEIGHT - mascot.height + 8

        alpha = mascot.getchannel("A")
        glow_alpha = alpha.filter(ImageFilter.GaussianBlur(10)).point(
            lambda value: value * 110 // 255
        )
        glow_color = Image.new("RGBA", mascot.size, (*accent, 0))
        glow_color.putalpha(glow_alpha)
        canvas.alpha_composite(glow_color, (x, y))
        canvas.alpha_composite(mascot, (x, y))
        return True
    except Exception as exc:
        logger.warning("Could not compose ninja mascot: %s", exc)
        return False


def _render(
    output_path: Path,
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    official_image: bytes | None,
    project_url: str | None,
) -> None:
    background, ink, accent, panel = PALETTES.get(category, PALETTES["waitlist"])
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (*background, 255))

    # Full-bleed visual first. Official project artwork is preferred; AI art is only
    # used when no usable official image was found. Keep the visual crisp instead of
    # blurring it into an indistinct background.
    if official_image:
        try:
            source = Image.open(BytesIO(official_image)).convert("RGB")
            fitted = ImageOps.fit(source, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
            fitted = ImageEnhance.Color(fitted).enhance(1.05)
            fitted = ImageEnhance.Contrast(fitted).enhance(1.04)
            canvas.paste(fitted, (0, 0))
        except Exception:
            official_image = None

    if not official_image:
        # Clean deterministic fallback: dark gradient + branded mascot, rather than
        # random low-quality shapes or fake project imagery.
        for x in range(WIDTH):
            ratio = x / max(WIDTH - 1, 1)
            r = int(background[0] * (1 - ratio) + panel[0] * ratio)
            g = int(background[1] * (1 - ratio) + panel[1] * ratio)
            b = int(background[2] * (1 - ratio) + panel[2] * ratio)
            ImageDraw.Draw(canvas).line((x, 0, x, HEIGHT), fill=(r, g, b, 255))
        _paste_mascot(canvas, settings.SOCIAL_CARD_MASCOT_PATH, accent)

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    # Strong left-to-right readability gradient.
    for x in range(760):
        alpha = int(215 * (1 - x / 760))
        overlay_draw.line((x, 0, x, HEIGHT), fill=(8, 10, 14, alpha))
    overlay_draw.rectangle((0, 0, 760, HEIGHT), fill=(8, 10, 14, 82))
    canvas.alpha_composite(overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle((0, 0, 12, HEIGHT), fill=accent)
    draw.text((52, 38), "NINJA SCOUT  •  OPPORTUNITY", fill=(245, 245, 245), font=_font(18, bold=True))

    ecosystem = _ascii_display(chain or "ECOSYSTEM TBD").upper()
    ecosystem_font = _fit_font(draw, ecosystem, 190, 18, 14)
    badge_x = WIDTH - 230
    draw.rounded_rectangle((badge_x, 28, WIDTH - 28, 68), radius=20, fill=(8, 10, 14, 205))
    draw.text(((badge_x + WIDTH - 28) // 2, 48), ecosystem, fill=(245, 245, 245), font=ecosystem_font, anchor="mm")

    display_name = _project_label(name, project_url)
    name_font = _fit_font(draw, display_name, 650, 94, 46, display=True)
    name_lines = _wrap(draw, display_name, name_font, 650, 2)
    y = 112
    for line in name_lines:
        draw.text((55, y + 4), line, fill=(0, 0, 0, 160), font=name_font)
        draw.text((51, y), line, fill=(250, 250, 250), font=name_font)
        y += int(name_font.size * 0.9)

    category_label = (category or "opportunity").upper()
    badge_font = _font(19, bold=True)
    badge_width = draw.textbbox((0, 0), category_label, font=badge_font)[2] + 32
    badge_y = min(y + 12, 330)
    draw.rounded_rectangle((54, badge_y, 54 + badge_width, badge_y + 38), radius=18, fill=accent)
    draw.text((70, badge_y + 8), category_label, fill=(10, 12, 16), font=badge_font)

    # Three short verified-looking actions; never let long AI text take over the card.
    step_y = badge_y + 64
    step_font = _font(17, bold=True)
    for index, step in enumerate(_steps(instructions), start=1):
        if index > 3:
            break
        node_y = step_y + (index - 1) * 58
        draw.rounded_rectangle((54, node_y, 690, node_y + 47), radius=10, fill=(8, 10, 14, 190))
        draw.rounded_rectangle((54, node_y, 101, node_y + 47), radius=10, fill=accent)
        draw.text((77, node_y + 23), f"{index:02d}", fill=(8, 10, 14), font=_font(16, bold=True), anchor="mm")
        lines = _wrap(draw, step, step_font, 560, 1)
        if lines:
            draw.text((120, node_y + 12), lines[0], fill=(248, 248, 248), font=step_font)

    draw.rounded_rectangle((54, 590, 690, 635), radius=10, fill=(8, 10, 14, 185))
    draw.text((74, 603), "REWARDS UNCONFIRMED  •  VERIFY OFFICIAL LINKS", fill=accent, font=_font(13, bold=True))

    draw.rounded_rectangle((WIDTH - 430, HEIGHT - 58, WIDTH - 24, HEIGHT - 20), radius=18, fill=accent)
    draw.text((WIDTH - 227, HEIGHT - 39), "CHECK  •  VERIFY  •  PARTICIPATE", fill=(8, 10, 14), font=_font(15, bold=True), anchor="mm")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=95, optimize=True, progressive=True)


async def generate_social_card(
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    official_image_url: str | None,
    image_prompt: str | None = None,
    project_url: str | None = None,
    generation_key: str | None = None,
) -> SocialCard | None:
    if not settings.ENABLE_SOCIAL_CARD_GENERATION:
        return None
    # Official project artwork is the default. AI artwork is a fallback for projects
    # without a usable official image; explicit regeneration can still create new art.
    use_cloudflare = cloudflare_configured() and bool(image_prompt) and not official_image_url
    fingerprint_source = (
        f"{name}|{category}|{chain}|{instructions}|{official_image_url}|{image_prompt}|{project_url}|"
        f"{generation_key or 'initial'}|"
        f"{settings.CLOUDFLARE_IMAGE_MODEL if use_cloudflare else 'local'}|"
        f"{settings.SOCIAL_CARD_MASCOT_PATH}|{CARD_STYLE_VERSION}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.SOCIAL_CARD_DIRECTORY).resolve() / f"{fingerprint}.jpg"
    if output_path.is_file() and not use_cloudflare:
        return SocialCard(str(output_path))

    artwork = None
    source = "generated_social_card"
    if official_image_url:
        artwork = await _download_image(official_image_url)
        if artwork is not None:
            source = "social_card_official_image"
    if artwork is None and use_cloudflare:
        try:
            artwork = await generate_cloudflare_image(_background_brief(category, image_prompt))
            source = "generated_social_card_cloudflare"
        except Exception as exc:
            logger.warning("Cloudflare artwork failed for %s; using deterministic local card: %s", name, exc)
    try:
        await asyncio.to_thread(
            _render,
            output_path,
            name,
            category,
            chain,
            instructions,
            artwork,
            project_url,
        )
        return SocialCard(str(output_path), source)
    except Exception as exc:
        logger.exception("Could not generate social card for %s: %s", name, exc)
        return None
