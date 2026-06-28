from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_keyboard_no_account():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add(KeyboardButton("📧 Создать почту"))
    return keyboard


def main_keyboard_with_account():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("📨 Проверить почту"),
        KeyboardButton("🗑 Удалить ящик")
    )
    return keyboard


def back_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard


def confirm_delete_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("✅ Да"),
        KeyboardButton("❌ Нет")
    )
    return keyboard
