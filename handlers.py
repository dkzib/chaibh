import asyncio
from html import escape
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ErrorEvent,
    Message,
    TelegramObject,
)

from db import Database
from price_parser import fetch_tea_prices
from utils import calculate_price, format_rubles, main_keyboard


MOSCOW_TZ = timezone(timedelta(hours=3), "Europe/Moscow")
PAGE_SIZE = 10
WEIGHT_OPTIONS = (10, 20, 30, 50, 100)
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
    custom_weight = State()
    cart_weight = State()
    manual_name = State()
    manual_price = State()
    edit_manual_name = State()
    edit_manual_price = State()
    split_cash = State()
    split_transfer = State()
    edit_order_total = State()
    edit_order_cash = State()
    edit_order_transfer = State()


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
        return await handler(event, data)


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _category_label(name: str) -> str:
    emoji = CATEGORY_EMOJIS.get(name, "🍵")
    return f"{emoji} {name}"


def _parse_positive_int(text: str, field_name: str, minimum: int = 1) -> int:
    try:
        value = int(text.strip())
    except ValueError as error:
        raise ValueError(f"{field_name} нужно указать целым числом.") from error
    if value < minimum:
        raise ValueError(f"{field_name} должно быть не меньше {minimum}.")
    return value


def _message_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")


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


