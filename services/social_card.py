"""Generate consistent Ninja Scout review/publishing cards matching reference design."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from config import settings
from services.cloudflare_image import configured as cloudflare_configured
from services.cloudflare_image import generate_image as generate_cloudflare_image

logger = logging.getLogger(__name__)

WIDTH = 1024
HEIGHT = 682
CARD_STYLE_VERSION = "ninja-scout-cyber-v6"

URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)

# Reference palette from Image 2
COLOR_BG = (6, 10, 8, 255)
COLOR_LIME = (166, 255, 0)         # #A6FF00 bright neon lime
COLOR_LIME_DARK = (70, 115, 0)
COLOR_WHITE = (245, 248, 245)
COLOR_MUTED = (160, 175, 165)
COLOR_PANEL_BG = (8, 14, 10, 225)  # Translucent dark card

FONT_BOLD = (
    "C:/Windows/Fonts/arialbd.ttf",
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
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _display_font(size: int):
    for candidate in FONT_DISPLAY:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                pass
    return _font(size, bold=True)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start: int, minimum: int, display: bool = False):
    for size in range(start, minimum - 1, -2):
        font = _display_font(size) if display else _font(size, bold=True)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            if bbox[2] - bbox[0] <= max_width:
                return font
        except Exception:
            pass
    return _display_font(minimum) if display else _font(minimum, bold=True)


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
    return (clean_name or "AIRDROP SCOUT").upper()


def _clean_step(step: str) -> str:
    cleaned = URL_RE.sub("", step)
    cleaned = _ascii_display(cleaned)
    cleaned = re.sub(r"\s+([:;,.])", r"\1", cleaned).rstrip(" :;,-")
    return cleaned


def _format_step_lines(step: str) -> tuple[str, str]:
    cleaned = _clean_step(step).strip()
    words = cleaned.split()
    if len(words) <= 3:
        return " ".join(words).upper(), ""
    mid = min(len(words) // 2 + 1, 3)
    return " ".join(words[:mid]).upper(), " ".join(words[mid:6]).upper()


def _steps(instructions: str) -> list[tuple[str, str]]:
    raw_steps = [part.strip() for part in re.split(r"\n+|(?=\d+\.\s)", instructions or "")]
    results: list[tuple[str, str]] = []
    for step in raw_steps:
        cleaned = re.sub(r"^\d+[.)]\s*", "", step).strip()
        if cleaned:
            l1, l2 = _format_step_lines(cleaned)
            if l1:
                results.append((l1, l2))
        if len(results) == 3:
            break

    defaults = [
        ("VISIT THE OFFICIAL", "PROJECT PAGE"),
        ("COMPLETE TASKS", "& FOLLOW RULES"),
        ("JOIN COMMUNITY", "& STAY ACTIVE"),
    ]
    while len(results) < 3:
        results.append(defaults[len(results)])
    return results


def _clean_reward(potential_reward: str | None) -> str:
    if not potential_reward:
        return "$2500+"
    text = _ascii_display(potential_reward).strip()
    match = re.search(r"(\$?\d[\d,]*(?:\.\d+)?(?:\s*(?:USDC|USDT|USD|POINTS|PTS|STAR))?\+?)", text, re.IGNORECASE)
    if match and len(match.group(1)) <= 14:
        val = match.group(1).upper()
        return val if "$" in val or " " in val else f"${val}"
    if len(text) <= 12 and not any(neg in text.lower() for neg in ("no ", "unconfirmed", "not ", "none")):
        return text.upper()
    return "$2500+"


def _draw_ninja_star(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, fill=COLOR_LIME):
    cx, cy = center
    r_in = radius * 0.38
    points = []
    for i in range(8):
        angle = i * math.pi / 4
        r = radius if i % 2 == 0 else r_in
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(points, fill=fill)


def _draw_eth_diamond(draw: ImageDraw.ImageDraw, center: tuple[int, int], size: int, fill=COLOR_WHITE):
    cx, cy = center
    h = size
    w = int(size * 0.62)
    # Top triangle
    draw.polygon([(cx, cy - h // 2), (cx + w // 2, cy), (cx, cy + h // 6), (cx - w // 2, cy)], fill=(210, 215, 210))
    # Top shading
    draw.polygon([(cx, cy - h // 2), (cx, cy + h // 6), (cx - w // 2, cy)], fill=(155, 160, 155))
    # Bottom triangle
    draw.polygon([(cx, cy + h // 4), (cx + w // 2, cy + h // 10), (cx, cy + h // 2), (cx - w // 2, cy + h // 10)], fill=(185, 190, 185))


def _draw_x_logo(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    x1, y1, x2, y2 = box
    draw.line([(x1, y1), (x2, y2)], fill=fill, width=2)
    draw.line([(x2, y1), (x1, y2)], fill=fill, width=2)


def _draw_tg_plane(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1
    pts = [
        (x1 + int(w * 0.95), y1 + int(h * 0.08)),
        (x1 + int(w * 0.05), y1 + int(h * 0.55)),
        (x1 + int(w * 0.38), y1 + int(h * 0.70)),
        (x1 + int(w * 0.55), y1 + int(h * 0.95)),
    ]
    draw.polygon(pts, fill=fill)
    draw.line([(x1 + int(w * 0.95), y1 + int(h * 0.08)), (x1 + int(w * 0.38), y1 + int(h * 0.70))], fill=(8, 14, 10), width=1)


def _draw_arrow(draw: ImageDraw.ImageDraw, x: int, y: int, fill=COLOR_LIME):
    draw.line([(x, y + 9), (x + 9, y)], fill=fill, width=2)
    draw.line([(x + 2, y), (x + 9, y)], fill=fill, width=2)
    draw.line([(x + 9, y), (x + 9, y + 7)], fill=fill, width=2)


def _local_ninja_artwork(prompt: str | None = None) -> Image.Image | None:
    template_path = Path(__file__).resolve().parents[1] / "images" / "brand" / "ninja-female-template.png"
    if template_path.is_file():
        im = Image.open(template_path).convert("RGBA")
        if prompt:
            p_lower = prompt.lower()
            if any(k in p_lower for k in ("синий", "голуб", "cyan", "blue", "teal")):
                tint = Image.new("RGBA", im.size, (0, 100, 200, 35))
                im = Image.alpha_composite(im, tint)
            elif any(k in p_lower for k in ("красн", "red", "crimson")):
                tint = Image.new("RGBA", im.size, (200, 30, 30, 40))
                im = Image.alpha_composite(im, tint)
            elif any(k in p_lower for k in ("фиолетов", "пурпур", "purple", "violet")):
                tint = Image.new("RGBA", im.size, (150, 20, 200, 35))
                im = Image.alpha_composite(im, tint)
            elif any(k in p_lower for k in ("желт", "золот", "gold", "yellow", "amber")):
                tint = Image.new("RGBA", im.size, (200, 160, 20, 35))
                im = Image.alpha_composite(im, tint)
        return im.convert("RGB")

    mascot_path = Path(__file__).resolve().parents[1] / "images" / "brand" / "ninja-mascot.png"
    if mascot_path.is_file():
        return Image.open(mascot_path).convert("RGB")
    return None


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
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), COLOR_BG)

    if artwork:
        try:
            art = Image.open(BytesIO(artwork)).convert("RGBA")
            art_fitted = ImageOps.fit(art, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS, centering=(0.75, 0.5))
            canvas.paste(art_fitted, (0, 0))
        except Exception:
            pass

    # Apply smooth dark gradient to left side (x=0 to 560) so text is always high-contrast
    hud_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    hud_draw = ImageDraw.Draw(hud_overlay)
    for x in range(0, 560):
        if x < 420:
            alpha = 255
        else:
            alpha = int(255 * (1 - (x - 420) / 140))
        hud_draw.line([(x, 0), (x, HEIGHT)], fill=(7, 12, 10, alpha))
    hud_draw.rectangle([(800, 580), (1015, 675)], fill=(8, 14, 11, 255))
    canvas = Image.alpha_composite(canvas, hud_overlay)

    draw = ImageDraw.Draw(canvas)

    # 1. Main Project Title
    display_name = _project_label(name, project_url)
    title_font = _fit_font(draw, display_name, 390, 68, 36, display=True)
    draw.text((43, 38), display_name, fill=(2, 4, 3), font=title_font)
    draw.text((40, 35), display_name, fill=COLOR_WHITE, font=title_font)

    # 2. Category Badge & Subtitle
    badge_y = 122
    cat_text = (category or "AIRDROP").upper()
    cat_font = _font(22, bold=True)
    badge_w = int(draw.textlength(cat_text, font=cat_font)) + 26
    badge_h = 34
    draw.rounded_rectangle((40, badge_y, 40 + badge_w, badge_y + badge_h), radius=5, fill=COLOR_LIME)
    draw.text((40 + badge_w // 2, badge_y + badge_h // 2), cat_text, fill=(5, 10, 5), font=cat_font, anchor="mm")

    sub_font = _font(18, bold=True)
    draw.text((40 + badge_w + 18, badge_y + 7), "DON'T MISS EARLY REWARDS", fill=COLOR_WHITE, font=sub_font)

    # 3. Tasks To Qualify Header
    tasks_y = 172
    _draw_ninja_star(draw, (52, tasks_y + 11), 10, fill=COLOR_LIME)
    draw.text((70, tasks_y), "TASKS TO QUALIFY", fill=COLOR_LIME, font=_font(18, bold=True))

    # 4. Tasks Box
    box_top = 198
    box_w = 400
    box_h = 216
    draw.rounded_rectangle((40, box_top, 40 + box_w, box_top + box_h), radius=14, outline=COLOR_LIME, width=2, fill=(8, 14, 10, 225))

    row_y = box_top + 16
    row_gap = 66
    step_font_bold = _font(13, bold=True)
    step_num_font = _font(16, bold=True)

    steps_list = _steps(instructions)
    for idx, (line1, line2) in enumerate(steps_list[:3], start=1):
        cy = row_y + (idx - 1) * row_gap
        # Number pill [ 01 ]
        draw.rounded_rectangle((56, cy, 56 + 36, cy + 36), radius=5, fill=COLOR_LIME)
        draw.text((56 + 18, cy + 18), f"0{idx}", fill=(5, 10, 5), font=step_num_font, anchor="mm")

        # Icon circle
        ic_x = 108
        draw.ellipse((ic_x, cy + 1, ic_x + 34, cy + 35), outline=COLOR_WHITE, width=2)
        if idx == 1:
            draw.ellipse((ic_x + 8, cy + 9, ic_x + 26, cy + 27), outline=COLOR_WHITE, width=1)
            draw.line([(ic_x + 17, cy + 2), (ic_x + 17, cy + 34)], fill=COLOR_WHITE, width=1)
            draw.line([(ic_x + 2, cy + 18), (ic_x + 32, cy + 18)], fill=COLOR_WHITE, width=1)
        elif idx == 2:
            draw.line([(ic_x + 8, cy + 18), (ic_x + 15, cy + 25)], fill=COLOR_WHITE, width=2)
            draw.line([(ic_x + 15, cy + 25), (ic_x + 27, cy + 11)], fill=COLOR_WHITE, width=2)
        else:
            _draw_tg_plane(draw, (ic_x + 7, cy + 7, ic_x + 27, cy + 27), fill=COLOR_WHITE)

        draw.text((156, cy + 2), line1.upper()[:24], fill=COLOR_WHITE, font=step_font_bold)
        draw.text((156, cy + 18), line2.upper()[:26], fill=COLOR_LIME, font=step_font_bold)

    # 5. POTENTIAL REWARDS & NETWORK Box
    stats_top = box_top + box_h + 14
    stats_h = 92
    draw.rounded_rectangle((40, stats_top, 40 + box_w, stats_top + stats_h), radius=14, outline=COLOR_LIME, width=2, fill=(8, 14, 10, 225))
    draw.line([(240, stats_top + 12), (240, stats_top + stats_h - 12)], fill=COLOR_LIME_DARK, width=2)

    # Left: POTENTIAL REWARDS
    draw.text((58, stats_top + 12), "POTENTIAL REWARDS", fill=COLOR_MUTED, font=_font(12, bold=True))
    reward_val = _clean_reward(potential_reward)
    rew_font = _fit_font(draw, reward_val, 165, 38, 22, display=True)
    draw.text((58, stats_top + 34), reward_val, fill=COLOR_LIME, font=rew_font)

    # Right: NETWORK
    draw.text((260, stats_top + 12), "NETWORK", fill=COLOR_MUTED, font=_font(12, bold=True))
    net_name = _ascii_display(chain or "ETHEREUM").upper()[:12]
    net_font = _fit_font(draw, net_name, 115, 22, 14, display=True)
    draw.text((260, stats_top + 38), net_name, fill=COLOR_LIME, font=net_font)
    _draw_eth_diamond(draw, (395, stats_top + 48), 32, fill=COLOR_WHITE)

    # 6. Bottom Social Bar:
    # 𝕏 @JanjezCrypto    ✈️ @janjezcrypto    |    LINK IN BIO ↗
    bbar_top = stats_top + stats_h + 12
    bbar_h = 36
    draw.rounded_rectangle((40, bbar_top, 40 + box_w, bbar_top + bbar_h), radius=8, outline=COLOR_LIME_DARK, width=2, fill=(6, 12, 8, 230))
    bbar_font = _font(13, bold=True)
    _draw_x_logo(draw, (55, bbar_top + 11, 68, bbar_top + 24), fill=COLOR_WHITE)
    draw.text((75, bbar_top + 10), "@JanjezCrypto", fill=COLOR_WHITE, font=bbar_font)

    _draw_tg_plane(draw, (185, bbar_top + 11, 198, bbar_top + 24), fill=COLOR_WHITE)
    draw.text((205, bbar_top + 10), "@janjezcrypto", fill=COLOR_WHITE, font=bbar_font)

    draw.line([(310, bbar_top + 8), (310, bbar_top + bbar_h - 8)], fill=COLOR_LIME_DARK, width=1)
    draw.text((322, bbar_top + 10), "LINK IN BIO", fill=COLOR_LIME, font=bbar_font)
    _draw_arrow(draw, 408, bbar_top + 14, fill=COLOR_LIME)

    # 7. Bottom-Right Callout Badge: EARLY USERS / GET THE EDGE
    callout_w = 175
    callout_h = 52
    callout_x = 815
    callout_y = 590
    draw.rounded_rectangle((callout_x, callout_y, callout_x + callout_w, callout_y + callout_h), radius=10, outline=COLOR_LIME, width=2, fill=(6, 12, 8, 235))
    _draw_ninja_star(draw, (callout_x + 26, callout_y + 26), 14, fill=COLOR_LIME)
    draw.text((callout_x + 50, callout_y + 9), "EARLY USERS", fill=COLOR_WHITE, font=_font(12, bold=True))
    draw.text((callout_x + 50, callout_y + 26), "GET THE EDGE", fill=COLOR_LIME, font=_font(14, bold=True))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=95, optimize=True)


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
            brief = f"Cyber ninja kunoichi, neon energy portal, ecosystem {chain or 'crypto'}, {image_prompt or ''}"
            artwork = await generate_cloudflare_image(brief)
            source = "generated_social_card_cloudflare"
            generated_artwork = True
        except Exception as exc:
            logger.warning("Cloudflare artwork failed for %s; using local master fallback: %s", name, exc)

    if artwork is None:
        local_art = await asyncio.to_thread(_local_ninja_artwork, image_prompt)
        if local_art is not None:
            buffer = BytesIO()
            local_art.save(buffer, format="PNG")
            artwork = buffer.getvalue()
            source = "generated_social_card_local_ninja"
        else:
            artwork = None
            source = "generated_social_card_local"

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
