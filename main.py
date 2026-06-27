import asyncio
import logging
import secrets
import string
from datetime import datetime
from collections import defaultdict
from email import message_from_bytes
from email.policy import default
from aiosmtpd.controller import Controller
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from dotenv import load_dotenv
import os

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ===== КОНФИГ =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
DOMAIN = os.getenv("DOMAIN", "temp.local")

# ===== ХРАНИЛИЩЕ В ПАМЯТИ =====
# {email: {user_id, messages: [{sender, subject, body, received_at}]}}
mailboxes = {}
# {user_id: [email1, email2, ...]}
user_emails = defaultdict(list)

def generate_email():
    alphabet = string.ascii_lowercase + string.digits
    local = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f"{local}@{DOMAIN}"

# ===== SMTP СЕРВЕР =====
class MailHandler:
    async def handle_DATA(self, server, session, envelope):
        try:
            msg = message_from_bytes(envelope.content, policy=default)
            recipient = envelope.rcpt_tos[0] if envelope.rcpt_tos else None
            
            if not recipient:
                return '550 No recipient'
            
            if recipient not in mailboxes:
                return f'550 Mailbox {recipient} not found'
            
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
            
            logging.info(f"Message stored for {recipient}")
            return '250 OK'
        except Exception as e:
            logging.error(f"SMTP error: {e}")
            return '550 Error'

def start_smtp():
    handler = MailHandler()
    controller = Controller(handler, hostname='0.0.0.0', port=2525)
    controller.start()
    return controller

# ===== TELEGRAM БОТ =====
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📧 Создать почту", callback_data="create"),
        InlineKeyboardButton("📬 Мои ящики", callback_data="list"),
        InlineKeyboardButton("📨 Проверить почту", callback_data="check"),
        InlineKeyboardButton("🗑 Удалить ящик", callback_data="delete")
    )
    await message.reply("📧 Временная почта\nВыберите действие:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "create")
async def create(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    
    if len(user_emails[user_id]) >= 10:
        await bot.answer_callback_query(callback.id, "❌ Максимум 10 ящиков", show_alert=True)
        return
    
    for _ in range(10):
        email = generate_email()
        if email not in mailboxes:
            mailboxes[email] = {
                'user_id': user_id,
                'messages': []
            }
            user_emails[user_id].append(email)
            
            await bot.answer_callback_query(callback.id)
            await bot.send_message(
                user_id,
                f"✅ Создан email: `{email}`\n\n"
                f"📩 Отправляйте письма на этот адрес\n"
                f"🔹 Ящик не удалится сам, только по вашей команде\n"
                f"🔹 Чтобы удалить — нажмите «🗑 Удалить ящик»",
                parse_mode='Markdown'
            )
            return
    
    await bot.answer_callback_query(callback.id, "❌ Ошибка, попробуйте снова", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "list")
async def list_emails(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = user_emails.get(user_id, [])
    emails = [e for e in emails if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "📭 У вас нет ящиков")
        return
    
    text = "📬 Ваши ящики:\n\n"
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        text += f"• `{email}` — 📨 {msg_count} писем\n"
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, text, parse_mode='Markdown')

@dp.callback_query_handler(lambda c: c.data == "check")
async def check(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = user_emails.get(user_id, [])
    emails = [e for e in emails if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "📭 Нет ящиков")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"📨 {email} ({msg_count} писем)",
            callback_data=f"view_{email}"
        ))
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, "Выберите ящик для просмотра:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("view_"))
async def view_mail(callback: types.CallbackQuery):
    email = callback.data.replace("view_", "")
    user_id = str(callback.from_user.id)
    
    if email not in mailboxes:
        await bot.answer_callback_query(callback.id, "❌ Ящик не найден", show_alert=True)
        return
    
    if mailboxes[email].get('user_id') != user_id:
        await bot.answer_callback_query(callback.id, "❌ Не ваш ящик", show_alert=True)
        return
    
    messages = mailboxes[email].get('messages', [])
    if not messages:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, f"📭 Писем для {email} нет")
        return
    
    text = f"📩 Письма для `{email}`:\n\n"
    for i, msg in enumerate(messages[-10:][::-1], 1):
        time = msg['received_at'].strftime('%H:%M %d.%m')
        text += f"{i}. [{time}] От: {msg['sender'][:30]}\n"
        text += f"   Тема: {msg['subject'][:40]}\n"
        preview = msg['body'][:80].replace('\n', ' ')
        text += f"   Текст: {preview}...\n\n"
    
    if len(messages) > 10:
        text += f"... и еще {len(messages)-10} писем"
    
    text += f"\n📌 Всего писем: {len(messages)}"
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, text, parse_mode='Markdown')

@dp.callback_query_handler(lambda c: c.data == "delete")
async def delete_menu(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = user_emails.get(user_id, [])
    emails = [e for e in emails if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "Нет ящиков для удаления")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"🗑 {email} ({msg_count} писем)",
            callback_data=f"del_{email}"
        ))
    keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, "Выберите ящик для удаления:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_confirm(callback: types.CallbackQuery):
    email = callback.data.replace("del_", "")
    user_id = str(callback.from_user.id)
    
    if email in mailboxes and mailboxes[email].get('user_id') == user_id:
        user_emails[user_id].remove(email)
        del mailboxes[email]
        await bot.answer_callback_query(callback.id, f"✅ {email} удалён")
        await bot.send_message(user_id, f"🗑 Ящик {email} удалён вместе со всеми письмами")
    else:
        await bot.answer_callback_query(callback.id, "❌ Не найден", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "cancel")
async def cancel(callback: types.CallbackQuery):
    await bot.answer_callback_query(callback.id)
    await bot.delete_message(callback.from_user.id, callback.message.message_id)

# ===== ЗАПУСК =====
if __name__ == "__main__":
    smtp = start_smtp()
    logging.info(f"✅ SMTP сервер запущен на порту 2525")
    logging.info(f"✅ Бот запущен")
    executor.start_polling(dp, skip_updates=True)
