import asyncio

from aiogram import Bot, Dispatcher

from db import Database
from handlers import create_router
from settings import ALLOWED_USER_IDS, BOT_TOKEN, DATABASE_PATH


async def main() -> None:
    if not ALLOWED_USER_IDS:
        raise RuntimeError(
            "Добавьте хотя бы один Telegram ID в ALLOWED_USER_IDS файла .env."
        )
    database = Database(DATABASE_PATH)
    database.set_allowed_users(ALLOWED_USER_IDS)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(database))

    async with Bot(token=BOT_TOKEN) as bot:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
