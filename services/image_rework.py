"""Detect whether reviewer feedback explicitly asks for a new image."""
from __future__ import annotations

import re


IMAGE_INTENT_RE = re.compile(
    r"(?:"
    r"картин\w*|изображен\w*|фот\w*|фон\w*|визуал\w*|облож\w*|карточк\w*|"
    r"цвет\w*|перерис\w*|image|picture|photo|visual|background|artwork|thumbnail|"
    r"colou?r|regenerate|social\s+card"
    r")",
    re.IGNORECASE,
)


def requests_image_rework(feedback: str | None) -> bool:
    return bool(IMAGE_INTENT_RE.search(feedback or ""))
