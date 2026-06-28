from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, types

from .config import BOT_TOKEN, MAILCAT_API, logger
from .db import delete_user, get_user, save_user
from .keyboards import back_keyboard, confirm_delete_keyboard, main_keyboard_no_account, main_keyboard_with_account
from .mailcat import check_mailcat, create_mailcat_mailbox
from .storage import bot_messages, user_accounts_cache

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)


def _safe_preview(value, limit):
    value = str(value or '').strip()
    return value[:limit]


def format_main_screen(account):
    valid_messages = [m for m in account['messages'] if isinstance(m, dict)]
    msg_count = len(valid_messages)
    return (
        "📬 **Ваш временный ящик готов**\n\n"
        f"`{account['email']}`\n\n"
        f"📨 **Писем:** {msg_count}\n"
        "🔎 Нажмите «Проверить почту», чтобы обновить входящие."
    )


def format_messages_list(messages):
    text = f"📩 **Входящие ({len(messages)}):**\n\n"
    for i, msg in enumerate(messages[-10:][::-1], 1):
        time = datetime.fromisoformat(msg.get('received_at', datetime.now().isoformat())).strftime('%H:%M')
        text += f"**{i}.** 🕒 `{time}` — **{_safe_preview(msg.get('subject', '(no subject)'), 35)}**\n"
        text += f"   👤 {_safe_preview(msg.get('sender', 'unknown'), 30)}\n"
        if msg.get('code'):
            text += f"   🔑 Код: `{msg['code']}`\n"
        if msg.get('links') and msg['links'][0]:
            text += f"   🔗 {msg['links'][0]}\n"
        text += "\n"
    if len(messages) > 10:
        text += f"…и ещё {len(messages)-10}\n"
    text += f"📌 **Всего писем:** {len(messages)}"
    return text


async def delete_user_message(message):
    try:
        await bot.delete_message(str(message.from_user.id), message.message_id)
    except:
        pass


async def send_bot_message(user_id, text, reply_markup=None):
    user_id = str(user_id)
    old_msg_id = bot_messages.get(user_id)
    if old_msg_id:
        try:
            await bot.delete_message(user_id, old_msg_id)
        except:
            pass
        bot_messages.pop(user_id, None)

    try:
        sent = await bot.send_message(
            user_id,
            text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        bot_messages[user_id] = sent.message_id
        return sent
    except Exception as e:
        logger.warning(f"Markdown failed, sending plain: {e}")
        try:
            sent = await bot.send_message(
                user_id,
                text,
                reply_markup=reply_markup
            )
            bot_messages[user_id] = sent.message_id
            return sent
        except Exception as e2:
            logger.error(f"Send error: {e2}")
            return None


async def show_main_screen(user_id):
    user_id = str(user_id)
    account = user_accounts_cache.get(user_id)
    if not account:
        account = await get_user(user_id)
        if account:
            user_accounts_cache[user_id] = account
    if not account:
        await send_bot_message(
            user_id,
            "📧 **Временная почта**\n\nСоздайте email для быстрой регистрации — бот покажет коды и ссылки из входящих.",
            main_keyboard_no_account()
        )
        return
    if not isinstance(account.get('messages'), list):
        account['messages'] = []
    await send_bot_message(user_id, format_main_screen(account), main_keyboard_with_account())


@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    user_id = str(message.from_user.id)
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if account:
        user_accounts_cache[user_id] = account
        await show_main_screen(user_id)
    else:
        await send_bot_message(
            user_id,
            "👋 **Добро пожаловать!**\n\n📧 Создайте временную почту в один клик.\n🔑 Коды и ссылки подтверждения появятся прямо здесь.",
            main_keyboard_no_account()
        )


@dp.message_handler(lambda message: message.text == "📧 Создать почту")
async def create_handler(message: types.Message):
    user_id = str(message.from_user.id)
    await delete_user_message(message)
    if user_id in user_accounts_cache:
        await show_main_screen(user_id)
        return
    await send_bot_message(user_id, "⏳ **Создаю ящик...**", None)
    try:
        account = await create_mailcat_mailbox()
        user_accounts_cache[user_id] = account
        await save_user(user_id, account)
        await show_main_screen(user_id)
    except Exception as e:
        logger.error(f"Create error: {e}")
        await send_bot_message(
            user_id,
            f"❌ **Ошибка создания**\n\n{str(e)[:200]}",
            main_keyboard_no_account()
        )


@dp.message_handler(lambda message: message.text == "📨 Проверить почту")
async def check_handler(message: types.Message):
    user_id = str(message.from_user.id)
    await delete_user_message(message)
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await show_main_screen(user_id)
        return
    if not isinstance(account.get('messages'), list):
        account['messages'] = []
    if not isinstance(account.get('read_ids'), list):
        account['read_ids'] = []
    await send_bot_message(user_id, "🔄 **Проверяю входящие...**", None)
    try:
        new = await check_mailcat(account)
        if new:
            account['messages'].extend(new)
            await save_user(user_id, account)
        valid_messages = [m for m in account['messages'] if isinstance(m, dict)]
        if not valid_messages:
            await send_bot_message(
                user_id,
                "📭 **Пока пусто**\n\nПисьма ещё не пришли. Попробуйте проверить ещё раз через несколько секунд.",
                back_keyboard()
            )
            return
        await send_bot_message(user_id, format_messages_list(valid_messages), back_keyboard())
    except Exception as e:
        logger.error(f"Check error: {e}")
        await send_bot_message(
            user_id,
            f"❌ **Ошибка проверки**\n\n{str(e)[:200]}",
            back_keyboard()
        )


@dp.message_handler(lambda message: message.text == "🔙 Назад")
async def back_handler(message: types.Message):
    await delete_user_message(message)
    await show_main_screen(str(message.from_user.id))


@dp.message_handler(lambda message: message.text == "🗑 Удалить ящик")
async def delete_handler(message: types.Message):
    user_id = str(message.from_user.id)
    await delete_user_message(message)
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await show_main_screen(user_id)
        return
    await send_bot_message(
        user_id,
        f"⚠️ **Удалить этот ящик?**\n\n`{account['email']}`\n\nПисьма и история по нему будут удалены.",
        confirm_delete_keyboard()
    )


@dp.message_handler(lambda message: message.text == "✅ Да")
async def confirm_delete_handler(message: types.Message):
    user_id = str(message.from_user.id)
    await delete_user_message(message)
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await show_main_screen(user_id)
        return
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {account['token']}"}
            await session.delete(f"{MAILCAT_API}/mailboxes", headers=headers)
    except:
        pass
    user_accounts_cache.pop(user_id, None)
    await delete_user(user_id)
    await send_bot_message(
        user_id,
        "🗑 **Ящик удалён**\n\nМожно сразу создать новый временный email.",
        main_keyboard_no_account()
    )


@dp.message_handler(lambda message: message.text == "❌ Нет")
async def cancel_delete_handler(message: types.Message):
    await delete_user_message(message)
    await show_main_screen(str(message.from_user.id))


@dp.message_handler()
async def any_message(message: types.Message):
    await delete_user_message(message)
    await show_main_screen(str(message.from_user.id))
