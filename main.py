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
    """Декодирует заголовки письма (типа =?UTF-8?Q?...)"""
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
    """Находит все URL-ссылки в тексте"""
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
    """Пытается найти код подтверждения в тексте"""
    if not text:
        return None
    
    # Паттерны для кодов: 6 цифр, 4-8 букв/цифр, и т.д.
    patterns = [
        r'\b\d{4,8}\b',          # 4-8 цифр
        r'\b[A-Z0-9]{4,8}\b',     # 4-8 символов (заглавные + цифры)
        r'код[:\s]*([A-Z0-9]{4,8})',  # "код: 123456"
        r'verification code[:\s]*([A-Z0-9]{4,8})',  # "verification code: 123456"
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
                        
                        # Декодируем тему и отправителя
                        raw_subject = email_data.get('email', {}).get('subject', '(no subject)')
                        subject = decode_header_value(raw_subject)
                        
                        raw_from = email_data.get('email', {}).get('from', 'unknown')
                        sender = decode_header_value(raw_from)
                        
                        # Получаем тело письма
                        body = email_data.get('email', {}).get('text', '')
                        if not body:
                            body = email_data.get('email', {}).get('html', '')
                            body = re.sub(r'<[^>]+>', '', body)
                        
                        # Извлекаем код и ссылки
                        code = extract_code(body) or email_data.get('code', '')
                        links = extract_links(body)
                        
                        new_messages.append({
                            'sender': sender,
                            'subject': subject,
                            'body': body[:5000],
                            'code': code,
                            'links': links[:5],  # максимум 5 ссылок
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

def get_main_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📧 Создать почту", callback_data="create"),
        InlineKeyboardButton("📬 Мои ящики", callback_data="list"),
        InlineKeyboardButton("📨 Проверить почту", callback_data="check"),
        InlineKeyboardButton("🗑 Удалить ящик", callback_data="delete")
    )
    return keyboard

async def update_or_send(user_id, text, reply_markup=None):
    msg_id = user_messages.get(user_id)
    if msg_id:
        try:
            await bot.edit_message_text(text, chat_id=user_id, message_id=msg_id, parse_mode='Markdown', reply_markup=reply_markup)
            return
        except:
            user_messages.pop(user_id, None)
    sent = await bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=reply_markup)
    user_messages[user_id] = sent.message_id

@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    await update_or_send(
        user_id,
        "📧 **Временная почта**\n\n"
        "Создавайте email и получайте письма\n"
        "🌐 Используется MailCat\n\n"
        "🔑 Бот находит коды и ссылки из писем!",
        get_main_menu()
    )

@dp.callback_query_handler(lambda c: c.data == "create")
async def create_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    if str(user_id) in user_accounts:
        await update_or_send(user_id, "❌ **У вас уже есть ящик**", get_main_menu())
        return
    
    try:
        account = await create_mailcat_mailbox()
        user_accounts[str(user_id)] = account
        await update_or_send(
            user_id,
            f"✅ **Создан email:**\n`{account['email']}`\n\n"
            f"📩 Используйте для регистрации",
            get_main_menu()
        )
    except Exception as e:
        logger.error(f"Create error: {e}")
        await update_or_send(user_id, f"❌ **Ошибка:** {str(e)[:200]}", get_main_menu())

@dp.callback_query_handler(lambda c: c.data == "list")
async def list_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    account = user_accounts.get(str(user_id))
    if not account:
        await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
        return
    await update_or_send(user_id, f"📬 **Ваш ящик:**\n`{account['email']}`\n\n📨 Писем: {len(account.get('messages', []))}", get_main_menu())

@dp.callback_query_handler(lambda c: c.data == "check")
async def check_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    account = user_accounts.get(str(user_id))
    if not account:
        await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
        return
    
    await update_or_send(user_id, "🔄 **Проверяю...**", get_main_menu())
    try:
        new = await check_mailcat(account)
        if new:
            account.setdefault('messages', []).extend(new)
            
            # Формируем сообщение с кодами и ссылками
            codes = [msg.get('code') for msg in new if msg.get('code')]
            links = []
            for msg in new:
                links.extend(msg.get('links', []))
            
            response = f"✅ **{len(new)} новых писем!**\n"
            
            if codes:
                response += "\n🔑 **Коды:**\n" + "\n".join([f"• `{c}`" for c in codes[:5]])
            
            if links:
                response += "\n\n🔗 **Ссылки:**\n" + "\n".join([f"• {link[:60]}..." if len(link) > 60 else f"• {link}" for link in links[:5]])
            
            if not codes and not links:
                response += "\n\n📝 Нажмите «Мои ящики» для просмотра"
            
            await update_or_send(user_id, response, get_main_menu())
        else:
            total = len(account.get('messages', []))
            await update_or_send(user_id, f"📭 **Новых писем нет**\nВсего: {total}", get_main_menu())
    except Exception as e:
        await update_or_send(user_id, f"❌ **Ошибка:** {str(e)[:200]}", get_main_menu())

@dp.callback_query_handler(lambda c: c.data.startswith("view_"))
async def view_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    account = user_accounts.get(str(user_id))
    if not account:
        await update_or_send(user_id, "❌ **Нет ящика**", get_main_menu())
        return
    
    messages = account.get('messages', [])
    if not messages:
        await update_or_send(user_id, "📭 **Нет писем**", get_main_menu())
        return
    
    text = f"📩 **Письма:**\n\n"
    for i, msg in enumerate(messages[-10:][::-1], 1):
        text += f"{i}. **{msg['subject'][:40]}**\n"
        text += f"   От: {msg['sender'][:35]}\n"
        if msg.get('code'):
            text += f"   🔑 Код: `{msg['code']}`\n"
        if msg.get('links'):
            for link in msg['links'][:2]:
                text += f"   🔗 {link[:50]}...\n"
        text += "\n"
    
    text += f"\n📌 **Всего:** {len(messages)}"
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔙 Назад", callback_data="check"),
        InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")
    )
    await update_or_send(user_id, text, keyboard)

@dp.callback_query_handler(lambda c: c.data == "delete")
async def delete_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    account = user_accounts.get(str(user_id))
    if not account:
        await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
        return
    
    del user_accounts[str(user_id)]
    await update_or_send(user_id, f"🗑 **Ящик удалён**", get_main_menu())

@dp.callback_query_handler(lambda c: c.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    await update_or_send(user_id, "📧 **Главное меню**", get_main_menu())

async def background_check():
    while True:
        try:
            for user_id, account in list(user_accounts.items()):
                try:
                    new = await check_mailcat(account)
                    if new:
                        account.setdefault('messages', []).extend(new)
                        
                        msg_text = f"📨 **Новое письмо!**\nОт: {new[0]['sender'][:35]}\nТема: {new[0]['subject'][:40]}"
                        
                        if new[0].get('code'):
                            msg_text += f"\n🔑 Код: `{new[0]['code']}`"
                        
                        if new[0].get('links'):
                            msg_text += f"\n🔗 {new[0]['links'][0][:60]}..."
                        
                        await bot.send_message(int(user_id), msg_text, parse_mode='Markdown')
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
