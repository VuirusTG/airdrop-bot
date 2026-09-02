"""Live health checks for discovery sources and external services."""
import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse

import feedparser
import httpx
from aiogram import Bot

from config import settings
from publishing.x import check_x_connection
from services.cloudflare_image import check_connection as check_cloudflare_connection
from services.gemini_client import client as gemini_client
from services.groq_client import check_connection as check_groq_connection


@dataclass(frozen=True)
class HealthItem:
    name: str
    working: bool
    detail: str


@dataclass(frozen=True)
class SystemHealth:
    sources: list[HealthItem]
    telegram: HealthItem
    x: HealthItem
    gemini: HealthItem
    groq: HealthItem
    cloudflare: HealthItem
    recommendations: list[str]

    @property
    def working_sources(self) -> int:
        return sum(item.working for item in self.sources)


PLACEHOLDER_TITLES = (
    "rss reader not yet whitelisted",
    "instance has been rate limited",
    "service unavailable",
)


def _feed_name(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return f"RSS: {host}"


async def _check_feed(
    client: httpx.AsyncClient,
    name: str,
    url: str,
) -> HealthItem:
    try:
        response = await client.get(url)
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        entries = list(getattr(feed, "entries", []) or [])
        titles = [str(getattr(entry, "title", "")).strip().lower() for entry in entries]
        if not entries:
            return HealthItem(name, False, f"HTTP {response.status_code}, RSS-записей нет")
        if titles and all(any(marker in title for marker in PLACEHOLDER_TITLES) for title in titles):
            return HealthItem(name, False, "зеркало вернуло служебную заглушку")
        return HealthItem(name, True, f"HTTP {response.status_code}, записей: {len(entries)}")
    except httpx.HTTPStatusError as exc:
        return HealthItem(name, False, f"HTTP {exc.response.status_code}")
    except Exception as exc:
        return HealthItem(name, False, str(exc)[:160])


async def check_sources() -> list[HealthItem]:
    headers = {"User-Agent": "Mozilla/5.0 AirdropAlphaBot/1.0"}
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers=headers,
    ) as client:
        direct_jobs: list[asyncio.Task[HealthItem]] = []
        if settings.ENABLE_AIRDROPALERT_SOURCE:
            direct_jobs.append(
                asyncio.create_task(
                    _check_feed(client, "AirdropAlert", settings.AIRDROPALERT_FEED_URL)
                )
            )
        if settings.ENABLE_RSS_SOURCE:
            for url in settings.RSS_FEEDS:
                direct_jobs.append(asyncio.create_task(_check_feed(client, _feed_name(url), url)))

        direct_results = await asyncio.gather(*direct_jobs) if direct_jobs else []
        x_results: list[HealthItem] = []
        if settings.ENABLE_TRUSTED_X_SOURCE and settings.ENABLE_FREE_X_FALLBACK:
            x_results = await asyncio.gather(
                *(
                    _check_x_account(client, username)
                    for username in settings.TRUSTED_X_ACCOUNTS
                )
            )
        return [*direct_results, *x_results]


async def _check_x_account(client: httpx.AsyncClient, username: str) -> HealthItem:
    attempts: list[str] = []
    for base_url in settings.FREE_X_RSS_BASE_URLS:
        url = f"{base_url.rstrip('/')}/{username}/rss"
        result = await _check_feed(client, f"X: @{username}", url)
        if result.working:
            mirror = urlparse(base_url).netloc
            return HealthItem(result.name, True, f"{mirror}; {result.detail}")
        attempts.append(f"{urlparse(base_url).netloc}: {result.detail}")
    return HealthItem(f"X: @{username}", False, "; ".join(attempts)[:240])


async def check_telegram(bot: Bot) -> HealthItem:
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(settings.PUBLISH_CHANNEL_ID)
        member = await bot.get_chat_member(chat.id, me.id)
        status = str(member.status)
        can_post = status.endswith("creator") or (
            status.endswith("administrator") and bool(getattr(member, "can_post_messages", False))
        )
        if not can_post:
            return HealthItem(
                "Telegram",
                False,
                f"@{me.username} видит «{chat.title}», но не может публиковать",
            )
        return HealthItem(
            "Telegram",
            True,
            f"@{me.username} → {chat.title}; право публикации есть",
        )
    except Exception as exc:
        return HealthItem("Telegram", False, str(exc)[:200])


