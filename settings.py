import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from dotenv import dotenv_values, load_dotenv


ENV_PATH = Path(__file__).with_name(".env")
LOG_EXPORT_USER_ID = 508084156
load_dotenv(ENV_PATH)


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


def add_allowed_user_id(telegram_id: int) -> bool:
    if telegram_id <= 0:
        raise ValueError("Telegram ID должен быть положительным числом.")
    if not ENV_PATH.is_file():
        raise FileNotFoundError(f"Файл настроек не найден: {ENV_PATH.name}")

    values = dotenv_values(ENV_PATH)
    raw_ids = values.get("ALLOWED_USER_IDS") or ""
    allowed_ids = [
        value.strip()
        for value in raw_ids.split(",")
        if value.strip()
    ]
    user_id_text = str(telegram_id)
    if user_id_text in allowed_ids:
        return False

    allowed_ids.append(user_id_text)
    _write_allowed_user_ids(allowed_ids)
    return True


def remove_allowed_user_id(telegram_id: int) -> bool:
    if telegram_id <= 0:
        raise ValueError("Telegram ID должен быть положительным числом.")
    if not ENV_PATH.is_file():
        raise FileNotFoundError(f"Файл настроек не найден: {ENV_PATH.name}")

    values = dotenv_values(ENV_PATH)
    raw_ids = values.get("ALLOWED_USER_IDS") or ""
    allowed_ids = [
        value.strip()
        for value in raw_ids.split(",")
        if value.strip() and value.strip() != str(telegram_id)
    ]
    if len(allowed_ids) == len(
        [value.strip() for value in raw_ids.split(",") if value.strip()]
    ):
        return False

    _write_allowed_user_ids(allowed_ids)
    return True


def _write_allowed_user_ids(allowed_ids: list[str]) -> None:
    env_text = ENV_PATH.read_text(encoding="utf-8")
    assignment = re.compile(r"(?m)^(?P<prefix>\s*(?:export\s+)?)ALLOWED_USER_IDS\s*=.*$")
    replacement = lambda match: (
        f"{match.group('prefix')}ALLOWED_USER_IDS={','.join(allowed_ids)}"
    )
    updated_text, count = assignment.subn(replacement, env_text)
    if count == 0:
        if updated_text and not updated_text.endswith(("\n", "\r")):
            updated_text += "\n"
        updated_text += f"ALLOWED_USER_IDS={','.join(allowed_ids)}\n"

    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=ENV_PATH.parent,
            prefix=".env.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(updated_text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, ENV_PATH)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise