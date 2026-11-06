# Mini Cash

Telegram bot for tracking tea sales, carts, orders, payments, and daily totals.

## Setup on Windows

1. Install the dependencies:

   ```powershell
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`. Set `BOT_TOKEN` and put allowed Telegram
   numeric IDs in `ALLOWED_USER_IDS`, separated by commas.
3. Start the bot:

   ```powershell
   .\venv\Scripts\python.exe .\main.py
   ```

The SQLite database is created as `mini_cash.sqlite3` beside the application by
default. Set `DATABASE_PATH` in `.env` to use another location.

Catalog synchronization is manual and available from the bot menu. The parser
discovers the ten tea categories from the shop's catalog menu and reads each
category's own collection page. Source prices with fractional rubles are rounded
up to whole rubles before being stored. The shop exposes the base price per gram,
so the parser multiplies it by 10 to store `price_per_10g` consistently.
