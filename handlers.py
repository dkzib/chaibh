import asyncio
from html import escape
import logging
import re
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ErrorEvent,
    FSInputFile,
    Message,
    TelegramObject,
)

from db import Database
from price_parser import fetch_tea_prices
from settings import (
    LOG_EXPORT_USER_ID,
    add_allowed_user_id,
    remove_allowed_user_id,
)
from utils import format_rubles, main_keyboard, settings_keyboard


MOSCOW_TZ = timezone(timedelta(hours=3), "Europe/Moscow")
PAGE_SIZE = 10
WEIGHT_OPTIONS = (10, 20, 30, 50, 100)
logger = logging.getLogger(__name__)
CATEGORY_EMOJIS = {
    "Белый чай": "⚪️",
    "Вулканический чай": "🌋",
    "Габа": "🧠",
    "Зелёный чай": "🍃",
    "Красный чай": "🔴",
    "Лю Бао": "💩",
    "Сочинский чай": "🇷🇺",
    "Улуны": "🌸",
    "Шен Пуэр": "🍵",
    "Шу Пуэр": "☕️",
    "Ручные товары": "✍️",
}


class Form(StatesGroup):
    searching = State()
    company_searching = State()
    catalog_price = State()
    custom_weight = State()
    cart_weight = State()
    manual_name = State()
    manual_price = State()
    edit_manual_name = State()
    edit_manual_price = State()
    split_cash = State()
    split_transfer = State()
    split_terminal = State()
    edit_order_total = State()
    edit_order_cash = State()
    edit_order_transfer = State()
    edit_order_terminal = State()
    add_user_id = State()


class AccessMiddleware(BaseMiddleware):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def __call__(
        self,
        handler: Any,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None

        user_id = self.database.get_active_user_id(user.id)
        if user_id is None:
            if isinstance(event, CallbackQuery):
                await event.answer("У вас нет доступа к этому боту.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("У вас нет доступа к этому боту.")
            return None

        data["user_id"] = user_id
        data["telegram_user_id"] = user.id
        return await handler(event, data)


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button("↩️ Отмена", "flow:cancel")]]
    )


def _company_tea_party_price(tea_price_per_10g: int) -> int:
    if tea_price_per_10g < 200:
        return 500
    if tea_price_per_10g < 300:
        return 700
    if tea_price_per_10g < 400:
        return 900
    if tea_price_per_10g <= 500:
        return 1100
    return 1500


def _category_label(name: str) -> str:
    emoji = CATEGORY_EMOJIS.get(name, "🍵")
    return f"{emoji} {name}"


def _catalog_view(
    database: Database,
) -> tuple[str, InlineKeyboardMarkup | None]:
    categories = database.list_categories()
    if not categories:
        return (
            "📚 <b>Каталог пока пуст</b>\n"
            "Сначала синхронизируйте каталог с сайтом.\n"
            "Услуги чаепития доступны отдельно.",
            InlineKeyboardMarkup(
                inline_keyboard=[[_button("🎎 Чаепитие", "tea-party:open")]]
            ),
        )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    f"{_category_label(str(row['name']))} · {row['product_count']}",
                    f"category:{row['id']}:0",
                )
            ]
            for row in categories
        ] + [[_button("🎎 Чаепитие", "tea-party:open")]]
    )
    return "🍵 <b>Каталог чая</b>\nВыберите категорию:", markup


def _tea_party_menu() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "🎎 <b>Чаепитие</b>\nВыберите услугу:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("🧍 В одного · 500 ₽", "tea-party:solo")],
                [
                    _button(
                        "👥 Компания · выбрать чай",
                        "tea-party:company:categories",
                    )
                ],
                [_button("🔥 Варка · 350 ₽/человек", "tea-party:boil")],
                [_button("🍵 К категориям", "catalog:categories")],
            ]
        ),
    )


