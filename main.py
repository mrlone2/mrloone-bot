"""
main.py — Точка входа для Telegram-бота «mrloone helper bot».
Отвечает за:
  - Настройку логирования (консоль + файл logs/bot.log с ротацией)
  - Генерацию баннера-заглушки (если файл assets/banner.png отсутствует)
  - Инициализацию БД SQLite и асинхронного клиента Playerok
  - Запуск фонового асинхронного процесса (asyncio.create_task), который каждые 5–10 сек.
    проверяет новые заказы со статусом "Оплачено", выполняет авто-выдачу и пересылает
    входящие сообщения покупателей владельцу
  - Запуск long-polling бота aiogram 3.x и корректное завершение работы
"""

import asyncio
from html import escape
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import random
import struct
import zlib

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BASE_DIR, Config, load_config
from database import Database
from handlers import router
from playerok_api import PlayerokAPI

logger = logging.getLogger("mrloone.main")


def setup_logging() -> None:
    """Настраивает логирование в stdout (для systemd/journalctl) и в logs/bot.log."""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bot.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


def ensure_stub_banner(banner_path: Path) -> None:
    """
    Создаёт аккуратный PNG-баннер-заглушку (800x320, тёмно-фиолетовый градиент)
    чистыми средствами Python (без сторонних библиотек), если файла ещё нет.
    Вы можете заменить assets/banner.png на любую свою картинку.
    """
    if banner_path.exists():
        return

    banner_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 800, 320

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        return struct.pack("!I", len(data)) + chunk + struct.pack("!I", zlib.crc32(chunk) & 0xFFFFFFFF)

    # Строим градиент с мягкой неоновой полосой посередине
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # Фильтр PNG: None
        t_y = y / max(1, height - 1)
        for x in range(width):
            t_x = x / max(1, width - 1)
            r = int(20 + 48 * t_x + 25 * (1.0 - abs(t_y - 0.5) * 2))
            g = int(16 + 18 * t_y + 20 * t_x)
            b = int(46 + 95 * (1.0 - t_y) + 45 * t_x)
            raw_rows.extend((min(r, 255), min(g, 255), min(b, 255)))

    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", idat)
        + png_chunk(b"IEND", b"")
    )
    banner_path.write_bytes(png_bytes)
    logger.info("Создан баннер-заглушка: %s", banner_path)


async def process_paid_orders(
    bot: Bot,
    config: Config,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """Проверяет новые оплаченные заказы и мгновенно выполняет авто-выдачу."""
    auto_delivery = await db.get_bool_setting("auto_delivery", True)
    if not auto_delivery:
        return

    orders = await api.get_active_orders()
    if not orders:
        return

    notify_orders = await db.get_bool_setting("notify_orders", True)
    notify_errors = await db.get_bool_setting("notify_errors", True)
    admin_id = config.admin_id or await db.get_admin_id()

    for order in orders:
        if order.status != "Оплачено":
            continue

        # Исключаем повторную обработку уже закрытого заказа
        if await db.is_order_processed(order.order_id):
            continue

        product = await db.get_product_for_delivery(order.product_id)
        if product is None:
            logger.warning(
                "Новый заказ #%s от %s, но товар ID=%s отсутствует в БД.",
                order.order_id,
                order.buyer_username,
                order.product_id,
            )
            await db.log_order(
                order_id=order.order_id,
                product_id=order.product_id,
                buyer_username=order.buyer_username,
                chat_id=order.chat_id,
                amount=order.amount,
                status="Нет товара в БД",
            )
            if admin_id and notify_errors:
                msg = await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"⚠️ Поступил заказ <code>#{escape(order.order_id)}</code> "
                        f"от <b>{escape(order.buyer_username)}</b>, но товар "
                        f"<code>{escape(order.product_id)}</code> отсутствует в базе!\n"
                        "Ответьте (Reply) на это сообщение, чтобы написать покупателю вручную."
                    ),
                    parse_mode="HTML",
                )
                if order.chat_id:
                    await db.save_message_link(
                        msg.message_id,
                        order.chat_id,
                        order.buyer_username,
                        order.order_id,
                    )
            continue

        # Отправляем товар в чат сделки на Playerok
        sent = await api.send_message(order.chat_id, product["content"])
        if not sent:
            logger.error(
                "Ошибка отправки товара по заказу #%s в чат %s.",
                order.order_id,
                order.chat_id,
            )
            if admin_id and notify_errors:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"❌ Не удалось выдать товар по заказу <code>#{escape(order.order_id)}</code> "
                        f"покупателю <b>{escape(order.buyer_username)}</b>. "
                        "Штучный товар сохранён в базе, проверьте логи."
                    ),
                    parse_mode="HTML",
                )
            continue

        # Если товар штучный — удаляем выданную запись из базы, чтобы исключить повторную продажу
        if product["product_type"] == "штучный":
            await db.delete_product_by_id(product["id"])
            logger.info(
                "Штучный товар #%s (Playerok ID=%s) удалён из БД после выдачи.",
                product["id"],
                order.product_id,
            )

        await db.log_order(
            order_id=order.order_id,
            product_id=order.product_id,
            buyer_username=order.buyer_username,
            chat_id=order.chat_id,
            amount=order.amount,
            status="Выдан",
        )
        logger.info(
            "Заказ #%s успешно выдан покупателю %s.",
            order.order_id,
            order.buyer_username,
        )

        # Отправляем уведомление администратору
        if admin_id and notify_orders:
            notify_msg = await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 Поступил новый заказ! Товар успешно выдан покупателю "
                    f"<b>{escape(order.buyer_username)}</b>\n"
                    f"🆔 Заказ: <code>#{escape(order.order_id)}</code> | "
                    f"Сумма: <b>{order.amount:.0f} ₽</b>"
                ),
                parse_mode="HTML",
            )
            if order.chat_id:
                await db.save_message_link(
                    notify_msg.message_id,
                    order.chat_id,
                    order.buyer_username,
                    order.order_id,
                )


