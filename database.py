"""
database.py — Асинхронный модуль работы с базой данных SQLite через aiosqlite.
Хранит:
  1. Сделки / Логи заказов (orders)
  2. Товары для авто-выдачи (products: "штучный" или "многоразовый")
  3. Связи сообщений Telegram <-> чаты Playerok (message_links) для пересылки ответов
  4. Просмотренные входящие сообщения (processed_messages), чтобы не дублировать уведомления
  5. Настройки бота (bot_settings)
"""

from datetime import datetime
from pathlib import Path
from typing import Any
import aiosqlite


class Database:
    """Асинхронная обёртка над SQLite для 24/7 работы бота."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Создаёт каталог БД, открывает соединение и инициализирует таблицы."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Включаем WAL-режим для стабильной асинхронной работы 24/7 без блокировок
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        # 1. Таблица сделок и логов заказов
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE NOT NULL,
                product_id TEXT NOT NULL,
                buyer_username TEXT NOT NULL,
                chat_id TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        # 2. Таблица товаров для авто-выдачи
        # Один и тот же product_id может иметь несколько записей для штучных товаров (например, 10 ключей)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                product_type TEXT NOT NULL CHECK(product_type IN ('штучный', 'многоразовый')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_product_id ON products(product_id);"
        )

        # 3. Таблица связей сообщений TG <-> чат Playerok (для ответа свайпом Reply)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                tg_message_id INTEGER PRIMARY KEY,
                playerok_chat_id TEXT NOT NULL,
                buyer_username TEXT NOT NULL,
                order_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        # 4. Таблица уже пересланных входящих сообщений от покупателей
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            """
        )

        # 5. Таблица пользовательских настроек бота
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Значения настроек по умолчанию
        defaults = {
            "auto_delivery": "1",
            "notify_orders": "1",
            "notify_messages": "1",
            "notify_errors": "1",
        }
        for key, value in defaults.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?);",
                (key, value),
            )

        await self._conn.commit()

    async def close(self) -> None:
        """Корректно закрывает соединение с SQLite."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не инициализирована. Вызовите await db.init().")
        return self._conn

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    # Работа с товарами (products)
    # ------------------------------------------------------------------

    async def add_product(
        self,
        product_id: str,
        title: str,
        product_type: str,
        content: str,
    ) -> int:
        """Добавляет товар в базу. Тип должен быть 'штучный' или 'многоразовый'."""
        if product_type not in ("штучный", "многоразовый"):
            raise ValueError("Тип товара должен быть 'штучный' или 'многоразовый'.")

        cursor = await self.conn.execute(
            """
            INSERT INTO products (product_id, title, product_type, content, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (product_id.strip(), title.strip(), product_type, content, self._now_str()),
        )
        await self.conn.commit()
        return int(cursor.lastrowid or 0)

    async def list_products(self, limit: int = 30) -> list[dict[str, Any]]:
        """Возвращает список товаров из базы (последние добавленные сверху)."""
        async with self.conn.execute(
            """
            SELECT id, product_id, title, product_type, content, created_at
            FROM products
            ORDER BY id DESC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_product_for_delivery(self, product_id: str) -> dict[str, Any] | None:
        """
        Возвращает самую раннюю запись товара по его product_id (FIFO).
        Удаление штучного товара выполняется отдельно после успешной отправки в чат,
        чтобы не потерять товар при сбое сети.
        """
        async with self.conn.execute(
            """
            SELECT id, product_id, title, product_type, content
            FROM products
            WHERE product_id = ?
            ORDER BY id ASC
            LIMIT 1;
            """,
            (product_id.strip(),),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_product_by_id(self, row_id: int) -> bool:
        """Удаляет конкретную запись товара по внутреннему ID (используется после выдачи штучного товара)."""
        cursor = await self.conn.execute("DELETE FROM products WHERE id = ?;", (row_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_products_by_product_id(self, product_id: str) -> int:
        """Удаляет все записи товара по ID товара на Playerok."""
        cursor = await self.conn.execute(
            "DELETE FROM products WHERE product_id = ?;",
            (product_id.strip(),),
        )
        await self.conn.commit()
        return cursor.rowcount

    async def count_products(self) -> int:
        """Возвращает общее количество позиций/единиц товара в базе."""
        async with self.conn.execute("SELECT COUNT(*) FROM products;") as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Работа со сделками и заказами (orders)
    # ------------------------------------------------------------------

    async def is_order_processed(self, order_id: str) -> bool:
        """Проверяет, был ли уже обработан заказ с данным order_id."""
        async with self.conn.execute(
            "SELECT 1 FROM orders WHERE order_id = ? LIMIT 1;",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def log_order(
        self,
        order_id: str,
        product_id: str,
        buyer_username: str,
        chat_id: str,
        amount: float,
        status: str,
    ) -> None:
        """Записывает или обновляет лог сделки в таблице orders."""
        await self.conn.execute(
            """
            INSERT INTO orders (order_id, product_id, buyer_username, chat_id, amount, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                status = excluded.status,
                chat_id = excluded.chat_id,
                amount = excluded.amount;
            """,
            (order_id, product_id, buyer_username, chat_id, amount, status, self._now_str()),
        )
        await self.conn.commit()

    async def get_recent_orders(self, limit: int = 15) -> list[dict[str, Any]]:
        """Возвращает последние заказы из логов."""
        async with self.conn.execute(
            """
            SELECT order_id, product_id, buyer_username, chat_id, amount, status, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT ?;
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_stats(self) -> dict[str, Any]:
        """Собирает агрегированную статистику продаж через бота из БД."""
        async with self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                SUM(CASE WHEN status = 'Выдан' THEN 1 ELSE 0 END) AS delivered_count,
                COALESCE(SUM(CASE WHEN status = 'Выдан' THEN amount ELSE 0 END), 0.0) AS total_revenue
            FROM orders;
            """
        ) as cursor:
            row = await cursor.fetchone()

        today_prefix = datetime.now().strftime("%Y-%m-%d")
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS today_count
            FROM orders
            WHERE status = 'Выдан' AND created_at LIKE ?;
            """,
            (f"{today_prefix}%",),
        ) as cursor:
            today_row = await cursor.fetchone()

        products_count = await self.count_products()

        return {
            "total_orders": int(row["total_orders"] or 0) if row else 0,
            "delivered_count": int(row["delivered_count"] or 0) if row else 0,
            "total_revenue": float(row["total_revenue"] or 0.0) if row else 0.0,
            "today_sales": int(today_row["today_count"] or 0) if today_row else 0,
            "products_count": products_count,
        }

    # ------------------------------------------------------------------
    # Связи сообщений для функции ответа покупателю (Reply в Telegram)
    # ------------------------------------------------------------------

    async def save_message_link(
        self,
        tg_message_id: int,
        playerok_chat_id: str,
        buyer_username: str,
        order_id: str = "",
    ) -> None:
        """Сохраняет связь между сообщением в Telegram и чатом на Playerok."""
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO message_links
                (tg_message_id, playerok_chat_id, buyer_username, order_id, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (tg_message_id, playerok_chat_id, buyer_username, order_id, self._now_str()),
        )
        await self.conn.commit()

    async def get_message_link(self, tg_message_id: int) -> dict[str, Any] | None:
        """Находит чат Playerok по ID сообщения в Telegram, на которое ответил админ."""
        async with self.conn.execute(
            """
            SELECT tg_message_id, playerok_chat_id, buyer_username, order_id
            FROM message_links
            WHERE tg_message_id = ?
            LIMIT 1;
            """,
            (tg_message_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def is_message_seen(self, message_id: str) -> bool:
        """Проверяет, пересылалось ли уже это сообщение от покупателя в Telegram."""
        async with self.conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1;",
            (message_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    async def mark_message_seen(self, message_id: str) -> None:
        """Помечает входящее сообщение покупателя как обработанное."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, created_at) VALUES (?, ?);",
            (message_id, self._now_str()),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Настройки бота
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: str = "1") -> str:
        """Читает булевую или строковую настройку бота."""
        async with self.conn.execute(
            "SELECT value FROM bot_settings WHERE key = ? LIMIT 1;",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
            return str(row["value"]) if row else default

    async def get_bool_setting(self, key: str, default: bool = True) -> bool:
        """Читает флаг настройки в виде bool."""
        raw = await self.get_setting(key, "1" if default else "0")
        return raw == "1"

    async def set_setting(self, key: str, value: str) -> None:
        """Обновляет значение настройки."""
        await self.conn.execute(
            """
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, value),
        )
        await self.conn.commit()

    async def toggle_bool_setting(self, key: str) -> bool:
        """Инвертирует настройку (0 <-> 1) и возвращает новое состояние."""
        current = await self.get_bool_setting(key, True)
        new_state = not current
        await self.set_setting(key, "1" if new_state else "0")
        return new_state

    async def get_admin_id(self) -> int:
        """Возвращает сохранённый в БД Telegram ID администратора (или 0)."""
        raw = await self.get_setting("admin_id", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    async def set_admin_id(self, admin_id: int) -> None:
        """Сохраняет Telegram ID администратора в БД."""
        await self.set_setting("admin_id", str(admin_id))

    async def get_playerok_cookies(self) -> str:
        """Возвращает сохранённые в БД cookies Playerok."""
        return await self.get_setting("playerok_cookies", "")

    async def set_playerok_cookies(self, cookies: str) -> None:
        """Сохраняет cookies Playerok в БД."""
        await self.set_setting("playerok_cookies", cookies.strip())
