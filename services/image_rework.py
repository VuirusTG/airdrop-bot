"""Detect whether reviewer feedback asks for image rework, text rework, or both."""
from __future__ import annotations

import re


IMAGE_INTENT_RE = re.compile(
    r"(?:"
    r"картин\w*|изображен\w*|фот\w*|фон\w*|задн\w*\s+план|визуал\w*|облож\w*|карточк\w*|"
    r"цвет\w*|оттен\w*|перерис\w*|девушк\w*|ниндз\w*|персонаж\w*|арт\w*|"
    r"image|picture|photo|visual|background|bg|artwork|thumbnail|character|ninja|girl|"
    r"colou?r|regenerate|social\s+card"
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

