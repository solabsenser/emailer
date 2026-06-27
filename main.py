import asyncio
import logging
import secrets
import string
from datetime import datetime
from collections import defaultdict
from email import message_from_bytes
from email.policy import default
from aiosmtpd.controller import Controller
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOMAIN = os.getenv("DOMAIN", "temp.local")
PORT = int(os.getenv("PORT", 10000))

# ===== ХРАНИЛИЩЕ =====
mailboxes = {}
user_emails = defaultdict(list)

def generate_email():
    alphabet = string.ascii_lowercase + string.digits
    local = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f"{local}@{DOMAIN}"

# ===== SMTP =====
class MailHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            msg = message_from_bytes(envelope.content, policy=default)
            recipient = envelope.rcpt_tos[0] if envelope.rcpt_tos else None
            
            if not recipient or recipient not in mailboxes:
                return '550 Mailbox not found'
            
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
        except Exception as e:
            logging.error(f"SMTP error: {e}")
            return '550 Error'

def start_smtp():
    handler = MailHandler()
    controller = Controller(handler, hostname='0.0.0.0', port=2525)
    controller.start()
    return controller

# ===== WEB СЕРВЕР =====
async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Web server running on port {PORT}")

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

# Храним ID последнего сообщения для каждого пользователя
user_last_message = {}

async def update_message(user_id, text, reply_markup=None, parse_mode='Markdown'):
    """Редактирует последнее сообщение пользователя или отправляет новое если его нет"""
    msg_id = user_last_message.get(user_id)
    
    try:
        if msg_id:
            await bot.edit_message_text(
                text,
                chat_id=user_id,
                message_id=msg_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        else:
            sent = await bot.send_message(
                user_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            user_last_message[user_id] = sent.message_id
    except Exception as e:
        # Если не удалось отредактировать (сообщение старое) - отправляем новое
        if "message to edit not found" in str(e) or "message is not modified" in str(e):
            sent = await bot.send_message(
                user_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            user_last_message[user_id] = sent.message_id
        else:
            logging.error(f"Update error: {e}")

@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    user_id = message.from_user.id
    user_last_message[user_id] = message.message_id
    
    await update_message(
        user_id,
        "📧 **Временная почта**\n\n"
        "Создавайте email и получайте письма в Telegram\n"
        f"🌐 Домен: `{DOMAIN}`",
        reply_markup=get_main_menu()
    )

@dp.message_handler()
async def any_message(message: types.Message):
    user_id = message.from_user.id
    user_last_message[user_id] = message.message_id
    
    await update_message(
        user_id,
        "📧 Используйте кнопки ниже:",
        reply_markup=get_main_menu()
    )

@dp.callback_query_handler(lambda c: c.data == "create")
async def create(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    if len(user_emails[str(user_id)]) >= 10:
        await update_message(
            user_id,
            "❌ **Максимум 10 ящиков**\n\nУдалите один из существующих",
            reply_markup=get_main_menu()
        )
        return
    
    for _ in range(10):
        email = generate_email()
        if email not in mailboxes:
            mailboxes[email] = {'user_id': str(user_id), 'messages': []}
            user_emails[str(user_id)].append(email)
            
            await update_message(
                user_id,
                f"✅ **Создан email:**\n`{email}`\n\n"
                f"📩 Отправляйте письма на этот адрес\n"
                f"🗑 Удалить можно через меню",
                reply_markup=get_main_menu()
            )
            return
    
    await update_message(
        user_id,
        "❌ **Ошибка создания**\nПопробуйте снова",
        reply_markup=get_main_menu()
    )

@dp.callback_query_handler(lambda c: c.data == "list")
async def list_emails(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
    
    if not emails:
        await update_message(
            user_id,
            "📭 **У вас нет ящиков**\n\nНажмите «Создать почту»",
            reply_markup=get_main_menu()
        )
        return
    
    text = "📬 **Ваши ящики:**\n\n"
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        text += f"• `{email}` — 📨 {msg_count} писем\n"
    
    await update_message(user_id, text, reply_markup=get_main_menu())

@dp.callback_query_handler(lambda c: c.data == "check")
async def check(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
    
    if not emails:
        await update_message(
            user_id,
            "📭 **Нет ящиков**\n\nНажмите «Создать почту»",
            reply_markup=get_main_menu()
        )
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"📨 {email} ({msg_count})",
            callback_data=f"view_{email}"
        ))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu"))
    
    await update_message(
        user_id,
        "📨 **Выберите ящик для просмотра:**",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith("view_"))
async def view_mail(callback: types.CallbackQuery):
    email = callback.data.replace("view_", "")
    user_id = callback.from_user.id
    await callback.answer()
    
    if email not in mailboxes or mailboxes[email].get('user_id') != str(user_id):
        await update_message(
            user_id,
            "❌ **Ящик не найден**",
            reply_markup=get_main_menu()
        )
        return
    
    messages = mailboxes[email].get('messages', [])
    
    if not messages:
        await update_message(
            user_id,
            f"📭 **Писем для `{email}` нет**",
            reply_markup=get_main_menu()
        )
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
    
    await update_message(user_id, text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "delete")
async def delete_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    emails = [e for e in user_emails.get(str(user_id), []) if e in mailboxes]
    
    if not emails:
        await update_message(
            user_id,
            "📭 **Нет ящиков для удаления**",
            reply_markup=get_main_menu()
        )
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"🗑 {email} ({msg_count})",
            callback_data=f"del_{email}"
        ))
    keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="back_to_menu"))
    
    await update_message(
        user_id,
        "⚠️ **Выберите ящик для удаления:**\n\n"
        "Ящик и все письма будут удалены безвозвратно",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_confirm(callback: types.CallbackQuery):
    email = callback.data.replace("del_", "")
    user_id = callback.from_user.id
    await callback.answer()
    
    if email in mailboxes and mailboxes[email].get('user_id') == str(user_id):
        user_emails[str(user_id)].remove(email)
        del mailboxes[email]
        
        await update_message(
            user_id,
            f"🗑 **Ящик удалён:**\n`{email}`",
            reply_markup=get_main_menu()
        )
    else:
        await update_message(
            user_id,
            "❌ **Ящик не найден**",
            reply_markup=get_main_menu()
        )

@dp.callback_query_handler(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    await update_message(
        user_id,
        "📧 **Главное меню**\n\nВыберите действие:",
        reply_markup=get_main_menu()
    )

# ===== ЗАПУСК =====
async def main():
    smtp = start_smtp()
    logging.info("✅ SMTP сервер запущен на порту 2525")
    
    await start_web()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
