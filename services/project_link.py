"""Discover a verified project/action URL from raw text and source-page links."""
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, parse_qs, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from config import settings


SOCIAL_OR_CONTENT_HOSTS = (
    "twitter.com",
    "x.com",
    "t.me",
    "telegram.me",
    "discord.com",
    "discord.gg",
    "youtube.com",
    "youtu.be",
    "medium.com",
    "mirror.xyz",
    "substack.com",
    "reddit.com",
    "coindesk.com",
    "cryptonews.com",
    "theblock.co",
    "airdropalert.com",
)

BAD_PATH_MARKERS = (
    "/blog/",
    "/blogs/",
    "/news/",
    "/article/",
    "/articles/",
    "/learn/",
    "/privacy",
    "/terms",
    "/about",
    "/contact",
    "/author/",
    "/category/",
)

ACTION_TERMS = (
    "launch app",
    "open app",
    "start here",
    "get started",
    "join",
    "participate",
    "testnet",
    "quest",
    "campaign",
    "claim",
    "faucet",
    "official website",
    "website",
    "app",
)


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    label: str
    score: int


def _unwrap_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "target", "redirect", "redirect_url"):
        value = query.get(key, [None])[0]
        if value:
            decoded = unquote(value)
            if decoded.startswith(("http://", "https://")):
                return decoded
    return url


def _is_valid_project_url(url: str, source_host: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host == source_host or any(blocked in host for blocked in SOCIAL_OR_CONTENT_HOSTS):
        return False
    if any(marker in path for marker in BAD_PATH_MARKERS):
        return False
    return True


def _clean_tracking(url: str) -> str:
    parsed = urlparse(url)
    blocked_keys = {"r", "ref", "referral", "affiliate", "aff", "source"}
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in blocked_keys and not key.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(clean_query)))


def _score(url: str, label: str, project_name: str) -> int:
    haystack = f"{label} {url}".lower()
    score = 1
    for index, term in enumerate(ACTION_TERMS):
        if term in haystack:
            score += 20 - min(index, 10)
    if any(host in urlparse(url).netloc.lower() for host in ("galxe.com", "zealy.io", "layer3.xyz")):
        score += 12
    if any(term in haystack for term in ("login", "sign in", "referral", "affiliate", "sponsored")):
        score -= 15
    project_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", project_name.lower())
        if len(token) >= 3 and token not in {"airdrop", "testnet", "protocol", "project"}
    ]
    if any(token in haystack for token in project_tokens):
        score += 30
    return score


def _raw_urls(raw_text: str) -> list[tuple[str, str]]:
    urls = re.findall(r"https?://[^\s<>\]\[\)\(\"']+", raw_text or "")
    return [(url.rstrip(".,;!?"), "raw content") for url in urls]


async def discover_project_link(
    source_url: str | None, raw_text: str, project_name: str = ""
) -> str | None:
    if not settings.ENABLE_PROJECT_LINK_DISCOVERY:
        return None

    source_host = urlparse(source_url or "").netloc.lower().removeprefix("www.")
    source_path = urlparse(source_url or "").path.lower()
    source_looks_like_project = bool(
        source_url
        and source_host
        and not any(blocked in source_host for blocked in SOCIAL_OR_CONTENT_HOSTS)
        and not any(marker in source_path for marker in BAD_PATH_MARKERS)
    )
    if source_looks_like_project:
        return _clean_tracking(source_url.split("#", 1)[0])

    found: list[tuple[str, str]] = _raw_urls(raw_text)
    if source_url:
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"},
            ) as client:
                response = await client.get(source_url)
                response.raise_for_status()
            source_host = response.url.host.lower().removeprefix("www.")
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urljoin(str(response.url), anchor["href"])
                label = anchor.get_text(" ", strip=True) or anchor.get("aria-label", "")
                found.append((url, label))
        except Exception:
            pass

    candidates: dict[str, LinkCandidate] = {}
    for raw_url, label in found:
        url = _clean_tracking(_unwrap_url(raw_url).split("#", 1)[0])
        if not _is_valid_project_url(url, source_host):
            continue
        candidate = LinkCandidate(url=url, label=label, score=_score(url, label, project_name))
        previous = candidates.get(url)
        if previous is None or candidate.score > previous.score:
            candidates[url] = candidate

    if not candidates:
        return None
    return max(candidates.values(), key=lambda candidate: candidate.score).url
