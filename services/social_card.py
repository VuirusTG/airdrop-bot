"""Generate consistent Ninja Scout 16:9 review/publishing cards."""
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
WIDTH = 1536
HEIGHT = 864
CARD_STYLE_VERSION = "ninja-scout-cyber-v4"
URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)
SCENE_BRIEFS = {
    "airdrop": "a glowing futuristic gateway, swirling energy and a mysterious project emblem in a dark cyber environment",
    "testnet": "a futuristic technology laboratory, luminous network pathways, modular architecture and a mysterious energy core",
    "quest": "a dangerous digital expedition through futuristic ruins with glowing checkpoints and a mysterious energy portal",
    "points": "ascending futuristic architecture, luminous pathways and layered milestones leading toward a powerful energy source",
    "waitlist": "a sealed luminous gateway in a dark futuristic facility, anticipation, discovery and controlled neon energy",
}
COLOR_WORDS = (
    "red", "cyan", "teal", "blue", "green", "lime", "yellow", "orange", "magenta", "violet", "white", "black", "silver", "gold",
)
ENVIRONMENT_CUES = (
    "city", "canyon", "forest", "desert", "space", "temple", "laboratory", "gateway", "network", "landscape", "architecture", "ocean", "mountains",
)

# The reference style is intentionally consistent across categories.
BG = (5, 10, 10)
INK = (244, 246, 242)
MUTED = (165, 174, 166)
ACCENT = (181, 244, 20)
ACCENT_DARK = (79, 112, 10)
PANEL = (12, 19, 18)
PANEL_2 = (20, 29, 27)

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


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, minimum: int, display: bool = False):
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
    return lines[:max_lines]


def _ascii_display(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip(" \t\r\n-|:;,.!?")


def _project_label(name: str, project_url: str | None = None) -> str:
    clean_name = _ascii_display(URL_RE.sub("", name)).lstrip("$")
    looks_like_headline = len(clean_name) > 34 or len(clean_name.split()) > 4 or bool(HEADLINE_WORDS.search(clean_name))
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


def _steps(instructions: str, limit: int = 3) -> list[str]:
    raw_steps = [part.strip() for part in re.split(r"\n+|(?=\d+\.\s)", instructions or "")]
    cleaned: list[str] = []
    for step in raw_steps:
        step = _clean_step(re.sub(r"^\d+[.)]\s*", "", step).strip())
        if step and step not in cleaned:
            cleaned.append(step)
        if len(cleaned) == limit:
            break
    return cleaned or ["Open the official project page", "Complete the required tasks", "Join the official community"]


def _background_brief(category: str, image_prompt: str | None, name: str, chain: str | None) -> str:
    scene = SCENE_BRIEFS.get(category, "an abstract futuristic gateway with layered light and architectural depth")
    prompt_lower = (image_prompt or "").lower()
    colors = [color for color in COLOR_WORDS if re.search(rf"\b{color}\b", prompt_lower)][:2]
    cues = [cue for cue in ENVIRONMENT_CUES if re.search(rf"\b{cue}\b", prompt_lower)][:2]
    details = []
    if colors:
        details.append(f"secondary accents: {', '.join(colors)}")
    if cues:
        details.append(f"environment cues: {', '.join(cues)}")
    if chain:
        details.append(f"ecosystem mood inspired by {chain}")
    extra = ". " + ". ".join(details) if details else ""
    return (
        f"Project {name}. Category: {category}. {scene}. {extra} "
        "Make the visual feel like a premium AAA cyberpunk game poster: black and charcoal base, neon lime-green lighting, "
        "masked cyber ninja on the RIGHT, black tactical outfit, subtle glowing accents, cinematic smoke, holographic grid, "
        "strong rim light, reflective ground, futuristic portal or abstract geometric project energy behind the character. "
        "Keep the LEFT side dark and relatively empty for application-rendered typography."
    )


async def _download_image(url: str | None) -> bytes | None:
    if not url or not url.startswith(("https://", "http://")):
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"}) as client:
            response = await client.get(url)
            response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            return None
        return response.content
    except Exception as exc:
        logger.warning("Could not download official image for social card: %s", exc)
        return None


def _prepare_artwork(artwork: bytes, box: tuple[int, int, int, int]) -> Image.Image:
    width = box[2] - box[0]
    height = box[3] - box[1]
    source = Image.open(BytesIO(artwork)).convert("RGB")
    fitted = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS, centering=(0.62, 0.5))
    fitted = ImageEnhance.Color(fitted).enhance(1.18)
    fitted = ImageEnhance.Contrast(fitted).enhance(1.10)
    return fitted


