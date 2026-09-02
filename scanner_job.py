"""One-shot source scanner entrypoint for GitHub Actions."""
import asyncio
import logging

from aiogram import Bot

from config import settings
from db.database import init_db
from ingestion.scheduler import source_scanner

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await init_db()
    bot = Bot(settings.BOT_TOKEN)
    try:
        source_scanner.configure(bot)
        summary = await source_scanner.scan_once()
        logging.info("Scheduled scanner finished: %s", summary)
        if summary.get("errors"):
            raise RuntimeError(f"Scanner completed with {summary['errors']} signal errors")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
