import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher

from db import Database
from handlers import create_router
from settings import ALLOWED_USER_IDS, BOT_TOKEN, DATABASE_PATH


def configure_logging() -> None:
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
        handlers=[
            RotatingFileHandler(
                Path(__file__).with_name("bot.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


async def main() -> None:
    logger = logging.getLogger(__name__)
    if not ALLOWED_USER_IDS:
        raise RuntimeError(
            "Добавьте хотя бы один Telegram ID в ALLOWED_USER_IDS файла .env."
        )
    logger.info("Starting bot")
    database = Database(DATABASE_PATH)
    database.set_allowed_users(ALLOWED_USER_IDS)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(database))

    async with Bot(token=BOT_TOKEN) as bot:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except Exception:
        logging.getLogger(__name__).exception("Bot stopped with an unhandled error")
        raise