async def check_gemini() -> HealthItem:
    if gemini_client is None:
        return HealthItem("Gemini", False, "API-ключ не настроен; доступен резервный режим")
    try:
        model = await gemini_client.aio.models.get(model=settings.LLM_MODEL)
        model_name = getattr(model, "name", settings.LLM_MODEL)
        return HealthItem("Gemini", True, f"модель доступна: {model_name}")
    except Exception as exc:
        detail = str(exc)
        if "API key not valid" in detail or "API_KEY_INVALID" in detail:
            detail = "API-ключ недействителен"
        elif "429" in detail or "RESOURCE_EXHAUSTED" in detail:
            detail = "исчерпана квота или превышен лимит запросов"
        elif "404" in detail or "NOT_FOUND" in detail:
            detail = f"модель {settings.LLM_MODEL} недоступна"
        elif "403" in detail or "PERMISSION_DENIED" in detail:
            detail = "API-ключ не имеет доступа к выбранной модели"
        return HealthItem("Gemini", False, detail[:200])


async def check_groq() -> HealthItem:
    working, detail = await check_groq_connection()
    return HealthItem("Groq", working, detail)


async def check_cloudflare() -> HealthItem:
    working, detail = await check_cloudflare_connection()
    return HealthItem("Cloudflare Images", working, detail)


def _recommendations(
    sources: list[HealthItem],
    telegram: HealthItem,
    x: HealthItem,
    gemini: HealthItem,
    groq: HealthItem,
    cloudflare: HealthItem,
) -> list[str]:
    recommendations: list[str] = []
    failed_sources = [item.name for item in sources if not item.working]
    if failed_sources:
        recommendations.append(
            "Отключить или заменить неработающие источники: " + ", ".join(failed_sources)
        )
    if any(item.name.startswith("X: @") and not item.working for item in sources):
        recommendations.append(
            "Бесплатные X RSS-зеркала нестабильны; для надежности нужен официальный X API или другие curated RSS."
        )
    if any(item.name.startswith("RSS:") and item.working for item in sources):
        recommendations.append(
            "Общие RSS CryptoNews/The Block/CoinDesk дают много рыночных новостей; сохраняйте строгий предфильтр, а для экономии AI-квоты можно оставить только AirdropAlert и trusted X."
        )
    if not telegram.working:
        recommendations.append(
            "Проверить PUBLISH_CHANNEL_ID и выдать боту права администратора на публикацию."
        )
    if not x.working:
        recommendations.append(
            "Настроить X OAuth Read and Write; до этого использовать кнопку Open in X."
        )
    if not groq.working:
        recommendations.append(
            "Проверить GROQ_API_KEY, GROQ_MODEL и бесплатную квоту основного AI-провайдера."
        )
    if not gemini.working and not groq.working:
        recommendations.append(
            "Резервный Gemini тоже недоступен; до восстановления обоих API используется локальный шаблон."
        )
    if not cloudflare.working:
        recommendations.append(
            "Проверить CLOUDFLARE_API_TOKEN и CLOUDFLARE_ACCOUNT_ID; без них social card продолжает работать локально."
        )
    if not recommendations:
        recommendations.append("Все основные компоненты работают; можно запускать обычное сканирование.")
    return recommendations


async def collect_system_health(bot: Bot) -> SystemHealth:
    sources, telegram, x_status, gemini, groq, cloudflare = await asyncio.gather(
        check_sources(),
        check_telegram(bot),
        check_x_connection(),
        check_gemini(),
        check_groq(),
        check_cloudflare(),
    )
    x = HealthItem("X", x_status[0], x_status[1])
    return SystemHealth(
        sources=sources,
        telegram=telegram,
        x=x,
        gemini=gemini,
        groq=groq,
        cloudflare=cloudflare,
        recommendations=_recommendations(sources, telegram, x, gemini, groq, cloudflare),
    )
