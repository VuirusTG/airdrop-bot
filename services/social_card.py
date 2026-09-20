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
CARD_STYLE_VERSION = "ninja-scout-cyber-v9"

URL_RE = re.compile(r"https?://[^\s)\]}>,]+", re.IGNORECASE)
HEADLINE_WORDS = re.compile(
    r"\b(?:airdrop|claim|opens?|launch(?:es|ed)?|tomorrow|today|live|alert|reward|campaign)\b",
    re.IGNORECASE,
)

# Reference palette from Image 2
COLOR_LIME = (166, 255, 0)         # #A6FF00 bright neon lime
COLOR_WHITE = (245, 248, 245)
COLOR_MUTED = (140, 160, 150)

THEME_COLORS: dict[str, tuple[int, int, int]] = {
    "lime": (166, 255, 0),      # Neon lime (default)
    "cyan": (0, 220, 255),      # Cyber cyan
    "violet": (215, 75, 255),   # Cyber violet
    "gold": (255, 205, 10),     # Cyber gold
    "red": (255, 55, 75),       # Crimson red
    "orange": (255, 130, 10),   # Neon orange
}


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


def _render_supersampled(draw_fn, size: int = 32, scale: int = 4, **kwargs) -> Image.Image:
    large_size = size * scale
    img = Image.new("RGBA", (large_size, large_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_fn(draw, large_size, **kwargs)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _draw_globe_hires(draw: ImageDraw.ImageDraw, s: int, color=(245, 248, 245, 255)):
    """Crisp wireframe globe with latitude and longitude lines (Step 1)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.44)
    w = max(2, int(s * 0.055))
    w_thin = max(1, int(s * 0.038))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=w)
    draw.line([(cx, cy - r), (cx, cy + r)], fill=color, width=w_thin)
    draw.ellipse((cx - int(r * 0.5), cy - r, cx + int(r * 0.5), cy + r), outline=color, width=w_thin)
    draw.line([(cx - r, cy), (cx + r, cy)], fill=color, width=w_thin)
    lat_off = int(r * 0.5)
    lat_w = int(math.sqrt(max(0, r * r - lat_off * lat_off)))
    draw.line([(cx - lat_w, cy - lat_off), (cx + lat_w, cy - lat_off)], fill=color, width=w_thin)
    draw.line([(cx - lat_w, cy + lat_off), (cx + lat_w, cy + lat_off)], fill=color, width=w_thin)


def _draw_check_hires(draw: ImageDraw.ImageDraw, s: int, color=(245, 248, 245, 255)):
    """Crisp circle with checkmark inside (Step 2)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.44)
    w = max(2, int(s * 0.055))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=w)
    pts = [
        (cx - int(r * 0.48), cy + int(r * 0.02)),
        (cx - int(r * 0.12), cy + int(r * 0.40)),
        (cx + int(r * 0.46), cy - int(r * 0.36)),
    ]
    draw.line(pts, fill=color, width=int(s * 0.075), joint="curve")


def _draw_origami_plane_hires(draw: ImageDraw.ImageDraw, s: int, color=(245, 248, 245, 255)):
    """Crisp origami paper airplane pointing up-right with inverted V-notch matching Photo 2 (Step 3)."""
    w = max(2, int(s * 0.065))
    nose = (int(s * 0.82), int(s * 0.16))
    left_tip = (int(s * 0.14), int(s * 0.48))
    left_tail = (int(s * 0.40), int(s * 0.86))
    center_notch = (int(s * 0.47), int(s * 0.62))
    right_tail = (int(s * 0.70), int(s * 0.86))

    pts = [nose, left_tip, left_tail, center_notch, right_tail, nose]
    draw.line(pts, fill=color, width=w, joint="curve")
    draw.line([nose, center_notch], fill=color, width=w)


