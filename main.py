import asyncio
import logging
import secrets
import string
import aiohttp
import re
import email.header
import json
import quopri
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
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

# Конвертируем libsql:// в https://
if TURSO_URL.startswith("libsql://"):
    TURSO_URL = TURSO_URL.replace("libsql://", "https://")
    logger.info(f"✅ Converted to HTTPS: {TURSO_URL}")

TURSO_URL = TURSO_URL.replace(":443", "").rstrip("/")

# ===== TURSO HTTP API =====
class TursoClient:
    def __init__(self, url, token):
        self.url = url
        self.token = token
    
    def _format_params(self, params):
        if not params:
            return []
        formatted = []
        for p in params:
            if p is None:
                formatted.append({"type": "null"})
            elif isinstance(p, bool):
                formatted.append({"type": "integer", "value": 1 if p else 0})
            elif isinstance(p, int):
                formatted.append({"type": "integer", "value": p})
            elif isinstance(p, float):
                formatted.append({"type": "real", "value": p})
            else:
                formatted.append({"type": "text", "value": str(p)})
        return formatted
    
    async def execute(self, sql, params=None):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            
            payload = {"stmt": {"sql": sql}}
            if params:
                payload["stmt"]["args"] = self._format_params(params)
            
            full_url = f"{self.url}/v1/execute"
            
            try:
                async with session.post(full_url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise Exception(f"Turso error {resp.status}: {error}")
                    data = await resp.json()
                    if data.get("error"):
                        raise Exception(f"Turso error: {data['error']}")
                    return data
            except aiohttp.ClientError as e:
                raise Exception(f"Connection error: {e}")

turso = TursoClient(TURSO_URL, TURSO_TOKEN)

# ===== ФУНКЦИИ ПАРСИНГА =====
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

def decode_quoted_printable(text):
    """Декодирует quoted-printable текст"""
    if not text:
        return ''
    try:
        decoded = quopri.decodestring(text.encode('utf-8'))
        return decoded.decode('utf-8', errors='ignore')
    except:
        return text

def extract_code(text):
    if not text:
        return None
    patterns = [
        r'\b(\d{4,8})\b',
        r'код[:\s]*([A-Z0-9]{4,8})',
        r'code[:\s]*([A-Z0-9]{4,8})',
        r'verification code[:\s]*([A-Z0-9]{4,8})',
        r'подтверждения[:\s]*([A-Z0-9]{4,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            code = match.group(1) if match.groups() else match.group(0)
            if len(code) >= 4:
                return code
    return None

def extract_links(text):
    if not text:
        return []
    url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    links = re.findall(url_pattern, text)
    clean_links = []
    for link in links:
        link = link.strip('.,;:!?()[]{}"\'')
        if link.startswith('http') or link.startswith('www'):
            clean_links.append(link)
    return clean_links

def escape_markdown(text):
    if not text:
        return ''
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars:
        text = text.replace(char, f'\\{char}')
    return text

# ===== БАЗА ДАННЫХ =====
def extract_value(data):
    if isinstance(data, dict) and 'value' in data:
        return data['value']
    return data

def serialize_messages(messages):
    if not messages:
        return '[]'
    return json.dumps(messages, ensure_ascii=False)

def deserialize_messages(data):
    if not data:
        return []
    if isinstance(data, str):
        try:
            return json.loads(data)
        except:
            return []
    if isinstance(data, (list, dict)):
        return data
    return []

async def init_db():
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
    user_id = str(user_id)
    sql = 'SELECT email, token, account_id, messages, read_ids FROM users WHERE user_id = ?'
    result = await turso.execute(sql, [user_id])
    rows = result.get('result', {}).get('rows', [])
    if rows:
        row = rows[0]
        user_data = {}
        if isinstance(row, (list, tuple)):
            user_data = {
                'email': str(extract_value(row[0])) if row[0] else '',
                'token': str(extract_value(row[1])) if row[1] else '',
                'account_id': str(extract_value(row[2])) if row[2] else '',
                'messages': deserialize_messages(row[3]),
                'read_ids': json.loads(row[4]) if isinstance(row[4], str) else (row[4] or [])
            }
        elif isinstance(row, dict):
            user_data = {
                'email': str(extract_value(row.get('email', ''))),
                'token': str(extract_value(row.get('token', ''))),
                'account_id': str(extract_value(row.get('account_id', ''))),
                'messages': deserialize_messages(row.get('messages', [])),
                'read_ids': json.loads(row.get('read_ids', '[]')) if isinstance(row.get('read_ids'), str) else (row.get('read_ids') or [])
            }
        if not isinstance(user_data.get('messages'), list):
            user_data['messages'] = []
        return user_data
    return None

async def save_user(user_id, account):
    user_id = str(user_id)
    email = extract_value(account.get('email', ''))
    token = extract_value(account.get('token', ''))
    account_id = extract_value(account.get('account_id', ''))
    
    messages = account.get('messages', [])
    if not isinstance(messages, list):
        messages = []
    
    sql = '''
        INSERT OR REPLACE INTO users (user_id, email, token, account_id, messages, read_ids)
        VALUES (?, ?, ?, ?, ?, ?)
    '''
    await turso.execute(sql, [
        user_id,
        str(email),
        str(token),
        str(account_id),
        serialize_messages(messages),
        json.dumps(account.get('read_ids', []))
    ])

async def delete_user(user_id):
    user_id = str(user_id)
    sql = 'DELETE FROM users WHERE user_id = ?'
    await turso.execute(sql, [user_id])

async def get_all_users():
    sql = 'SELECT user_id FROM users'
    result = await turso.execute(sql)
    rows = result.get('result', {}).get('rows', [])
    user_ids = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            user_ids.append(str(extract_value(row[0])))
        elif isinstance(row, dict):
            user_ids.append(str(extract_value(row.get('user_id', ''))))
    return user_ids

# ===== ХРАНИЛИЩЕ =====
user_accounts_cache = {}
bot_messages = {}

async def load_all_users_to_cache():
    sql = 'SELECT user_id, email, token, account_id, messages, read_ids FROM users'
    result = await turso.execute(sql)
    rows = result.get('result', {}).get('rows', [])
    
    for row in rows:
        if isinstance(row, (list, tuple)):
            if len(row) >= 6:
                user_id = str(extract_value(row[0]))
                messages = deserialize_messages(row[4])
                if not isinstance(messages, list):
                    messages = []
                user_accounts_cache[user_id] = {
                    'email': str(extract_value(row[1])) if row[1] else '',
                    'token': str(extract_value(row[2])) if row[2] else '',
                    'account_id': str(extract_value(row[3])) if row[3] else '',
                    'messages': messages,
                    'read_ids': json.loads(row[5]) if isinstance(row[5], str) else (row[5] or [])
                }
        elif isinstance(row, dict):
            user_id = str(extract_value(row.get('user_id', '')))
            messages = deserialize_messages(row.get('messages', []))
            if not isinstance(messages, list):
                messages = []
            user_accounts_cache[user_id] = {
                'email': str(extract_value(row.get('email', ''))),
                'token': str(extract_value(row.get('token', ''))),
                'account_id': str(extract_value(row.get('account_id', ''))),
                'messages': messages,
                'read_ids': json.loads(row.get('read_ids', '[]')) if isinstance(row.get('read_ids'), str) else (row.get('read_ids') or [])
            }
    
    for uid in user_accounts_cache:
        if not isinstance(user_accounts_cache[uid].get('messages'), list):
            user_accounts_cache[uid]['messages'] = []
    
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
    if 'messages' not in account or not isinstance(account['messages'], list):
        account['messages'] = []
    
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
                        
                        # Получаем тело
                        body = email_data.get('email', {}).get('html', '') or email_data.get('email', {}).get('text', '')
                        
                        # ДЕКОДИРУЕМ quoted-printable
                        body = decode_quoted_printable(body)
                        
                        # Убираем HTML теги
                        body = re.sub(r'<[^>]+>', ' ', body)
                        body = re.sub(r'\s+', ' ', body)
                        body = body[:500].strip()
                        
                        code = extract_code(body)
                        links = extract_links(body)
                        
                        new_messages.append({
                            'sender': sender,
                            'subject': subject,
                            'body': body,
                            'code': code,
                            'links': links[:3],
                            'received_at': datetime.now().isoformat()
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
        logger.error(f"Send error: {e}")
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
            "📧 **Временная почта**\n\n"
            "Создайте временный email для регистрации\n"
            "Письма приходят автоматически",
            main_keyboard_no_account()
        )
        return
    
    if not isinstance(account.get('messages'), list):
        account['messages'] = []
    
    valid_messages = [m for m in account['messages'] if isinstance(m, dict)]
    msg_count = len(valid_messages)
    codes = [m.get('code') for m in valid_messages if m.get('code')]
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
    if account:
        user_accounts_cache[user_id] = account
        await show_main_screen(user_id)
    else:
        await send_bot_message(
            user_id,
            "👋 **Добро пожаловать!**\n\n"
            "📧 Временная почта\n"
            "Создайте email для регистрации",
            main_keyboard_no_account()
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

# ===== ОБРАБОТЧИК ДЛЯ ПРОСМОТРА ТЕКСТА ПИСЬМА =====
@dp.callback_query_handler(lambda c: c.data.startswith("show_body_"))
async def show_body_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    parts = callback.data.split('_')
    if len(parts) < 3:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    
    try:
        idx = int(parts[2])
    except:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    
    account = user_accounts_cache.get(user_id) or await get_user(user_id)
    if not account:
        await callback.answer("❌ Аккаунт не найден", show_alert=True)
        return
    
    valid_messages = [m for m in account.get('messages', []) if isinstance(m, dict)]
    if idx < 1 or idx > len(valid_messages):
        await callback.answer("❌ Письмо не найдено", show_alert=True)
        return
    
    msg = valid_messages[-idx]
    
    # Получаем тело и декодируем
    body = msg.get('body', 'Нет текста')
    body = decode_quoted_printable(body)
    body = escape_markdown(body)
    
    text = f"📄 **Полный текст письма**\n\n"
    text += f"📌 **От:** {msg.get('sender', 'unknown')}\n"
    text += f"📌 **Тема:** {msg.get('subject', '(no subject)')}\n"
    text += f"📌 **Время:** {datetime.fromisoformat(msg.get('received_at', datetime.now().isoformat())).strftime('%H:%M %d.%m.%Y')}\n\n"
    text += f"📝 **Текст:**\n{body[:1000]}"
    
    if len(body) > 1000:
        text += f"\n\n... (текст обрезан, всего символов: {len(body)})"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("🔙 Назад к письмам", callback_data="back_to_messages"))
    
    try:
        await callback.message.edit_text(text, parse_mode='Markdown', reply_markup=keyboard)
    except Exception as e:
        if "Can't parse entities" in str(e):
            await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            logger.error(f"Show body error: {e}")
            await callback.answer("❌ Ошибка отображения", show_alert=True)
    
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data == "back_to_messages")
async def back_to_messages_callback(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    await callback.answer()
    await check_handler(callback.message)

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
    if not isinstance(account.get('messages'), list):
        account['messages'] = []
    await send_bot_message(user_id, "🔄 **Проверяю почту...**", None)
    try:
        new = await check_mailcat(account)
        if new:
            account['messages'].extend(new)
            await save_user(user_id, account)
        valid_messages = [m for m in account['messages'] if isinstance(m, dict)]
        if not valid_messages:
            await send_bot_message(
                user_id,
                "📭 **Писем нет**\n\nНажмите «Назад»",
                back_keyboard()
            )
            return
        
        text = f"📩 **Письма ({len(valid_messages)}):**\n\n"
        for i, msg in enumerate(valid_messages[-10:][::-1], 1):
            time = datetime.fromisoformat(msg.get('received_at', datetime.now().isoformat())).strftime('%H:%M')
            text += f"{i}. [{time}] **{msg.get('subject', '(no subject)')[:35]}**\n"
            text += f"   От: {msg.get('sender', 'unknown')[:30]}\n"
            if msg.get('code'):
                text += f"   🔑 Код: `{msg['code']}`\n"
            if msg.get('links'):
                link = msg['links'][0]
                if isinstance(link, str):
                    text += f"   🔗 {link}\n"
                else:
                    text += f"   🔗 {str(link)}\n"
            text += "\n"
        
        if len(valid_messages) > 10:
            text += f"... и еще {len(valid_messages)-10} писем\n"
        text += f"\n📌 **Всего:** {len(valid_messages)}"
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        for idx in range(1, min(len(valid_messages), 10) + 1):
            keyboard.insert(InlineKeyboardButton(
                f"📄 Письмо {idx}",
                callback_data=f"show_body_{idx}"
            ))
        keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
        
        await send_bot_message(user_id, text, keyboard)
        
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
                if not user_id:
                    continue
                try:
                    account = user_accounts_cache.get(user_id) or await get_user(user_id)
                    if account:
                        if not isinstance(account.get('messages'), list):
                            logger.warning(f"⚠️ messages is {type(account.get('messages'))}, fixing for user {user_id}")
                            account['messages'] = []
                            await save_user(user_id, account)
                            logger.info(f"✅ Fixed messages for user {user_id}")
                        
                        new = await check_mailcat(account)
                        if new:
                            account['messages'].extend(new)
                            await save_user(user_id, account)
                            
                            msg = new[0]
                            text = f"📨 **Новое письмо!**\n\n"
                            text += f"От: {msg['sender'][:35]}\n"
                            text += f"Тема: {msg['subject'][:40]}\n"
                            if msg.get('code'):
                                text += f"🔑 Код: `{msg['code']}`\n"
                            if msg.get('links'):
                                link = msg['links'][0]
                                if isinstance(link, str):
                                    text += f"🔗 {link}\n"
                                else:
                                    text += f"🔗 {str(link)}\n"
                            text += f"\n📌 Нажмите «Проверить почту» для просмотра"
                            await bot.send_message(int(user_id), text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Background check error for {user_id}: {e}")
        except Exception as e:
            logger.error(f"Background check error: {e}")
        await asyncio.sleep(30)

async def main():
    try:
        await init_db()
        logger.info("✅ Database initialized")
        await load_all_users_to_cache()
        await start_web()
        asyncio.create_task(background_check())
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook deleted")
        except:
            pass
        logger.info("🚀 Bot started")
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
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
