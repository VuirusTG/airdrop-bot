"""Convert stored local paths or remote URLs into aiogram photo inputs."""
from pathlib import Path

from aiogram.types import FSInputFile


def telegram_photo(value: str):
    if value.startswith(("https://", "http://")):
        return value
    path = Path(value)
    return FSInputFile(path) if path.is_file() else value
