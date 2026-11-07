from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from settings import LOG_EXPORT_USER_ID


def calculate_price(weight_g: int, price_per_10g: int) -> int:
    if weight_g < 1:
        raise ValueError("Вес должен быть положительным числом.")
    if price_per_10g < 1:
        raise ValueError("Цена должна быть положительным числом.")
    return (weight_g * price_per_10g + 9) // 10


def format_rubles(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🍵 Каталог"), KeyboardButton(text="🔎 Поиск")],
            [KeyboardButton(text="🛒 Корзина"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def settings_keyboard(telegram_user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text="🧾 Ручные товары"),
            KeyboardButton(text="📦 История заказов"),
        ],
        [
            KeyboardButton(text="💰 Изменить цену товара"),
            KeyboardButton(text="📊 Отчёт за сегодня"),
        ],
        [KeyboardButton(text="🔄 Синхронизировать каталог")],
        [KeyboardButton(text="👤 Добавить пользователя")],
        [KeyboardButton(text="🚫 Удалить пользователя")],
    ]
    if telegram_user_id == LOG_EXPORT_USER_ID:
        keyboard.append([KeyboardButton(text="📤 Выгрузить логи")])
        keyboard.append([KeyboardButton(text="🗄 Выгрузить базу")])
    keyboard.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )