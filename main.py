import asyncio
import logging
import secrets
import string
import aiohttp
from datetime import datetime
from collections import defaultdict
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.exceptions import TerminatedByOtherGetUpdates
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
DOMAIN = "mail.tm"  # Используем mail.tm

# ===== ХРАНИЛИЩЕ =====
user_accounts = {}  # {user_id: {email, password, token, messages: []}}
user_messages = {}

# ===== MAIL.TM API =====
MAIL_API = "https://api.mail.tm"

async def create_mail_account():
    """Создает временный email через mail.tm"""
    async with aiohttp.ClientSession() as session:
        # Генерируем случайный адрес
        local = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        email = f"{local}@{DOMAIN}"
        password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
        
        # Регистрируем аккаунт
        async with session.post(f"{MAIL_API}/accounts", json={
            "address": email,
            "password": password
        }) as resp:
            if resp.status != 201:
                raise Exception("Failed to create account")
            data = await resp.json()
            account_id = data.get('id')
        
        # Получаем токен
        async with session.post(f"{MAIL_API}/token", json={
            "address": email,
            "password": password
        }) as resp:
            if resp.status != 200:
                raise Exception("Failed to get token")
            data = await resp.json()
            token = data.get('token')
        
        return {
            'email': email,
            'password': password,
            'token': token,
            'account_id': account_id,
            'messages': []
        }