def _rounded_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=PANEL, outline=ACCENT, width=2, radius=20):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _render(
    output_path: Path,
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    potential_reward: str | None,
    artwork: bytes | None,
    project_url: str | None,
    generated_artwork: bool,
) -> None:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BG)

    # Full-bleed generated art, with a deliberate dark left zone for readable UI.
    if artwork:
        try:
            art = _prepare_artwork(artwork, (0, 0, WIDTH, HEIGHT))
            canvas.paste(art, (0, 0))
        except Exception:
            artwork = None

    if not artwork:
        draw = ImageDraw.Draw(canvas)
        for x in range(790, WIDTH, 80):
            draw.line((x, 0, x - 180, HEIGHT), fill=(15, 55, 30), width=2)
        for y in range(80, HEIGHT, 80):
            draw.line((790, y, WIDTH, y), fill=(12, 46, 28), width=2)

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Heavy black gradient on the left, but preserve the right-side character/art.
    for x in range(0, 900):
        alpha = int(220 * (1 - x / 980) ** 0.55)
        od.line((x, 0, x, HEIGHT), fill=(0, 5, 5, max(0, alpha)), width=1)
    od.rectangle((0, 0, 620, HEIGHT), fill=(0, 5, 5, 92))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # Accent edge and diagonal separator.
    draw.rectangle((0, 0, 10, HEIGHT), fill=ACCENT)
    draw.line((720, HEIGHT, 860, 0), fill=ACCENT, width=5)

    # Header.
    draw.text((44, 30), "NINJA SCOUT", fill=ACCENT, font=_font(22, bold=True))
    draw.text((206, 30), " /  OPPORTUNITY", fill=INK, font=_font(22, bold=True))

    ecosystem = _ascii_display(chain or "ECOSYSTEM").upper()
    draw.rounded_rectangle((1188, 26, 1488, 72), radius=6, fill=(28, 10, 18))
    eco_font = _fit_font(draw, ecosystem, 260, 20, 14)
    draw.text((1338, 49), ecosystem, fill=INK, font=eco_font, anchor="mm")

    # Project name — intentionally large like the reference.
    display_name = _project_label(name, project_url)
    name_font = _fit_font(draw, display_name, 635, 126, 54, display=True)
    name_lines = _wrap(draw, display_name, name_font, 635, 2)
    y = 88
    for line in name_lines:
        # Small shadow/offset gives the distressed poster feel without requiring an external font.
        draw.text((43, y + 5), line, fill=(20, 35, 20), font=name_font)
        draw.text((38, y), line, fill=INK, font=name_font)
        y += int(name_font.size * 0.91)

    category_label = (category or "OPPORTUNITY").upper()
    badge_font = _display_font(28)
    badge_width = min(300, draw.textbbox((0, 0), category_label, font=badge_font)[2] + 50)
    draw.polygon(((40, y + 10), (40 + badge_width, y + 10), (29 + badge_width, y + 68), (40, y + 68)), fill=ACCENT)
    draw.text((62, y + 19), category_label, fill=(4, 9, 8), font=badge_font)
    draw.text((330, y + 23), "DON'T MISS EARLY REWARDS", fill=INK, font=_font(24, bold=True))

    # Tasks panel.
    panel_top = y + 105
    _rounded_panel(draw, (38, panel_top, 685, panel_top + 300), fill=(4, 12, 11), outline=ACCENT, width=2, radius=22)
    draw.text((68, panel_top - 18), "✦  TASKS TO QUALIFY", fill=ACCENT, font=_font(27, bold=True))
    step_font = _font(22, bold=True)
    small_font = _font(19, bold=True)
    for index, step in enumerate(_steps(instructions), start=1):
        row_y = panel_top + 45 + (index - 1) * 82
        draw.rectangle((68, row_y, 138, row_y + 62), fill=ACCENT)
        draw.text((103, row_y + 31), f"0{index}", fill=(4, 9, 8), font=_display_font(27), anchor="mm")
        icon_x = 175
        draw.ellipse((icon_x, row_y + 4, icon_x + 54, row_y + 58), outline=INK, width=3)
        # Simple task glyphs.
        if index == 1:
            draw.ellipse((icon_x + 13, row_y + 17, icon_x + 41, row_y + 45), outline=INK, width=2)
            draw.line((icon_x + 27, row_y + 6, icon_x + 27, row_y + 56), fill=INK, width=2)
            draw.line((icon_x + 5, row_y + 31, icon_x + 49, row_y + 31), fill=INK, width=2)
        elif index == 2:
            draw.line((icon_x + 13, row_y + 31, icon_x + 24, row_y + 42), fill=INK, width=4)
            draw.line((icon_x + 24, row_y + 42, icon_x + 44, row_y + 18), fill=INK, width=4)
        else:
            draw.polygon(((icon_x + 12, row_y + 18), (icon_x + 44, row_y + 29), (icon_x + 16, row_y + 46)), outline=INK, fill=None)
        lines = _wrap(draw, step, step_font, 420, 2)
        for line_index, line in enumerate(lines):
            draw.text((250, row_y + 8 + line_index * 28), line, fill=INK if line_index == 0 else ACCENT, font=step_font)

    # Reward / network panel. Never invent a numeric reward.
    reward_top = panel_top + 322
    _rounded_panel(draw, (38, reward_top, 685, reward_top + 116), fill=(5, 13, 12), outline=ACCENT, width=2, radius=20)
    draw.text((68, reward_top + 22), "POTENTIAL REWARDS", fill=INK, font=_font(17, bold=True))
    reward_text = _ascii_display(potential_reward or "UNCONFIRMED").upper()
    reward_font = _fit_font(draw, reward_text, 275, 48, 22, display=True)
    draw.text((68, reward_top + 52), reward_text, fill=ACCENT, font=reward_font)
    draw.line((375, reward_top + 18, 375, reward_top + 98), fill=ACCENT_DARK, width=2)
    draw.text((410, reward_top + 22), "NETWORK", fill=INK, font=_font(17, bold=True))
    draw.text((410, reward_top + 54), _ascii_display(chain or "UNKNOWN").upper(), fill=ACCENT, font=_font(28, bold=True))

    # Footer.
    draw.rectangle((38, 792, 685, 836), outline=ACCENT, width=2)
    draw.text((60, 803), "INFO", fill=INK, font=_font(18, bold=True))
    draw.ellipse((118, 816, 126, 824), fill=ACCENT)
    draw.text((145, 803), "TASKS", fill=INK, font=_font(18, bold=True))
    draw.ellipse((211, 816, 219, 824), fill=ACCENT)
    draw.text((238, 803), "UPDATES", fill=INK, font=_font(18, bold=True))
    draw.ellipse((325, 816, 333, 824), fill=ACCENT)
    draw.text((350, 803), "VERIFY OFFICIAL LINKS", fill=ACCENT, font=_font(18, bold=True))

    # Right-side callout.
    callout = (1190, 755, 1495, 835)
    draw.rounded_rectangle(callout, radius=18, fill=(5, 10, 9), outline=ACCENT, width=2)
    draw.text((1216, 770), "EARLY USERS", fill=INK, font=_font(20, bold=True))
    draw.text((1216, 798), "GET THE EDGE", fill=ACCENT, font=_font(20, bold=True))

    # Keep the AI artwork untouched on the right; only add a subtle green grade.
    if generated_artwork:
        grade = Image.new("RGBA", (WIDTH, HEIGHT), (35, 120, 20, 18))
        mask = Image.new("L", (WIDTH, HEIGHT), 0)
        md = ImageDraw.Draw(mask)
        md.rectangle((690, 0, WIDTH, HEIGHT), fill=120)
        grade.putalpha(mask)
        canvas = Image.alpha_composite(canvas.convert("RGBA"), grade).convert("RGB")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=94, optimize=True)


