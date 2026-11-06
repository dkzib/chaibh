import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _read_allowed_ids() -> set[int]:
    raw_ids = os.getenv("ALLOWED_USER_IDS", "")
    try:
        return {int(value.strip()) for value in raw_ids.split(",") if value.strip()}
    except ValueError as error:
        raise ValueError(
            "ALLOWED_USER_IDS должен содержать Telegram ID через запятую."
        ) from error


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env.")

ALLOWED_USER_IDS = _read_allowed_ids()
DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", str(Path(__file__).with_name("mini_cash.sqlite3")))
)
if not DATABASE_PATH.is_absolute():
    DATABASE_PATH = Path(__file__).resolve().parent / DATABASE_PATH