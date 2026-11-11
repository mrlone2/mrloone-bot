"""
handlers.py — Обработчики команд, Inline-кнопок меню, FSM-сценариев управления товарами
и пересылки ответов администратора в чаты сделок на Playerok.
"""

from html import escape
import logging
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from config import BASE_DIR, Config
from database import Database
from keyboards import (
    get_after_connect_keyboard,
    get_back_to_menu_keyboard,
    get_cancel_fsm_keyboard,
    get_chats_keyboard,
    get_main_menu_keyboard,
    get_notifications_keyboard,
    get_product_type_keyboard,
    get_products_menu_keyboard,
    get_profile_keyboard,
    get_settings_keyboard,
)
from playerok_api import PlayerokAPI, parse_cookies_input

logger = logging.getLogger("mrloone.handlers")
router = Router(name="mrloone_router")


# ----------------------------------------------------------------------
# Фильтр доступа: бот строго для личного использования владельцем
# ----------------------------------------------------------------------


class AdminOnlyFilter(BaseFilter):
    """Пропускает сообщения и нажатия кнопок только от администратора (ADMIN_ID)."""

    async def __call__(
        self,
        event: Message | CallbackQuery,
        config: Config,
        db: Database,
    ) -> bool:
        user = event.from_user
        if user is None:
            return False

        admin_id = config.admin_id or await db.get_admin_id()
        # Если ADMIN_ID ещё не задан (0), разрешаем первый /start для авто-привязки владельца
        if admin_id == 0:
            if isinstance(event, Message) and (event.text or "").startswith("/start"):
                return True
            return False
        return user.id == admin_id


router.message.filter(AdminOnlyFilter())
router.callback_query.filter(AdminOnlyFilter())


# ----------------------------------------------------------------------
# Состояния FSM для добавления/удаления товаров и ответа в чат
# ----------------------------------------------------------------------


class AddProductFSM(StatesGroup):
    product_id = State()
    title = State()
    product_type = State()
    content = State()


class DeleteProductFSM(StatesGroup):
    product_id = State()


class ReplyChatFSM(StatesGroup):
    waiting_text = State()


class ConnectPlayerokFSM(StatesGroup):
    waiting_cookies = State()


# ----------------------------------------------------------------------
# Вспомогательные функции форматирования и рендеринга
# ----------------------------------------------------------------------


async def build_main_menu_caption(db: Database, api: PlayerokAPI) -> str:
    """Формирует динамический текст главного меню под баннером."""
    profile = await api.get_profile()
    proxy_info = await api.check_proxy()
    stats = await db.get_stats()

    # Если Playerok не вернул общий доход — берём статистику из локальной БД
    revenue = profile.total_revenue if profile.total_revenue > 0 else stats["total_revenue"]
    username = escape(profile.username)
    proxy_status = proxy_info["status_text"]

    return (
        "✨ <b>mrloone helper bot</b> ✨\n"
        f"🦝 Аккаунт PlayerOK: <b>{username}</b>\n"
        f"🌐 Прокси: <b>{proxy_status}</b>\n"
        "————————————————————\n"
        f"💰 Общий доход: <b>{revenue:,.0f} ₽</b>\n"
        f"📦 Продаж через бота: <b>{stats['delivered_count']}</b>\n"
        f"⭐ Рейтинг продавца: <b>{profile.rating:.1f} / 5.0</b>\n"
        f"💬 Отзывов получено: <b>{profile.reviews_count}</b>\n"
        "————————————————————\n"
        "📋 Выберите раздел управления:"
    ).replace(",", " ")


async def safe_edit_menu(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """
    Безопасно обновляет подпись под баннером (или текст сообщения),
    обрезая текст под лимит Telegram и игнорируя ошибку 'message is not modified'.
    """
    msg = callback.message
    if not isinstance(msg, Message):
        return

    # Лимит подписи к фото в Telegram — 1024 символа
    if msg.photo and len(text) > 1000:
        text = text[:990] + "\n..."

    try:
        if msg.photo:
            await msg.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        else:
            await msg.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Не удалось отредактировать сообщение меню: %s", exc)


def _read_tail_logs(log_path: Path, lines_count: int = 12) -> str:
    """Читает последние строки лог-файла для раздела Модули/Логи."""
    if not log_path.exists():
        return "Лог-файл ещё пуст."
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-lines_count:] if lines else ["Записей пока нет."]
        return "\n".join(tail)
    except Exception as exc:
        return f"Не удалось прочитать логи: {exc}"


