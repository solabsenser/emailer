import asyncio
import logging
import secrets
import string
import aiohttp
import re
import email.header
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))

# ===== ХРАНИЛИЩЕ =====
user_accounts = {}
user_messages = {}

def decode_header_value(value):
    if not value:
        return ''
    try:
        decoded_parts = []
        for part, encoding in email.header.decode_header(value):
            if isinstance(part, bytes):
                try:
                    if encoding:
                        part = part.decode(encoding, errors='ignore')
                    else:
                        part = part.decode('utf-8', errors='ignore')
                except:
                    part = part.decode('utf-8', errors='ignore')
            decoded_parts.append(str(part))
        return ' '.join(decoded_parts)
    except:
        return value

def extract_links(text):
    if not text:
        return []
    url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    links = re.findall(url_pattern, text)
    clean_links = []
    for link in links:
        link = link.strip('.,;:!?()[]{}"\'' )
        if link.startswith('http') or link.startswith('www'):
            clean_links.append(link)
    return clean_links

def extract_code(text):
    if not text:
        return None
    patterns = [
        r'\b\d{4,8}\b',
        r'\b[A-Z0-9]{4,8}\b',
        r'код[:\s]*([A-Z0-9]{4,8})',
        r'verification code[:\s]*([A-Z0-9]{4,8})',
        r'код подтверждения[:\s]*([A-Z0-9]{4,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = match.group(1) if match.groups() else match.group(0)
            if len(code) >= 4:
                return code
    return None

# ===== MAILCAT API =====
MAILCAT_API = "https://api.mailcat.ai"

async def create_mailcat_mailbox():
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{MAILCAT_API}/mailboxes") as resp:
            if resp.status not in [200, 201]:
                error_text = await resp.text()
                raise Exception(f"Failed: {resp.status} - {error_text}")
            data = await resp.json()
            mailbox = data.get('data', {})
            email = mailbox.get('email')
            token = mailbox.get('token')
            if not email or not token:
                raise Exception(f"Invalid response: {data}")
            return {
                'email': email,
                'token': token,
                'messages': [],
                'read_ids': []
            }

async def check_mailcat(account):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {account['token']}"}
        async with session.get(f"{MAILCAT_API}/inbox", headers=headers) as resp:
            if resp.status not in [200, 201]:
                return []
            data = await resp.json()
            messages = data.get('data', [])
        new_messages = []
        for msg in messages:
            msg_id = msg.get('id')
            if msg_id not in account.get('read_ids', []):
                account.setdefault('read_ids', []).append(msg_id)
                async with session.get(f"{MAILCAT_API}/emails/{msg_id}", headers=headers) as resp2:
                    if resp2.status in [200, 201]:
                        full = await resp2.json()
                        email_data = full.get('data', {})
                        raw_subject = email_data.get('email', {}).get('subject', '(no subject)')
                        subject = decode_header_value(raw_subject)
                        raw_from = email_data.get('email', {}).get('from', 'unknown')
                        sender = decode_header_value(raw_from)
                        body = email_data.get('email', {}).get('text', '')
                        if not body:
                            body = email_data.get('email', {}).get('html', '')
                            body = re.sub(r'<[^>]+>', '', body)
                        code = extract_code(body) or email_data.get('code', '')
                        links = extract_links(body)
                        new_messages.append({
                            'sender': sender,
                            'subject': subject,
                            'body': body[:5000],
                            'code': code,
                            'links': links[:5],
                            'received_at': datetime.now()
                        })
        return new_messages

# ===== WEB СЕРВЕР =====
async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    return app

# ===== TELEGRAM БОТ =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== КЛАВИАТУРЫ =====
def main_menu_no_account():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("📧 Создать почту", callback_data="create")
    )
    return keyboard

def main_menu_with_account(email, msg_count, code_count):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(f"📨 Почта ({msg_count})", callback_data="check"),
        InlineKeyboardButton("🗑 Удалить", callback_data="delete")
    )
    return keyboard

def back_menu():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    )
    return keyboard