def create_router(database: Database) -> Router:
    router = Router()
    access_middleware = AccessMiddleware(database)
    router.message.outer_middleware(access_middleware)
    router.callback_query.outer_middleware(access_middleware)

    @router.error()
    async def report_database_error(event: ErrorEvent) -> bool:
        if not isinstance(event.exception, sqlite3.Error):
            raise event.exception
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

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "↩️ <b>Действие отменено</b>",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.searching))
    async def search_products(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        await state.clear()
        if not query:
            await message.answer("Введите непустую часть названия.")
            return
        results = database.search_products(query)
        if not results:
            await message.answer(
                "🔎 <b>Ничего не найдено</b>\nПопробуйте изменить запрос.",
                parse_mode=ParseMode.HTML,
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(
                        f"{CATEGORY_EMOJIS.get(str(row['category']), '🍵')} "
                        f"{row['name']} · {format_rubles(row['price_per_10g'])}/10 г",
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

    @router.message(StateFilter(Form.custom_weight))
    async def custom_weight(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            weight = _parse_positive_int(message.text or "", "Граммовка", 5)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите вес ещё раз или /cancel."
            )
            return
        product = database.get_product(int(data["product_id"]))
        await state.clear()
        if product is None or not product["is_active"]:
            await message.answer("Товар больше недоступен.")
            return
        await _show_weight_preview(
            message, int(product["id"]), weight, str(product["name"]),
            int(product["price_per_10g"]),
        )

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
                f"⚠️ {error}\nВведите вес ещё раз или /cancel."
            )
            return
        await state.clear()
        await show_cart(message, user_id)

    @router.message(StateFilter(Form.manual_name))
    async def manual_product_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name or len(name) > 200:
            await message.answer(
                "⚠️ Название должно содержать от 1 до 200 символов."
            )
            return
        await state.update_data(name=name)
        await state.set_state(Form.manual_price)
        await message.answer("💰 Введите цену за 10 г в целых рублях.")

    @router.message(StateFilter(Form.manual_price))
    async def manual_product_price(
        message: Message, state: FSMContext
    ) -> None:
        try:
            price = _parse_positive_int(message.text or "", "Цена")
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите цену ещё раз или /cancel."
            )
            return
        data = await state.get_data()
        product_id = database.add_manual_product(str(data["name"]), price)
        await state.clear()
        await message.answer(
            f"✅ <b>Товар добавлен</b>\n"
            f"{escape(str(data['name']))}\n"
            f"{format_rubles(price)} за 10 г",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    @router.message(StateFilter(Form.edit_manual_name))
    async def manual_edit_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name or len(name) > 200:
            await message.answer(
                "⚠️ Название должно содержать от 1 до 200 символов."
            )
            return
        await state.update_data(name=name)
        await state.set_state(Form.edit_manual_price)
        await message.answer("💰 Введите новую цену за 10 г в целых рублях.")

    @router.message(StateFilter(Form.edit_manual_price))
    async def manual_edit_price(
        message: Message, state: FSMContext
    ) -> None:
        try:
            price = _parse_positive_int(message.text or "", "Цена")
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите цену ещё раз или /cancel."
            )
            return
        data = await state.get_data()
        if not database.update_manual_product(
            int(data["product_id"]), str(data["name"]), price
        ):
            await message.answer("⚠️ Ручной товар не найден или уже удалён.")
        else:
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
                f"⚠️ {error}\nВведите сумму ещё раз или /cancel."
            )
            return
        await state.update_data(cash=cash)
        await state.set_state(Form.split_transfer)
        await message.answer("💳 Введите сумму перевода в целых рублях.")

    @router.message(StateFilter(Form.split_transfer))
    async def split_transfer_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            transfer = _parse_positive_int(message.text or "", "Перевод", 0)
            order_id = database.checkout(user_id, int(data["cash"]), transfer)
        except ValueError as error:
            await message.answer(f"⚠️ {error}\nПроверьте суммы или /cancel.")
            return
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
                f"⚠️ {error}\nВведите сумму ещё раз или /cancel."
            )
            return
        await state.update_data(total=total)
        await state.set_state(Form.edit_order_cash)
        await message.answer("💵 Введите новую сумму наличных.")

    @router.message(StateFilter(Form.edit_order_cash))
    async def edit_order_cash_input(message: Message, state: FSMContext) -> None:
        try:
            cash = _parse_positive_int(message.text or "", "Наличные", 0)
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nВведите сумму ещё раз или /cancel."
            )
            return
        await state.update_data(cash=cash)
        await state.set_state(Form.edit_order_transfer)
        await message.answer("💳 Введите новую сумму перевода.")

    @router.message(StateFilter(Form.edit_order_transfer))
    async def edit_order_transfer_input(
        message: Message, state: FSMContext, user_id: int
    ) -> None:
        data = await state.get_data()
        try:
            transfer = _parse_positive_int(message.text or "", "Перевод", 0)
            database.update_order_finances(
                int(data["order_id"]),
                user_id,
                int(data["total"]),
                int(data["cash"]),
                transfer,
            )
        except ValueError as error:
            await message.answer(
                f"⚠️ {error}\nПроверьте суммы или /cancel, чтобы прервать."
            )
            return
        order_id = int(data["order_id"])
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
        categories = database.list_categories()
        if not categories:
            await message.answer(
                "📚 <b>Каталог пока пуст</b>\n"
                "Сначала синхронизируйте каталог с сайтом.",
                parse_mode=ParseMode.HTML,
            )
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(
                        f"{_category_label(str(row['name']))} · {row['product_count']}",
                        f"category:{row['id']}:0",
                    )
                ]
                for row in categories
            ]
        )
        await message.answer(
            "🍵 <b>Каталог чая</b>\nВыберите категорию:",
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )

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
                    f"{row['name']} · {format_rubles(row['price_per_10g'])}/10 г",
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
        categories = database.list_categories()
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button(
                        f"{_category_label(str(row['name']))} · {row['product_count']}",
                        f"category:{row['id']}:0",
                    )
                ]
                for row in categories
            ]
        )
        await _edit_callback_message(
            callback, "🍵 <b>Каталог чая</b>\nВыберите категорию:", markup
        )

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
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        _, product_id_text, weight_text = (callback.data or "").split(":")
        product_id = int(product_id_text)
        product = database.get_product(product_id)
        await callback.answer()
        if product is None or not product["is_active"]:
            await _edit_callback_message(callback, "Товар больше недоступен.")
            return
        if weight_text == "custom":
            await state.update_data(product_id=product_id)
            await state.set_state(Form.custom_weight)
            await _edit_callback_message(
                callback,
                "Введите граммовку целым числом (минимум 5 г), "
                "или отправьте /cancel.",
            )
            return
        weight = _parse_positive_int(weight_text, "Граммовка", 5)
        await _edit_weight_preview(
            callback,
            product_id,
            weight,
            str(product["name"]),
            int(product["price_per_10g"]),
        )

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
        await state.clear()
        await callback.answer("Добавлено в корзину.")
        await _edit_callback_message(
            callback,
            "Позиция добавлена в корзину.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [_button("🛒 Открыть корзину", "cart:show")],
                    [_button("🍵 Продолжить выбор", "catalog:categories")],
                ]
            ),
        )

    @router.message(F.text.in_({"🔎 Поиск", "Поиск"}))
    async def begin_search(message: Message, state: FSMContext) -> None:
        await state.set_state(Form.searching)
        await message.answer(
            "🔎 <b>Поиск товара</b>\nВведите часть названия или отправьте /cancel.",
            parse_mode=ParseMode.HTML,
        )

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
            callback, "Введите новую граммовку (минимум 5 г) или /cancel."
        )

    @router.callback_query(F.data.startswith("cart:delete:"))
    async def delete_cart_item(
        callback: CallbackQuery, user_id: int
    ) -> None:
        item_id = int((callback.data or "").split(":")[2])
        removed = database.remove_cart_item(user_id, item_id)
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
        await callback.answer("Корзина очищена.")
        await edit_cart(callback, user_id)

    @router.callback_query(F.data == "cart:clear:cancel")
    async def cancel_clear_cart(callback: CallbackQuery) -> None:
        await callback.answer("Очистка отменена.")
        await _edit_callback_message(
            callback,
            "Очистка отменена.",
            InlineKeyboardMarkup(
                inline_keyboard=[[_button("🛒 Вернуться в корзину", "cart:show")]]
            ),
        )

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
                [_button("💵 + 💳 Смешанная оплата", "checkout:split")],
            ]
        )
        await callback.answer()
        await _edit_callback_message(
            callback, "💳 <b>Оплата заказа</b>\nВыберите способ оплаты:", markup
        )

    @router.callback_query(F.data.in_({"checkout:cash", "checkout:transfer"}))
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
        try:
            order_id = database.checkout(user_id, cash, transfer)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
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
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.set_state(Form.split_cash)
        await callback.answer()
        await _edit_callback_message(
            callback,
            "💵 <b>Смешанная оплата</b>\n"
            "Введите сумму наличных в рублях. Затем укажите сумму перевода.",
        )

    @router.message(F.text.in_({"🧾 Ручные товары", "Ручные товары"}))
    async def manual_products(message: Message) -> None:
        rows = database.list_manual_products()
        buttons = [
            [
                _button(
                    f"✍️ {row['name']} · {format_rubles(row['price_per_10g'])}/10 г",
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
            callback, "Введите название товара или /cancel."
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
            "Введите новое название или /cancel.",
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
    async def delete_manual_product(callback: CallbackQuery) -> None:
        product_id = int((callback.data or "").split(":")[3])
        deleted = database.deactivate_manual_product(product_id)
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
        await callback.answer("Удаление отменено.")
        await _edit_callback_message(
            callback,
            "Удаление отменено.",
            InlineKeyboardMarkup(
                inline_keyboard=[[_button("🧾 К ручным товарам", "manual:list")]]
            ),
        )

    @router.callback_query(F.data == "manual:list")
    async def reopen_manual_products(callback: CallbackQuery) -> None:
        rows = database.list_manual_products()
        buttons = [
            [
                _button(
                    f"{row['name']} — {format_rubles(row['price_per_10g'])}/10 г",
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
    async def sync_catalog(message: Message) -> None:
        status = await message.answer("Получаю каталог сайта…")
        try:
            products, parser_errors = await fetch_tea_prices()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            await status.edit_text(
                f"⚠️ <b>Не удалось получить каталог</b>\n{escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            updated, errors, changes = database.sync_site_products(products)
        except sqlite3.Error as error:
            await status.edit_text(
                "⚠️ <b>Ошибка базы данных при синхронизации</b>\n"
                f"{escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
            return
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
            lines.append(
                f"• <b>{escape(str(item['product_name']))}</b>\n"
                f"  {item['weight_g']} г · {format_rubles(item['total_price'])} "
                f"({format_rubles(item['unit_price_snapshot'])}/10 г)"
            )
        lines.extend(
            [
                "",
                f"<b>Итого: {format_rubles(order['total_amount'])}</b>",
                f"💵 Наличные: {format_rubles(order['cash_amount'])}",
                f"💳 Перевод: {format_rubles(order['transfer_amount'])}",
            ]
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("✏️ Изменить суммы", f"order:edit:{order_id}")],
                [_button("📝 История правок", f"order:audit:{order_id}")],
            ]
        )
        await _edit_callback_message(callback, "\n".join(lines), markup)

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
            "После этого укажите наличные и перевод.\nДля отмены отправьте /cancel.",
        )

    @router.message(F.text.in_({"📊 Отчёт за сегодня", "Отчёт за сегодня"}))
    async def today_report(message: Message) -> None:
        today = datetime.now(MOSCOW_TZ).date().isoformat()
        report = database.today_report(today)
        await message.answer(
            f"📊 <b>Отчёт за {datetime.now(MOSCOW_TZ):%d.%m.%Y}</b>\n\n"
            f"Всего: <b>{format_rubles(report['total'])}</b>\n"
            f"💵 Наличные: {format_rubles(report['cash'])}\n"
            f"💳 Переводы: {format_rubles(report['transfer'])}\n"
            f"📦 Заказов: {report['order_count']}",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    async def _show_weight_preview(
        message: Message,
        product_id: int,
        weight: int,
        name: str,
        price_per_10g: int,
    ) -> None:
        total = calculate_price(weight, price_per_10g)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("➕ В корзину", f"cart:add:{product_id}:{weight}")]
            ]
        )
        await message.answer(
            f"🍃 <b>{escape(name)}</b>\n"
            f"⚖️ {weight} г · <b>{format_rubles(total)}</b>",
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )

    async def _edit_weight_preview(
        callback: CallbackQuery,
        product_id: int,
        weight: int,
        name: str,
        price_per_10g: int,
    ) -> None:
        total = calculate_price(weight, price_per_10g)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("➕ В корзину", f"cart:add:{product_id}:{weight}")],
                [_button("⚖️ Изменить вес", f"product:{product_id}")],
            ]
        )
        await _edit_callback_message(
            callback,
            f"🍃 <b>{escape(name)}</b>\n"
            f"⚖️ {weight} г · <b>{format_rubles(total)}</b>",
            markup,
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
                    f"№{order['id']} · {order['telegram_id']} · "
                    f"{_message_datetime(order['created_at'])} · "
                    f"{format_rubles(order['total_amount'])}",
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
            "📦 <b>История заказов</b>\nВыберите заказ:",
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def show_orders(message: Message, db: Database, page: int) -> None:
        text, markup = orders_view(db, page)
        await message.answer(
            text, reply_markup=markup, parse_mode=ParseMode.HTML
        )

    return router
