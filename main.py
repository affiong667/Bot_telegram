"""
Entrypoint. Wires up logging, state DB, connects to Deriv, recovers any open
positions from a previous run, then starts the hourly scheduler.

Run with:  python main.py
"""

import asyncio
import logging
import sys

import config
import state
import telegram_bot
import bot_commands
import runtime_settings
from deriv_client import client as deriv_client
from trading_engine import run_cycle, recover_open_trades, start_scheduler, shutdown_scheduler

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def main():
    problems = config.validate()
    hard_fail = [p for p in problems if "TWELVEDATA" not in p]  # missing TwelveData is a soft warning, not fatal
    if hard_fail:
        for p in hard_fail:
            logger.error(f"Config problem: {p}")
        logger.error("Fix the above (check your .env file) before running.")
        sys.exit(1)
    for p in problems:
        if "TWELVEDATA" in p:
            logger.warning(p)

    state.init_db()

    logger.info("Connecting to Deriv API...")
    await deriv_client.connect()

    await recover_open_trades()

    # Start listening for /status, /pause, /resume, /history in the background
    logger.info("Creating Telegram command polling task...")
    polling_task = asyncio.create_task(bot_commands.poll_commands())

    def _on_polling_task_done(task: asyncio.Task):
        if task.cancelled():
            logger.info("Polling task was cancelled (normal on shutdown).")
            return
        exc = task.exception()
        if exc:
            logger.error(f"Polling task died with an exception: {exc!r}", exc_info=exc)

    polling_task.add_done_callback(_on_polling_task_done)

    await telegram_bot.send_message(
        "🤖 <b>Binary options consensus bot started</b>\n"
        f"Universe: {len(config.INSTRUMENTS)} instruments (forex + synthetic indices)\n"
        f"Signals/cycle: {runtime_settings.num_signals} | Stake: ${runtime_settings.stake_per_trade:.2f}\n"
        f"Min confidence: {runtime_settings.min_confidence}% | Contract duration: {runtime_settings.trade_duration_minutes}min\n"
        f"Cycle interval: every {runtime_settings.trade_duration_minutes}min (matches trade duration)\n"
        f"Broker: Deriv ({'DEMO' if config.DERIV_IS_DEMO else 'REAL ⚠️'})\n"
        f"Send /help to see available commands."
    )

    start_scheduler()

    logger.info("Scheduler started. Running first cycle immediately...")
    await run_cycle()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")
        polling_task.cancel()
        shutdown_scheduler()
        await deriv_client.close()


if __name__ == "__main__":
    asyncio.run(main())
