"""Push one synthetic opportunity through the complete review pipeline."""
import asyncio

from bot.main import bot
from db.database import init_db
from services.pipeline import process_raw_signal

FAKE_RAW_TEXT = """
ZKFlow Protocol has launched its public testnet on a zkEVM rollup.
The public team reports a $12M seed round and active GitHub development.
Users can bridge testnet ETH, perform swaps, and provide testnet liquidity.
Future rewards are unconfirmed. No mainnet funds or private keys are requested.
"""


async def main() -> None:
    try:
        await init_db()
        result = await process_raw_signal(
            bot=bot,
            name="ZKFlow Protocol",
            raw_text=FAKE_RAW_TEXT,
            source="manual_test",
            source_url="https://example.com/zkflow-testnet",
        )
        if result.outcome == "review":
            print(f"Sent project #{result.project.id} for Telegram review.")
        else:
            print(f"Project outcome: {result.outcome}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