async def generate_social_card(
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    official_image_url: str | None,
    image_prompt: str | None = None,
    project_url: str | None = None,
    generation_key: str | None = None,
    potential_reward: str | None = None,
) -> SocialCard | None:
    if not settings.ENABLE_SOCIAL_CARD_GENERATION:
        return None

    # Cloudflare artwork is the primary path. The local renderer is responsible for all text.
    use_cloudflare = cloudflare_configured()
    fingerprint_source = (
        f"{name}|{category}|{chain}|{instructions}|{official_image_url}|{image_prompt}|{project_url}|"
        f"{generation_key or 'initial'}|{potential_reward}|"
        f"{settings.CLOUDFLARE_IMAGE_MODEL if use_cloudflare else 'official/local'}|{CARD_STYLE_VERSION}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.SOCIAL_CARD_DIRECTORY).resolve() / f"{fingerprint}.jpg"
    if output_path.is_file():
        return SocialCard(str(output_path), "generated_social_card_cached")

    artwork = None
    source = "generated_social_card"
    generated_artwork = False

    if use_cloudflare:
        try:
            artwork = await generate_cloudflare_image(
                _background_brief(category, image_prompt, name, chain)
            )
            source = "generated_social_card_cloudflare"
            generated_artwork = True
        except Exception as exc:
            logger.warning("Cloudflare artwork failed for %s; using official image/local fallback: %s", name, exc)

    if artwork is None:
        artwork = await _download_image(official_image_url)
        source = "generated_social_card_official" if artwork else "generated_social_card_local"

    try:
        await asyncio.to_thread(
            _render,
            output_path,
            name,
            category,
            chain,
            instructions,
            potential_reward,
            artwork,
            project_url,
            generated_artwork,
        )
        return SocialCard(str(output_path), source)
    except Exception as exc:
        logger.exception("Could not generate social card for %s: %s", name, exc)
        return None
