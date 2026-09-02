"""Background source scanning with detailed outcome statistics."""
import logging
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from ingestion.sources import collect_all_signals
from services.pipeline import process_raw_signal

logger = logging.getLogger(__name__)


class SourceScanScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self.bot: Bot | None = None

    def configure(self, bot: Bot) -> None:
        self.bot = bot

    def start(self) -> None:
        if not self.bot:
            raise RuntimeError("SourceScanScheduler.configure(bot) must be called before start().")
        self.scheduler.add_job(
            self.scan_once,
            trigger=IntervalTrigger(minutes=settings.SOURCE_SCAN_INTERVAL_MINUTES),
            id="scan_sources",
            name="Scan free crypto opportunity sources",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if settings.RUN_SCAN_ON_START:
            self.scheduler.add_job(
                self.scan_once,
                trigger=DateTrigger(run_date=datetime.now()),
                id="startup_scan",
                name="Initial source scan",
                replace_existing=True,
            )
        self.scheduler.start()
        logger.info("Source scanner started: every %s minutes", settings.SOURCE_SCAN_INTERVAL_MINUTES)

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def scan_once(self) -> dict[str, int]:
        summary = {
            "collected": 0,
            "sent_for_review": 0,
            "filtered": 0,
            "duplicates": 0,
            "groq": 0,
            "fallback": 0,
            "errors": 0,
        }
        if not self.bot:
            return summary

        signals = await collect_all_signals()
        summary["collected"] = len(signals)
        for signal in signals:
            try:
                result = await process_raw_signal(
                    bot=self.bot,
                    name=signal.name,
                    raw_text=signal.raw_text,
                    source=signal.source,
                    source_url=signal.source_url,
                )
                if result.used_fallback:
                    summary["fallback"] += 1
                if result.used_groq:
                    summary["groq"] += 1
                if result.outcome == "review":
                    summary["sent_for_review"] += 1
                elif result.outcome == "filtered":
                    summary["filtered"] += 1
                else:
                    summary["duplicates"] += 1
            except Exception as exc:
                summary["errors"] += 1
                logger.exception("Source signal failed (%s): %s", signal.name, exc)

        logger.info("Source scan summary: %s", summary)
        return summary


source_scanner = SourceScanScheduler()
