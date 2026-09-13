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
        total_processed = (
            summary.get("sent_for_review", 0)
            + summary.get("filtered", 0)
            + summary.get("duplicates", 0)
        )
        if summary.get("errors") and total_processed == 0 and summary.get("collected", 0) > 0:
            raise RuntimeError(f"Scanner completed with {summary['errors']} signal errors and 0 processed")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
