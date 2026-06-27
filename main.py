import asyncio
import logging
import secrets
import string
from datetime import datetime
from collections import defaultdict
from email import message_from_bytes
from email.policy import default
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.exceptions import TerminatedByOtherGetUpdates
from dotenv import load_dotenv
import os

# Полностью отключаем логи SMTP
logging.getLogger('mail').setLevel(logging.ERROR)
logging.getLogger('aiosmtpd').setLevel(logging.ERROR)

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOMAIN = os.getenv("DOMAIN", "temp.local")
PORT = int(os.getenv("PORT", 10000))

# ===== ХРАНИЛИЩЕ =====
mailboxes = {}
user_emails = defaultdict(list)
user_messages = {}

def generate_email():
    alphabet = string.ascii_lowercase + string.digits
    local = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f"{local}@{DOMAIN}"

# ===== SMTP (запускается в отдельном потоке, тихо) =====
def start_smtp():
    try:
        from aiosmtpd.controller import Controller
        
        class MailHandler:
            async def handle_DATA(self, server, session, envelope):
                try:
                    if not envelope or not envelope.rcpt_tos:
                        return '500 Ignored'
                    
                    recipient = envelope.rcpt_tos[0]
                    if recipient not in mailboxes:
                        return f'550 Mailbox {recipient} not found'
                    
                    msg = message_from_bytes(envelope.content, policy=default)
                    subject = msg.get('Subject', '(no subject)')
                    sender = msg.get('From', 'unknown')
                    
                    body = ''
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == 'text/plain':
                                body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='ignore')
                    
                    mailboxes[recipient].setdefault('messages', []).append({
                        'sender': sender,
                        'subject': subject,
                        'body': body[:5000],
                        'received_at': datetime.utcnow()
                    })
                    
                    return '250 OK'
                except Exception:
                    return '550 Error'
        
        controller = Controller(MailHandler(), hostname='127.0.0.1', port=2525)
        controller.start()
        logger.info("✅ SMTP server running on 127.0.0.1:2525")
        return controller
    except Exception as e:
        logger.warning(f"SMTP not started: {e}")
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
        f"🌐 Домен: `{DOMAIN}`",
        get_main_menu()
    )

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = message.from_user.id
    user_messages[user_id] = message.message_id
    
    await safe_edit(
        user_id,
        "📧 Используйте кнопки ниже:",
        get_main_menu()
    )

@dp.callback_query_handler(lambda c: True)
async def handle_callbacks(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    
    await callback.answer()
    
    if data == "create":
        if len(user_emails[str(user_id)]) >= 10:
            await safe_edit(user_id, "❌ **Максимум 10 ящиков**", get_main_menu())
            return
        
        for _ in range(10):
            email = generate_email()
            if email not in mailboxes:
                mailboxes[email] = {'user_id': str(user_id), 'messages': []}
                user_emails[str(user_id)].append(email)
                
                await safe_edit(
                    user_id,
                    f"✅ **Создан email:**\n`{email}`\n\n📩 Отправляйте письма на этот адрес",
                    get_main_menu()
                )
                return
        
        await safe_edit(user_id, "❌ **Ошибка создания**", get_main_menu())
    
    elif data == "list":
        emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
        
        if not emails:
            await safe_edit(user_id, "📭 **У вас нет ящиков**", get_main_menu())
            return
        
        text = "📬 **Ваши ящики:**\n\n"
        for email in emails:
            msg_count = len(mailboxes[email].get('messages', []))
            text += f"• `{email}` — 📨 {msg_count} писем\n"
        
        await safe_edit(user_id, text, get_main_menu())
    
    elif data == "check":
        emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
        
        if not emails:
            await safe_edit(user_id, "📭 **Нет ящиков**", get_main_menu())
            return
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        for email in emails:
            msg_count = len(mailboxes[email].get('messages', []))
            keyboard.add(InlineKeyboardButton(
                f"📨 {email} ({msg_count})",
                callback_data=f"view_{email}"
            ))
        keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu"))
        
        await safe_edit(user_id, "📨 **Выберите ящик:**", keyboard)
    
    elif data.startswith("view_"):
        email = data.replace("view_", "")
        
        if email not in mailboxes or mailboxes[email].get('user_id') != str(user_id):
            await safe_edit(user_id, "❌ **Ящик не найден**", get_main_menu())
            return
        
        messages = mailboxes[email].get('messages', [])
        
        if not messages:
            await safe_edit(user_id, f"📭 **Писем для `{email}` нет**", get_main_menu())
            return
        
        text = f"📩 **Письма для `{email}`:**\n\n"
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
            InlineKeyboardButton("🔙 К ящикам", callback_data="check"),
            InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")
        )
        
        await safe_edit(user_id, text, keyboard)
    
    elif data == "delete":
        emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
        
        if not emails:
            await safe_edit(user_id, "📭 **Нет ящиков**", get_main_menu())
            return
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        for email in emails:
            msg_count = len(mailboxes[email].get('messages', []))
            keyboard.add(InlineKeyboardButton(
                f"🗑 {email} ({msg_count})",
                callback_data=f"del_{email}"
            ))
        keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="back_to_menu"))
        
        await safe_edit(user_id, "⚠️ **Выберите ящик для удаления:**", keyboard)
    
    elif data.startswith("del_"):
        email = data.replace("del_", "")
        
        if email in mailboxes and mailboxes[email].get('user_id') == str(user_id):
            user_emails[str(user_id)].remove(email)
            del mailboxes[email]
            
            await safe_edit(user_id, f"🗑 **Ящик удалён:**\n`{email}`", get_main_menu())
        else:
            await safe_edit(user_id, "❌ **Ящик не найден**", get_main_menu())
    
    elif data == "back_to_menu":
        await safe_edit(user_id, "📧 **Главное меню**", get_main_menu())

# ===== ЗАПУСК С ПЕРЕЗАПУСКОМ ПРИ КОНФЛИКТЕ =====
async def start_bot_with_retry():
    """Запускает бота с обработкой конфликта"""
    while True:
        try:
            logger.info("🚀 Starting bot...")
            await dp.start_polling(bot)
            break
        except TerminatedByOtherGetUpdates:
            logger.warning("⚠️ Conflict detected, waiting 5 seconds and retrying...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Bot error: {e}")
            await asyncio.sleep(5)

async def main():
    # Запускаем SMTP (тихо)
    smtp = start_smtp()
    
    # Запускаем Web
    await start_web()
    
    # Запускаем бота с автоматическим перезапуском
    await start_bot_with_retry()

if __name__ == "__main__":
    asyncio.run(main())
