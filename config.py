"""
Central configuration, loaded from environment variables (.env file).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default).strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    # Telegram
    BOT_TOKEN: str = _require("BOT_TOKEN")
    ADMIN_USER_ID: int = int(_require("ADMIN_USER_ID"))
    PUBLISH_CHANNEL_ID: str = _require("PUBLISH_CHANNEL_ID")

    # Gemini is retained as an optional cloud fallback for Groq.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-flash-lite-latest")
    LLM_MIN_REQUEST_INTERVAL_SECONDS: float = max(
        0.0, float(os.getenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "13"))
    )
    LLM_MAX_RATE_RETRIES: int = max(0, _int("LLM_MAX_RATE_RETRIES", 2))
    # Groq is the primary cloud model for filtering, drafting, and rework.
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    GROQ_MIN_REQUEST_INTERVAL_SECONDS: float = max(
        0.0, float(os.getenv("GROQ_MIN_REQUEST_INTERVAL_SECONDS", "2.1"))
    )
    GROQ_MAX_RATE_RETRIES: int = max(0, _int("GROQ_MAX_RATE_RETRIES", 2))
    FILTER_MIN_SCORE: float = float(os.getenv("FILTER_MIN_SCORE", "4.0"))
    FILTER_VERSION: int = max(1, _int("FILTER_VERSION", 2))
    ENABLE_IMAGE_DISCOVERY: bool = _bool("ENABLE_IMAGE_DISCOVERY", True)
    ENABLE_SOCIAL_CARD_GENERATION: bool = _bool("ENABLE_SOCIAL_CARD_GENERATION", True)
    SOCIAL_CARD_DIRECTORY: str = os.getenv("SOCIAL_CARD_DIRECTORY", "images/generated")
    SOCIAL_CARD_MASCOT_PATH: str = os.getenv(
        "SOCIAL_CARD_MASCOT_PATH", "images/brand/ninja-mascot.png"
    )
    CLOUDFLARE_API_TOKEN: str = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    CLOUDFLARE_IMAGE_MODEL: str = os.getenv(
        "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"
    )
    CLOUDFLARE_IMAGE_STEPS: int = min(8, max(1, _int("CLOUDFLARE_IMAGE_STEPS", 4)))
    ENABLE_PROJECT_LINK_DISCOVERY: bool = _bool("ENABLE_PROJECT_LINK_DISCOVERY", True)

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./airdrop_bot.db")
    WEBHOOK_BASE_URL: str = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

    # Free/limited ingestion sources.
    SOURCE_SCAN_INTERVAL_MINUTES: int = max(5, _int("SOURCE_SCAN_INTERVAL_MINUTES", 60))
    RUN_SCAN_ON_START: bool = _bool("RUN_SCAN_ON_START", True)
    ENABLE_AIRDROPALERT_SOURCE: bool = _bool("ENABLE_AIRDROPALERT_SOURCE", True)
    ENABLE_RSS_SOURCE: bool = _bool("ENABLE_RSS_SOURCE", True)
    ENABLE_TRUSTED_X_SOURCE: bool = _bool("ENABLE_TRUSTED_X_SOURCE", True)
    ENABLE_FREE_X_FALLBACK: bool = _bool("ENABLE_FREE_X_FALLBACK", True)
    AIRDROPALERT_FEED_URL: str = os.getenv("AIRDROPALERT_FEED_URL", "https://airdropalert.com/feed/")
    RSS_FEEDS: list[str] = _list(
        "RSS_FEEDS",
        "https://cryptonews.com/feed/,https://www.theblock.co/rss.xml,https://www.coindesk.com/arc/outboundfeeds/rss/",
    )
    TRUSTED_X_ACCOUNTS: list[str] = _list(
        "TRUSTED_X_ACCOUNTS",
        "airdropalertcom,arbitrum,optimismFND,Starknet,zksync",
    )
    FREE_X_RSS_BASE_URLS: list[str] = _list(
        "FREE_X_RSS_BASE_URLS",
        "https://nitter.net,https://xcancel.com",
    )

    # Stage 4 (optional for now)
    X_API_BEARER_TOKEN: str = os.getenv("X_API_BEARER_TOKEN", "")
    X_API_KEY: str = os.getenv("X_API_KEY", "")
    X_API_SECRET: str = os.getenv("X_API_SECRET", "")
    X_ACCESS_TOKEN: str = os.getenv("X_ACCESS_TOKEN", "")
    X_ACCESS_TOKEN_SECRET: str = os.getenv("X_ACCESS_TOKEN_SECRET", "")
    X_AUTO_PUBLISH: bool = _bool("X_AUTO_PUBLISH", True)
    INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    INSTAGRAM_BUSINESS_ACCOUNT_ID: str = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")


settings = Settings()
