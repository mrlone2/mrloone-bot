"""
keyboards.py — Генерация Inline-клавиатур для Telegram-бота на базе InlineKeyboardBuilder (aiogram 3.x).
Главное меню строится строго в 2 столбца (5 строк по 2 кнопки) согласно макету.
"""

from typing import Any
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Генерирует клавиатуру главного меню строго в 2 столбца:
    [📊 Профиль] | [⚙️ Настройки бота]
    [📈 Статистика] | [📦 Мои Товары]
    [💬 Чат с клиентами] | [🤝 Сделки и Заказы]
    [🔔 Уведомления] | [💸 Вывод баланса]
    [🔌 Модули/Логи] | [💬 Тех. Поддержка]
    """
    builder = InlineKeyboardBuilder()

    buttons = [
        ("📊 Профиль", "menu:profile"),
        ("⚙️ Настройки бота", "menu:settings"),
        ("📈 Статистика", "menu:stats"),
        ("📦 Мои Товары", "menu:products"),
        ("💬 Чат с клиентами", "menu:chats"),
        ("🤝 Сделки и Заказы", "menu:orders"),
        ("🔔 Уведомления", "menu:notifications"),
        ("💸 Вывод баланса", "menu:withdraw"),
        ("🔌 Модули/Логи", "menu:logs"),
        ("💬 Тех. Поддержка", "menu:support"),
    ]

    for text, callback_data in buttons:
        builder.button(text=text, callback_data=callback_data)

    # Строго 2 кнопки в каждом ряду
    builder.adjust(2)
    return builder.as_markup()


def get_back_to_menu_keyboard(refresh_callback: str | None = None) -> InlineKeyboardMarkup:
    """Клавиатура возврата в главное меню (и опциональной кнопки обновления)."""
    builder = InlineKeyboardBuilder()
    if refresh_callback:
        builder.button(text="🔄 Обновить", callback_data=refresh_callback)
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def get_products_menu_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура раздела управления товарами авто-выдачи."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить товар", callback_data="prod:add")
    builder.button(text="📋 Список товаров", callback_data="prod:list")
    builder.button(text="🗑 Удалить по ID", callback_data="prod:delete")
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def get_product_type_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора типа товара при добавлении в БД."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎟 Штучный (удаляется после выдачи)", callback_data="prod_type:штучный")
    builder.button(text="♾ Многоразовый (выдаётся много раз)", callback_data="prod_type:многоразовый")
    builder.button(text="❌ Отмена", callback_data="prod:cancel")
    builder.adjust(1)
    return builder.as_markup()


def get_cancel_fsm_keyboard(cancel_callback: str = "prod:cancel") -> InlineKeyboardMarkup:
    """Кнопка отмены текущего ввода FSM."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=cancel_callback)
    return builder.as_markup()


def get_profile_keyboard(authorized: bool) -> InlineKeyboardMarkup:
    """Клавиатура раздела Профиль с кнопкой подключения/смены аккаунта Playerok."""
    builder = InlineKeyboardBuilder()
    auth_text = "🔄 Сменить аккаунт Playerok" if authorized else "🔐 Подключить Playerok"
    builder.button(text=auth_text, callback_data="auth:connect")
    builder.button(text="🔄 Обновить профиль", callback_data="menu:profile")
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def get_after_connect_keyboard() -> InlineKeyboardMarkup:
    """Кнопка возврата в главное меню после успешного подключения аккаунта."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Открыть главное меню", callback_data="menu:main")
    return builder.as_markup()


def get_settings_keyboard(auto_delivery: bool) -> InlineKeyboardMarkup:
    """Клавиатура настроек работы бота."""
    builder = InlineKeyboardBuilder()
    delivery_status = "✅ ВКЛ" if auto_delivery else "❌ ВЫКЛ"
    builder.button(
        text=f"🤖 Авто-выдача: {delivery_status}",
        callback_data="setting:toggle:auto_delivery",
    )
    builder.button(text="🔐 Подключить / обновить Playerok", callback_data="auth:connect")
    builder.button(text="🌐 Проверить прокси", callback_data="setting:check_proxy")
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def get_notifications_keyboard(
    notify_orders: bool,
    notify_messages: bool,
    notify_errors: bool,
) -> InlineKeyboardMarkup:
    """Клавиатура управления уведомлениями для администратора."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💰 Новые заказы: {'✅' if notify_orders else '❌'}",
        callback_data="notify:toggle:notify_orders",
    )
    builder.button(
        text=f"💬 Сообщения клиентов: {'✅' if notify_messages else '❌'}",
        callback_data="notify:toggle:notify_messages",
    )
    builder.button(
        text=f"⚠️ Ошибки и сбои: {'✅' if notify_errors else '❌'}",
        callback_data="notify:toggle:notify_errors",
    )
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def get_chats_keyboard(chats: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура быстрого выбора чата с покупателем для отправки ответа."""
    builder = InlineKeyboardBuilder()
    for chat in chats[:8]:
        username = chat.get("buyer_username", "Покупатель")
        chat_id = chat.get("chat_id", "")
        builder.button(
            text=f"✉️ Написать {username}",
            callback_data=f"chat:reply:{chat_id}",
        )
    builder.button(text="🔄 Обновить чаты", callback_data="menu:chats")
    builder.button(text="◀️ Назад в меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()
