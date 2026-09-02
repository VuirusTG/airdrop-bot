"""
Free/limited discovery sources.

Sources return raw candidates only. The main pipeline still owns deduping,
Gemini scoring, draft generation, and the admin review flow.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from config import settings


ACTIONABLE_KEYWORDS = (
    "airdrop",
    "air drop",
    "retrodrop",
    "retroactive",
    "testnet",
    "incentivized",
    "claim",
    "snapshot",
    "quest",
    "campaign",
    "points",
    "xp",
    "faucet",
    "missions",
    "waitlist",
)

EDITORIAL_KEYWORDS = (
    "report",
    "analysis",
    "explained",
    "price prediction",
    "market update",
    "weekly update",
    "newsletter",
    "airdrop definition",
    "named global exchange of the year",
    "technical analysis",
)

CURRENT_ACTION_MARKERS = (
    "is live",
    "now live",
    "launched",
    "launches",
    "open now",
    "now open",
    "claim now",
    "starts on",
    "ending",
    "deadline",
    "join the",
    "complete tasks",
    "earn points",
)

NON_PROJECT_HOST_MARKERS = (
    "twitter.com",
    "x.com",
    "t.me",
    "telegram.me",
    "discord.gg",
    "discord.com",
    "facebook.com",
    "instagram.com",
    "medium.com",
    "mirror.xyz",
    "substack.com",
    "reddit.com",
    "nitter.",
)


@dataclass(frozen=True)
class RawSignal:
    name: str
    raw_text: str
    source: str
    source_url: str | None = None


def _clean_text(value: str) -> str:
    decoded = unescape(value or "")
    text = (
        BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
        if "<" in decoded and ">" in decoded
        else decoded
    )
    return re.sub(r"\s+", " ", text).strip()


def _looks_actionable(text: str, link: str | None = None) -> bool:
    lowered = (text or "").lower()
    has_action_keyword = any(
        re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered)
        for keyword in ACTIONABLE_KEYWORDS
    )
    if not has_action_keyword:
        return False
    if any(keyword in lowered for keyword in EDITORIAL_KEYWORDS) and not any(
        marker in lowered for marker in CURRENT_ACTION_MARKERS
    ):
        return False

    if not link:
        return True

    path = urlparse(link).path.lower()
    source_markers = ("airdrop", "airdrops", "retrodrop", "testnet", "quest", "campaign", "points")
    return any(marker in path for marker in source_markers) or _is_project_link(link)


def _is_project_link(url: str | None) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not host:
        return False
    if any(marker in path for marker in ("/blog/", "/blogs/", "/article/", "/news/", "/learn/")):
        return False
    return not any(marker in host for marker in NON_PROJECT_HOST_MARKERS)


def _normalize_external_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "q", "target"):
        if key in query and query[key]:
            candidate = unquote(query[key][0])
            if candidate.startswith("http"):
                return candidate

    if parsed.scheme in {"http", "https"}:
        return url
    return None


def _entry_links(entry) -> list[str]:
    links: list[str] = []
    for item in getattr(entry, "links", []) or []:
        href = item.get("href") if isinstance(item, dict) else getattr(item, "href", None)
        normalized = _normalize_external_url(href)
        if normalized:
            links.append(normalized)

    summary = getattr(entry, "summary", "") or ""
    soup = BeautifulSoup(summary, "html.parser")
    for anchor in soup.find_all("a", href=True):
        normalized = _normalize_external_url(anchor["href"])
        if normalized:
            links.append(normalized)

    deduped: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped


def _best_project_link(entry, fallback: str | None) -> str | None:
    for link in _entry_links(entry):
        if _is_project_link(link):
            return link
    return fallback


def _stable_name(title: str, source: str) -> str:
    cleaned = re.sub(r"^(airdrop|testnet|retrodrop)\s*:\s*", "", title, flags=re.IGNORECASE)
    cleaned = cleaned.split(":")[0].strip(" -")
    if cleaned:
        return cleaned[:100]

    digest = hashlib.sha256(f"{source}:{title}".encode("utf-8")).hexdigest()[:8]
    return f"Candidate {digest}"


async def _fetch_text(url: str) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def _signals_from_feed(feed_url: str, source: str, limit: int = 20) -> list[RawSignal]:
    try:
        payload = await _fetch_text(feed_url)
    except Exception:
        return []

    feed = feedparser.parse(payload or "")
    signals: list[RawSignal] = []
    for entry in (getattr(feed, "entries", []) or [])[:limit]:
        title = _clean_text(getattr(entry, "title", "") or "")
        summary = _clean_text(getattr(entry, "summary", "") or "")
        link = getattr(entry, "link", None)
        best_link = _best_project_link(entry, link)
        combined = f"{title}\n\n{summary}\n\nLink: {best_link or link or feed_url}"
        if not _looks_actionable(f"{title}\n{summary}", best_link or link):
            continue

        signals.append(
            RawSignal(
                name=_stable_name(title, source),
                raw_text=combined,
                source=source,
                source_url=best_link or link or feed_url,
            )
        )
    return signals


async def collect_airdropalert() -> list[RawSignal]:
    if not settings.ENABLE_AIRDROPALERT_SOURCE:
        return []
    return await _signals_from_feed(settings.AIRDROPALERT_FEED_URL, "airdropalert", limit=30)


async def collect_rss_feeds() -> list[RawSignal]:
    if not settings.ENABLE_RSS_SOURCE:
        return []

    signals: list[RawSignal] = []
    for feed_url in settings.RSS_FEEDS:
        signals.extend(await _signals_from_feed(feed_url, "rss_feed", limit=20))
    return signals


async def collect_trusted_x() -> list[RawSignal]:
    if not settings.ENABLE_TRUSTED_X_SOURCE or not settings.ENABLE_FREE_X_FALLBACK:
        return []

    signals: list[RawSignal] = []
    for username in settings.TRUSTED_X_ACCOUNTS:
        for base_url in settings.FREE_X_RSS_BASE_URLS:
            feed_url = f"{base_url.rstrip('/')}/{username}/rss"
            items = await _signals_from_feed(feed_url, f"trusted_x:{username}", limit=8)
            if items:
                signals.extend(items)
                break
    return signals


async def collect_all_signals() -> list[RawSignal]:
    all_signals: list[RawSignal] = []
    for collector in (collect_airdropalert, collect_trusted_x, collect_rss_feeds):
        all_signals.extend(await collector())

    unique: list[RawSignal] = []
    seen: set[str] = set()
    for signal in all_signals:
        key = (signal.source_url or signal.name).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(signal)
    return unique
