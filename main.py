"""Entry point for the Telegram signal bot."""
import asyncio
import signal
import sys

from loguru import logger

from telegram_bot.bot import build_application, pocket_client


async def main() -> None:
    logger.info("Starting Telegram PocketOption Signal Bot...")

    application = build_application()

    async def connect_pocketoption():
        try:
            await pocket_client.connect()
            logger.info("PocketOption background connection established")
        except Exception as e:
            logger.warning(f"PocketOption background connection failed: {e}")

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
    # Start Telegram polling first. Broker initialization must never delay
    # command handling or make /start appear unresponsive.
    polling_task = asyncio.create_task(
        application.updater.start_polling(drop_pending_updates=True)
    )
    await asyncio.sleep(0.2)
    logger.info("Bot is running. Press Ctrl+C to stop.")
    asyncio.create_task(connect_pocketoption())

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
