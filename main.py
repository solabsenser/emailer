import asyncio
import logging
import secrets
import string
import aiohttp
import base64
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.middlewares.logging import LoggingMiddleware
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

# ===== GUERRILLA MAIL API =====
GM_API = "https://api.guerrillamail.com/ajax.php"

async def create_guerrilla_email():
    async with aiohttp.ClientSession() as session:
        local = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(8))
        
        params = {
            'f': 'get_email_address',
            'ip': '127.0.0.1',
            'agent': 'bot',
            'email_user': local
        }
        
        async with session.get(GM_API, params=params) as resp:
            if resp.status != 200:
                raise Exception("Failed to create email")
            data = await resp.json()
            
            sid = data.get('sid')
            email = data.get('email_addr')
            
            if not sid or not email:
                raise Exception("Invalid response")
            
            return {
                'email': email,
                'sid': sid,
                'messages': [],
                'ids': []
            }

async def check_guerrilla_mail(account):
    async with aiohttp.ClientSession() as session:
        params = {
            'f': 'fetch_email',
            'sid': account['sid'],
            'seq': 0
        }
        
        async with session.get(GM_API, params=params) as resp:
            if resp.status != 200:
                return []
            
            data = await resp.json()
            emails = data.get('list', [])
            
            new_messages = []
            for email_data in emails:
                mail_id = email_data.get('mail_id')
                if mail_id not in account.get('ids', []):
                    account.setdefault('ids', []).append(mail_id)
                    
                    params2 = {
                        'f': 'fetch_email',
                        'sid': account['sid'],
                        'email_id': mail_id
                    }
                    
                    async with session.get(GM_API, params=params2) as resp2:
                        if resp2.status == 200:
                            full = await resp2.json()
                            
                            body_raw = full.get('mail_body', '')
                            try:
                                body = base64.b64decode(body_raw).decode('utf-8', errors='ignore')
                            except:
                                body = body_raw
                            
                            new_messages.append({
                                'sender': full.get('mail_from', 'unknown'),
                                'subject': full.get('mail_subject', '(no subject)'),
                                'body': body[:5000],
                                'received_at': datetime.fromtimestamp(int(full.get('mail_timestamp', 0)))
                            })
            
            return new_messages

# ===== WEB СЕРВЕР (только для health check) =====
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
    logger.info(f"✅ Web server on port {PORT}")
    return app