async def check_mail(account):
    """Проверяет новые письма"""
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {account['token']}"}
        
        # Получаем список писем
        async with session.get(f"{MAIL_API}/messages", headers=headers) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            messages = data.get('hydra:member', [])
        
        new_messages = []
        for msg in messages:
            msg_id = msg.get('id')
            # Проверяем, не читали ли уже
            if msg_id not in account.get('read_ids', []):
                # Получаем полное письмо
                async with session.get(f"{MAIL_API}/messages/{msg_id}", headers=headers) as resp2:
                    if resp2.status == 200:
                        full = await resp2.json()
                        account.setdefault('read_ids', []).append(msg_id)
                        new_messages.append({
                            'sender': full.get('from', {}).get('address', 'unknown'),
                            'subject': full.get('subject', '(no subject)'),
                            'body': full.get('text', '') or full.get('html', ''),
                            'received_at': datetime.fromisoformat(full.get('createdAt', datetime.now().isoformat()).replace('Z', '+00:00'))
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
    logger.info(f"✅ Web server running on port {PORT}")

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

async def safe_edit(user_id, text, reply_markup=None):
    msg_id = user_messages.get(user_id)
    
    if not msg_id:
        try:
            sent = await bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=reply_markup)
            user_messages[user_id] = sent.message_id
            return
        except Exception as e:
            logger.error(f"Send error: {e}")
            return
    
    try:
        await bot.edit_message_text(
            text,
            chat_id=user_id,
            message_id=msg_id,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        if "message can't be edited" in str(e).lower() or "message to edit not found" in str(e).lower():
            try:
                sent = await bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=reply_markup)
                user_messages[user_id] = sent.message_id
            except Exception as e2:
                logger.error(f"Send error: {e2}")

# ===== ОБРАБОТЧИКИ =====
@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    
    await safe_edit(
        user_id,
        "📧 **Временная почта**\n\n"
        "Создавайте email и получайте письма в Telegram\n"
        "🌐 Используется mail.tm\n\n"
        "⚠️ Письма приходят с задержкой до 30 секунд",
        get_main_menu()
    )

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    await safe_edit(user_id, "📧 Используйте кнопки ниже:", get_main_menu())

@dp.callback_query_handler(lambda c: True)
async def handle_callbacks(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    
    await callback.answer()
    
    if data == "create":
        if str(user_id) in user_accounts:
            await safe_edit(user_id, "❌ **У вас уже есть ящик**\n\nУдалите старый перед созданием нового", get_main_menu())
            return
        
        try:
            account = await create_mail_account()
            user_accounts[str(user_id)] = account
            
            await safe_edit(
                user_id,
                f"✅ **Создан email:**\n`{account['email']}`\n\n"
                f"📩 Отправляйте письма на этот адрес\n"
                f"🔄 Нажмите «Проверить почту» для получения писем\n"
                f"⏳ Письма приходят с задержкой до 30 секунд",
                get_main_menu()
            )
        except Exception as e:
            logger.error(f"Create error: {e}")
            await safe_edit(user_id, "❌ **Ошибка создания**\nПопробуйте позже", get_main_menu())
    
    elif data == "list":
        account = user_accounts.get(str(user_id))
        if not account:
            await safe_edit(user_id, "📭 **У вас нет ящика**\n\nНажмите «Создать почту»", get_main_menu())
            return
        
        msg_count = len(account.get('messages', []))
        await safe_edit(
            user_id,
            f"📬 **Ваш ящик:**\n`{account['email']}`\n\n📨 Писем: {msg_count}",
            get_main_menu()
        )
    
    elif data == "check":
        account = user_accounts.get(str(user_id))
        if not account:
            await safe_edit(user_id, "📭 **Нет ящика**\n\nНажмите «Создать почту»", get_main_menu())
            return
        
        await safe_edit(user_id, "🔄 **Проверяю почту...**", get_main_menu())
        
        try:
            new_messages = await check_mail(account)
            if new_messages:
                account.setdefault('messages', []).extend(new_messages)
                await safe_edit(
                    user_id,
                    f"✅ **Получено {len(new_messages)} новых писем!**\n\nНажмите ещё раз для просмотра",
                    get_main_menu()
                )
            else:
                await safe_edit(
                    user_id,
                    f"📭 **Новых писем нет**\n\nВсего писем: {len(account.get('messages', []))}",
                    get_main_menu()
                )
        except Exception as e:
            logger.error(f"Check error: {e}")
            await safe_edit(user_id, "❌ **Ошибка проверки**\nПопробуйте позже", get_main_menu())
    
    elif data.startswith("view_"):
        account = user_accounts.get(str(user_id))
        if not account:
            await safe_edit(user_id, "❌ **Нет ящика**", get_main_menu())
            return
        
        messages = account.get('messages', [])
        if not messages:
            await safe_edit(user_id, "📭 **Нет писем**", get_main_menu())
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
        
        await safe_edit(user_id, text, keyboard)
    
    elif data == "delete":
        account = user_accounts.get(str(user_id))
        if not account:
            await safe_edit(user_id, "📭 **Нет ящика для удаления**", get_main_menu())
            return
        
        # Удаляем аккаунт на mail.tm
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {account['token']}"}
                await session.delete(f"{MAIL_API}/accounts/{account['account_id']}", headers=headers)
        except Exception as e:
            logger.error(f"Delete error: {e}")
        
        del user_accounts[str(user_id)]
        await safe_edit(user_id, f"🗑 **Ящик удалён:**\n`{account['email']}`", get_main_menu())
    
    elif data == "back_to_menu":
        await safe_edit(user_id, "📧 **Главное меню**", get_main_menu())
    
    else:
        # Если нажали на письмо - показываем его
        if data.startswith("msg_"):
            # Можно реализовать просмотр конкретного письма
            await safe_edit(user_id, "📖 Функция в разработке", get_main_menu())

# ===== ФОНОВАЯ ПРОВЕРКА ПОЧТЫ =====
async def background_check():
    """Проверяет почту каждые 30 секунд для всех пользователей"""
    while True:
        try:
            for user_id, account in list(user_accounts.items()):
                if account:
                    try:
                        new_messages = await check_mail(account)
                        if new_messages:
                            account.setdefault('messages', []).extend(new_messages)
                            # Уведомляем пользователя
                            try:
                                await bot.send_message(
                                    int(user_id),
                                    f"📨 **Новое письмо!**\n\n"
                                    f"От: {new_messages[0]['sender']}\n"
                                    f"Тема: {new_messages[0]['subject']}\n\n"
                                    f"Нажмите «Проверить почту» для просмотра",
                                    parse_mode='Markdown'
                                )
                            except:
                                pass
                    except:
                        pass
        except:
            pass
        await asyncio.sleep(30)

async def start_bot_with_retry():
    while True:
        try:
            logger.info("🚀 Starting bot...")
            await dp.start_polling(bot)
            break
        except TerminatedByOtherGetUpdates:
            logger.warning("⚠️ Conflict detected, waiting 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Bot error: {e}")
            await asyncio.sleep(5)

async def main():
    await start_web()
    
    # Запускаем фоновую проверку
    asyncio.create_task(background_check())
    logger.info("🔄 Background mail checker started")
    
    await start_bot_with_retry()

if __name__ == "__main__":
    asyncio.run(main())