async def process_incoming_messages(
    bot: Bot,
    config: Config,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """Пересылает новые входящие сообщения от покупателей админу для ответа через Reply."""
    admin_id = config.admin_id or await db.get_admin_id()
    if not admin_id:
        return

    notify_messages = await db.get_bool_setting("notify_messages", True)
    if not notify_messages:
        return

    messages = await api.get_incoming_messages()
    for msg in messages:
        if await db.is_message_seen(msg.message_id):
            continue

        await db.mark_message_seen(msg.message_id)
        sent = await bot.send_message(
            chat_id=admin_id,
            text=(
                f"💬 <b>Новое сообщение от {escape(msg.buyer_username)}</b>\n"
                "————————————————————\n"
                f"{escape(msg.text)}\n\n"
                "<i>↩️ Ответьте (Reply) на это сообщение, чтобы отправить ответ на Playerok.</i>"
            ),
            parse_mode="HTML",
        )
        await db.save_message_link(
            tg_message_id=sent.message_id,
            playerok_chat_id=msg.chat_id,
            buyer_username=msg.buyer_username,
            order_id=msg.order_id,
        )


async def playerok_polling_loop(
    bot: Bot,
    config: Config,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """
    Фоновый асинхронный таск (24/7).
    Каждые 5–10 секунд запрашивает список активных заказов и сообщений Playerok.
    """
    logger.info(
        "Фоновый мониторинг Playerok запущен (интервал %.1f–%.1f сек.).",
        config.poll_interval_min,
        config.poll_interval_max,
    )

    while True:
        try:
            await process_paid_orders(bot, config, db, api)
            await process_incoming_messages(bot, config, db, api)
        except asyncio.CancelledError:
            logger.info("Фоновый мониторинг Playerok остановлен.")
            raise
        except Exception as exc:
            logger.exception("Неожиданная ошибка в фоновом цикле Playerok: %s", exc)

        sleep_time = random.uniform(config.poll_interval_min, config.poll_interval_max)
        await asyncio.sleep(sleep_time)


async def main() -> None:
    """Асинхронная точка входа приложения."""
    setup_logging()
    config = load_config()
    ensure_stub_banner(config.banner_path)

    db = Database(config.db_path)
    await db.init()

    cookies = config.playerok_cookies or await db.get_playerok_cookies()
    api = PlayerokAPI(
        cookies=cookies,
        proxy_url=config.proxy_url,
        mock_mode=config.mock_mode,
    )
    await api.start()

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    # Передаём зависимости во все обработчики и фильтры aiogram
    dp["config"] = config
    dp["db"] = db
    dp["api"] = api
    dp.include_router(router)

    # Запускаем параллельный фоновый таск опроса Playerok
    polling_task = asyncio.create_task(
        playerok_polling_loop(bot, config, db, api),
        name="playerok_polling_loop",
    )

    logger.info("Бот «mrloone helper bot» запущен и готов к работе 24/7.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        await api.close()
        await db.close()
        await bot.session.close()
        logger.info("Бот корректно завершил работу.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
