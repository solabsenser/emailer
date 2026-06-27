import asyncio
import logging
import secrets
import string
import aiohttp
import re
import email.header
import json
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")

if not TURSO_URL or not TURSO_TOKEN:
    logger.error("❌ TURSO_URL or TURSO_TOKEN not set")
    exit(1)

# ===== TURSO HTTP API =====
class TursoClient:
    def __init__(self, url, token):
        self.url = url
        self.token = token
    
    async def execute(self, sql, params=None):
        """Выполняет SQL через HTTP API"""
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            payload = {
                "statements": [{"sql": sql, "args": params or []}]
            }
            
            async with session.post(self.url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"Turso error: {error}")
                data = await resp.json()
                return data

turso = TursoClient(TURSO_URL, TURSO_TOKEN)

async def init_db():
    """Инициализация базы данных"""
    sql = '''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            email TEXT,
            token TEXT,
            account_id TEXT,
            messages TEXT DEFAULT '[]',
            read_ids TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''
    await turso.execute(sql)
    logger.info("✅ Database initialized")

async def get_user(user_id):
    sql = 'SELECT email, token, account_id, messages, read_ids FROM users WHERE user_id = ?'
    result = await turso.execute(sql, [user_id])
    
    rows = result.get('result', {}).get('rows', [])
    if rows:
        row = rows[0]
        return {
            'email': row[0],
            'token': row[1],
            'account_id': row[2],
            'messages': json.loads(row[3]) if row[3] else [],
            'read_ids': json.loads(row[4]) if row[4] else []
        }
    return None

async def save_user(user_id, account):
    sql = '''
        INSERT OR REPLACE INTO users (user_id, email, token, account_id, messages, read_ids)
        VALUES (?, ?, ?, ?, ?, ?)
    '''
    await turso.execute(sql, [
        user_id,
        account['email'],
        account['token'],
        account.get('account_id', ''),
        json.dumps(account.get('messages', [])),
        json.dumps(account.get('read_ids', []))
    ])

async def delete_user(user_id):
    sql = 'DELETE FROM users WHERE user_id = ?'
    await turso.execute(sql, [user_id])

async def get_all_users():
    sql = 'SELECT user_id FROM users'
    result = await turso.execute(sql)
    rows = result.get('result', {}).get('rows', [])
    return [row[0] for row in rows]

# ===== ХРАНИЛИЩЕ (кэш в памяти) =====
user_accounts_cache = {}
bot_messages = {}

async def load_all_users_to_cache():
    sql = 'SELECT user_id, email, token, account_id, messages, read_ids FROM users'
    result = await turso.execute(sql)
    rows = result.get('result', {}).get('rows', [])
    
    for row in rows:
        user_id = row[0]
        user_accounts_cache[user_id] = {
            'email': row[1],
            'token': row[2],
            'account_id': row[3],
            'messages': json.loads(row[4]) if row[4] else [],
            'read_ids': json.loads(row[5]) if row[5] else []
        }
    
    if rows:
        logger.info(f"✅ Loaded {len(rows)} users from Turso")

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
                'account_id': '',
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
                            'received_at': datetime.now().isoformat()
                        })
        return new_messages

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
        KeyboardButton("✅ Да, удалить"),
        KeyboardButton("❌ Отмена")
    )
    return keyboard

async def send_bot_message(user_id, text, reply_markup=None):
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
        logger.error(f"Send error: {e}")
        return None

async def show_main_screen(user_id):
    account = user_accounts_cache.get(str(user_id))
    if not account:
        account = await get_user(str(user_id))
        if account:
            user_accounts_cache[str(user_id)] = account
    
    if not account:
        await send_bot_message(
            user_id,
            "📧 **Временная почта**\n\n"
            "Создайте временный email для регистрации\n"
            "Письма приходят автоматически",
            main_keyboard_no_account()
        )
        return
    
    msg_count = len(account.get('messages', []))
    codes = [m.get('code') for m in account.get('messages', []) if m.get('code')]
    code_count = len(codes)
    
    text = f"📧 **Ваш ящик:**\n`{account['email']}`\n\n"
    text += f"📨 Писем: **{msg_count}**\n"
    if code_count:
        text += f"🔑 Кодов: **{code_count}**"
    
    await send_bot_message(
        user_id,
        text,
        main_keyboard_with_account()
    )

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    user_id = str(message.from_user.id)
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    await send_bot_message(
        user_id,
        "👋 **Добро пожаловать!**\n\n"
        "📧 Временная почта\n"
        "Создайте email для регистрации",
        main_keyboard_with_account() if account else main_keyboard_no_account()
    )

@dp.message_handler(lambda message: message.text == "📧 Создать почту")
async def create_handler(message: types.Message):
    user_id = str(message.from_user.id)
    
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    
    if user_id in user_accounts_cache:
        await show_main_screen(user_id)
        return
    
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
    
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await show_main_screen(user_id)
        return
    
    await send_bot_message(user_id, "🔄 **Проверяю почту...**", None)
    
    try:
        new = await check_mailcat(account)
        if new:
            account.setdefault('messages', []).extend(new)
            await save_user(user_id, account)
        
        messages = account.get('messages', [])
        if not messages:
            await send_bot_message(
                user_id,
                "📭 **Писем нет**\n\nНажмите «Назад»",
                back_keyboard()
            )
            return
        
        text = f"📩 **Письма ({len(messages)}):**\n\n"
        for i, msg in enumerate(messages[-10:][::-1], 1):
            time = datetime.fromisoformat(msg['received_at']).strftime('%H:%M')
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
        
        await send_bot_message(user_id, text, back_keyboard())
        
    except Exception as e:
        logger.error(f"Check error: {e}")
        await send_bot_message(
            user_id,
            f"❌ **Ошибка проверки**\n\n{str(e)[:200]}",
            back_keyboard()
        )

@dp.message_handler(lambda message: message.text == "🗑 Удалить ящик")
async def delete_handler(message: types.Message):
    user_id = str(message.from_user.id)
    
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await show_main_screen(user_id)
        return
    
    await send_bot_message(
        user_id,
        f"⚠️ **Вы уверены, что хотите удалить ящик?**\n\n"
        f"📧 `{account['email']}`\n\n"
        f"Все письма будут удалены безвозвратно.",
        confirm_delete_keyboard()
    )

@dp.message_handler(lambda message: message.text == "✅ Да, удалить")
async def confirm_delete_handler(message: types.Message):
    user_id = str(message.from_user.id)
    
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    
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
        "🗑 **Ящик удалён**\n\nСоздайте новый при необходимости",
        main_keyboard_no_account()
    )

@dp.message_handler(lambda message: message.text == "❌ Отмена")
async def cancel_delete_handler(message: types.Message):
    user_id = str(message.from_user.id)
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    await show_main_screen(user_id)

@dp.message_handler(lambda message: message.text == "🔙 Назад")
async def back_handler(message: types.Message):
    user_id = str(message.from_user.id)
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    await show_main_screen(user_id)

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = str(message.from_user.id)
    try:
        await bot.delete_message(user_id, message.message_id)
    except:
        pass
    await show_main_screen(user_id)

async def background_check():
    while True:
        try:
            users = await get_all_users()
            for user_id in users:
                account = user_accounts_cache.get(user_id) or await get_user(user_id)
                if account:
                    try:
                        new = await check_mailcat(account)
                        if new:
                            account.setdefault('messages', []).extend(new)
                            await save_user(user_id, account)
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
    # Инициализация БД
    await init_db()
    
    # Загружаем всех пользователей в кэш
    await load_all_users_to_cache()
    
    await start_web()
    asyncio.create_task(background_check())
    
    # Удаляем webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook deleted")
    except:
        pass
    
    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception as e:
            if "ConflictError" in str(e) or "TerminatedByOtherGetUpdates" in str(e):
                logger.warning("⚠️ Conflict detected, waiting 5 seconds...")
                await asyncio.sleep(5)
            else:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