# ----------------------------------------------------------------------
# Команда /start и возврат в главное меню
# ----------------------------------------------------------------------


def _save_env_value(key: str, value: str) -> None:
    """Автоматически обновляет или добавляет переменную в файле .env."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines = []
        prefix = f"{key}="
        for line in lines:
            if line.strip().startswith(prefix):
                new_lines.append(f"{key}={value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Не удалось автоматически обновить %s в .env: %s", key, exc)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    config: Config,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """Обработчик /start: присылает баннер-заглушку и динамическое главное меню."""
    await state.clear()

    saved_admin_id = config.admin_id or await db.get_admin_id()
    if saved_admin_id == 0 and message.from_user:
        owner_id = message.from_user.id
        await db.set_admin_id(owner_id)
        _save_env_value("ADMIN_ID", str(owner_id))
        await message.answer(
            f"✅ Вы автоматически назначены владельцем бота (ID: <code>{owner_id}</code>).",
            parse_mode="HTML",
        )

    await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    caption = await build_main_menu_caption(db, api)
    keyboard = get_main_menu_keyboard()

    if config.banner_path.exists():
        photo = FSInputFile(config.banner_path)
        await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await message.answer(
            text=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """Возвращает пользователя в главное меню и обновляет показатели."""
    await state.clear()
    await callback.answer("Обновляю меню...")
    caption = await build_main_menu_caption(db, api)
    await safe_edit_menu(callback, caption, get_main_menu_keyboard())


# ----------------------------------------------------------------------
# Разделы главного меню
# ----------------------------------------------------------------------


@router.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery, api: PlayerokAPI) -> None:
    """📊 Профиль — подробная информация об аккаунте Playerok."""
    await callback.answer()
    profile = await api.get_profile()
    auth_badge = "✅ Авторизован" if profile.authorized else "❌ Не авторизован"

    text = (
        "📊 <b>Профиль Playerok</b>\n"
        "————————————————————\n"
        f"👤 Никнейм: <b>{escape(profile.username)}</b>\n"
        f"🔐 Статус: <b>{auth_badge}</b>\n"
        f"💳 Доступно: <b>{profile.balance:.2f} ₽</b>\n"
        f"❄️ Заморожено: <b>{profile.frozen_balance:.2f} ₽</b>\n"
        f"⭐ Рейтинг: <b>{profile.rating:.1f} / 5.0</b>\n"
        f"💬 Отзывов: <b>{profile.reviews_count}</b>\n"
    )
    if profile.error:
        text += f"\n⚠️ <i>Детали: {escape(profile.error)}</i>"

    await safe_edit_menu(callback, text, get_profile_keyboard(profile.authorized))


@router.callback_query(F.data == "auth:connect")
async def cb_auth_connect_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Запускает пошаговое подключение аккаунта Playerok прямо из Telegram."""
    await callback.answer()
    await state.set_state(ConnectPlayerokFSM.waiting_cookies)
    await callback.message.answer(
        "🔐 <b>Подключение аккаунта Playerok</b>\n"
        "————————————————————\n"
        "1️⃣ Войдите в свой аккаунт на сайте <b>playerok.com</b> в обычном браузере.\n"
        "2️⃣ Скопируйте cookies одним из способов:\n\n"
        "• <b>Самый простой способ (через расширение):</b>\n"
        "Установите в Chrome расширение <b>Cookie-Editor</b>, откройте <code>playerok.com</code>, "
        "нажмите на иконку расширения → <b>Export</b> → <b>Export as JSON</b> (или <b>Header String</b>) "
        "и отправьте скопированное сюда.\n\n"
        "• <b>Без расширений (через F12):</b>\n"
        "Нажмите на сайте <code>F12</code> → вкладка <b>Application</b> (Приложение) → "
        "<b>Cookies</b> → <code>https://playerok.com</code> → скопируйте значение строки "
        "<code>token</code> и отправьте его сюда.\n\n"
        "<i>Отправьте скопированный текст следующим сообщением (после проверки я удалю его из чата ради безопасности).</i>",
        reply_markup=get_cancel_fsm_keyboard("auth:cancel"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "auth:cancel")
async def cb_auth_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменяет ввод cookies Playerok."""
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer("❌ Подключение аккаунта отменено.", reply_markup=get_after_connect_keyboard())


@router.message(ConnectPlayerokFSM.waiting_cookies)
async def fsm_auth_save_cookies(
    message: Message,
    state: FSMContext,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """Принимает cookies/токен или JSON, проверяет вход на Playerok и сохраняет в БД + .env."""
    raw_input = (message.text or "").strip()
    if not raw_input:
        await message.answer("Отправьте текст с cookies/токеном или нажмите «Отмена».")
        return

    parsed = parse_cookies_input(raw_input)
    if not parsed:
        await message.answer("❌ Не удалось распознать cookies. Попробуйте скопировать ещё раз.")
        return

    # Удаляем сообщение с секретными cookies из истории чата (если Telegram позволяет)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    status_msg = await message.answer("⏳ Проверяю подключение к Playerok (пауза 2–6 сек.)...")
    old_cookies = api.cookies
    api.update_cookies(parsed)
    profile = await api.get_profile()

    if profile.authorized:
        await db.set_playerok_cookies(parsed)
        _save_env_value("PLAYEROK_COOKIES", parsed)
        await state.clear()
        await status_msg.edit_text(
            "✅ <b>Аккаунт Playerok успешно подключён!</b>\n"
            "————————————————————\n"
            f"👤 Никнейм: <b>{escape(profile.username)}</b>\n"
            f"💳 Баланс: <b>{profile.balance:.2f} ₽</b>\n\n"
            "Данные сохранены — бот уже готов принимать заказы и отвечать клиентам.",
            reply_markup=get_after_connect_keyboard(),
            parse_mode="HTML",
        )
    else:
        api.cookies = old_cookies
        err = escape(profile.error or "Проверьте правильность cookies")
        await status_msg.edit_text(
            f"❌ <b>Не удалось войти в аккаунт Playerok</b>\n"
            f"Причина: <i>{err}</i>\n\n"
            "Убедитесь, что вы вошли в аккаунт в браузере и скопировали свежий <code>token</code> "
            "(или весь JSON из Cookie-Editor). Отправьте ещё раз или нажмите «Отмена».",
            reply_markup=get_cancel_fsm_keyboard("auth:cancel"),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, config: Config, db: Database) -> None:
    """⚙️ Настройки бота — управление авто-выдачей и прокси."""
    await callback.answer()
    auto_delivery = await db.get_bool_setting("auto_delivery", True)
    proxy_text = escape(config.proxy_url) if config.proxy_url else "Не задан (прямое соединение)"
    mode_text = "Тестовый (MOCK)" if config.mock_mode else "Боевой (24/7)"

    text = (
        "⚙️ <b>Настройки бота</b>\n"
        "————————————————————\n"
        f"🤖 Авто-выдача: <b>{'Включена' if auto_delivery else 'Выключена'}</b>\n"
        f"⏱ Интервал опроса: <b>{config.poll_interval_min:.0f}–{config.poll_interval_max:.0f} сек.</b>\n"
        "🛡 Антифрод-пауза: <b>2–6 сек.</b>\n"
        f"🌐 Прокси: <code>{proxy_text}</code>\n"
        f"🔧 Режим: <b>{mode_text}</b>"
    )
    await safe_edit_menu(callback, text, get_settings_keyboard(auto_delivery))


@router.callback_query(F.data == "setting:toggle:auto_delivery")
async def cb_toggle_auto_delivery(
    callback: CallbackQuery,
    config: Config,
    db: Database,
) -> None:
    """Переключает авто-выдачу заказов."""
    new_state = await db.toggle_bool_setting("auto_delivery")
    await callback.answer(f"Авто-выдача {'включена' if new_state else 'выключена'}")
    await cb_settings(callback, config, db)


@router.callback_query(F.data == "setting:check_proxy")
async def cb_check_proxy(callback: CallbackQuery, api: PlayerokAPI) -> None:
    """Запускает мгновенную проверку прокси."""
    await callback.answer("Проверяю прокси...")
    res = await api.check_proxy()
    await callback.message.answer(
        f"🌐 <b>Проверка прокси:</b> {res['status_text']}\n"
        f"<i>Детали: {escape(str(res['detail']))}</i>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery, db: Database) -> None:
    """📈 Статистика — локальная аналитика выдач из SQLite."""
    await callback.answer()
    stats = await db.get_stats()
    text = (
        "📈 <b>Статистика работы бота</b>\n"
        "————————————————————\n"
        f"📦 Всего выдано товаров: <b>{stats['delivered_count']}</b>\n"
        f"🗓 Продаж за сегодня: <b>{stats['today_sales']}</b>\n"
        f"📋 Всего записей сделок: <b>{stats['total_orders']}</b>\n"
        f"💰 Сумма выдач по БД: <b>{stats['total_revenue']:.2f} ₽</b>\n"
        f"🗂 Позиций в хранилище: <b>{stats['products_count']}</b>"
    )
    await safe_edit_menu(callback, text, get_back_to_menu_keyboard("menu:stats"))


# ----------------------------------------------------------------------
# Раздел 📦 Мои Товары (добавление / просмотр / удаление)
# ----------------------------------------------------------------------


@router.callback_query(F.data == "menu:products")
async def cb_products_menu(callback: CallbackQuery, db: Database) -> None:
    """📦 Мои Товары — меню управления базой авто-выдачи."""
    await callback.answer()
    count = await db.count_products()
    text = (
        "📦 <b>Мои Товары (Авто-выдача)</b>\n"
        "————————————————————\n"
        f"Сейчас в базе загружено единиц товара: <b>{count}</b>\n\n"
        "• <b>Штучный</b> — выдаётся 1 раз и сразу удаляется из БД.\n"
        "• <b>Многоразовый</b> — выдаётся каждому покупателю бесконечно."
    )
    await safe_edit_menu(callback, text, get_products_menu_keyboard())


@router.callback_query(F.data == "prod:list")
async def cb_products_list(callback: CallbackQuery, db: Database) -> None:
    """Показывает список товаров, загруженных в SQLite."""
    await callback.answer()
    items = await db.list_products(limit=15)
    if not items:
        text = "📋 <b>Список товаров пуст</b>\n\nНажмите «➕ Добавить товар», чтобы загрузить выдачу."
    else:
        lines = ["📋 <b>Товары в базе (последние 15):</b>\n"]
        for item in items:
            badge = "🎟" if item["product_type"] == "штучный" else "♾"
            preview = escape(item["content"][:28].replace("\n", " "))
            title = escape(item["title"] or "Без названия")
            lines.append(
                f"{badge} <b>ID:</b> <code>{escape(item['product_id'])}</code> "
                f"— {title} [{item['product_type']}]\n"
                f"   └ <i>{preview}...</i>"
            )
        text = "\n".join(lines)

    await safe_edit_menu(callback, text, get_products_menu_keyboard())


@router.callback_query(F.data == "prod:cancel")
async def cb_product_cancel(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Отменяет добавление/удаление товара и возвращает в раздел товаров."""
    await state.clear()
    await callback.answer("Действие отменено")
    await cb_products_menu(callback, db)


@router.callback_query(F.data == "prod:add")
async def cb_product_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1 добавления товара: запрос ID товара на Playerok."""
    await callback.answer()
    await state.set_state(AddProductFSM.product_id)
    await callback.message.answer(
        "➕ <b>Добавление товара (Шаг 1/4)</b>\n\n"
        "Введите <b>ID товара</b> на Playerok (его можно взять из ссылки или карточки лота):",
        reply_markup=get_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


@router.message(AddProductFSM.product_id)
async def fsm_product_id(message: Message, state: FSMContext) -> None:
    product_id = (message.text or "").strip()
    if not product_id:
        await message.answer("Введите непустой ID товара.")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(AddProductFSM.title)
    await message.answer(
        "📝 <b>Шаг 2/4:</b> Введите короткое <b>название товара</b> (для удобства в списке):",
        reply_markup=get_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


@router.message(AddProductFSM.title)
async def fsm_product_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip() or "Товар Playerok"
    await state.update_data(title=title)
    await state.set_state(AddProductFSM.product_type)
    await message.answer(
        "🏷 <b>Шаг 3/4:</b> Выберите тип выдачи товара:",
        reply_markup=get_product_type_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(AddProductFSM.product_type, F.data.startswith("prod_type:"))
async def fsm_product_type(callback: CallbackQuery, state: FSMContext) -> None:
    product_type = callback.data.split(":", 1)[1]
    await state.update_data(product_type=product_type)
    await state.set_state(AddProductFSM.content)
    await callback.answer()
    await callback.message.answer(
        "📩 <b>Шаг 4/4:</b> Отправьте <b>текст/ключ/аккаунт</b>, который бот автоматически "
        "отправит покупателю после оплаты:",
        reply_markup=get_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


@router.message(AddProductFSM.content)
async def fsm_product_content(message: Message, state: FSMContext, db: Database) -> None:
    content = (message.text or "").strip()
    if not content:
        await message.answer("Текст выдачи не может быть пустым.")
        return

    data = await state.get_data()
    row_id = await db.add_product(
        product_id=data["product_id"],
        title=data["title"],
        product_type=data["product_type"],
        content=content,
    )
    await state.clear()

    await message.answer(
        "✅ <b>Товар добавлен в базу!</b>\n"
        f"• Внутренний ID: <code>#{row_id}</code>\n"
        f"• ID Playerok: <code>{escape(data['product_id'])}</code>\n"
        f"• Название: <b>{escape(data['title'])}</b>\n"
        f"• Тип: <b>{escape(data['product_type'])}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "prod:delete")
async def cb_product_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Запрашивает ID товара Playerok для удаления из базы."""
    await callback.answer()
    await state.set_state(DeleteProductFSM.product_id)
    await callback.message.answer(
        "🗑 Введите <b>ID товара Playerok</b>, все записи которого нужно удалить из базы:",
        reply_markup=get_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


@router.message(DeleteProductFSM.product_id)
async def fsm_product_delete(message: Message, state: FSMContext, db: Database) -> None:
    product_id = (message.text or "").strip()
    deleted = await db.delete_products_by_product_id(product_id)
    await state.clear()

    if deleted:
        await message.answer(
            f"🗑 Удалено записей для товара <code>{escape(product_id)}</code>: <b>{deleted}</b>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"⚠️ Товар с ID <code>{escape(product_id)}</code> не найден в базе.",
            parse_mode="HTML",
        )


# ----------------------------------------------------------------------
# Раздел 💬 Чат с клиентами и пересылка ответов админа
# ----------------------------------------------------------------------


@router.callback_query(F.data == "menu:chats")
async def cb_chats(callback: CallbackQuery, api: PlayerokAPI) -> None:
    """💬 Чат с клиентами — показывает последние сообщения и позволяет быстро ответить."""
    await callback.answer()
    messages = await api.get_incoming_messages(limit=8)

    if not messages:
        text = (
            "💬 <b>Чат с клиентами</b>\n"
            "————————————————————\n"
            "Активных диалогов не найдено.\n\n"
            "💡 <i>Подсказка: когда покупатель напишет в чат на Playerok, бот пришлёт "
            "сообщение сюда. Просто сделайте Reply (Ответить) на него — и текст "
            "уйдёт покупателю на Playerok!</i>"
        )
        await safe_edit_menu(callback, text, get_back_to_menu_keyboard("menu:chats"))
        return

    lines = ["💬 <b>Последние чаты на Playerok:</b>\n"]
    chat_items: list[dict[str, Any]] = []
    for m in messages:
        lines.append(
            f"👤 <b>{escape(m.buyer_username)}</b>: {escape(m.text[:60])}"
        )
        chat_items.append({"chat_id": m.chat_id, "buyer_username": m.buyer_username})

    await safe_edit_menu(callback, "\n".join(lines), get_chats_keyboard(chat_items))


@router.callback_query(F.data.startswith("chat:reply:"))
async def cb_chat_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает ввод сообщения для выбранного чата Playerok."""
    chat_id = callback.data.split(":", 2)[2]
    await state.set_state(ReplyChatFSM.waiting_text)
    await state.update_data(chat_id=chat_id)
    await callback.answer()
    await callback.message.answer(
        f"✉️ Введите текст сообщения для отправки в чат <code>{escape(chat_id)}</code>:",
        reply_markup=get_cancel_fsm_keyboard("menu:main"),
        parse_mode="HTML",
    )


@router.message(ReplyChatFSM.waiting_text)
async def fsm_chat_reply_send(message: Message, state: FSMContext, api: PlayerokAPI) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Сообщение не может быть пустым.")
        return

    data = await state.get_data()
    chat_id = data.get("chat_id", "")
    await state.clear()

    ok = await api.send_message(chat_id, text)
    if ok:
        await message.answer("✅ Сообщение успешно отправлено в чат Playerok!")
    else:
        await message.answer("❌ Не удалось отправить сообщение на Playerok. Проверьте cookies и логи.")


@router.message(StateFilter(None), F.reply_to_message, F.text)
async def handle_admin_reply_forward(
    message: Message,
    db: Database,
    api: PlayerokAPI,
) -> None:
    """
    Пересылка ответов админа:
    Если админ отвечает (Reply) на пересланное сообщение покупателя или уведомление о заказе
    в Telegram, бот находит chat_id в БД и отправляет текст в чат сделки на Playerok.
    """
    if message.reply_to_message is None or not message.text:
        return

    link = await db.get_message_link(message.reply_to_message.message_id)
    if not link:
        return

    chat_id = link["playerok_chat_id"]
    buyer = link["buyer_username"]

    if not chat_id:
        await message.reply("⚠️ У этой сделки не найден ID чата Playerok.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    ok = await api.send_message(chat_id, message.text.strip())
    if ok:
        await message.reply(
            f"✅ Сообщение отправлено покупателю <b>{escape(buyer)}</b> на Playerok!",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            "❌ Ошибка отправки сообщения на Playerok. Проверьте подключение и cookies.",
        )


# ----------------------------------------------------------------------
# Остальные разделы меню: Заказы, Уведомления, Вывод, Модули/Логи, Поддержка
# ----------------------------------------------------------------------


@router.callback_query(F.data == "menu:orders")
async def cb_orders(callback: CallbackQuery, db: Database) -> None:
    """🤝 Сделки и Заказы — журнал последних обработанных заказов."""
    await callback.answer()
    orders = await db.get_recent_orders(limit=10)
    if not orders:
        text = "🤝 <b>Сделки и Заказы</b>\n————————————————————\nЗаказов пока нет."
    else:
        lines = ["🤝 <b>Последние сделки (до 10):</b>\n"]
        for order in orders:
            lines.append(
                f"• <code>#{escape(order['order_id'])}</code> | "
                f"<b>{escape(order['buyer_username'])}</b> | "
                f"{order['amount']:.0f} ₽ — <i>{escape(order['status'])}</i>"
            )
        text = "\n".join(lines)

    await safe_edit_menu(callback, text, get_back_to_menu_keyboard("menu:orders"))


@router.callback_query(F.data == "menu:notifications")
async def cb_notifications(callback: CallbackQuery, db: Database) -> None:
    """🔔 Уведомления — настройка оповещений для владельца."""
    await callback.answer()
    notify_orders = await db.get_bool_setting("notify_orders", True)
    notify_messages = await db.get_bool_setting("notify_messages", True)
    notify_errors = await db.get_bool_setting("notify_errors", True)

    text = (
        "🔔 <b>Настройка уведомлений</b>\n"
        "————————————————————\n"
        "Выберите, какие события бот должен присылать вам в Telegram:"
    )
    await safe_edit_menu(
        callback,
        text,
        get_notifications_keyboard(notify_orders, notify_messages, notify_errors),
    )


@router.callback_query(F.data.startswith("notify:toggle:"))
async def cb_toggle_notification(callback: CallbackQuery, db: Database) -> None:
    """Переключает выбранный тип уведомлений."""
    key = callback.data.split(":", 2)[2]
    if key not in {"notify_orders", "notify_messages", "notify_errors"}:
        await callback.answer("Неизвестная настройка")
        return

    new_state = await db.toggle_bool_setting(key)
    await callback.answer(f"Уведомление {'включено' if new_state else 'отключено'}")
    await cb_notifications(callback, db)


@router.callback_query(F.data == "menu:withdraw")
async def cb_withdraw(callback: CallbackQuery, api: PlayerokAPI) -> None:
    """💸 Вывод баланса — отображение доступных средств и заморозки."""
    await callback.answer()
    bal = await api.get_balance()

    text = (
        "💸 <b>Баланс и вывод средств</b>\n"
        "————————————————————\n"
        f"💳 Доступно к выводу: <b>{bal['available']:.2f} ₽</b>\n"
        f"❄️ В холде (заморожено): <b>{bal['frozen']:.2f} ₽</b>\n\n"
        "ℹ️ <i>Для безопасности аккаунта вывод средств на карту или кошелёк "
        "подтверждается непосредственно на сайте Playerok (2FA/SMS).</i>"
    )
    if bal.get("error"):
        text += f"\n\n⚠️ <i>{escape(str(bal['error']))}</i>"

    await safe_edit_menu(callback, text, get_back_to_menu_keyboard("menu:withdraw"))


@router.callback_query(F.data == "menu:logs")
async def cb_logs(callback: CallbackQuery, config: Config, db: Database) -> None:
    """🔌 Модули/Логи — статус фоновых служб и последние строки лога."""
    await callback.answer()
    auto_delivery = await db.get_bool_setting("auto_delivery", True)
    log_path = config.db_path.parent.parent / "logs" / "bot.log"
    tail = escape(_read_tail_logs(log_path, lines_count=10))

    text = (
        "🔌 <b>Модули и системные логи</b>\n"
        "————————————————————\n"
        f"• Модуль авто-выдачи: <b>{'🟢 Активен' if auto_delivery else '🔴 На паузе'}</b>\n"
        "• Фоновый опрос заказов: <b>🟢 Работает (24/7)</b>\n"
        "• База данных SQLite: <b>🟢 Подключена</b>\n\n"
        f"<pre>{tail}</pre>"
    )
    await safe_edit_menu(callback, text, get_back_to_menu_keyboard("menu:logs"))


@router.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery) -> None:
    """💬 Тех. Поддержка — краткая памятка владельца по эксплуатации бота."""
    await callback.answer()
    text = (
        "💬 <b>Справка по работе mrloone helper bot</b>\n"
        "————————————————————\n"
        "1️⃣ <b>Авто-выдача:</b> добавьте товар в разделе «📦 Мои Товары», указав "
        "точный ID лота с Playerok.\n"
        "2️⃣ <b>Ответ клиентам:</b> на любое сообщение покупателя в этом чате можно "
        "ответить через стандартный <i>Reply (Ответить)</i> — бот сам перешлёт текст в сделку.\n"
        "3️⃣ <b>Антифрод:</b> перед каждым обращением к Playerok бот автоматически "
        "выдерживает паузу 2–6 сек. и меняет браузерный User-Agent.\n"
        "4️⃣ <b>Режим 24/7:</b> бот работает автономно на VPS через systemd."
    )
    await safe_edit_menu(callback, text, get_back_to_menu_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Показывает Telegram ID владельца (удобно при настройке .env)."""
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")
