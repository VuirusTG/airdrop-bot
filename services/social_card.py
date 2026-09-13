"""Generate consistent Ninja Scout review/publishing cards matching reference design (Photo 2)."""
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

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from config import settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
FONTS_DIR = ROOT / "fonts"
TEMPLATE_PATH = ROOT / "images" / "brand" / "ninja-female-template.png"

WIDTH = 1024
HEIGHT = 682
CARD_STYLE_VERSION = "ninja-scout-cyber-v7"

URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)

# Reference palette from Image 2
COLOR_LIME = (166, 255, 0)         # #A6FF00 bright neon lime
COLOR_WHITE = (245, 248, 245)
COLOR_MUTED = (140, 160, 150)


@dataclass(frozen=True)
class SocialCard:
    path: str
    source: str = "generated_social_card"


def _get_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONTS_DIR / name
    if path.is_file():
        try:
            return ImageFont.truetype(str(path), size=size)
        except Exception:
            pass
    candidates = []
    if "impact" in name.lower():
        candidates = ["C:/Windows/Fonts/impact.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"]
    elif "bd" in name.lower() or "bold" in name.lower():
        candidates = ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    else:
        candidates = ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for c in candidates:
        if Path(c).is_file():
            try:
                return ImageFont.truetype(c, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


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


def _draw_shuriken(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int = 14, fill=COLOR_LIME):
    """4-bladed curved ninja throwing star matching Photo 2."""
    cx, cy = center
    r_outer = radius
    r_inner = radius * 0.35
    r_curve = radius * 0.65
    points = []
    for i in range(4):
        angle = i * (math.pi / 2) - math.pi / 4
        tip_x = cx + r_outer * math.cos(angle)
        tip_y = cy + r_outer * math.sin(angle)
        mid_angle = angle + 0.35
        mid_x = cx + r_curve * math.cos(mid_angle)
        mid_y = cy + r_curve * math.sin(mid_angle)
        notch_angle = angle + math.pi / 4
        notch_x = cx + r_inner * math.cos(notch_angle)
        notch_y = cy + r_inner * math.sin(notch_angle)
        points.extend([(tip_x, tip_y), (mid_x, mid_y), (notch_x, notch_y)])
    draw.polygon(points, fill=fill)
    hole_r = max(2, int(radius * 0.18))
    draw.ellipse((cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r), fill=(10, 15, 12))


def _draw_globe_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    """Clean wireframe globe with longitude and latitude lines (Step 1)."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    r = (x1 - x0) // 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=2)
    draw.ellipse((cx - r // 2, cy - r, cx + r // 2, cy + r), outline=fill, width=1)
    draw.line([(cx, cy - r), (cx, cy + r)], fill=fill, width=1)
    draw.line([(cx - r, cy), (cx + r, cy)], fill=fill, width=1)
    lat_offset = int(r * 0.5)
    lat_w = int(math.sqrt(max(0, r * r - lat_offset * lat_offset)))
    draw.line([(cx - lat_w, cy - lat_offset), (cx + lat_w, cy - lat_offset)], fill=fill, width=1)
    draw.line([(cx - lat_w, cy + lat_offset), (cx + lat_w, cy + lat_offset)], fill=fill, width=1)


def _draw_check_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    """Circle with checkmark inside (Step 2)."""
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    r = (x1 - x0) // 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=2)
    pts = [
        (cx - int(r * 0.45), cy),
        (cx - int(r * 0.1), cy + int(r * 0.4)),
        (cx + int(r * 0.45), cy - int(r * 0.35)),
    ]
    draw.line(pts, fill=fill, width=2)


def _draw_paper_plane(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    """Clean origami paper airplane pointing up-right without circle (Step 3)."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    nose = (x1, y0)
    left_wing = (x0, y0 + int(h * 0.65))
    bottom_notch = (x0 + int(w * 0.4), y1)
    center_fold = (x0 + int(w * 0.55), y0 + int(h * 0.55))

    draw.polygon([nose, left_wing, center_fold], outline=fill, fill=(255, 255, 255, 40))
    draw.polygon([nose, center_fold, bottom_notch], outline=fill, fill=(255, 255, 255, 90))
    draw.line([nose, bottom_notch], fill=fill, width=2)


def _draw_eth_3d_diamond(draw: ImageDraw.ImageDraw, center: tuple[int, int], size: int = 36):
    """Faceted 3D shaded Ethereum polygon diamond matching Photo 2."""
    cx, cy = center
    hw = size // 2
    hh_top = int(size * 0.62)
    hh_bot = int(size * 0.42)
    gap = 2

    top_v = (cx, cy - hh_top)
    mid_l = (cx - hw, cy - gap)
    mid_r = (cx + hw, cy - gap)
    mid_c = (cx, cy - gap)

    draw.polygon([top_v, mid_l, mid_c], fill=(245, 250, 248))
    draw.polygon([top_v, mid_r, mid_c], fill=(160, 175, 170))

    bot_v = (cx, cy + hh_bot)
    bot_mid_l = (cx - hw, cy + gap)
    bot_mid_r = (cx + hw, cy + gap)
    bot_mid_c = (cx, cy + gap)

    draw.polygon([bot_mid_c, bot_mid_l, bot_v], fill=(200, 215, 210))
    draw.polygon([bot_mid_c, bot_mid_r, bot_v], fill=(125, 140, 135))


def _draw_x_logo(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill=COLOR_WHITE):
    x0, y0, x1, y1 = box
    draw.line([(x0, y0), (x1, y1)], fill=fill, width=2)
    draw.line([(x1, y0), (x0, y1)], fill=fill, width=2)


def _draw_arrow(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 10, fill=COLOR_LIME):
    draw.line([(x, y + size), (x + size, y)], fill=fill, width=2)
    draw.line([(x + size, y), (x + size - 6, y)], fill=fill, width=2)
    draw.line([(x + size, y), (x + size, y + 6)], fill=fill, width=2)


def _transform_artwork(img: Image.Image, prompt_or_feedback: str | None) -> tuple[Image.Image, tuple[int, int, int]]:
    """Transform master template hue, brightness, and contrast based on feedback or cycle.
    Returns the transformed image and the matching accent color for the HUD.
    """
    fb = (prompt_or_feedback or "").lower()
    target_hue = None
    accent = COLOR_LIME

    if any(w in fb for w in ["син", "голуб", "blue", "cyan", "лазур", "azure"]):
        target_hue = 205
        accent = (0, 220, 255)      # Cyber cyan
    elif any(w in fb for w in ["фиолетов", "пурпур", "purple", "violet", "magenta"]):
        target_hue = 285
        accent = (215, 75, 255)     # Cyber violet
    elif any(w in fb for w in ["красн", "red", "crimson", "алый"]):
        target_hue = 355
        accent = (255, 55, 75)      # Cyber red
    elif any(w in fb for w in ["желт", "золот", "gold", "yellow", "янтарь", "amber"]):
        target_hue = 45
        accent = (255, 205, 10)     # Cyber gold
    elif any(w in fb for w in ["оранж", "orange"]):
        target_hue = 25
        accent = (255, 130, 10)     # Cyber orange
    elif any(w in fb for w in ["розов", "pink"]):
        target_hue = 320
        accent = (255, 90, 190)     # Cyber pink

    out = img.copy().convert("RGB")
    if target_hue is not None and abs(target_hue - 85) > 10:
        hsv = out.convert("HSV")
        h_channel, s_channel, v_channel = hsv.split()
        target_h_byte = int((target_hue / 360.0) * 255)
        h_lut = [target_h_byte if (35 <= i <= 115) else i for i in range(256)]
        h_channel = h_channel.point(h_lut)
        out = Image.merge("HSV", (h_channel, s_channel, v_channel)).convert("RGB")

    if any(w in fb for w in ["темн", "dark", "stealth"]):
        out = ImageEnhance.Brightness(out).enhance(0.85)
        out = ImageEnhance.Contrast(out).enhance(1.18)
    elif any(w in fb for w in ["ярк", "bright", "neon"]):
        out = ImageEnhance.Brightness(out).enhance(1.08)
        out = ImageEnhance.Contrast(out).enhance(1.12)

    return out, accent


def _render(
    output_path: Path,
    name: str,
    category: str,
    chain: str | None,
    instructions: str,
    potential_reward: str | None,
    artwork: Image.Image | None,
    accent: tuple[int, int, int],
    project_url: str | None,
) -> None:
    if artwork is not None:
        canvas = artwork.convert("RGBA")
    elif TEMPLATE_PATH.is_file():
        canvas = Image.open(TEMPLATE_PATH).convert("RGBA")
    else:
        canvas = Image.new("RGBA", (WIDTH, HEIGHT), (6, 10, 8, 255))

    # Clean left HUD with rich black background
    hud_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    hud_draw = ImageDraw.Draw(hud_overlay)
    for x in range(480):
        if x < 410:
            alpha = 248
        else:
            alpha = int(248 * (1.0 - (x - 410) / 70))
        hud_draw.line([(x, 0), (x, HEIGHT)], fill=(6, 10, 8, max(0, min(255, alpha))))
    canvas = Image.alpha_composite(canvas, hud_overlay)
    draw = ImageDraw.Draw(canvas)

    # 1. Project Title
    display_name = _project_label(name, project_url)
    clean_title = display_name[:24]
    if len(clean_title) <= 10:
        fsize = 72
    elif len(clean_title) <= 15:
        fsize = 58
    elif len(clean_title) <= 20:
        fsize = 46
    else:
        fsize = 38
    font_title = _get_font("impact.ttf", fsize)
    draw.text((32, 70), clean_title, fill=COLOR_WHITE, font=font_title)

    # 2. Category badge & Subheader
    cat_text = (category or "AIRDROP").upper()
    font_cat = _get_font("impact.ttf", 26)
    cat_bbox = draw.textbbox((0, 0), cat_text, font=font_cat)
    cat_w = (cat_bbox[2] - cat_bbox[0]) + 28
    cat_h = 42
    cat_top = 160

    draw.rounded_rectangle((32, cat_top, 32 + cat_w, cat_top + cat_h), radius=6, fill=accent)
    draw.text((32 + 14, cat_top + 4), cat_text, fill=(8, 12, 10), font=font_cat)

    font_sub = _get_font("arialbd.ttf", 17)
    draw.text((32 + cat_w + 14, cat_top + 10), "DON'T MISS EARLY REWARDS", fill=COLOR_WHITE, font=font_sub)

    # 3. Tasks To Qualify Header
    header_top = 224
    _draw_shuriken(draw, (44, header_top + 12), radius=12, fill=accent)
    font_head = _get_font("arialbd.ttf", 17)
    draw.text((64, header_top + 2), "TASKS TO QUALIFY", fill=accent, font=font_head)

    # 4. Tasks Box
    box_top = 252
    box_w = 405
    box_h = 205
    draw.rounded_rectangle((32, box_top, 32 + box_w, box_top + box_h), radius=16, outline=accent, width=2, fill=(8, 14, 10, 230))

    step_font_bold = _get_font("arialbd.ttf", 15)
    font_pill = _get_font("arialbd.ttf", 16)
    steps_list = _steps(instructions)

    for idx, (line1, line2) in enumerate(steps_list[:3], start=1):
        cy = box_top + 18 + (idx - 1) * 62
        # Number pill 01, 02, 03
        draw.rounded_rectangle((48, cy, 48 + 44, cy + 38), radius=8, fill=accent)
        draw.text((58, cy + 9), f"0{idx}", fill=(6, 10, 8), font=font_pill)

        # Icons
        ic_box = (112, cy + 5, 140, cy + 33)
        if idx == 1:
            _draw_globe_icon(draw, ic_box, fill=COLOR_WHITE)
        elif idx == 2:
            _draw_check_icon(draw, ic_box, fill=COLOR_WHITE)
        else:
            _draw_paper_plane(draw, ic_box, fill=COLOR_WHITE)

        # Two lines of step text
        draw.text((156, cy + 2), line1.upper()[:24], fill=COLOR_WHITE, font=step_font_bold)
        draw.text((156, cy + 20), line2.upper()[:26], fill=accent, font=step_font_bold)

    # 5. Stats Box: POTENTIAL REWARDS & NETWORK
    stats_top = box_top + box_h + 14
    stats_h = 92
    draw.rounded_rectangle((32, stats_top, 32 + box_w, stats_top + stats_h), radius=14, outline=accent, width=2, fill=(8, 14, 10, 230))
    accent_dark = (int(accent[0] * 0.45), int(accent[1] * 0.45), int(accent[2] * 0.45))
    draw.line([(230, stats_top + 12), (230, stats_top + stats_h - 12)], fill=accent_dark, width=2)

    # Left: POTENTIAL REWARDS
    font_lbl = _get_font("arialbd.ttf", 12)
    draw.text((48, stats_top + 12), "POTENTIAL REWARDS", fill=COLOR_MUTED, font=font_lbl)
    reward_val = _clean_reward(potential_reward)
    font_rew = _get_font("impact.ttf", 44)
    draw.text((48, stats_top + 32), reward_val, fill=accent, font=font_rew)

    # Right: NETWORK
    draw.text((245, stats_top + 12), "NETWORK", fill=COLOR_MUTED, font=font_lbl)
    net_name = _ascii_display(chain or "ETHEREUM").upper()[:10]
    font_net = _get_font("arialbd.ttf", 20)
    draw.text((245, stats_top + 38), net_name, fill=accent, font=font_net)
    _draw_eth_3d_diamond(draw, (390, stats_top + 48), size=36)

    # 6. Bottom Social Bar
    # 𝕏 @JanjezCrypto    ✈️ @janjezcrypto    |    LINK IN BIO ↗
    bbar_top = stats_top + stats_h + 12
    bbar_h = 36
    draw.rounded_rectangle((32, bbar_top, 32 + box_w, bbar_top + bbar_h), radius=8, outline=accent_dark, width=2, fill=(6, 12, 8, 235))
    bbar_font = _get_font("arialbd.ttf", 13)
    _draw_x_logo(draw, (47, bbar_top + 11, 59, bbar_top + 23), fill=COLOR_WHITE)
    draw.text((66, bbar_top + 9), "@JanjezCrypto", fill=COLOR_WHITE, font=bbar_font)

    _draw_paper_plane(draw, (178, bbar_top + 10, 192, bbar_top + 24), fill=COLOR_WHITE)
    draw.text((198, bbar_top + 9), "@janjezcrypto", fill=COLOR_WHITE, font=bbar_font)

    draw.line([(305, bbar_top + 8), (305, bbar_top + bbar_h - 8)], fill=accent_dark, width=1)
    draw.text((318, bbar_top + 9), "LINK IN BIO", fill=accent, font=bbar_font)
    _draw_arrow(draw, 400, bbar_top + 13, size=9, fill=accent)

    # 7. Bottom-Right Callout Badge: EARLY USERS / GET THE EDGE
    callout_w = 180
    callout_h = 54
    callout_x = 808
    callout_y = 582
    draw.rounded_rectangle((callout_x, callout_y, callout_x + callout_w, callout_y + callout_h), radius=10, outline=accent, width=2, fill=(6, 12, 8, 235))
    _draw_shuriken(draw, (callout_x + 24, callout_y + 27), radius=13, fill=accent)
    draw.text((callout_x + 48, callout_y + 10), "EARLY USERS", fill=COLOR_WHITE, font=_get_font("arialbd.ttf", 12))
    draw.text((callout_x + 48, callout_y + 27), "GET THE EDGE", fill=accent, font=_get_font("arialbd.ttf", 14))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=96, optimize=True)


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

    fingerprint_source = (
        f"{name}|{category}|{chain}|{instructions}|{image_prompt}|{project_url}|"
        f"{generation_key or 'initial'}|{potential_reward}|{CARD_STYLE_VERSION}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.SOCIAL_CARD_DIRECTORY).resolve() / f"{fingerprint}.jpg"
    if output_path.is_file():
        return SocialCard(str(output_path), "generated_social_card_cached")

    # Load and transform master artwork
    try:
        if TEMPLATE_PATH.is_file():
            base_art = Image.open(TEMPLATE_PATH)
        else:
            base_art = Image.new("RGB", (WIDTH, HEIGHT), (6, 10, 8))

        transformed_art, accent = await asyncio.to_thread(
            _transform_artwork, base_art, image_prompt
        )

        await asyncio.to_thread(
            _render,
            output_path,
            name,
            category,
            chain,
            instructions,
            potential_reward,
            transformed_art,
            accent,
            project_url,
        )
        return SocialCard(str(output_path), "generated_social_card_master")
    except Exception as exc:
        logger.exception("Could not generate social card for %s: %s", name, exc)
        return None