def _get_official_x_icon(size: int = 18, color=(245, 248, 245)) -> Image.Image:
    """Official Twitter/X mathematical double-struck mark."""
    s = size * 4
    sc = s / 24.0
    mask = Image.new("L", (s, s), 0)
    mdraw = ImageDraw.Draw(mask)
    outer = [
        (18.244 * sc, 2.25 * sc),
        (21.552 * sc, 2.25 * sc),
        (14.325 * sc, 10.51 * sc),
        (22.827 * sc, 21.75 * sc),
        (16.170 * sc, 21.75 * sc),
        (10.956 * sc, 14.933 * sc),
        (4.990 * sc, 21.75 * sc),
        (1.680 * sc, 21.75 * sc),
        (9.410 * sc, 12.915 * sc),
        (1.254 * sc, 2.25 * sc),
        (8.080 * sc, 2.25 * sc),
        (12.793 * sc, 8.481 * sc),
    ]
    mdraw.polygon(outer, fill=255)
    inner = [
        (17.083 * sc, 19.77 * sc),
        (18.916 * sc, 19.77 * sc),
        (7.084 * sc, 4.126 * sc),
        (5.117 * sc, 4.126 * sc),
    ]
    mdraw.polygon(inner, fill=0)
    x_img = Image.new("RGBA", (s, s), (*color, 255))
    x_img.putalpha(mask)
    return x_img.resize((size, size), Image.Resampling.LANCZOS)


