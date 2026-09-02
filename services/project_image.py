"""Free image discovery from standard page metadata."""
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import settings


@dataclass(frozen=True)
class ProjectImage:
    url: str
    source: str


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def discover_project_image(source_url: str | None) -> ProjectImage | None:
    if not settings.ENABLE_IMAGE_DISCOVERY or not _valid_http_url(source_url):
        return None

    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"},
        ) as client:
            response = await client.get(source_url)
            response.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    selectors = (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="twitter:image"]', "content"),
    )
    for selector, attribute in selectors:
        tag = soup.select_one(selector)
        candidate = urljoin(str(response.url), tag.get(attribute, "")) if tag else None
        if _valid_http_url(candidate):
            return ProjectImage(url=candidate, source="source_page")
    return None
