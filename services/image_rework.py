"""Detect whether reviewer feedback asks for image rework, text rework, or both."""
from __future__ import annotations

import re


IMAGE_INTENT_RE = re.compile(
    r"(?:"
    r"картин\w*|изображен\w*|фот\w*|фон\w*|задн\w*\s+план|визуал\w*|облож\w*|карточк\w*|"
    r"цвет\w*|оттен\w*|тем\w*|стил\w*|палитр\w*|перерис\w*|девушк\w*|ниндз\w*|персонаж\w*|арт\w*|"
    r"киберпанк\w*|космос\w*|город\w*|неон\w*|"
    r"image|picture|photo|visual|background|bg|artwork|thumbnail|character|ninja|girl|"
    r"colou?r|theme|style|preset|cyberpunk|space|neon|regenerate|social\s+card"
    r")",
    re.IGNORECASE,
)

TEXT_INTENT_RE = re.compile(
    r"(?:"
    r"текст\w*|заголов\w*|шапк\w*|описан\w*|инструкц\w*|шаг\w*|пункт\w*|"
    r"наград\w*|риск\w*|твиттер\w*|пост\w*|слов\w*|перефразир\w*|сократ\w*|добав\w*|убер\w*|"
    r"text|title|summary|instruction\w*|step\w*|reward|risk|twitter|tweet|wording|rewrite|shorten"
    r")",
    re.IGNORECASE,
)


def requests_image_rework(feedback: str | None) -> bool:
    return bool(IMAGE_INTENT_RE.search(feedback or ""))


def requests_text_rework(feedback: str | None) -> bool:
    return bool(TEXT_INTENT_RE.search(feedback or ""))


def classify_rework_intent(feedback: str | None) -> str:
    """Classify user rework intent:
    - 'image_only': user only wants to change visual details (e.g. 'поменяй фон', 'сделай синий цвет')
    - 'text_only': user only wants to change text (e.g. 'измени заголовок', 'сократи описание')
    - 'both': user asks for both or general change (e.g. 'поменяй заголовок и сделай фон синим')
    """
    fb = (feedback or "").strip()
    has_image = bool(IMAGE_INTENT_RE.search(fb))
    has_text = bool(TEXT_INTENT_RE.search(fb))

    if has_image and not has_text:
        return "image_only"
    if has_text and not has_image:
        return "text_only"
    return "both"


def detect_theme_color(feedback: str | None) -> str | None:
    """Extract requested color theme if present in feedback."""
    fb = (feedback or "").lower()
    if any(w in fb for w in ["син", "голуб", "blue", "cyan", "лазур", "azure"]):
        return "cyan"
    if any(w in fb for w in ["фиолетов", "пурпур", "purple", "violet", "magenta"]):
        return "violet"
    if any(w in fb for w in ["красн", "red", "crimson", "алый"]):
        return "red"
    if any(w in fb for w in ["желт", "золот", "gold", "yellow", "янтарь", "amber"]):
        return "gold"
    if any(w in fb for w in ["оранж", "orange"]):
        return "orange"
    if any(w in fb for w in ["зелен", "лайм", "lime", "green"]):
        return "lime"
    return None


def detect_preset(feedback: str | None) -> str | None:
    """Extract preset style name if matched in feedback."""
    fb = (feedback or "").lower()
    if any(w in fb for w in ["самура", "мужик", "парен", "мужчин", "воин", "shinobi", "samurai"]):
        return "shinobi"
    if any(w in fb for w in ["город", "мегаполис", "небоскреб", "улиц", "кибер", "cyber", "cyberpunk", "city", "cybercity", "неон", "neon"]):
        return "cybercity"
    if any(w in fb for w in ["портал", "космос", "галактик", "звезд", "space", "galaxy", "portal"]):
        return "portal"
    if any(w in fb for w in ["сервер", "матриц", "терминал", "хакер", "data", "server", "matrix"]):
        return "matrix"
    if any(w in fb for w in ["девушк", "тян", "kunoichi", "girl", "ninja girl"]):
        return "kunoichi"
    return None


def build_ai_art_prompt(feedback: str, project_name: str) -> str:
    """Build English prompt for background artwork from Russian/English feedback."""
    fb = (feedback or "").strip()
    preset = detect_preset(fb)
    color = detect_theme_color(fb)
    color_hint = f" with vibrant {color} neon energy glow" if color else ""

    if preset:
        return f"{fb}. Cyberpunk {project_name} theme{color_hint}"
    return f"Atmospheric cyberpunk crypto editorial scene for {project_name}: {fb}{color_hint}"


