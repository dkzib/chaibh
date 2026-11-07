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
Application logs are written to `bot.log` beside `main.py` and also printed
to the console. When the file reaches 5 MiB, it is rotated; three backup files
are kept, limiting log storage to approximately 20 MiB. Log files are excluded
from Git. Only Telegram user ID `508084156` sees and can use the log export
and database export buttons. Database export creates a consistent SQLite
backup and sends it only in that user's private chat.
Any currently allowed bot user can add another Telegram ID from **Настройки**.
The ID is saved to `.env` and activated immediately, without restarting the bot.
Allowed users can also revoke another user's access from **Удалить пользователя**.
The list shows active Telegram IDs; revoking access preserves that user's order
history, and self-removal or removal of the last active user is blocked.

Catalog synchronization is manual and available from the bot menu. The parser
discovers the ten tea categories from the shop's catalog menu and reads each
category's own collection page. Source prices with fractional rubles are rounded
up to whole rubles before being stored. The shop exposes the base price per gram,
so the parser multiplies it by 10 to store `price_per_10g` consistently. For
products whose page offers a variant priced per 10 g (such as tea cakes sold
whole or cut), the parser uses that listed price directly instead.

The catalog also includes tea-party services. A solo tea party costs 500 ₽.
For a company, choose a tea to set the service price by its catalog price per
10 g: 500 ₽ below 200 ₽, 700 ₽ from 200–299 ₽, 900 ₽ from 300–399 ₽, 1,100 ₽
from 400–500 ₽, and 1,500 ₽ above 500 ₽. Boiling costs 350 ₽ per person for
1–4 people. Service prices are fixed in the cart and order.

Catalog prices can be corrected from the bot using **Изменить цену**. A manual
price is used until the next catalog synchronization, when the price parsed
from the site replaces it. **Вернуть цену сайта** restores the latest parsed
price before the next synchronization.