# ===== TELEGRAM БОТ =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

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
    """Единый метод для обновления сообщения - без дублей"""
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
    
    # Отправляем новое
    sent = await bot.send_message(
        user_id,
        text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    user_messages[user_id] = sent.message_id

@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    
    await update_or_send(
        user_id,
        "📧 **Временная почта**\n\n"
        "Создавайте email и получайте письма\n"
        "🌐 Используется Guerrilla Mail\n\n"
        "⏳ Письма приходят с задержкой до 30 сек",
        get_main_menu()
    )

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    await update_or_send(user_id, "📧 Используйте кнопки:", get_main_menu())

@dp.callback_query_handler(lambda c: True)
async def handle_callbacks(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    
    await callback.answer()
    
    if data == "create":
        if str(user_id) in user_accounts:
            await update_or_send(user_id, "❌ **У вас уже есть ящик**", get_main_menu())
            return
        
        try:
            account = await create_guerrilla_email()
            user_accounts[str(user_id)] = account
            
            await update_or_send(
                user_id,
                f"✅ **Создан email:**\n`{account['email']}`\n\n"
                f"📩 Используйте этот адрес для регистрации",
                get_main_menu()
            )
        except Exception as e:
            logger.error(f"Create error: {e}")
            await update_or_send(user_id, "❌ **Ошибка создания**", get_main_menu())
    
    elif data == "list":
        account = user_accounts.get(str(user_id))
        if not account:
            await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
            return
        
        msg_count = len(account.get('messages', []))
        await update_or_send(
            user_id,
            f"📬 **Ваш ящик:**\n`{account['email']}`\n\n📨 Писем: {msg_count}",
            get_main_menu()
        )
    
    elif data == "check":
        account = user_accounts.get(str(user_id))
        if not account:
            await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
            return
        
        await update_or_send(user_id, "🔄 **Проверяю...**", get_main_menu())
        
        try:
            new_messages = await check_guerrilla_mail(account)
            if new_messages:
                account.setdefault('messages', []).extend(new_messages)
                await update_or_send(
                    user_id,
                    f"✅ **Получено {len(new_messages)} писем!**\n\nНажмите «Проверить почту» для просмотра",
                    get_main_menu()
                )
            else:
                total = len(account.get('messages', []))
                await update_or_send(
                    user_id,
                    f"📭 **Новых писем нет**\n\nВсего: {total}",
                    get_main_menu()
                )
        except Exception as e:
            logger.error(f"Check error: {e}")
            await update_or_send(user_id, "❌ **Ошибка проверки**", get_main_menu())
    
    elif data.startswith("view_"):
        account = user_accounts.get(str(user_id))
        if not account:
            await update_or_send(user_id, "❌ **Нет ящика**", get_main_menu())
            return
        
        messages = account.get('messages', [])
        if not messages:
            await update_or_send(user_id, "📭 **Нет писем**", get_main_menu())
            return
        
        text = f"📩 **Письма для `{account['email']}`:**\n\n"
        for i, msg in enumerate(messages[-10:][::-1], 1):
            time = msg['received_at'].strftime('%H:%M %d.%m')
            text += f"{i}. [{time}] **{msg['subject'][:35]}**\n"
            text += f"   От: {msg['sender'][:30]}\n"
            preview = msg['body'][:70].replace('\n', ' ')
            if preview:
                text += f"   📝 {preview}...\n"
            text += "\n"
        
        if len(messages) > 10:
            text += f"... и еще {len(messages)-10} писем\n"
        
        text += f"\n📌 **Всего:** {len(messages)} писем"
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("🔄 Проверить", callback_data="check"),
            InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")
        )
        
        await update_or_send(user_id, text, keyboard)
    
    elif data == "delete":
        account = user_accounts.get(str(user_id))
        if not account:
            await update_or_send(user_id, "📭 **Нет ящика**", get_main_menu())
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'f': 'forget_me',
                    'sid': account['sid']
                }
                await session.get(GM_API, params=params)
        except:
            pass
        
        del user_accounts[str(user_id)]
        await update_or_send(user_id, f"🗑 **Ящик удалён**", get_main_menu())
    
    elif data == "back_to_menu":
        await update_or_send(user_id, "📧 **Главное меню**", get_main_menu())

# ===== ФОНОВАЯ ПРОВЕРКА =====
async def background_check():
    while True:
        try:
            for user_id, account in list(user_accounts.items()):
                try:
                    new_messages = await check_guerrilla_mail(account)
                    if new_messages:
                        account.setdefault('messages', []).extend(new_messages)
                        await bot.send_message(
                            int(user_id),
                            f"📨 **Новое письмо!**\n\n"
                            f"От: {new_messages[0]['sender']}\n"
                            f"Тема: {new_messages[0]['subject']}",
                            parse_mode='Markdown'
                        )
                except:
                    pass
        except:
            pass
        await asyncio.sleep(30)

# ===== ЗАПУСК (POLLING) =====
async def main():
    # Запускаем web сервер для health check
    await start_web()
    
    # Запускаем фоновую проверку
    asyncio.create_task(background_check())
    logger.info("🔄 Mail checker started")
    
    # Удаляем webhook если был
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Webhook deleted, using polling")
    
    # Запускаем polling с защитой от конфликтов
    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception as e:
            if "Conflict" in str(e):
                logger.warning("⚠️ Conflict, waiting 5 sec...")
                await asyncio.sleep(5)
            else:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
