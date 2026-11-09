import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


MOSCOW_TZ = timezone(timedelta(hours=3), "Europe/Moscow")
logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                yield connection
        finally:
            connection.close()

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source:
            backup = sqlite3.connect(destination, timeout=30)
            try:
                source.backup(backup)
            finally:
                backup.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    external_id TEXT UNIQUE,
                    name TEXT NOT NULL,
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    price_per_10g INTEGER NOT NULL CHECK (price_per_10g > 0),
                    source_price_per_10g INTEGER NOT NULL DEFAULT 0,
                    price_override INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_products_active_category
                    ON products(is_active, category_id, name);
                CREATE INDEX IF NOT EXISTS idx_products_name
                    ON products(name COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS carts (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cart_items (
                    id INTEGER PRIMARY KEY,
                    cart_id INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    product_name TEXT NOT NULL,
                    weight_g INTEGER NOT NULL CHECK (weight_g >= 5),
                    price_snapshot INTEGER NOT NULL CHECK (price_snapshot > 0),
                    UNIQUE(cart_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    total_amount INTEGER NOT NULL CHECK (total_amount >= 0)
                );
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    product_name TEXT NOT NULL,
                    weight_g INTEGER NOT NULL CHECK (weight_g >= 5),
                    unit_price_snapshot INTEGER NOT NULL CHECK (unit_price_snapshot > 0),
                    total_price INTEGER NOT NULL CHECK (total_price >= 0),
                    is_service INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
                    cash_amount INTEGER NOT NULL CHECK (cash_amount >= 0),
                    transfer_amount INTEGER NOT NULL CHECK (transfer_amount >= 0),
                    terminal_amount INTEGER NOT NULL DEFAULT 0 CHECK (terminal_amount >= 0)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    field_name TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            cart_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cart_items)")
            }
            if "fixed_price" not in cart_columns:
                connection.execute(
                    "ALTER TABLE cart_items ADD COLUMN fixed_price INTEGER "
                    "CHECK (fixed_price IS NULL OR fixed_price > 0)"
                )
            order_item_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(order_items)")
            }
            if "is_service" not in order_item_columns:
                connection.execute(
                    "ALTER TABLE order_items ADD COLUMN is_service "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            product_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(products)")
            }
            if "source_price_per_10g" not in product_columns:
                connection.execute(
                    "ALTER TABLE products ADD COLUMN source_price_per_10g "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "price_override" not in product_columns:
                connection.execute(
                    "ALTER TABLE products ADD COLUMN price_override "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            payment_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(payments)")
            }
            if "terminal_amount" not in payment_columns:
                connection.execute(
                    "ALTER TABLE payments ADD COLUMN terminal_amount "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (terminal_amount >= 0)"
                )
            connection.execute(
                "UPDATE products SET source_price_per_10g = price_per_10g "
                "WHERE source_price_per_10g = 0"
            )

    def set_allowed_users(self, telegram_ids: set[int]) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE users SET is_active = 0")
            connection.executemany(
                """
                INSERT INTO users (telegram_id, is_active) VALUES (?, 1)
                ON CONFLICT(telegram_id) DO UPDATE SET is_active = 1
                """,
                [(telegram_id,) for telegram_id in telegram_ids],
            )

    def add_allowed_user(self, telegram_id: int) -> None:
        if telegram_id <= 0:
            raise ValueError("Telegram ID must be positive")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, is_active) VALUES (?, 1)
                ON CONFLICT(telegram_id) DO UPDATE SET is_active = 1
                """,
                (telegram_id,),
            )

    def list_active_users(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT telegram_id FROM users WHERE is_active = 1 "
                "ORDER BY telegram_id"
            ).fetchall()
        return [int(row["telegram_id"]) for row in rows]

    def deactivate_allowed_user(self, telegram_id: int) -> bool:
        if telegram_id <= 0:
            raise ValueError("Telegram ID must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = ? AND is_active = 1",
                (telegram_id,),
            ).fetchone()
            if target is None:
                return False
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE is_active = 1"
                ).fetchone()["count"]
            )
            if active_count <= 1:
                raise ValueError("Cannot deactivate the last active user")
            cursor = connection.execute(
                "UPDATE users SET is_active = 0 "
                "WHERE telegram_id = ? AND is_active = 1",
                (telegram_id,),
            )
        return cursor.rowcount == 1

    def get_active_user_id(self, telegram_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE telegram_id = ? AND is_active = 1",
                (telegram_id,),
            ).fetchone()
        return int(row["id"]) if row else None

    def _category_id(
        self, connection: sqlite3.Connection, name: str, source: str
    ) -> int:
        connection.execute(
            "INSERT INTO categories (name, source) VALUES (?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name.strip(), source),
        )
        row = connection.execute(
            "SELECT id FROM categories WHERE name = ? COLLATE NOCASE",
            (name.strip(),),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to create category: {name}")
        return int(row["id"])

    def list_categories(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT c.id, c.name, COUNT(p.id) AS product_count
                    FROM categories c
                    JOIN products p ON p.category_id = c.id
                    WHERE p.is_active = 1 AND p.source != 'service'
                    GROUP BY c.id, c.name
                    ORDER BY c.name COLLATE NOCASE
                    """
                )
            )

    def list_products(self, category_id: int, limit: int, offset: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT p.id, p.name, p.price_per_10g, p.price_override,
                           p.source_price_per_10g,
                           c.name AS category
                    FROM products p
                    JOIN categories c ON c.id = p.category_id
                    WHERE p.category_id = ? AND p.is_active = 1
                      AND p.source != 'service'
                    ORDER BY p.name COLLATE NOCASE
                    LIMIT ? OFFSET ?
                    """,
                    (category_id, limit, offset),
                )
            )

    def count_products(self, category_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM products "
                "WHERE category_id = ? AND is_active = 1 AND source != 'service'",
                (category_id,),
            ).fetchone()
        return int(row["count"])

    def update_catalog_product_price(self, product_id: int, price: int) -> bool:
        if price <= 0:
            raise ValueError("Цена должна быть положительным числом.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products SET price_per_10g = ?, price_override = 1
                WHERE id = ? AND is_active = 1 AND source != 'service'
                """,
                (price, product_id),
            )
        return cursor.rowcount == 1

    def reset_catalog_product_price(self, product_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products
                SET price_per_10g = source_price_per_10g, price_override = 0
                WHERE id = ? AND is_active = 1 AND source != 'service'
                  AND price_override = 1
                """,
                (product_id,),
            )
        return cursor.rowcount == 1

    def search_products(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as connection:
            rows = list(
                connection.execute(
                    """
                    SELECT p.id, p.name, p.price_per_10g, p.price_override,
                           c.name AS category
                    FROM products p
                    JOIN categories c ON c.id = p.category_id
                    WHERE p.is_active = 1 AND p.source != 'service'
                    """,
                )
            )
        normalized_query = self._normalize_name(query)
        matches = [
            row
            for row in rows
            if normalized_query in self._normalize_name(str(row["name"]))
        ]
        return sorted(matches, key=lambda row: str(row["name"]).casefold())[:limit]

    @staticmethod
    def _normalize_name(value: str) -> str:
        return " ".join(value.casefold().split())

    def get_product(self, product_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT p.id, p.name, p.price_per_10g, p.source_price_per_10g,
                       p.price_override,
                       p.source, p.is_active,
                       c.name AS category
                FROM products p JOIN categories c ON c.id = p.category_id
                WHERE p.id = ? AND p.source != 'service'
                """,
                (product_id,),
            ).fetchone()

    def add_manual_product(self, name: str, price: int) -> int:
        with self._connect() as connection:
            category_id = self._category_id(connection, "Ручные товары", "manual")
            cursor = connection.execute(
                """
                INSERT INTO products
                    (name, category_id, price_per_10g, source_price_per_10g,
                     source, is_active)
                VALUES (?, ?, ?, ?, 'manual', 1)
                """,
                (name.strip(), category_id, price, price),
            )
            return int(cursor.lastrowid)

    def list_manual_products(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, name, price_per_10g FROM products
                    WHERE source = 'manual' AND is_active = 1
                    ORDER BY name COLLATE NOCASE
                    """
                )
            )

    def update_manual_product(
        self, product_id: int, name: str, price: int
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products SET name = ?, price_per_10g = ?,
                    source_price_per_10g = ?, price_override = 0
                WHERE id = ? AND source = 'manual' AND is_active = 1
                """,
                (name.strip(), price, price, product_id),
            )
        return cursor.rowcount == 1

    def deactivate_manual_product(self, product_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE products SET is_active = 0
                WHERE id = ? AND source = 'manual' AND is_active = 1
                """,
                (product_id,),
            )
        return cursor.rowcount == 1

    def sync_site_products(
        self, products: list[dict[str, Any]]
    ) -> tuple[int, int, list[dict[str, Any]]]:
        updated_count = 0
        error_count = 0
        price_changes: list[dict[str, Any]] = []

        for item in products:
            try:
                name = str(item["name"]).strip()
                category = str(item["category"]).strip()
                external_id = str(item["external_id"]).strip()
                price = int(item["price_per_10g"])
                if not name or not category or not external_id or price <= 0:
                    raise ValueError("Invalid product fields")

                changed = False
                price_change: dict[str, Any] | None = None
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    category_id = self._category_id(connection, category, "site")
                    existing = connection.execute(
                        "SELECT * FROM products WHERE external_id = ?",
                        (external_id,),
                    ).fetchone()
                    if existing is None:
                        normalized_name = self._normalize_name(name)
                        same_name_category = [
                            row
                            for row in connection.execute(
                                """
                                SELECT * FROM products WHERE category_id = ?
                                    AND source = 'site'
                                    AND (external_id IS NULL OR external_id = '')
                                """,
                                (category_id,),
                            )
                            if self._normalize_name(str(row["name"])) == normalized_name
                        ]
                        if len(same_name_category) > 1:
                            raise ValueError("Ambiguous name and category match")
                        if len(same_name_category) == 1:
                            existing = same_name_category[0]
                        else:
                            same_name = [
                                row
                                for row in connection.execute(
                                    "SELECT * FROM products WHERE source = 'site' "
                                    "AND (external_id IS NULL OR external_id = '')"
                                )
                                if self._normalize_name(str(row["name"]))
                                == normalized_name
                            ]
                            if len(same_name) == 1:
                                existing = same_name[0]
                            elif len(same_name) > 1:
                                raise ValueError("Ambiguous product name match")

                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO products
                                (external_id, name, category_id, price_per_10g,
                                 source_price_per_10g, source, is_active)
                            VALUES (?, ?, ?, ?, ?, 'site', 1)
                            """,
                            (external_id, name, category_id, price, price),
                        )
                        changed = True
                    elif existing["source"] == "manual":
                        raise ValueError("Site ID is assigned to a manual product")
                    else:
                        if (
                            existing["external_id"] not in (None, external_id)
                            and existing["external_id"] != ""
                        ):
                            raise ValueError("External ID conflicts with existing product")
                        old_price = int(existing["price_per_10g"])
                        old_source_price = int(existing["source_price_per_10g"])
                        price_override = bool(existing["price_override"])
                        changed = (
                            existing["external_id"] != external_id
                            or existing["name"] != name
                            or int(existing["category_id"]) != category_id
                            or old_source_price != price
                            or old_price != price
                            or price_override
                            or not bool(existing["is_active"])
                        )
                        if changed:
                            connection.execute(
                                """
                                UPDATE products SET external_id = ?, name = ?,
                                    category_id = ?, price_per_10g = ?,
                                    source_price_per_10g = ?,
                                    price_override = 0, source = 'site', is_active = 1
                                WHERE id = ?
                                """,
                                (
                                    external_id,
                                    name,
                                    category_id,
                                    price,
                                    price,
                                    existing["id"],
                                ),
                            )
                            if old_price != price:
                                price_change = {
                                    "name": name,
                                    "old_price": old_price,
                                    "new_price": price,
                                }
                if changed:
                    updated_count += 1
                    if price_change is not None:
                        price_changes.append(price_change)
            except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
                error_count += 1
                logger.warning("Skipped a catalog item during synchronization: %s", error)

        return updated_count, error_count, price_changes

    @staticmethod
    def _position_total(weight_g: int, price_per_10g: int) -> int:
        return (weight_g * price_per_10g + 9) // 10

    @classmethod
    def _cart_item_total(cls, item: sqlite3.Row) -> int:
        fixed_price = item["fixed_price"]
        if fixed_price is not None:
            return int(fixed_price)
        return cls._position_total(item["weight_g"], item["price_snapshot"])

    def add_to_cart(self, user_id: int, product_id: int, weight_g: int) -> None:
        if weight_g < 5:
            raise ValueError("Минимальная граммовка — 5 г.")
        now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            product = connection.execute(
                "SELECT id, name, price_per_10g FROM products "
                "WHERE id = ? AND is_active = 1 AND source != 'service'",
                (product_id,),
            ).fetchone()
            if product is None:
                raise ValueError("Товар больше недоступен в каталоге.")
            connection.execute(
                """
                INSERT INTO carts (user_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (user_id, now, now),
            )
            cart = connection.execute(
                "SELECT id FROM carts WHERE user_id = ?", (user_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO cart_items
                    (cart_id, product_id, product_name, weight_g, price_snapshot)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cart_id, product_id) DO UPDATE SET
                    weight_g = cart_items.weight_g + excluded.weight_g
                """,
                (
                    cart["id"],
                    product_id,
                    product["name"],
                    weight_g,
                    product["price_per_10g"],
                ),
            )

    def add_fixed_service_to_cart(
        self, user_id: int, name: str, amount: int
    ) -> None:
        if amount <= 0:
            raise ValueError("Стоимость услуги должна быть положительной.")
        now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            category_id = self._category_id(connection, "Чаепитие", "service")
            product_cursor = connection.execute(
                """
                INSERT INTO products
                    (external_id, name, category_id, price_per_10g, source, is_active)
                VALUES (?, ?, ?, ?, 'service', 0)
                """,
                (f"service:{uuid4().hex}", name, category_id, amount),
            )
            product_id = int(product_cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO carts (user_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (user_id, now, now),
            )
            cart = connection.execute(
                "SELECT id FROM carts WHERE user_id = ?", (user_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO cart_items
                    (cart_id, product_id, product_name, weight_g, price_snapshot, fixed_price)
                VALUES (?, ?, ?, 10, ?, ?)
                """,
                (cart["id"], product_id, name, amount, amount),
            )

    def list_cart_items(self, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT ci.id, ci.product_id, ci.product_name AS name,
                           ci.weight_g, ci.price_snapshot, ci.fixed_price,
                           (ci.fixed_price IS NOT NULL) AS is_service,
                           CASE WHEN ci.fixed_price IS NOT NULL THEN ci.fixed_price
                                ELSE ((ci.weight_g * ci.price_snapshot + 9) / 10)
                           END AS total_price
                    FROM carts c JOIN cart_items ci ON ci.cart_id = c.id
                    WHERE c.user_id = ? ORDER BY ci.id
                    """,
                    (user_id,),
                )
            )

    def update_cart_weight(self, user_id: int, item_id: int, weight_g: int) -> None:
        if weight_g < 5:
            raise ValueError("Минимальная граммовка — 5 г.")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE cart_items SET weight_g = ?
                WHERE id = ? AND cart_id = (SELECT id FROM carts WHERE user_id = ?)
                  AND fixed_price IS NULL
                """,
                (weight_g, item_id, user_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Позиция корзины не найдена.")

    def remove_cart_item(self, user_id: int, item_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM cart_items
                WHERE id = ? AND cart_id = (SELECT id FROM carts WHERE user_id = ?)
                """,
                (item_id, user_id),
            )
        return cursor.rowcount == 1

    def clear_cart(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM cart_items WHERE cart_id = "
                "(SELECT id FROM carts WHERE user_id = ?)",
                (user_id,),
            )

    def checkout(
        self, user_id: int, cash: int, transfer: int, terminal: int = 0
    ) -> int:
        if min(cash, transfer, terminal) < 0:
            raise ValueError("Суммы оплаты не могут быть отрицательными.")
        now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cart = connection.execute(
                "SELECT id FROM carts WHERE user_id = ?", (user_id,)
            ).fetchone()
            if cart is None:
                raise ValueError("Корзина пуста.")
            items = list(
                connection.execute(
                    "SELECT * FROM cart_items WHERE cart_id = ? ORDER BY id",
                    (cart["id"],),
                )
            )
            if not items:
                raise ValueError("Корзина пуста.")
            total = sum(self._cart_item_total(item) for item in items)
            if cash + transfer + terminal != total:
                raise ValueError("Суммы оплат должны равняться сумме заказа.")

            order_cursor = connection.execute(
                "INSERT INTO orders (user_id, created_at, total_amount) VALUES (?, ?, ?)",
                (user_id, now, total),
            )
            order_id = int(order_cursor.lastrowid)
            for item in items:
                connection.execute(
                    """
                    INSERT INTO order_items
                        (order_id, product_name, weight_g, unit_price_snapshot,
                         total_price, is_service)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        item["product_name"],
                        item["weight_g"],
                        item["price_snapshot"],
                        self._cart_item_total(item),
                        int(item["fixed_price"] is not None),
                    ),
                )
            connection.execute(
                "INSERT INTO payments "
                "(order_id, cash_amount, transfer_amount, terminal_amount) "
                "VALUES (?, ?, ?, ?)",
                (order_id, cash, transfer, terminal),
            )
            connection.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart["id"],))
            return order_id

    def list_orders(self, limit: int = 20, offset: int = 0) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT o.id, o.created_at, o.total_amount, u.telegram_id
                    FROM orders o JOIN users u ON u.id = o.user_id
                    ORDER BY o.id DESC LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )
            )

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            order = connection.execute(
                """
                SELECT o.id, o.created_at, o.total_amount, u.telegram_id,
                       p.cash_amount, p.transfer_amount, p.terminal_amount
                FROM orders o
                JOIN users u ON u.id = o.user_id
                JOIN payments p ON p.order_id = o.id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            if order is None:
                return None
            items = list(
                connection.execute(
                    "SELECT product_name, weight_g, unit_price_snapshot, total_price, "
                    "is_service "
                    "FROM order_items WHERE order_id = ? ORDER BY id",
                    (order_id,),
                )
            )
            return {"order": order, "items": items}

    def delete_order(self, order_id: int) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                "DELETE FROM audit_log WHERE order_id = ?", (order_id,)
            )
            connection.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return True

    def update_order_finances(
        self,
        order_id: int,
        actor_user_id: int,
        total: int,
        cash: int,
        transfer: int,
        terminal: int = 0,
    ) -> None:
        if min(total, cash, transfer, terminal) < 0:
            raise ValueError("Суммы не могут быть отрицательными.")
        if cash + transfer + terminal != total:
            raise ValueError("Суммы оплат должны равняться общей сумме.")
        now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old_order = connection.execute(
                "SELECT total_amount FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            old_payment = connection.execute(
                "SELECT cash_amount, transfer_amount, terminal_amount "
                "FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if old_order is None or old_payment is None:
                raise ValueError("Заказ не найден.")
            values = {
                "total_amount": (int(old_order["total_amount"]), total),
                "cash_amount": (int(old_payment["cash_amount"]), cash),
                "transfer_amount": (int(old_payment["transfer_amount"]), transfer),
                "terminal_amount": (
                    int(old_payment["terminal_amount"]),
                    terminal,
                ),
            }
            connection.execute(
                "UPDATE orders SET total_amount = ? WHERE id = ?", (total, order_id)
            )
            connection.execute(
                "UPDATE payments SET cash_amount = ?, transfer_amount = ?, "
                "terminal_amount = ? "
                "WHERE order_id = ?",
                (cash, transfer, terminal, order_id),
            )
            for field_name, (old_value, new_value) in values.items():
                if old_value != new_value:
                    connection.execute(
                        """
                        INSERT INTO audit_log
                            (order_id, user_id, field_name, old_value, new_value, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            order_id,
                            actor_user_id,
                            field_name,
                            str(old_value),
                            str(new_value),
                            now,
                        ),
                    )

    def list_audit_log(self, order_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT a.field_name, a.old_value, a.new_value, a.created_at,
                           u.telegram_id
                    FROM audit_log a JOIN users u ON u.id = a.user_id
                    WHERE a.order_id = ? ORDER BY a.id
                    """,
                    (order_id,),
                )
            )

    def today_report(self, local_date: str) -> sqlite3.Row:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT COALESCE(SUM(o.total_amount), 0) AS total,
                       COALESCE(SUM(p.cash_amount), 0) AS cash,
                       COALESCE(SUM(p.transfer_amount), 0) AS transfer,
                       COALESCE(SUM(p.terminal_amount), 0) AS terminal,
                       COUNT(o.id) AS order_count
                FROM orders o JOIN payments p ON p.order_id = o.id
                WHERE substr(o.created_at, 1, 10) = ?
                """,
                (local_date,),
            ).fetchone()
