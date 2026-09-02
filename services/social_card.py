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
CARD_STYLE_VERSION = "ninja-editorial-v3"
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
    draw = ImageDraw.Draw(canvas)

    art_region = (535, 0, WIDTH, HEIGHT)
    if official_image:
        try:
            source = Image.open(BytesIO(official_image)).convert("RGB")
            fitted = ImageOps.fit(source, (art_region[2] - art_region[0], HEIGHT))
            fitted = ImageEnhance.Color(fitted).enhance(1.12)
            fitted = ImageEnhance.Contrast(fitted).enhance(1.08)
            fitted = fitted.filter(ImageFilter.GaussianBlur(1.4))
            canvas.paste(fitted, (art_region[0], 0))
            tint = Image.new("RGBA", (art_region[2] - art_region[0], HEIGHT), (*ink, 42))
            canvas.alpha_composite(tint, (art_region[0], 0))
        except Exception:
            _draw_fallback_art(draw, art_region, accent, ink, name)
    else:
        _draw_fallback_art(draw, art_region, accent, ink, name)

    draw.polygon(((0, 0), (650, 0), (535, HEIGHT), (0, HEIGHT)), fill=background)
    draw.rectangle((0, 0, 18, HEIGHT), fill=accent)
    draw.line((535, HEIGHT, 650, 0), fill=accent, width=5)
    draw.text((62, 42), "NINJA SCOUT  /  OPPORTUNITY", fill=accent, font=_font(20, bold=True))
    ecosystem = _ascii_display(chain or "ECOSYSTEM TBD").upper()
    ecosystem_font = _fit_font(draw, ecosystem, 170, 18, 14)
    draw.rectangle((930, 24, 1142, 62), fill=panel)
    draw.text((1036, 43), ecosystem, fill=ink, font=ecosystem_font, anchor="mm")

    display_name = _project_label(name, project_url)
    name_font = _fit_font(draw, display_name, 465, 96, 50, display=True)
    name_lines = _wrap(draw, display_name, name_font, 465, 2)
    y = 86
    for line in name_lines:
        draw.text((67, y + 4), line, fill=accent, font=name_font)
        draw.text((62, y), line, fill=ink, font=name_font)
        y += int(name_font.size * 0.92)

    category_label = category.upper() if category else "OPPORTUNITY"
    badge_font = _display_font(25)
    badge_width = draw.textbbox((0, 0), category_label, font=badge_font)[2] + 36
    badge_y = y + 12
    draw.polygon(
        (
            (62, badge_y),
            (62 + badge_width, badge_y),
            (52 + badge_width, badge_y + 43),
            (62, badge_y + 43),
        ),
        fill=accent,
    )
    draw.text((80, badge_y + 7), category_label, fill=background, font=badge_font)

    step_y = badge_y + 82
    step_font = _font(18, bold=True)
    number_font = _display_font(24)
    for index, step in enumerate(_steps(instructions), start=1):
        node_y = step_y + (index - 1) * 76
        draw.line((84, node_y - 18, 84, node_y), fill=accent, width=3)
        draw.rectangle((62, node_y, 505, node_y + 61), fill=panel)
        draw.rectangle((62, node_y, 108, node_y + 61), fill=accent)
        draw.text((85, node_y + 30), f"0{index}", fill=background, font=number_font, anchor="mm")
        lines = _wrap(draw, step, step_font, 370, 2)
        for line_index, line in enumerate(lines):
            draw.text((122, node_y + 8 + line_index * 22), line, fill=ink, font=step_font)

    draw.rectangle((62, 612, 520, 644), fill=panel)
    draw.text(
        (78, 619),
        "REWARDS UNCONFIRMED  /  VERIFY OFFICIAL LINKS",
        fill=accent,
        font=_font(14, bold=True),
    )

    mascot_visible = _paste_mascot(canvas, settings.SOCIAL_CARD_MASCOT_PATH, accent)
    draw = ImageDraw.Draw(canvas)
    if not mascot_visible:
        draw.text((895, 330), "NINJA SCOUT", fill=accent, font=_display_font(54), anchor="mm")
    draw.rectangle((720, 617, WIDTH, HEIGHT), fill=accent)
    draw.text(
        (960, 646),
        "CHECK  /  VERIFY  /  PARTICIPATE",
        fill=background,
        font=_font(18, bold=True),
        anchor="mm",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=93, optimize=True)


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
    use_cloudflare = cloudflare_configured() and bool(image_prompt)
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
    if use_cloudflare:
        try:
            artwork = await generate_cloudflare_image(_background_brief(category, image_prompt))
            source = "generated_social_card_cloudflare"
        except Exception as exc:
            logger.warning("Cloudflare artwork failed for %s; using free local fallback: %s", name, exc)
    if artwork is None:
        artwork = await _download_image(official_image_url)
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
