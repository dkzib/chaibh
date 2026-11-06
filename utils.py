from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


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
            [KeyboardButton(text="🛒 Корзина"), KeyboardButton(text="📦 История заказов")],
            [KeyboardButton(text="📊 Отчёт за сегодня")],
            [KeyboardButton(text="🧾 Ручные товары"), KeyboardButton(text="🔄 Синхронизировать каталог")],
        ],
        resize_keyboard=True,
    )