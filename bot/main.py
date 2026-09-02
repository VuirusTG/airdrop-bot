import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import settings
from db.database import init_db
from bot.handlers import admin_review
from ingestion.scheduler import source_scanner

logging.basicConfig(level=logging.INFO)

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
dp.include_router(admin_review.router)


async def main():
    await init_db()
    source_scanner.configure(bot)
    logging.info("Database ready. Starting bot polling (local mode; scheduled scanner runs separately)...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