def _get_official_tg_icon(size: int = 18) -> Image.Image:
    """Official Telegram circular blue badge with flying paper airplane silhouette."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(36, 161, 222, 255))
    sc = (s * 0.58) / 24.0
    ox = cx - int(12 * sc) - int(s * 0.02)
    oy = cy - int(12 * sc)

    body = [
        (ox + 21.0 * sc, oy + 3.8 * sc),
        (ox + 3.2 * sc, oy + 10.8 * sc),
        (ox + 8.2 * sc, oy + 13.2 * sc),
        (ox + 18.0 * sc, oy + 6.8 * sc),
    ]
    draw.polygon(body, fill=(255, 255, 255, 255))

    flap = [
        (ox + 8.2 * sc, oy + 13.2 * sc),
        (ox + 10.2 * sc, oy + 18.8 * sc),
        (ox + 13.2 * sc, oy + 15.8 * sc),
    ]
    draw.polygon(flap, fill=(210, 235, 250, 255))

    fold = [
        (ox + 21.0 * sc, oy + 3.8 * sc),
        (ox + 8.2 * sc, oy + 13.2 * sc),
        (ox + 13.2 * sc, oy + 15.8 * sc),
        (ox + 19.2 * sc, oy + 17.5 * sc),
    ]
    draw.polygon(fold, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _draw_eth_3d_hires(draw: ImageDraw.ImageDraw, s: int):
    """Faceted 3D shaded Ethereum polygon diamond matching Photo 2."""
    cx, cy = s // 2, s // 2
    hw = int(s * 0.38)
    hh_top = int(s * 0.46)
    hh_bot = int(s * 0.36)
    gap = max(1, int(s * 0.03))

    top_v = (cx, cy - hh_top)
    mid_l = (cx - hw, cy - gap)
    mid_r = (cx + hw, cy - gap)
    mid_c = (cx, cy - gap)
    draw.polygon([top_v, mid_l, mid_c], fill=(250, 255, 252, 255))
    draw.polygon([top_v, mid_r, mid_c], fill=(165, 185, 175, 255))

    bot_v = (cx, cy + hh_bot)
    bot_mid_l = (cx - hw, cy + gap)
    bot_mid_r = (cx + hw, cy + gap)
    bot_mid_c = (cx, cy + gap)
    draw.polygon([bot_mid_c, bot_mid_l, bot_v], fill=(205, 225, 215, 255))
    draw.polygon([bot_mid_c, bot_mid_r, bot_v], fill=(130, 150, 140, 255))


def _draw_solana_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Solana 3-bar gradient logo."""
    cx, cy = s // 2, s // 2
    w = int(s * 0.72)
    h = int(s * 0.16)
    skew = int(s * 0.16)
    y1 = int(s * 0.22)
    draw.polygon([(cx - w//2 + skew, y1), (cx + w//2, y1), (cx + w//2 - skew, y1 + h), (cx - w//2, y1 + h)], fill=(0, 255, 163, 255))
    y2 = int(s * 0.44)
    draw.polygon([(cx + w//2 - skew, y2), (cx - w//2, y2), (cx - w//2 + skew, y2 + h), (cx + w//2, y2 + h)], fill=(3, 225, 255, 255))
    y3 = int(s * 0.66)
    draw.polygon([(cx - w//2 + skew, y3), (cx + w//2, y3), (cx + w//2 - skew, y3 + h), (cx - w//2, y3 + h)], fill=(220, 31, 255, 255))


def _draw_bnb_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official BNB diamond cross logo in gold."""
    cx, cy = s // 2, s // 2
    gold = (243, 186, 47, 255)
    cd = int(s * 0.16)
    draw.polygon([(cx, cy - cd), (cx + cd, cy), (cx, cy + cd), (cx - cd, cy)], fill=gold)
    dist = int(s * 0.32)
    sd = int(s * 0.12)
    draw.polygon([(cx, cy - dist - sd), (cx + sd, cy - dist), (cx, cy - dist + sd), (cx - sd, cy - dist)], fill=gold)
    draw.polygon([(cx, cy + dist - sd), (cx + sd, cy + dist), (cx, cy + dist + sd), (cx - sd, cy + dist)], fill=gold)
    draw.polygon([(cx - dist, cy - sd), (cx - dist + sd, cy), (cx - dist, cy + sd), (cx - dist - sd, cy)], fill=gold)
    draw.polygon([(cx + dist, cy - sd), (cx + dist + sd, cy), (cx + dist, cy + sd), (cx + dist - sd, cy)], fill=gold)


def _draw_btc_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Bitcoin ₿ logo in gold token."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.44)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(247, 147, 26, 255))
    font = _get_font("arialbd.ttf", int(s * 0.55))
    draw.text((cx, cy - int(s * 0.02)), "₿", fill=(255, 255, 255, 255), font=font, anchor="mm")


def _draw_base_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Base logo (Coinbase blue token with white mark)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 82, 255, 255))
    r_in = int(s * 0.28)
    draw.ellipse((cx - r_in, cy - r_in, cx + r_in, cy + r_in), fill=(255, 255, 255, 255))
    draw.rectangle((cx, cy - int(s * 0.09), cx + r_in + 2, cy + int(s * 0.09)), fill=(0, 82, 255, 255))


def _draw_arbitrum_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Arbitrum logo (navy token with geometric white & cyan 'A')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(18, 44, 76, 255))
    draw.polygon([
        (cx - int(s * 0.30), cy + int(s * 0.26)),
        (cx - int(s * 0.16), cy + int(s * 0.26)),
        (cx - int(s * 0.02), cy - int(s * 0.26)),
        (cx - int(s * 0.16), cy - int(s * 0.26)),
    ], fill=(40, 160, 240, 255))
    draw.polygon([
        (cx + int(s * 0.30), cy + int(s * 0.26)),
        (cx + int(s * 0.16), cy + int(s * 0.26)),
        (cx + int(s * 0.02), cy - int(s * 0.26)),
        (cx + int(s * 0.16), cy - int(s * 0.26)),
    ], fill=(255, 255, 255, 255))
    draw.polygon([
        (cx - int(s * 0.08), cy + int(s * 0.05)),
        (cx + int(s * 0.08), cy + int(s * 0.05)),
        (cx, cy - int(s * 0.10)),
    ], fill=(40, 160, 240, 255))


def _draw_polygon_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Polygon logo (purple token with white geometric loop)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(130, 71, 229, 255))
    w = max(2, int(s * 0.065))
    pts = [
        (cx - int(s * 0.24), cy - int(s * 0.02)),
        (cx - int(s * 0.12), cy - int(s * 0.18)),
        (cx + int(s * 0.04), cy - int(s * 0.18)),
        (cx + int(s * 0.16), cy - int(s * 0.02)),
        (cx + int(s * 0.04), cy + int(s * 0.16)),
        (cx - int(s * 0.04), cy + int(s * 0.16)),
        (cx - int(s * 0.16), cy + int(s * 0.02)),
    ]
    draw.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
    draw.line([
        (cx + int(s * 0.16), cy - int(s * 0.02)),
        (cx + int(s * 0.24), cy + int(s * 0.08)),
        (cx + int(s * 0.16), cy + int(s * 0.20)),
        (cx + int(s * 0.04), cy + int(s * 0.16)),
    ], fill=(255, 255, 255, 255), width=w, joint="curve")


def _draw_optimism_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Optimism logo (red token with white 'OP')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 4, 32, 255))
    font = _get_font("arialbd.ttf", int(s * 0.42))
    draw.text((cx, cy - int(s * 0.02)), "OP", fill=(255, 255, 255, 255), font=font, anchor="mm")


def _draw_zksync_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official zkSync logo (dark token with white and blue chevrons)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(16, 20, 36, 255))
    draw.polygon([
        (cx - int(s * 0.22), cy - int(s * 0.22)),
        (cx - int(s * 0.08), cy - int(s * 0.22)),
        (cx + int(s * 0.04), cy),
        (cx - int(s * 0.08), cy + int(s * 0.22)),
        (cx - int(s * 0.22), cy + int(s * 0.22)),
        (cx - int(s * 0.10), cy),
    ], fill=(255, 255, 255, 255))
    draw.polygon([
        (cx - int(s * 0.04), cy - int(s * 0.22)),
        (cx + int(s * 0.10), cy - int(s * 0.22)),
        (cx + int(s * 0.22), cy),
        (cx + int(s * 0.10), cy + int(s * 0.22)),
        (cx - int(s * 0.04), cy + int(s * 0.22)),
        (cx + int(s * 0.08), cy),
    ], fill=(68, 98, 245, 255))


def _draw_ton_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official TON logo (cyan token with faceted diamond crystal)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 152, 234, 255))
    top_y = cy - int(s * 0.22)
    bot_y = cy + int(s * 0.24)
    mid_y = cy - int(s * 0.04)
    w_out = int(s * 0.24)
    pts = [(cx, top_y), (cx + w_out, mid_y), (cx, bot_y), (cx - w_out, mid_y)]
    draw.polygon(pts, fill=(255, 255, 255, 255))
    draw.polygon([(cx, top_y), (cx + w_out, mid_y), (cx, bot_y)], fill=(215, 240, 255, 255))
    draw.line([(cx, top_y), (cx, bot_y)], fill=(0, 152, 234, 255), width=max(2, int(s * 0.03)))


def _draw_sui_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Sui logo (blue token with white droplet)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(42, 130, 228, 255))
    pts = [
        (cx, cy - int(s * 0.26)),
        (cx + int(s * 0.20), cy + int(s * 0.10)),
        (cx, cy + int(s * 0.24)),
        (cx - int(s * 0.20), cy + int(s * 0.10)),
    ]
    draw.polygon(pts, fill=(255, 255, 255, 255))
    pts_in = [
        (cx, cy - int(s * 0.12)),
        (cx + int(s * 0.10), cy + int(s * 0.08)),
        (cx, cy + int(s * 0.16)),
        (cx - int(s * 0.10), cy + int(s * 0.08)),
    ]
    draw.polygon(pts_in, fill=(42, 130, 228, 255))


def _draw_avax_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Avalanche logo (red token with white split 'A')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(232, 65, 66, 255))
    draw.polygon([
        (cx + int(s * 0.02), cy - int(s * 0.24)),
        (cx + int(s * 0.26), cy + int(s * 0.22)),
        (cx - int(s * 0.06), cy + int(s * 0.22)),
    ], fill=(255, 255, 255, 255))
    draw.polygon([
        (cx - int(s * 0.16), cy - int(s * 0.04)),
        (cx - int(s * 0.04), cy + int(s * 0.22)),
        (cx - int(s * 0.26), cy + int(s * 0.22)),
    ], fill=(255, 255, 255, 255))


def _draw_aptos_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Aptos logo (dark token with horizontal white bars)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(20, 25, 24, 255))
    w = int(s * 0.54)
    bh = int(s * 0.08)
    draw.rectangle((cx - w//2, cy - int(s * 0.18), cx + w//2, cy - int(s * 0.18) + bh), fill=(255, 255, 255, 255))
    draw.rectangle((cx - w//2, cy - bh//2, cx + w//2, cy + bh//2), fill=(255, 255, 255, 255))
    draw.rectangle((cx - w//2, cy + int(s * 0.18) - bh, cx + w//2, cy + int(s * 0.18)), fill=(255, 255, 255, 255))


def _draw_scroll_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Scroll logo (warm parchment token with scroll swirl)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 236, 209, 255))
    sw = max(2, int(s * 0.06))
    pts = [
        (cx - int(s * 0.20), cy - int(s * 0.12)),
        (cx + int(s * 0.12), cy - int(s * 0.12)),
        (cx + int(s * 0.18), cy),
        (cx + int(s * 0.12), cy + int(s * 0.12)),
        (cx - int(s * 0.14), cy + int(s * 0.12)),
        (cx - int(s * 0.18), cy),
        (cx - int(s * 0.12), cy - int(s * 0.04)),
        (cx + int(s * 0.06), cy - int(s * 0.04)),
    ]
    draw.line(pts, fill=(210, 115, 30, 255), width=sw, joint="curve")


def _draw_linea_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Linea logo (dark token with cyan 'L')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(18, 20, 24, 255))
    lw = max(2, int(s * 0.08))
    draw.line([
        (cx - int(s * 0.16), cy - int(s * 0.20)),
        (cx - int(s * 0.16), cy + int(s * 0.18)),
        (cx + int(s * 0.18), cy + int(s * 0.18)),
    ], fill=(97, 218, 251, 255), width=lw)


def _draw_near_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Near logo (black token with green 'N')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(0, 0, 0, 255))
    font = _get_font("arialbd.ttf", int(s * 0.55))
    draw.text((cx, cy - int(s * 0.02)), "N", fill=(0, 236, 151, 255), font=font, anchor="mm")


def _draw_tron_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Tron logo (red token with white geometric prism)."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(235, 0, 41, 255))
    pts = [
        (cx, cy - int(s * 0.24)),
        (cx + int(s * 0.22), cy - int(s * 0.08)),
        (cx, cy + int(s * 0.24)),
        (cx - int(s * 0.22), cy - int(s * 0.08)),
    ]
    draw.polygon(pts, outline=(255, 255, 255, 255), width=max(2, int(s * 0.04)), fill=None)
    draw.line([(cx, cy - int(s * 0.24)), (cx, cy + int(s * 0.24))], fill=(255, 255, 255, 255), width=max(2, int(s * 0.03)))


def _draw_monad_hires(draw: ImageDraw.ImageDraw, s: int):
    """Official Monad logo (purple token with 'M')."""
    cx, cy = s // 2, s // 2
    r = int(s * 0.46)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(131, 96, 246, 255))
    font = _get_font("impact.ttf", int(s * 0.52))
    draw.text((cx, cy - int(s * 0.02)), "M", fill=(255, 255, 255, 255), font=font, anchor="mm")


def _detect_chain(chain: str | None, name: str = "", instructions: str = "") -> str:
    """Detect or normalize chain name from explicit field or keywords in name/steps."""
    raw = (chain or "").strip()
    if raw and raw.upper() not in {"EVM", "CRYPTO", "MULTICHAIN", "TESTNET", "MAINNET", "BLOCKCHAIN", ""}:
        return raw

    combined = f"{name} {instructions}".upper()
    candidates = [
        ("BASE", "BASE"),
        ("ARBITRUM", "ARBITRUM"),
        ("ARB", "ARBITRUM"),
        ("SOLANA", "SOLANA"),
        ("SOL", "SOLANA"),
        ("POLYGON", "POLYGON"),
        ("MATIC", "POLYGON"),
        ("OPTIMISM", "OPTIMISM"),
        ("OP MAINNET", "OPTIMISM"),
        ("ZKSYNC", "ZKSYNC"),
        ("BNB", "BNB CHAIN"),
        ("BSC", "BNB CHAIN"),
        ("BINANCE", "BNB CHAIN"),
        ("BITCOIN", "BITCOIN"),
        ("BTC", "BITCOIN"),
        ("RUNES", "BITCOIN"),
        ("TON", "TON"),
        ("SUI", "SUI"),
        ("AVALANCHE", "AVALANCHE"),
        ("AVAX", "AVALANCHE"),
        ("APTOS", "APTOS"),
        ("SCROLL", "SCROLL"),
        ("LINEA", "LINEA"),
        ("NEAR", "NEAR"),
        ("TRON", "TRON"),
        ("MONAD", "MONAD"),
    ]
    for keyword, chain_label in candidates:
        if re.search(rf"\b{re.escape(keyword)}\b", combined):
            return chain_label
    return raw or "ETHEREUM"


def _get_chain_icon(chain: str | None, size: int = 36) -> Image.Image:
    """Dynamically return the matching high-resolution official chain icon."""
    c = (chain or "").upper().strip()
    
    # Specific L2s and alternative L1s FIRST to prevent generic Ethereum match
    if any(k in c for k in ["BASE", "COINBASE"]):
        return _render_supersampled(_draw_base_hires, size=size)
    elif any(k in c for k in ["ARBITRUM", "ARB"]):
        return _render_supersampled(_draw_arbitrum_hires, size=size)
    elif any(k in c for k in ["POLYGON", "MATIC", "POL"]):
        return _render_supersampled(_draw_polygon_hires, size=size)
    elif any(k in c for k in ["OPTIMISM", "OP MAINNET", "OPTIMISTIC"]):
        return _render_supersampled(_draw_optimism_hires, size=size)
    elif any(k in c for k in ["ZKSYNC", "ERA"]):
        return _render_supersampled(_draw_zksync_hires, size=size)
    elif any(k in c for k in ["SCROLL"]):
        return _render_supersampled(_draw_scroll_hires, size=size)
    elif any(k in c for k in ["LINEA"]):
        return _render_supersampled(_draw_linea_hires, size=size)
    elif any(k in c for k in ["SOL", "SOLANA"]):
        return _render_supersampled(_draw_solana_hires, size=size)
    elif any(k in c for k in ["BNB", "BSC", "BINANCE"]):
        return _render_supersampled(_draw_bnb_hires, size=size)
    elif any(k in c for k in ["BTC", "BITCOIN", "RUNES", "BRC", "ORDINALS"]):
        return _render_supersampled(_draw_btc_hires, size=size)
    elif any(k in c for k in ["TON", "THE OPEN NETWORK"]):
        return _render_supersampled(_draw_ton_hires, size=size)
    elif any(k in c for k in ["SUI"]):
        return _render_supersampled(_draw_sui_hires, size=size)
    elif any(k in c for k in ["AVAX", "AVALANCHE"]):
        return _render_supersampled(_draw_avax_hires, size=size)
    elif any(k in c for k in ["APTOS", "APT"]):
        return _render_supersampled(_draw_aptos_hires, size=size)
    elif any(k in c for k in ["NEAR"]):
        return _render_supersampled(_draw_near_hires, size=size)
    elif any(k in c for k in ["TRON", "TRX"]):
        return _render_supersampled(_draw_tron_hires, size=size)
    elif any(k in c for k in ["MONAD"]):
        return _render_supersampled(_draw_monad_hires, size=size)
    else:
        # Default: Ethereum 3D diamond
        return _render_supersampled(_draw_eth_3d_hires, size=size)


def _draw_arrow(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 10, fill=COLOR_LIME):
    draw.line([(x, y + size), (x + size, y)], fill=fill, width=2)
    draw.line([(x + size, y), (x + size - 6, y)], fill=fill, width=2)
    draw.line([(x + size, y), (x + size, y + 6)], fill=fill, width=2)


def _transform_artwork(
    img: Image.Image,
    prompt_or_feedback: str | None,
    theme_color: str | None = None,
) -> tuple[Image.Image, tuple[int, int, int]]:
    """Transform master template hue, brightness, and contrast based on feedback, theme, or cycle.
    Returns the transformed image and the matching accent color for the HUD.
    """
    fb = (prompt_or_feedback or "").lower()
    target_hue = None
    accent = COLOR_LIME

    if theme_color and theme_color.lower() in THEME_COLORS:
        tc = theme_color.lower()
        accent = THEME_COLORS[tc]
        hue_map = {"cyan": 205, "violet": 285, "red": 355, "gold": 45, "orange": 25, "lime": 85}
        target_hue = hue_map.get(tc)
    elif any(w in fb for w in ["син", "голуб", "blue", "cyan", "лазур", "azure"]):
        target_hue = 205
        accent = THEME_COLORS["cyan"]
    elif any(w in fb for w in ["фиолетов", "пурпур", "purple", "violet", "magenta"]):
        target_hue = 285
        accent = THEME_COLORS["violet"]
    elif any(w in fb for w in ["красн", "red", "crimson", "алый"]):
        target_hue = 355
        accent = THEME_COLORS["red"]
    elif any(w in fb for w in ["желт", "золот", "gold", "yellow", "янтарь", "amber"]):
        target_hue = 45
        accent = THEME_COLORS["gold"]
    elif any(w in fb for w in ["оранж", "orange"]):
        target_hue = 25
        accent = THEME_COLORS["orange"]
    elif any(w in fb for w in ["розов", "pink"]):
        target_hue = 320
        accent = (255, 90, 190)

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
    custom_steps: list[str] | None = None,
) -> None:
    if artwork is not None:
        if artwork.size != (WIDTH, HEIGHT):
            canvas = ImageOps.fit(artwork, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS).convert("RGBA")
        else:
            canvas = artwork.convert("RGBA")
    elif TEMPLATE_PATH.is_file():
        canvas = Image.open(TEMPLATE_PATH).convert("RGBA")
    else:
        canvas = Image.new("RGBA", (WIDTH, HEIGHT), (6, 10, 8, 255))

    # Soft ambient vignette for HUD area (subtle gradient, not hard solid black)
    hud_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    hud_draw = ImageDraw.Draw(hud_overlay)
    for x in range(480):
        if x < 350:
            alpha = 85
        else:
            alpha = int(85 * (1.0 - (x - 350) / 130))
        hud_draw.line([(x, 0), (x, HEIGHT)], fill=(4, 8, 6, max(0, min(255, alpha))))
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
    font_pill = _get_font("arialbd.ttf", 18)
    if custom_steps:
        steps_list = []
        for step in custom_steps:
            cleaned = re.sub(r"^\d+[.)]\s*", "", step).strip()
            if cleaned:
                l1, l2 = _format_step_lines(cleaned)
                if l1:
                    steps_list.append((l1, l2))
        while len(steps_list) < 3:
            defaults = [
                ("VISIT THE OFFICIAL", "PROJECT PAGE"),
                ("COMPLETE TASKS", "& FOLLOW RULES"),
                ("JOIN COMMUNITY", "& STAY ACTIVE"),
            ]
            steps_list.append(defaults[len(steps_list)])
    else:
        steps_list = _steps(instructions)

    # Pre-render step icons with supersampling
    ic_globe = _render_supersampled(_draw_globe_hires, size=28)
    ic_check = _render_supersampled(_draw_check_hires, size=28)
    ic_plane = _render_supersampled(_draw_origami_plane_hires, size=28)

    for idx, (line1, line2) in enumerate(steps_list[:3], start=1):
        cy = box_top + 16 + (idx - 1) * 62
        # Number pill 01, 02, 03: 42x42 square-ish rounded pill
        px0, py0 = 48, cy
        px1, py1 = 48 + 42, cy + 42
        draw.rounded_rectangle((px0, py0, px1, py1), radius=6, fill=accent)
        # PERFECT CENTERING via anchor="mm"
        cx_pill = (px0 + px1) / 2
        cy_pill = (py0 + py1) / 2
        draw.text((cx_pill, cy_pill), f"0{idx}", fill=(6, 10, 8), font=font_pill, anchor="mm")

        # Step Icon
        if idx == 1:
            ic = ic_globe
        elif idx == 2:
            ic = ic_check
        else:
            ic = ic_plane
        canvas.paste(ic, (110, cy + 7), ic)

        # Two lines of step text
        draw.text((156, cy + 3), line1.upper()[:24], fill=COLOR_WHITE, font=step_font_bold)
        draw.text((156, cy + 22), line2.upper()[:26], fill=accent, font=step_font_bold)

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
    draw.text((248, stats_top + 12), "NETWORK", fill=COLOR_MUTED, font=font_lbl)
    detected_chain = _detect_chain(chain, name, instructions)
    net_name = _ascii_display(detected_chain).upper()[:10]
    font_net = _get_font("arialbd.ttf", 20 if len(net_name) <= 8 else 17)
    draw.text((248, stats_top + 40), net_name, fill=accent, font=font_net)
    
    # Dynamic Chain Icon
    chain_ic = _get_chain_icon(detected_chain, size=36)
    canvas.paste(chain_ic, (384, stats_top + 28), chain_ic)

    # 6. Bottom Social Bar
    # 𝕏 @JanjezCrypto    ✈️ @janjezcrypto    |    LINK IN BIO ↗
    bbar_top = stats_top + stats_h + 12
    bbar_h = 36
    draw.rounded_rectangle((32, bbar_top, 32 + box_w, bbar_top + bbar_h), radius=8, outline=accent_dark, width=2, fill=(6, 12, 8, 235))
    bbar_font = _get_font("arialbd.ttf", 13)

    # Authentic 𝕏 logo
    x_ic = _get_official_x_icon(size=16)
    canvas.paste(x_ic, (48, bbar_top + 10), x_ic)
    draw.text((70, bbar_top + 9), "@JanjezCrypto", fill=COLOR_WHITE, font=bbar_font)

    # Authentic Telegram logo
    tg_ic = _get_official_tg_icon(size=18)
    canvas.paste(tg_ic, (180, bbar_top + 9), tg_ic)
    draw.text((204, bbar_top + 9), "@janjezcrypto", fill=COLOR_WHITE, font=bbar_font)

    # Link in bio
    draw.line([(308, bbar_top + 8), (308, bbar_top + bbar_h - 8)], fill=accent_dark, width=1)
    draw.text((320, bbar_top + 9), "LINK IN BIO", fill=accent, font=bbar_font)
    _draw_arrow(draw, 402, bbar_top + 13, size=9, fill=accent)

    # 7. Bottom-Right Callout Badge: EARLY USERS / GET THE EDGE
    callout_w = 186
    callout_h = 54
    callout_x = 804
    callout_y = 596
    draw.rounded_rectangle((callout_x, callout_y, callout_x + callout_w, callout_y + callout_h), radius=10, outline=accent, width=2, fill=(8, 12, 10, 255))
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
    custom_artwork_path: str | None = None,
    theme_color: str | None = None,
    custom_steps: list[str] | None = None,
) -> SocialCard | None:
    if not settings.ENABLE_SOCIAL_CARD_GENERATION:
        return None

    steps_key = "|".join(custom_steps) if custom_steps else "std"
    fingerprint_source = (
        f"{name}|{category}|{chain}|{instructions}|{image_prompt}|{project_url}|"
        f"{generation_key or 'initial'}|{potential_reward}|{custom_artwork_path or ''}|"
        f"{theme_color or ''}|{steps_key}|{CARD_STYLE_VERSION}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.SOCIAL_CARD_DIRECTORY).resolve() / f"{fingerprint}.jpg"
    if output_path.is_file():
        return SocialCard(str(output_path), "generated_social_card_cached")

    # Load and transform master or custom artwork
    try:
        if custom_artwork_path and Path(custom_artwork_path).is_file():
            base_art = Image.open(custom_artwork_path)
        elif TEMPLATE_PATH.is_file():
            base_art = Image.open(TEMPLATE_PATH)
        else:
            base_art = Image.new("RGB", (WIDTH, HEIGHT), (6, 10, 8))

        transformed_art, accent = await asyncio.to_thread(
            _transform_artwork, base_art, image_prompt, theme_color
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
            custom_steps,
        )
        return SocialCard(str(output_path), "generated_social_card_master")
    except Exception as exc:
        logger.exception("Could not generate social card for %s: %s", name, exc)
        return None


async def render_social_card_from_content(content: Any) -> SocialCard | None:
    """Deterministic social card renderer directly from DraftContent.

    Reads title, category, network, tasks, reward, artwork, and theme_color.
    Never calls AI image models. Rerenders pure typography, HUD panels, and icons
    over the selected artwork layer.
    """
    from services.draft_content import DraftContent
    if not isinstance(content, DraftContent):
        return None

    art_meta = content.artwork
    custom_art = art_meta.custom_artwork_path or (art_meta.path if (art_meta.path and not art_meta.path.endswith((".png", ".jpg"))) else None)
    if custom_art and not Path(custom_art).is_file():
        custom_art = None

    return await generate_social_card(
        name=content.title,
        category=content.category,
        chain=content.network,
        instructions=content.render_instructions_text(),
        official_image_url=None,
        image_prompt=art_meta.prompt,
        project_url=content.project_link,
        generation_key=f"content-{art_meta.preset or 'std'}-{art_meta.theme_color}",
        potential_reward=content.potential_reward,
        custom_artwork_path=custom_art,
        theme_color=art_meta.theme_color,
        custom_steps=content.tasks,
    )
