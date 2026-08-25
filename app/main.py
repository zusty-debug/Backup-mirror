from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .bot_ui import BotController
from .config import Settings
from .crypto import SecretBox
from .database import Database
from .logging_setup import configure_logging
from .telegram_gateway import TelegramGateway
from .worker import BackupWorker, WorkerManager

logger = logging.getLogger(__name__)


def cleanup_temp_directory(settings: Settings) -> int:
    """Remove temporary project directories left by an unclean process termination."""
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
    configure_logging(settings.log_level)
    database = Database(settings.database_path)
    database.initialize()
    retryable = database.cleanup_incomplete_items()
    removed = cleanup_temp_directory(settings)
    logger.info("Startup recovery prepared %s incomplete transfers and removed %s temporary paths", retryable, removed)

    secret_box = SecretBox(settings.encryption_key)
    gateway = TelegramGateway(settings, database, secret_box)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    workers = WorkerManager(BackupWorker(settings, database, gateway, bot), database)
    controller = BotController(settings=settings, database=database, gateway=gateway, workers=workers, bot=bot)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(controller.router)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await workers.resume_after_restart()
        logger.info("Telegram Media Mirror Bot started")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        logger.info("Stopping Telegram Media Mirror Bot")
        await workers.shutdown()
        with suppress(Exception):
            await bot.session.close()
        database.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
