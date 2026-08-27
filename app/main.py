from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import suppress

from .config import Settings
from .control_bot import TelegramControlBot
from .crypto import SecretBox
from .database import Database
from .logging_setup import configure_logging
from .telegram_gateway import TelegramGateway
from .worker import BackupWorker, WorkerManager

logger = logging.getLogger(__name__)


def cleanup_temp_directory(settings: Settings) -> int:
    removed = 0
    if not settings.temp_dir.exists():
        return 0
    for child in settings.temp_dir.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            logger.warning("Could not clean temporary item %s", child)
    return removed


async def run() -> None:
    settings = Settings()
    settings.prepare_directories()
    configure_logging(settings.log_level, settings.data_dir / "bot.log")
    database = Database(settings.database_path)
    database.initialize()
    retryable = database.cleanup_incomplete_items()
    removed = cleanup_temp_directory(settings)
    logger.info("Application build reference: %s", os.getenv("APP_BUILD_REF", "local"))
    logger.info("Startup recovery prepared %s incomplete transfers and removed %s temporary paths", retryable, removed)

    gateway = TelegramGateway(settings, database, SecretBox(settings.encryption_key))
    worker = BackupWorker(settings, database, gateway, bot=None)
    workers = WorkerManager(worker, database)
    control = TelegramControlBot(settings, database, gateway, workers)
    worker.bot = control
    try:
        await control.start()
    finally:
        logger.info("Stopping Telegram Media Mirror Bot")
        await workers.shutdown()
        with suppress(Exception):
            await control.close()
        database.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