def _tea_party_categories_view(
    database: Database,
) -> tuple[str, InlineKeyboardMarkup]:
    categories = database.list_categories()
    buttons = [[_button("🔎 Поиск чая", "tea-party:company:search")]]
    buttons.extend(
        [
        [
            _button(
                f"{_category_label(str(category['name']))} · "
                f"{category['product_count']}",
                f"tea-party:company:category:{category['id']}:0",
            )
        ]
        for category in categories
        ]
    )
    buttons.append([_button("↩️ К услугам чаепития", "tea-party:open")])
    if not categories:
        return (
            "👥 <b>Нет доступных категорий чая</b>",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    return (
        "👥 <b>Чаепитие для компании</b>\nВыберите категорию чая:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _tea_party_category_view(
    database: Database, category_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    teas = database.list_products(category_id, PAGE_SIZE, page * PAGE_SIZE)
    count = database.count_products(category_id)
    category_name = str(teas[0]["category"]) if teas else "Чай"
    buttons = [
        [
            _button(
                f"{CATEGORY_EMOJIS.get(str(tea['category']), '🍵')} "
                f"{tea['name']} · {format_rubles(int(tea['price_per_10g']))} за 10 г → "
                f"{format_rubles(_company_tea_party_price(int(tea['price_per_10g'])))}",
                f"tea-party:company:add:{tea['id']}",
            )
        ]
        for tea in teas
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            _button(
                "‹ Назад",
                f"tea-party:company:category:{category_id}:{page - 1}",
            )
        )
    if (page + 1) * PAGE_SIZE < count:
        navigation.append(
            _button(
                "Далее ›",
                f"tea-party:company:category:{category_id}:{page + 1}",
            )
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [_button("↩️ К категориям чая", "tea-party:company:categories")]
    )
    if not teas:
        return (
            "👥 <b>В этой категории нет доступного чая</b>",
            InlineKeyboardMarkup(inline_keyboard=buttons[-1:]),
        )
    return (
        f"👥 <b>Чаепитие для компании · {escape(category_name)}</b>\n"
        "Выберите чай — стоимость услуги зависит от цены за 10 г.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _price_categories_view(
    database: Database,
) -> tuple[str, InlineKeyboardMarkup]:
    categories = database.list_categories()
    buttons = [
        [
            _button(
                f"{_category_label(str(category['name']))} · "
                f"{category['product_count']}",
                f"price:category:{category['id']}:0",
            )
        ]
        for category in categories
    ]
    buttons.append([_button("↩️ Отмена", "flow:cancel")])
    return (
        "💰 <b>Изменить цену</b>\nВыберите категорию товара:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _price_products_view(
    database: Database, category_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    products = database.list_products(category_id, PAGE_SIZE, page * PAGE_SIZE)
    count = database.count_products(category_id)
    category_name = str(products[0]["category"]) if products else "Категория"
    buttons: list[list[InlineKeyboardButton]] = []
    for product in products:
        override_mark = " ✍️" if product["price_override"] else ""
        buttons.append(
            [
                _button(
                    f"{product['name']} · "
                    f"{format_rubles(int(product['price_per_10g']))} за 10 г"
                    f"{override_mark}",
                    f"price:product:{product['id']}",
                )
            ]
        )
        if product["price_override"]:
            buttons.append(
                [
                    _button(
                        f"↩️ Вернуть цену сайта "
                        f"({format_rubles(int(product['source_price_per_10g']))})",
                        f"price:reset:{product['id']}",
                    )
                ]
            )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            _button("‹ Назад", f"price:category:{category_id}:{page - 1}")
        )
    if (page + 1) * PAGE_SIZE < count:
        navigation.append(
            _button("Далее ›", f"price:category:{category_id}:{page + 1}")
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [_button("↩️ К категориям", "price:categories")]
    )
    if not products:
        return (
            "В этой категории нет товаров.",
            InlineKeyboardMarkup(inline_keyboard=buttons[-1:]),
        )
    return (
        f"💰 <b>Изменить цену · {escape(category_name)}</b>\n"
        "Выберите товар:",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _parse_positive_int(text: str, field_name: str, minimum: int = 1) -> int:
    try:
        value = int(text.strip())
    except ValueError as error:
        raise ValueError(f"{field_name} нужно указать целым числом.") from error
    if value < minimum:
        raise ValueError(f"{field_name} должно быть не меньше {minimum}.")
    return value


def _message_datetime(value: str) -> str:
    date, time = _message_date_time(value)
    return f"{date} {time}".strip()


def _message_date_time(value: str) -> tuple[str, str]:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value, ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    local = parsed.astimezone(MOSCOW_TZ)
    return local.strftime("%d.%m.%Y"), local.strftime("%H:%M")


def _order_history_label(created_at: str, total_amount: int) -> str:
    date, time = _message_date_time(created_at)
    return f"{date}; {time}; {format_rubles(total_amount)}"


async def _edit_callback_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise


async def _finish_cart_add(
    callback: CallbackQuery, database: Database
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("✅ Выбранная позиция добавлена в корзину.")
    text, markup = _catalog_view(database)
    await _edit_callback_message(callback, text, markup)


def create_router(database: Database) -> Router:
    router = Router()
    access_middleware = AccessMiddleware(database)
    router.message.outer_middleware(access_middleware)
    router.callback_query.outer_middleware(access_middleware)

    @router.error()
    async def report_database_error(event: ErrorEvent) -> bool:
        if not isinstance(event.exception, sqlite3.Error):
            raise event.exception
        logger.exception("Database operation failed while processing update")
        message = event.update.message
        if message is None and event.update.callback_query is not None:
            message = event.update.callback_query.message
        if message is not None:
            await message.answer(
                "⚠️ <b>Не удалось выполнить операцию</b>\n"
                "Ошибка базы данных. Сохранённые данные не были намеренно удалены.",
                parse_mode=ParseMode.HTML,
            )
        return True

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "🍵 <b>Mini Cash</b>\n"
            "Учёт продаж чая — выберите действие в меню.",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(F.text == "⚙️ Настройки")
    async def open_settings(
        message: Message, state: FSMContext, telegram_user_id: int
    ) -> None:
        await state.clear()
        await message.answer(
            "⚙️ <b>Настройки</b>\nВыберите действие:",
            reply_markup=settings_keyboard(telegram_user_id),
            parse_mode=ParseMode.HTML,
        )

    @router.message(F.text == "⬅️ Назад")
    async def back_to_main_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Главное меню.",
            reply_markup=main_keyboard(),
        )

    @router.message(F.text == "📤 Выгрузить логи")
    async def export_logs(message: Message, user_id: int) -> None:
        if message.from_user is None or message.from_user.id != LOG_EXPORT_USER_ID:
            logger.warning("Unauthorized log export attempt: user_id=%s", user_id)
            await message.answer("У вас нет доступа к выгрузке логов.")
            return

        log_path = Path(__file__).with_name("bot.log")
        if not log_path.is_file():
            logger.warning("Log export failed: log file does not exist")
            await message.answer("Файл bot.log пока не создан.")
            return

        try:
            for handler in logging.getLogger().handlers:
                handler.flush()
            await message.answer_document(
                FSInputFile(log_path, filename="bot.log"),
                caption="📄 Лог-файл бота",
            )
        except (OSError, TelegramAPIError):
            logger.exception("Log export failed: user_id=%s", user_id)
            await message.answer("⚠️ Не удалось выгрузить файл bot.log.")
            return

        logger.info("Log file exported: user_id=%s", user_id)

    @router.message(F.text == "🗄 Выгрузить базу")
    async def export_database(message: Message, user_id: int) -> None:
        if (
            message.from_user is None
            or message.from_user.id != LOG_EXPORT_USER_ID
        ):
            logger.warning("Unauthorized database export attempt: user_id=%s", user_id)
            await message.answer("У вас нет доступа к выгрузке базы данных.")
            return
        if message.chat.type != "private" or message.chat.id != LOG_EXPORT_USER_ID:
            await message.answer(
                "Для безопасности откройте личный чат с ботом и повторите выгрузку."
            )
            return

        try:
            with TemporaryDirectory(prefix="mini_cash_backup_") as temporary_dir:
                backup_path = Path(temporary_dir) / "mini_cash.sqlite3"
                database.backup_to(backup_path)
                await message.answer_document(
                    FSInputFile(backup_path, filename="mini_cash_backup.sqlite3"),
                    caption="🗄 Резервная копия базы Mini Cash",
                )
        except (OSError, sqlite3.Error, TelegramAPIError):
            logger.exception("Database export failed: user_id=%s", user_id)
            await message.answer("⚠️ Не удалось создать или отправить копию базы.")
            return

        logger.info("Database backup exported: user_id=%s", user_id)

    @router.message(F.text == "👤 Добавить пользователя")
    async def begin_add_user(message: Message, state: FSMContext) -> None:
        await state.set_state(Form.add_user_id)
        await message.answer(
            "👤 <b>Добавление пользователя</b>\n"
            "Отправьте его Telegram ID числом. Пользователь сразу получит доступ "
            "ко всем функциям бота.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.add_user_id))
    async def add_user(
        message: Message,
        state: FSMContext,
        user_id: int,
        telegram_user_id: int,
    ) -> None:
        raw_telegram_id = (message.text or "").strip()
        if not re.fullmatch(r"[0-9]+", raw_telegram_id):
            await message.answer(
                "⚠️ Отправьте Telegram ID цифрами.",
                reply_markup=_cancel_markup(),
            )
            return

        telegram_id = int(raw_telegram_id)
        if telegram_id <= 0 or telegram_id > 2**63 - 1:
            await message.answer(
                "⚠️ Telegram ID вне допустимого диапазона.",
                reply_markup=_cancel_markup(),
            )
            return

        try:
            added_to_env = add_allowed_user_id(telegram_id)
        except (OSError, ValueError):
            logger.exception(
                "Failed to persist allowed user ID: actor_user_id=%s target_user_id=%s",
                user_id,
                telegram_id,
            )
            await message.answer(
                "⚠️ Не удалось сохранить ID в .env. Проверьте права на файл и "
                "попробуйте ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return

        try:
            database.add_allowed_user(telegram_id)
        except sqlite3.Error:
            logger.exception(
                "Allowed user persisted but could not be activated: "
                "actor_user_id=%s target_user_id=%s",
                user_id,
                telegram_id,
            )
            await state.clear()
            await message.answer(
                "⚠️ ID сохранён в .env, но не активирован из-за ошибки базы данных. "
                "Перезапустите бота после проверки bot.log.",
                reply_markup=settings_keyboard(telegram_user_id),
            )
            return

        logger.info(
            "Allowed user %s: actor_user_id=%s target_user_id=%s",
            "added" if added_to_env else "already configured and activated",
            user_id,
            telegram_id,
        )
        await state.clear()
        await message.answer(
            f"✅ Пользователь <code>{telegram_id}</code> "
            f"{'добавлен' if added_to_env else 'уже был в списке'} и теперь "
            "может пользоваться ботом.",
            reply_markup=settings_keyboard(telegram_user_id),
            parse_mode=ParseMode.HTML,
        )

    def users_removal_view(
        actor_telegram_id: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        telegram_ids = database.list_active_users()
        buttons = [
            [
                _button(
                    f"{telegram_id}{' · вы' if telegram_id == actor_telegram_id else ''}",
                    f"users:remove:select:{telegram_id}",
                )
            ]
            for telegram_id in telegram_ids
        ]
        buttons.append([_button("↩️ Закрыть", "users:remove:cancel")])
        return (
            "🚫 <b>Удаление пользователя</b>\n"
            "Выберите ID, чтобы отозвать доступ. История заказов сохранится:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.message(F.text == "🚫 Удалить пользователя")
    async def open_user_removal(
        message: Message, telegram_user_id: int
    ) -> None:
        text, markup = users_removal_view(telegram_user_id)
        await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    @router.callback_query(F.data == "users:remove:cancel")
    async def cancel_user_removal(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit_callback_message(
            callback,
            "🚫 <b>Удаление отменено</b>\n"
            "Выберите действие в меню настроек, чтобы продолжить.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button("↩️ К списку пользователей", "users:remove:list")]
                ]
            ),
        )

    @router.callback_query(F.data == "users:remove:list")
    async def user_removal_list(
        callback: CallbackQuery, telegram_user_id: int
    ) -> None:
        text, markup = users_removal_view(telegram_user_id)
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.regexp(r"^users:remove:select:\d+$"))
    async def confirm_user_removal(callback: CallbackQuery) -> None:
        target_telegram_id = int((callback.data or "").rsplit(":", 1)[1])
        target = (
            f"<code>{target_telegram_id}</code>"
            if target_telegram_id != callback.from_user.id
            else f"<code>{target_telegram_id}</code> (это ваш ID)"
        )
        await callback.answer()
        await _edit_callback_message(
            callback,
            f"⚠️ <b>Отозвать доступ у пользователя {target}?</b>\n"
            "Его история заказов сохранится.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _button(
                            "🚫 Отозвать доступ",
                            f"users:remove:confirm:{target_telegram_id}",
                        ),
                        _button("↩️ Отмена", "users:remove:list"),
                    ]
                ]
            ),
        )

    @router.callback_query(F.data.regexp(r"^users:remove:confirm:\d+$"))
    async def remove_user_access(
        callback: CallbackQuery, user_id: int
    ) -> None:
        target_telegram_id = int((callback.data or "").rsplit(":", 1)[1])
        if target_telegram_id == callback.from_user.id:
            await callback.answer(
                "Нельзя отозвать собственный доступ.", show_alert=True
            )
            return

        try:
            deactivated = database.deactivate_allowed_user(target_telegram_id)
        except ValueError:
            await callback.answer(
                "Нельзя отозвать доступ у последнего активного пользователя.",
                show_alert=True,
            )
            return
        if not deactivated:
            await callback.answer(
                "Пользователь не найден или уже отключён.", show_alert=True
            )
            return

        try:
            removed_from_env = remove_allowed_user_id(target_telegram_id)
        except OSError:
            logger.exception(
                "Failed to remove allowed user from .env: actor_user_id=%s target_user_id=%s",
                user_id,
                target_telegram_id,
            )
            try:
                database.add_allowed_user(target_telegram_id)
            except sqlite3.Error:
                logger.exception(
                    "Access revoked in database but could not be restored after "
                    ".env update failure: target_user_id=%s",
                    target_telegram_id,
                )
                await callback.answer(
                    "Ошибка обновления .env; проверьте bot.log.",
                    show_alert=True,
                )
                return
            await callback.answer(
                "Не удалось изменить .env, доступ пользователя восстановлен.",
                show_alert=True,
            )
            return

        logger.info(
            "User access revoked: actor_user_id=%s target_user_id=%s removed_from_env=%s",
            user_id,
            target_telegram_id,
            removed_from_env,
        )
        await callback.answer("Доступ отозван.")
        await _edit_callback_message(
            callback,
            f"✅ Доступ пользователя <code>{target_telegram_id}</code> отозван. "
            "История заказов сохранена.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button("👤 К списку пользователей", "users:remove:list")]
                ]
            ),
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        company_search = await state.get_state() == Form.company_searching.state
        await state.clear()
        if company_search:
            text, markup = _tea_party_categories_view(database)
        else:
            text, markup = _catalog_view(database)
        await message.answer(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )

    @router.callback_query(F.data == "flow:cancel")
    async def cancel_flow(callback: CallbackQuery, state: FSMContext) -> None:
        company_search = await state.get_state() == Form.company_searching.state
        await state.clear()
        await callback.answer()
        if company_search:
            text, markup = _tea_party_categories_view(database)
        else:
            text, markup = _catalog_view(database)
        await _edit_callback_message(callback, text, markup)

    @router.message(StateFilter(Form.searching))
    async def search_products(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if not query:
            await message.answer(
                "Введите непустую часть названия.", reply_markup=_cancel_markup()
            )
            return
        results = database.search_products(query)
        if not results:
            await message.answer(
                "🔎 <b>Ничего не найдено</b>\nПопробуйте изменить запрос.",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        await state.clear()
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(
                        f"{CATEGORY_EMOJIS.get(str(row['category']), '🍵')} "
                        f"{row['name']} · {format_rubles(row['price_per_10g'])} за 10 г",
                        f"product:{row['id']}",
                    )
                ]
                for row in results
            ]
        )
        await message.answer(
            f"🔎 <b>Результаты поиска</b>\nНайдено: {len(results)}",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.company_searching))
    async def search_company_tea(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if not query:
            await message.answer(
                "Введите название чая для поиска.",
                reply_markup=_cancel_markup(),
            )
            return
        results = database.search_products(query)
        if not results:
            await message.answer(
                "🔎 <b>Чай не найден</b>\nПопробуйте другой запрос.",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        await state.clear()
        buttons = [
            [
                _button(
                    f"{CATEGORY_EMOJIS.get(str(tea['category']), '🍵')} "
                    f"{tea['name']} · {format_rubles(int(tea['price_per_10g']))} за 10 г → "
                    f"{format_rubles(_company_tea_party_price(int(tea['price_per_10g'])))}",
                    f"tea-party:company:add:{tea['id']}",
                )
            ]
            for tea in results
        ]
        buttons.append(
            [_button("↩️ К категориям чая", "tea-party:company:categories")]
        )
        await message.answer(
            f"🔎 <b>Результаты поиска чая</b>\nНайдено: {len(results)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.catalog_price))
    async def set_catalog_price(
        message: Message,
        state: FSMContext,
        user_id: int,
        telegram_user_id: int,
    ) -> None:
        try:
            price = _parse_positive_int(message.text or "", "Цена за 10 г")
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите новую цену.",
                reply_markup=_cancel_markup(),
            )
            return
        data = await state.get_data()
        product_id = int(data["product_id"])
        previous_product = database.get_product(product_id)
        if not database.update_catalog_product_price(product_id, price):
            await state.clear()
            await message.answer(
                "⚠️ Товар больше недоступен в каталоге.",
                reply_markup=settings_keyboard(telegram_user_id),
            )
            return
        logger.info(
            "Catalog price manually changed: user_id=%s product_id=%s old_price=%s new_price=%s",
            user_id,
            product_id,
            previous_product["price_per_10g"] if previous_product else None,
            price,
        )
        await state.clear()
        product = database.get_product(product_id)
        if product is None:
            await message.answer(
                "Цена обновлена, но товар больше недоступен в каталоге.",
                reply_markup=settings_keyboard(telegram_user_id),
            )
            return
        reset_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(
                        "↩️ Вернуть цену сайта "
                        f"({format_rubles(int(product['source_price_per_10g']))})",
                        f"price:reset:{product_id}",
                    )
                ]
            ]
        )
        await message.answer(
            f"✅ Цена обновлена: <b>{format_rubles(price)}</b> за 10 г.\n"
            "Ручная цена будет действовать до следующей синхронизации каталога.",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(telegram_user_id),
        )
        await message.answer(
            "При необходимости можно сразу восстановить цену сайта:",
            reply_markup=reset_markup,
        )

    @router.message(StateFilter(Form.custom_weight))
    async def custom_weight(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            weight = _parse_positive_int(message.text or "", "Граммовка", 5)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите вес ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        product = database.get_product(int(data["product_id"]))
        if product is None or not product["is_active"]:
            await state.clear()
            await message.answer("Товар больше недоступен.")
            return
        try:
            database.add_to_cart(user_id, int(product["id"]), weight)
        except ValueError as error:
            await message.answer(
                f"⚠️ {escape(str(error))}",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        logger.info(
            "Product added to cart: user_id=%s product_id=%s weight_g=%s",
            user_id,
            product["id"],
            weight,
        )
        await state.clear()
        await message.answer("✅ Выбранная позиция добавлена в корзину.")
        text, markup = _catalog_view(database)
        await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    @router.message(StateFilter(Form.cart_weight))
    async def cart_weight_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            weight = _parse_positive_int(message.text or "", "Граммовка", 5)
            database.update_cart_weight(user_id, int(data["item_id"]), weight)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите вес ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        logger.info(
            "Cart item weight changed: user_id=%s item_id=%s weight_g=%s",
            user_id,
            data["item_id"],
            weight,
        )
        await state.clear()
        await show_cart(message, user_id)

    @router.message(StateFilter(Form.manual_name))
    async def manual_product_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name or len(name) > 200:
            await message.answer(
                "⚠️ Название должно содержать от 1 до 200 символов.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(name=name)
        await state.set_state(Form.manual_price)
        await message.answer(
            "💰 Введите цену за 10 г в целых рублях.",
            reply_markup=_cancel_markup(),
        )

    @router.message(StateFilter(Form.manual_price))
    async def manual_product_price(
        message: Message,
        state: FSMContext,
        user_id: int,
        telegram_user_id: int,
    ) -> None:
        try:
            price = _parse_positive_int(message.text or "", "Цена")
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите цену ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        data = await state.get_data()
        product_id = database.add_manual_product(str(data["name"]), price)
        logger.info(
            "Manual product added: user_id=%s product_id=%s price_per_10g=%s",
            user_id,
            product_id,
            price,
        )
        await state.clear()
        await message.answer(
            f"✅ <b>Товар добавлен</b>\n"
            f"{escape(str(data['name']))}\n"
            f"{format_rubles(price)} за 10 г",
            reply_markup=settings_keyboard(telegram_user_id),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_manual_name))
    async def manual_edit_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name or len(name) > 200:
            await message.answer(
                "⚠️ Название должно содержать от 1 до 200 символов.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(name=name)
        await state.set_state(Form.edit_manual_price)
        await message.answer(
            "💰 Введите новую цену за 10 г в целых рублях.",
            reply_markup=_cancel_markup(),
        )

    @router.message(StateFilter(Form.edit_manual_price))
    async def manual_edit_price(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        try:
            price = _parse_positive_int(message.text or "", "Цена")
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите цену ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        data = await state.get_data()
        product_id = int(data["product_id"])
        if not database.update_manual_product(
            product_id, str(data["name"]), price
        ):
            logger.warning(
                "Manual product update failed: user_id=%s product_id=%s",
                user_id,
                product_id,
            )
            await message.answer("⚠️ Ручной товар не найден или уже удалён.")
        else:
            logger.info(
                "Manual product updated: user_id=%s product_id=%s price_per_10g=%s",
                user_id,
                product_id,
                price,
            )
            await message.answer("✅ Товар обновлён.")
        await state.clear()

    @router.message(StateFilter(Form.split_cash))
    async def split_cash_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        try:
            cash = _parse_positive_int(message.text or "", "Наличные", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(cash=cash)
        await state.set_state(Form.split_transfer)
        await message.answer(
            "💳 <b>Шаг 2 из 3 — перевод.</b> Введите сумму перевода "
            "в целых рублях.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.split_transfer))
    async def split_transfer_input(
        message: Message, state: FSMContext
    ) -> None:
        data = await state.get_data()
        try:
            transfer = _parse_positive_int(message.text or "", "Перевод", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nПроверьте суммы.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(transfer=transfer)
        await state.set_state(Form.split_terminal)
        await message.answer(
            "🏧 <b>Шаг 3 из 3 — терминал.</b> Введите сумму оплаты "
            "терминалом в целых рублях.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.split_terminal))
    async def split_terminal_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            terminal = _parse_positive_int(message.text or "", "Терминал", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму терминала целым числом рублей.",
                reply_markup=_cancel_markup(),
            )
            return

        order_total = int(data["order_total"])
        cash = int(data["cash"])
        transfer = int(data["transfer"])
        expected_terminal = order_total - cash - transfer
        if expected_terminal < 0:
            await state.update_data(cash=0, transfer=0)
            await state.set_state(Form.split_cash)
            await message.answer(
                "⚠️ <b>Наличные и перевод уже превышают сумму заказа.</b>\n"
                f"Сумма заказа: <b>{format_rubles(order_total)}</b>\n"
                f"Указано наличными и переводом: "
                f"<b>{format_rubles(cash + transfer)}</b>\n"
                "Начните распределение заново: введите сумму наличными "
                "для первого шага.",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        if terminal != expected_terminal:
            await message.answer(
                "⚠️ <b>Суммы не сходятся.</b>\n"
                f"Сумма заказа: <b>{format_rubles(order_total)}</b>\n"
                f"Наличные: {format_rubles(cash)}\n"
                f"Перевод: {format_rubles(transfer)}\n"
                f"Ожидаемая сумма терминалом: "
                f"<b>{format_rubles(expected_terminal)}</b>\n"
                "<b>Введите сумму терминалом.</b>",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            order_id = database.checkout(
                user_id,
                cash,
                transfer,
                terminal,
            )
        except ValueError as error:
            await message.answer(
                f"⚠️ Не удалось оформить заказ: {error}",
                reply_markup=_cancel_markup(),
            )
            return
        logger.info(
            "Order completed: user_id=%s order_id=%s cash=%s transfer=%s "
            "terminal=%s total=%s",
            user_id,
            order_id,
            data["cash"],
            data["transfer"],
            terminal,
            int(data["cash"]) + int(data["transfer"]) + terminal,
        )
        await state.clear()
        await message.answer(
            f"✅ <b>Заказ №{order_id} оформлен</b>",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_order_total))
    async def edit_order_total_input(message: Message, state: FSMContext) -> None:
        try:
            total = _parse_positive_int(message.text or "", "Общая сумма", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(total=total)
        await state.set_state(Form.edit_order_cash)
        await message.answer(
            "💵 <b>Шаг 1 из 3 — наличные.</b> Введите новую сумму наличными.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_order_cash))
    async def edit_order_cash_input(message: Message, state: FSMContext) -> None:
        try:
            cash = _parse_positive_int(message.text or "", "Наличные", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(cash=cash)
        await state.set_state(Form.edit_order_transfer)
        await message.answer(
            "💳 <b>Шаг 2 из 3 — перевод.</b> Введите новую сумму перевода.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_order_transfer))
    async def edit_order_transfer_input(
        message: Message, state: FSMContext
    ) -> None:
        try:
            transfer = _parse_positive_int(message.text or "", "Перевод", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму ещё раз.",
                reply_markup=_cancel_markup(),
            )
            return
        await state.update_data(transfer=transfer)
        await state.set_state(Form.edit_order_terminal)
        data = await state.get_data()
        await message.answer(
            "🏧 <b>Шаг 3 из 3 — терминал.</b> Введите новую сумму оплаты "
            f"терминалом.\nСумма заказа: <b>{format_rubles(int(data['total']))}</b>.",
            reply_markup=_cancel_markup(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_order_terminal))
    async def edit_order_terminal_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            terminal = _parse_positive_int(message.text or "", "Терминал", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму терминала целым числом рублей.",
                reply_markup=_cancel_markup(),
            )
            return

        total = int(data["total"])
        cash = int(data["cash"])
        transfer = int(data["transfer"])
        expected_terminal = total - cash - transfer
        if expected_terminal < 0:
            await state.update_data(cash=0, transfer=0)
            await state.set_state(Form.edit_order_cash)
            await message.answer(
                "⚠️ <b>Наличные и перевод превышают сумму заказа.</b>\n"
                f"Сумма заказа: <b>{format_rubles(total)}</b>\n"
                f"Уже указано наличными и переводом: "
                f"<b>{format_rubles(cash + transfer)}</b>\n"
                "Введите сумму наличными заново, затем бот попросит "
                "перевод и терминал.",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return
        if terminal != expected_terminal:
            await message.answer(
                "⚠️ <b>Суммы оплат не совпадают с суммой заказа.</b>\n"
                f"Сумма заказа: <b>{format_rubles(total)}</b>\n"
                f"Наличные: {format_rubles(cash)}\n"
                f"Перевод: {format_rubles(transfer)}\n"
                f"Ожидаемая сумма терминалом: "
                f"<b>{format_rubles(expected_terminal)}</b>\n"
                "<b>Введите сумму терминалом.</b>",
                reply_markup=_cancel_markup(),
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            database.update_order_finances(
                int(data["order_id"]),
                user_id,
                total,
                cash,
                transfer,
                terminal,
            )
        except ValueError as error:
            await message.answer(
                f"⚠️ Не удалось сохранить суммы: {error}",
                reply_markup=_cancel_markup(),
            )
            return
        order_id = int(data["order_id"])
        logger.info(
            "Order finances updated: user_id=%s order_id=%s total=%s cash=%s "
            "transfer=%s terminal=%s",
            user_id,
            order_id,
            data["total"],
            data["cash"],
            data["transfer"],
            terminal,
        )
        await state.clear()
        await message.answer(
            f"✅ <b>Финансы заказа №{order_id} обновлены</b>",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("menu"))
    @router.message(F.text.in_({"Меню"}))
    async def menu(message: Message) -> None:
        await message.answer(
            "📋 <b>Главное меню</b>",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(F.text.in_({"🍵 Каталог", "Каталог"}))
    async def catalog(message: Message) -> None:
        text, markup = _catalog_view(database)
        await message.answer(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )

    @router.message(
        F.text.in_(
            {
                "💰 Изменить цену товара",
                "💰 Изменить цену",
                "Изменить цену",
            }
        )
    )
    async def begin_catalog_price_change(message: Message) -> None:
        text, markup = _price_categories_view(database)
        await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

    @router.callback_query(F.data == "price:categories")
    async def price_categories(callback: CallbackQuery) -> None:
        text, markup = _price_categories_view(database)
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.startswith("price:category:"))
    async def price_category_page(callback: CallbackQuery) -> None:
        _, _, category_id, page = (callback.data or "").split(":")
        text, markup = _price_products_view(
            database, int(category_id), int(page)
        )
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.startswith("price:product:"))
    async def begin_price_entry(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        product_id = int((callback.data or "").rsplit(":", 1)[1])
        product = database.get_product(product_id)
        if product is None or not product["is_active"]:
            await callback.answer("Товар больше недоступен.", show_alert=True)
            return
        await state.update_data(product_id=product_id)
        await state.set_state(Form.catalog_price)
        await callback.answer()
        await _edit_callback_message(
            callback,
            f"💰 <b>{escape(str(product['name']))}</b>\n"
            f"Текущая цена: {format_rubles(int(product['price_per_10g']))} за 10 г.\n"
            "Введите новую цену целым числом в рублях:",
            _cancel_markup(),
        )

    @router.callback_query(F.data.startswith("price:reset:"))
    async def reset_catalog_price(callback: CallbackQuery, user_id: int) -> None:
        product_id = int((callback.data or "").rsplit(":", 1)[1])
        if not database.reset_catalog_product_price(product_id):
            await callback.answer("У товара нет ручной цены.", show_alert=True)
            return
        product = database.get_product(product_id)
        logger.info(
            "Catalog price reset to site price: user_id=%s product_id=%s price=%s",
            user_id,
            product_id,
            product["price_per_10g"] if product else None,
        )
        await callback.answer("Цена сайта восстановлена.")
        if product is None:
            await _edit_callback_message(callback, "Товар больше недоступен.")
            return
        category_id = next(
            (
                int(category["id"])
                for category in database.list_categories()
                if str(category["name"]) == str(product["category"])
            ),
            None,
        )
        if category_id is None:
            text, markup = _price_categories_view(database)
        else:
            text, markup = _price_products_view(database, category_id, 0)
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.startswith("category:"))
    async def category_page(callback: CallbackQuery) -> None:
        _, category_id_text, page_text = (callback.data or "").split(":")
        category_id, page = int(category_id_text), int(page_text)
        products = database.list_products(
            category_id, PAGE_SIZE, page * PAGE_SIZE
        )
        count = database.count_products(category_id)
        buttons = [
            [
                _button(
                    f"{CATEGORY_EMOJIS.get(str(row['category']), '🍵')} "
                    f"{row['name']} · {format_rubles(row['price_per_10g'])} за 10 г",
                    f"product:{row['id']}",
                )
            ]
            for row in products
        ]
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(_button("‹ Назад", f"category:{category_id}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < count:
            navigation.append(_button("Далее ›", f"category:{category_id}:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([_button("🍵 Категории", "catalog:categories")])
        await callback.answer()
        await _edit_callback_message(
            callback,
            f"{_category_label(str(products[0]['category'])) if products else '🍵'} "
            f"<b>Товары</b>  ·  страница {page + 1}",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data == "catalog:categories")
    async def category_list_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        text, markup = _catalog_view(database)
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data == "tea-party:open")
    async def open_tea_party(callback: CallbackQuery) -> None:
        await callback.answer()
        text, markup = _tea_party_menu()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data == "tea-party:solo")
    async def add_solo_tea_party(callback: CallbackQuery, user_id: int) -> None:
        database.add_fixed_service_to_cart(user_id, "Чаепитие — в одного", 500)
        logger.info("Service added to cart: user_id=%s service=solo amount=500", user_id)
        await _finish_cart_add(callback, database)

    @router.callback_query(F.data == "tea-party:boil")
    async def choose_boiling_people(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit_callback_message(
            callback,
            "🔥 <b>Варка</b>\nВыберите количество человек:",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _button(
                            f"{people} чел. · {format_rubles(350 * people)}",
                            f"tea-party:boil:add:{people}",
                        )
                    ]
                    for people in range(1, 5)
                ]
                + [[_button("↩️ К услугам чаепития", "tea-party:open")]]
            ),
        )

    @router.callback_query(F.data.startswith("tea-party:boil:add:"))
    async def add_boiling_service(
        callback: CallbackQuery, user_id: int
    ) -> None:
        people = int((callback.data or "").rsplit(":", 1)[1])
        if people not in range(1, 5):
            await callback.answer("Выберите от 1 до 4 человек.", show_alert=True)
            return
        amount = 350 * people
        database.add_fixed_service_to_cart(
            user_id, f"Варка — {people} чел.", amount
        )
        logger.info(
            "Service added to cart: user_id=%s service=boiling people=%s amount=%s",
            user_id,
            people,
            amount,
        )
        await _finish_cart_add(callback, database)

    @router.callback_query(F.data.startswith("tea-party:company:add:"))
    async def add_company_tea_party(
        callback: CallbackQuery, user_id: int
    ) -> None:
        tea_id = int((callback.data or "").rsplit(":", 1)[1])
        tea = database.get_product(tea_id)
        if tea is None or not tea["is_active"] or tea["source"] == "service":
            await callback.answer("Чай больше недоступен.", show_alert=True)
            return
        amount = _company_tea_party_price(int(tea["price_per_10g"]))
        database.add_fixed_service_to_cart(
            user_id,
            f"Чаепитие для компании — {tea['name']}",
            amount,
        )
        logger.info(
            "Service added to cart: user_id=%s service=company tea_product_id=%s amount=%s",
            user_id,
            tea_id,
            amount,
        )
        await _finish_cart_add(callback, database)

    @router.callback_query(F.data == "tea-party:company:categories")
    async def company_tea_party_categories(callback: CallbackQuery) -> None:
        text, markup = _tea_party_categories_view(database)
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data == "tea-party:company:search")
    async def begin_company_tea_search(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.set_state(Form.company_searching)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "🔎 <b>Поиск чая для чаепития</b>\nВведите часть названия:",
            _cancel_markup(),
        )

    @router.callback_query(F.data.startswith("tea-party:company:category:"))
    async def company_tea_party_category(callback: CallbackQuery) -> None:
        _, _, _, category_id_text, page_text = (callback.data or "").split(":")
        text, markup = _tea_party_category_view(
            database, int(category_id_text), int(page_text)
        )
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.startswith("product:"))
    async def product_card(callback: CallbackQuery) -> None:
        product_id = int((callback.data or "").split(":")[1])
        product = database.get_product(product_id)
        await callback.answer()
        if product is None or not product["is_active"]:
            await _edit_callback_message(callback, "Товар больше недоступен.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(f"{weight} г", f"weight:{product_id}:{weight}")
                    for weight in WEIGHT_OPTIONS[:3]
                ],
                [
                    _button(f"{weight} г", f"weight:{product_id}:{weight}")
                    for weight in WEIGHT_OPTIONS[3:]
                ],
                [_button("✍️ Свой вес", f"weight:{product_id}:custom")],
            ]
        )
        await _edit_callback_message(
            callback,
            f"{CATEGORY_EMOJIS.get(str(product['category']), '🍵')} "
            f"<b>{escape(str(product['name']))}</b>\n"
            f"Категория: {escape(_category_label(str(product['category'])))}\n"
            f"Цена: <b>{format_rubles(product['price_per_10g'])}</b> за 10 г\n"
            "Выберите граммовку:",
            keyboard,
        )

    @router.callback_query(F.data.startswith("weight:"))
    async def select_weight(
        callback: CallbackQuery, state: FSMContext, user_id: int
    ) -> None:
        _, product_id_text, weight_text = (callback.data or "").split(":")
        product_id = int(product_id_text)
        product = database.get_product(product_id)
        if product is None or not product["is_active"]:
            await callback.answer()
            await _edit_callback_message(callback, "Товар больше недоступен.")
            return
        if weight_text == "custom":
            await callback.answer()
            await state.update_data(product_id=product_id)
            await state.set_state(Form.custom_weight)
            await _edit_callback_message(
                callback,
                "Введите граммовку целым числом (минимум 5 г).",
                _cancel_markup(),
            )
            return
        weight = _parse_positive_int(weight_text, "Граммовка", 5)
        try:
            database.add_to_cart(user_id, product_id, weight)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        logger.info(
            "Product added to cart: user_id=%s product_id=%s weight_g=%s",
            user_id,
            product_id,
            weight,
        )
        await state.clear()
        await _finish_cart_add(callback, database)

    @router.callback_query(F.data.startswith("cart:add:"))
    async def add_product_to_cart(
        callback: CallbackQuery, user_id: int, state: FSMContext
    ) -> None:
        _, _, product_id_text, weight_text = (callback.data or "").split(":")
        try:
            database.add_to_cart(user_id, int(product_id_text), int(weight_text))
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        logger.info(
            "Product added to cart: user_id=%s product_id=%s weight_g=%s",
            user_id,
            product_id_text,
            weight_text,
        )
        await state.clear()
        await _finish_cart_add(callback, database)

    @router.message(F.text.in_({"🔎 Поиск", "Поиск"}))
    async def begin_search(message: Message, state: FSMContext) -> None:
        await state.set_state(Form.searching)
        await message.answer(
            "🔎 <b>Поиск товара</b>\nВведите часть названия:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[_button("↩️ Отмена", "search:cancel")]]
            ),
            parse_mode=ParseMode.HTML,
        )

    @router.callback_query(F.data == "search:cancel")
    async def cancel_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        text, markup = _catalog_view(database)
        await _edit_callback_message(callback, text, markup)

    @router.message(F.text.in_({"🛒 Корзина", "Корзина"}))
    async def cart(message: Message, user_id: int) -> None:
        await show_cart(message, user_id)

    @router.callback_query(F.data.startswith("cart:weight:"))
    async def begin_cart_weight(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        item_id = int((callback.data or "").split(":")[2])
        await state.update_data(item_id=item_id)
        await state.set_state(Form.cart_weight)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "Введите новую граммовку (минимум 5 г).",
            _cancel_markup(),
        )

    @router.callback_query(F.data.startswith("cart:delete:"))
    async def delete_cart_item(
        callback: CallbackQuery, user_id: int
    ) -> None:
        item_id = int((callback.data or "").split(":")[2])
        removed = database.remove_cart_item(user_id, item_id)
        logger.info(
            "Cart item removal %s: user_id=%s item_id=%s",
            "succeeded" if removed else "not_found",
            user_id,
            item_id,
        )
        await callback.answer("Позиция удалена." if removed else "Позиция не найдена.")
        await edit_cart(callback, user_id)

    @router.callback_query(F.data == "cart:clear")
    async def ask_clear_cart(callback: CallbackQuery) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("🗑 Очистить", "cart:clear:confirm"),
                    _button("↩️ Отмена", "cart:clear:cancel"),
                ]
            ]
        )
        await callback.answer()
        await _edit_callback_message(
            callback, "⚠️ <b>Очистить всю корзину?</b>", markup
        )

    @router.callback_query(F.data == "cart:clear:confirm")
    async def clear_cart(callback: CallbackQuery, user_id: int) -> None:
        database.clear_cart(user_id)
        logger.info("Cart cleared: user_id=%s", user_id)
        await callback.answer("Корзина очищена.")
        await edit_cart(callback, user_id)

    @router.callback_query(F.data == "cart:clear:cancel")
    async def cancel_clear_cart(callback: CallbackQuery, user_id: int) -> None:
        await callback.answer()
        await edit_cart(callback, user_id)

    @router.callback_query(F.data == "cart:show")
    async def open_cart(callback: CallbackQuery, user_id: int) -> None:
        await callback.answer()
        await edit_cart(callback, user_id)

    @router.callback_query(F.data == "checkout:start")
    async def checkout_start(callback: CallbackQuery) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("💵 Наличные", "checkout:cash")],
                [_button("💳 Перевод", "checkout:transfer")],
                [_button("🏧 Терминал", "checkout:terminal")],
                [_button("💵 + 💳 + 🏧 Разделить оплату", "checkout:split")],
                [_button("↩️ Отмена", "flow:cancel")],
            ]
        )
        await callback.answer()
        await _edit_callback_message(
            callback, "💳 <b>Оплата заказа</b>\nВыберите способ оплаты:", markup
        )

    @router.callback_query(
        F.data.in_({"checkout:cash", "checkout:transfer", "checkout:terminal"})
    )
    async def checkout_single_payment(
        callback: CallbackQuery, user_id: int, state: FSMContext
    ) -> None:
        items = database.list_cart_items(user_id)
        total = sum(int(item["total_price"]) for item in items)
        if not items:
            await callback.answer("Корзина пуста.", show_alert=True)
            return
        cash = total if callback.data == "checkout:cash" else 0
        transfer = total if callback.data == "checkout:transfer" else 0
        terminal = total if callback.data == "checkout:terminal" else 0
        try:
            order_id = database.checkout(user_id, cash, transfer, terminal)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        logger.info(
            "Order completed: user_id=%s order_id=%s total=%s payment_method=%s",
            user_id,
            order_id,
            total,
            (
                "cash"
                if cash
                else "transfer"
                if transfer
                else "terminal"
            ),
        )
        await state.clear()
        await callback.answer("Заказ оформлен.")
        await _edit_callback_message(
            callback,
            f"✅ <b>Заказ №{order_id} оформлен</b>\n"
            f"Сумма: <b>{format_rubles(total)}</b>",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button("📦 К истории заказов", "orders:0")]
                ]
            ),
        )

    @router.callback_query(F.data == "checkout:split")
    async def checkout_split(
        callback: CallbackQuery, state: FSMContext, user_id: int
    ) -> None:
        items = database.list_cart_items(user_id)
        total = sum(int(item["total_price"]) for item in items)
        if not items:
            await callback.answer("Корзина пуста.", show_alert=True)
            return
        await state.update_data(order_total=total)
        await state.set_state(Form.split_cash)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "💵 <b>Смешанная оплата</b>\n"
            f"Сумма заказа: <b>{format_rubles(total)}</b>\n\n"
            "<b>Шаг 1 из 3 — наличные.</b> Введите сумму наличными "
            "в целых рублях.\n"
            "Затем бот попросит сумму перевода и оплаты терминалом. "
            "Сумма всех трёх частей должна равняться сумме заказа.",
            _cancel_markup(),
        )

    @router.message(F.text.in_({"🧾 Ручные товары", "Ручные товары"}))
    async def manual_products(message: Message) -> None:
        rows = database.list_manual_products()
        buttons = [
            [
                _button(
                    f"✍️ {row['name']} · {format_rubles(row['price_per_10g'])} за 10 г",
                    f"manual:edit:{row['id']}",
                ),
                _button("🗑 Удалить", f"manual:delete:{row['id']}"),
            ]
            for row in rows
        ]
        buttons.append([_button("➕ Добавить позицию", "manual:add")])
        await message.answer(
            "🧾 <b>Ручные товары</b>\n"
            "Выберите позицию для изменения или удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.HTML,
        )

    @router.callback_query(F.data == "manual:add")
    async def begin_manual_add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Form.manual_name)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "Введите название товара.",
            _cancel_markup(),
        )

    @router.callback_query(F.data.startswith("manual:edit:"))
    async def begin_manual_edit(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        product_id = int((callback.data or "").split(":")[2])
        rows = {int(row["id"]): row for row in database.list_manual_products()}
        row = rows.get(product_id)
        await callback.answer()
        if row is None:
            await _edit_callback_message(callback, "Ручной товар не найден.")
            return
        await state.update_data(product_id=product_id)
        await state.set_state(Form.edit_manual_name)
        await _edit_callback_message(
            callback,
            f"Текущее название: <b>{escape(str(row['name']))}</b>\n"
            "Введите новое название.",
            _cancel_markup(),
        )

    @router.callback_query(F.data.regexp(r"^manual:delete:\d+$"))
    async def ask_manual_delete(callback: CallbackQuery) -> None:
        product_id = int((callback.data or "").split(":")[2])
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("🗑 Удалить", f"manual:delete:confirm:{product_id}"),
                    _button("↩️ Отмена", "manual:delete:cancel"),
                ]
            ]
        )
        await callback.answer()
        await _edit_callback_message(
            callback, "⚠️ <b>Удалить ручной товар из каталога?</b>", markup
        )

    @router.callback_query(F.data.startswith("manual:delete:confirm:"))
    async def delete_manual_product(
        callback: CallbackQuery, user_id: int
    ) -> None:
        product_id = int((callback.data or "").split(":")[3])
        deleted = database.deactivate_manual_product(product_id)
        logger.info(
            "Manual product deletion %s: user_id=%s product_id=%s",
            "succeeded" if deleted else "not_found",
            user_id,
            product_id,
        )
        await callback.answer("Товар удалён." if deleted else "Товар не найден.")
        await _edit_callback_message(
            callback,
            "Ручной товар удалён из каталога."
            if deleted
            else "Ручной товар не найден или уже удалён.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button("🧾 К ручным товарам", "manual:list")]
                ]
            ),
        )

    @router.callback_query(F.data == "manual:delete:cancel")
    async def cancel_manual_delete(callback: CallbackQuery) -> None:
        rows = database.list_manual_products()
        buttons = [
            [
                _button(
                    f"{row['name']} — {format_rubles(row['price_per_10g'])} за 10 г",
                    f"manual:edit:{row['id']}",
                ),
                _button("🗑 Удалить", f"manual:delete:{row['id']}"),
            ]
            for row in rows
        ]
        buttons.append([_button("➕ Добавить позицию", "manual:add")])
        await callback.answer()
        await _edit_callback_message(
            callback,
            "🧾 <b>Ручные товары</b>\n"
            "Выберите позицию для изменения или удаления:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data == "manual:list")
    async def reopen_manual_products(callback: CallbackQuery) -> None:
        rows = database.list_manual_products()
        buttons = [
            [
                _button(
                    f"{row['name']} — {format_rubles(row['price_per_10g'])} за 10 г",
                    f"manual:edit:{row['id']}",
                ),
                _button("🗑 Удалить", f"manual:delete:{row['id']}"),
            ]
            for row in rows
        ]
        buttons.append([_button("➕ Добавить позицию", "manual:add")])
        await callback.answer()
        await _edit_callback_message(
            callback,
            "🧾 <b>Ручные товары</b>\n"
            "Выберите позицию для изменения или удаления:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.message(F.text.in_({"🔄 Синхронизировать каталог", "Синхронизировать каталог"}))
    async def sync_catalog(message: Message, user_id: int) -> None:
        status = await message.answer("Получаю каталог сайта…")
        try:
            products, parser_errors = await fetch_tea_prices()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.exception(
                "Catalog fetch failed: user_id=%s",
                user_id,
            )
            await status.edit_text(
                f"⚠️ <b>Не удалось получить каталог</b>\n{escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            updated, errors, changes = database.sync_site_products(products)
        except sqlite3.Error as error:
            logger.exception(
                "Catalog database synchronization failed: user_id=%s",
                user_id,
            )
            await status.edit_text(
                "⚠️ <b>Ошибка базы данных при синхронизации</b>\n"
                f"{escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
            return
        logger.info(
            "Catalog synchronization completed: user_id=%s products=%s updated=%s "
            "errors=%s price_changes=%s",
            user_id,
            len(products),
            updated,
            errors + parser_errors,
            len(changes),
        )
        await status.edit_text(
            "🔄 <b>Синхронизация завершена</b>\n"
            f"Обновлено: <b>{updated}</b>\n"
            f"Ошибок: <b>{errors + parser_errors}</b>",
            parse_mode=ParseMode.HTML,
        )
        for change in changes:
            await message.answer(
                f"💸 <b>Изменилась цена</b>\n"
                f"{escape(str(change['name']))}\n"
                f"Было: {format_rubles(change['old_price'])}\n"
                f"Стало: <b>{format_rubles(change['new_price'])}</b>",
                parse_mode=ParseMode.HTML,
            )

    @router.message(F.text.in_({"📦 История заказов", "История заказов"}))
    async def order_history(message: Message) -> None:
        await show_orders(message, database, 0)

    @router.callback_query(F.data.startswith("orders:"))
    async def orders_page(callback: CallbackQuery) -> None:
        page = int((callback.data or "").split(":")[1])
        text, markup = orders_view(database, page)
        await callback.answer()
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.regexp(r"^order:\d+$"))
    async def order_details(callback: CallbackQuery) -> None:
        order_id = int((callback.data or "").split(":")[1])
        details = database.get_order(order_id)
        await callback.answer()
        if details is None:
            await _edit_callback_message(callback, "Заказ не найден.")
            return
        order = details["order"]
        lines = [
            f"📦 <b>Заказ №{order['id']}</b>",
            f"🕒 {_message_datetime(order['created_at'])}",
            f"👤 Telegram ID: {order['telegram_id']}",
            "",
        ]
        for item in details["items"]:
            if item["is_service"]:
                lines.append(
                    f"• <b>{escape(str(item['product_name']))}</b> · "
                    f"{format_rubles(item['total_price'])}"
                )
            else:
                lines.append(
                    f"• <b>{escape(str(item['product_name']))}</b>\n"
                    f"  {item['weight_g']} г · {format_rubles(item['total_price'])} "
                    f"({format_rubles(item['unit_price_snapshot'])} за 10 г)"
                )
        lines.extend(
            [
                "",
                f"<b>Итого: {format_rubles(order['total_amount'])}</b>",
                f"💳 Перевод: {format_rubles(order['transfer_amount'])}",
                f"💵 Наличные: {format_rubles(order['cash_amount'])}",
                f"🏧 Терминал: {format_rubles(order['terminal_amount'])}",
            ]
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Изменить суммы", f"order:edit:{order_id}")],
                [_button("📝 История правок", f"order:audit:{order_id}")],
                [_button("🗑 Удалить заказ", f"order:delete:{order_id}")],
                [_button("‹ Назад к списку заказов", "orders:0")],
            ]
        )
        await _edit_callback_message(callback, "\n".join(lines), markup)

    @router.callback_query(F.data.regexp(r"^order:delete:\d+$"))
    async def confirm_order_deletion(callback: CallbackQuery) -> None:
        order_id = int((callback.data or "").rsplit(":", 1)[1])
        if database.get_order(order_id) is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        await callback.answer()
        await _edit_callback_message(
            callback,
            f"⚠️ <b>Удалить заказ №{order_id} из истории?</b>\n"
            "Будут удалены его позиции, информация об оплате и журнал правок. "
            "Действие нельзя отменить.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        _button("🗑 Да, удалить", f"order:delete:confirm:{order_id}"),
                        _button("↩️ Отмена", f"order:{order_id}"),
                    ]
                ]
            ),
        )

    @router.callback_query(F.data.regexp(r"^order:delete:confirm:\d+$"))
    async def delete_order_from_history(
        callback: CallbackQuery, user_id: int
    ) -> None:
        order_id = int((callback.data or "").rsplit(":", 1)[1])
        try:
            deleted = database.delete_order(order_id)
        except sqlite3.Error:
            logger.exception(
                "Order deletion failed: user_id=%s order_id=%s",
                user_id,
                order_id,
            )
            await callback.answer(
                "Не удалось удалить заказ из-за ошибки базы данных.",
                show_alert=True,
            )
            return
        if not deleted:
            await callback.answer("Заказ уже удалён или не найден.", show_alert=True)
            return

        logger.info("Order deleted from history: user_id=%s order_id=%s", user_id, order_id)
        await callback.answer("Заказ удалён.")
        text, markup = orders_view(database, 0)
        await _edit_callback_message(callback, text, markup)

    @router.callback_query(F.data.startswith("order:audit:"))
    async def order_audit(callback: CallbackQuery) -> None:
        order_id = int((callback.data or "").split(":")[2])
        entries = database.list_audit_log(order_id)
        await callback.answer()
        if not entries:
            await _edit_callback_message(
                callback,
                "Для этого заказа изменений нет.",
                InlineKeyboardMarkup(
                    inline_keyboard=[[_button("📦 К заказу", f"order:{order_id}")]]
                ),
            )
            return
        lines = [f"📝 <b>Изменения заказа №{order_id}</b>"]
        for entry in entries:
            lines.append(
                f"• {_message_datetime(entry['created_at'])} · "
                f"ID {entry['telegram_id']}\n"
                f"{escape(str(entry['field_name']))}: "
                f"{escape(str(entry['old_value']))} → "
                f"<b>{escape(str(entry['new_value']))}</b>"
            )
        await _edit_callback_message(
            callback,
            "\n".join(lines),
            InlineKeyboardMarkup(
                inline_keyboard=[[_button("📦 К заказу", f"order:{order_id}")]]
            ),
        )

    @router.callback_query(F.data.startswith("order:edit:"))
    async def begin_order_edit(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        order_id = int((callback.data or "").split(":")[2])
        if database.get_order(order_id) is None:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        await state.update_data(order_id=order_id)
        await state.set_state(Form.edit_order_total)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "Введите новую общую сумму заказа в целых рублях. "
            "После этого укажите наличные, перевод и терминал.",
            _cancel_markup(),
        )

    @router.message(F.text.in_({"📊 Отчёт за сегодня", "Отчёт за сегодня"}))
    async def today_report(message: Message, telegram_user_id: int) -> None:
        today = datetime.now(MOSCOW_TZ).date().isoformat()
        report = database.today_report(today)
        logger.info(
            "Daily report viewed: date=%s total=%s cash=%s transfer=%s "
            "terminal=%s orders=%s",
            today,
            report["total"],
            report["cash"],
            report["transfer"],
            report["terminal"],
            report["order_count"],
        )
        await message.answer(
            f"📊 <b>Отчёт за {datetime.now(MOSCOW_TZ):%d.%m.%Y}</b>\n\n"
            f"📦 Заказов: {report['order_count']}\n"
            f"💳 Переводы: {format_rubles(report['transfer'])}\n"
            f"💵 Наличные: {format_rubles(report['cash'])}\n"
            f"🏧 Терминал: {format_rubles(report['terminal'])}\n\n"
            f"<b>Итого: {format_rubles(report['total'])}</b>",
            reply_markup=settings_keyboard(telegram_user_id),
            parse_mode=ParseMode.HTML,
        )

    async def show_cart(message: Message, user_id: int) -> None:
        text, markup = cart_view(database, user_id)
        await message.answer(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )

    async def edit_cart(callback: CallbackQuery, user_id: int) -> None:
        text, markup = cart_view(database, user_id)
        await _edit_callback_message(callback, text, markup)

    def cart_view(
        db: Database, user_id: int
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        items = db.list_cart_items(user_id)
        if not items:
            return (
                "🛒 <b>Корзина пуста</b>\nДобавьте чай из каталога.",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [_button("🍵 Перейти в каталог", "catalog:categories")]
                    ]
                ),
            )
        lines = ["🛒 <b>Корзина</b>", ""]
        buttons: list[list[InlineKeyboardButton]] = []
        total = 0
        for item in items:
            line_total = int(item["total_price"])
            total += line_total
            if item["is_service"]:
                lines.append(
                    f"• <b>{escape(str(item['name']))}</b> · "
                    f"{format_rubles(line_total)}"
                )
                buttons.append(
                    [_button("🗑 Удалить", f"cart:delete:{item['id']}")]
                )
            else:
                lines.append(
                    f"• <b>{escape(str(item['name']))}</b>\n"
                    f"  {item['weight_g']} г · {format_rubles(line_total)}"
                )
                buttons.append(
                    [
                        _button("⚖️ Изменить вес", f"cart:weight:{item['id']}"),
                        _button("🗑 Удалить", f"cart:delete:{item['id']}"),
                    ]
                )
        lines.extend(
            [
                "",
                f"Позиций: {len(items)}",
                f"<b>Итого: {format_rubles(total)}</b>",
            ]
        )
        buttons.extend(
            [
                [_button("✅ Оформить заказ", "checkout:start")],
                [_button("🗑 Очистить корзину", "cart:clear")],
            ]
        )
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    def orders_view(
        db: Database, page: int
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        orders = db.list_orders(PAGE_SIZE, page * PAGE_SIZE)
        if not orders and page == 0:
            return (
                "📦 <b>Заказов пока нет</b>",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [_button("🍵 Каталог", "catalog:categories")]
                    ]
                ),
            )
        buttons = [
            [
                _button(
                    _order_history_label(
                        str(order["created_at"]), int(order["total_amount"])
                    ),
                    f"order:{order['id']}",
                )
            ]
            for order in orders
        ]
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(_button("‹ Назад", f"orders:{page - 1}"))
        if len(orders) == PAGE_SIZE:
            navigation.append(_button("Далее ›", f"orders:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        return (
            "📦 <b>История заказов</b>\nВыберите заказ 👇:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def show_orders(message: Message, db: Database, page: int) -> None:
        text, markup = orders_view(db, page)
        await message.answer(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )

    return router