def confirm_delete_menu():
    """Меню подтверждения удаления"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete"),
        InlineKeyboardButton("❌ Отмена", callback_data="back_to_main")
    )
    return keyboard

async def update_or_send(user_id, text, reply_markup=None):
    msg_id = user_messages.get(user_id)
    if msg_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=user_id,
                message_id=msg_id,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return
            if "message to edit not found" in str(e).lower():
                user_messages.pop(user_id, None)
            else:
                logger.error(f"Edit error: {e}")
                user_messages.pop(user_id, None)
    try:
        sent = await bot.send_message(
            user_id,
            text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        user_messages[user_id] = sent.message_id
    except Exception as e:
        logger.error(f"Send error: {e}")

async def show_main_screen(user_id):
    account = user_accounts.get(str(user_id))
    
    if not account:
        await update_or_send(
            user_id,
            "📧 **Временная почта**\n\n"
            "Создайте временный email для регистрации\n"
            "Письма приходят автоматически",
            main_menu_no_account()
        )
        return
    
    msg_count = len(account.get('messages', []))
    codes = [m.get('code') for m in account.get('messages', []) if m.get('code')]
    code_count = len(codes)
    
    text = f"📧 **Ваш ящик:**\n`{account['email']}`\n\n"
    text += f"📨 Писем: **{msg_count}**\n"
    if code_count:
        text += f"🔑 Кодов: **{code_count}**"
    
    await update_or_send(
        user_id,
        text,
        main_menu_with_account(account['email'], msg_count, code_count)
    )

@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    await show_main_screen(user_id)

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    await show_main_screen(user_id)

@dp.callback_query_handler(lambda c: c.data == "create")
async def create_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    if str(user_id) in user_accounts:
        await show_main_screen(user_id)
        return
    
    try:
        account = await create_mailcat_mailbox()
        user_accounts[str(user_id)] = account
        await show_main_screen(user_id)
    except Exception as e:
        logger.error(f"Create error: {e}")
        await update_or_send(
            user_id,
            f"❌ **Ошибка создания**\n\n{str(e)[:200]}",
            main_menu_no_account()
        )

@dp.callback_query_handler(lambda c: c.data == "check")
async def check_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    account = user_accounts.get(str(user_id))
    if not account:
        await show_main_screen(user_id)
        return
    
    await update_or_send(user_id, "🔄 **Проверяю почту...**", None)
    
    try:
        new = await check_mailcat(account)
        if new:
            account.setdefault('messages', []).extend(new)
        
        messages = account.get('messages', [])
        if not messages:
            await update_or_send(
                user_id,
                "📭 **Писем нет**\n\nНажмите «Назад» для возврата",
                back_menu()
            )
            return
        
        text = f"📩 **Письма ({len(messages)}):**\n\n"
        for i, msg in enumerate(messages[-10:][::-1], 1):
            time = msg['received_at'].strftime('%H:%M')
            text += f"{i}. [{time}] **{msg['subject'][:35]}**\n"
            text += f"   От: {msg['sender'][:30]}\n"
            if msg.get('code'):
                text += f"   🔑 Код: `{msg['code']}`\n"
            if msg.get('links'):
                for link in msg['links'][:1]:
                    text += f"   🔗 {link[:40]}...\n"
            text += "\n"
        
        if len(messages) > 10:
            text += f"... и еще {len(messages)-10} писем\n"
        
        text += f"\n📌 **Всего:** {len(messages)}"
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("🔄 Обновить", callback_data="check"),
            InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
        )
        
        await update_or_send(user_id, text, keyboard)
        
    except Exception as e:
        logger.error(f"Check error: {e}")
        await update_or_send(
            user_id,
            f"❌ **Ошибка проверки**\n\n{str(e)[:200]}",
            back_menu()
        )

@dp.callback_query_handler(lambda c: c.data == "delete")
async def delete_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    account = user_accounts.get(str(user_id))
    if not account:
        await show_main_screen(user_id)
        return
    
    await update_or_send(
        user_id,
        f"⚠️ **Вы уверены, что хотите удалить ящик?**\n\n"
        f"📧 `{account['email']}`\n\n"
        f"Все письма будут удалены безвозвратно.",
        confirm_delete_menu()
    )

@dp.callback_query_handler(lambda c: c.data == "confirm_delete")
async def confirm_delete_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    account = user_accounts.get(str(user_id))
    if not account:
        await show_main_screen(user_id)
        return
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {account['token']}"}
            await session.delete(f"{MAILCAT_API}/mailboxes", headers=headers)
    except:
        pass
    
    del user_accounts[str(user_id)]
    
    await update_or_send(
        user_id,
        "🗑 **Ящик удалён**\n\nСоздайте новый при необходимости",
        main_menu_no_account()
    )

@dp.callback_query_handler(lambda c: c.data == "back_to_main")
async def back_to_main_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    await show_main_screen(user_id)

async def background_check():
    while True:
        try:
            for user_id, account in list(user_accounts.items()):
                try:
                    new = await check_mailcat(account)
                    if new:
                        account.setdefault('messages', []).extend(new)
                        msg = new[0]
                        text = f"📨 **Новое письмо!**\n\n"
                        text += f"От: {msg['sender'][:35]}\n"
                        text += f"Тема: {msg['subject'][:40]}\n"
                        if msg.get('code'):
                            text += f"🔑 Код: `{msg['code']}`\n"
                        if msg.get('links'):
                            text += f"🔗 {msg['links'][0][:60]}"
                        await bot.send_message(int(user_id), text, parse_mode='Markdown')
                except:
                    pass
        except:
            pass
        await asyncio.sleep(30)

async def main():
    await start_web()
    asyncio.create_task(background_check())
    await bot.delete_webhook(drop_pending_updates=True)
    while True:
        try:
            await dp.start_polling(bot)
            break
        except:
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
