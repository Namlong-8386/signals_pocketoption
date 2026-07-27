"""Entry point for the Telegram signal bot."""
import asyncio
import signal
import sys

from loguru import logger

from telegram_bot.bot import build_application, pocket_client


async def main() -> None:
    logger.info("Starting Telegram PocketOption Signal Bot...")

    # Pre-connect to PocketOption so asset lists are ready when users arrive
    try:
        await pocket_client.connect()
        logger.info("PocketOption pre-connected successfully")
    except Exception as e:
        logger.warning(f"Could not pre-connect to PocketOption: {e}")
        logger.info("Bot will retry connection when users interact.")

    application = build_application()

    # Graceful shutdown helper
    async def shutdown():
        logger.info("Shutting down...")
        await application.stop()
        await application.shutdown()
        await pocket_client.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await application.initialize()
    await application.start()
    logger.info("Bot is running. Press Ctrl+C to stop.")
    await application.updater.start_polling(drop_pending_updates=True)

    # Keep running until interrupted
    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    await stop_event.wait()

    await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
