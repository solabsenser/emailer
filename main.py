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
from aiogram.utils import executor
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

# ===== WEB СЕРВЕР ДЛЯ RENDER =====
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

# Главное меню — доступно всегда
def get_main_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📧 Создать почту", callback_data="create"),
        InlineKeyboardButton("📬 Мои ящики", callback_data="list"),
        InlineKeyboardButton("📨 Проверить почту", callback_data="check"),
        InlineKeyboardButton("🗑 Удалить ящик", callback_data="delete"),
        InlineKeyboardButton("📊 Помощь", callback_data="help")
    )
    return keyboard

@dp.message_handler(commands=['start', 'menu'])
async def start(message: types.Message):
    await message.reply(
        "📧 **Временная почта**\n\n"
        "Бот создаёт временные email-адреса\n"
        "Письма приходят прямо в Telegram\n\n"
        "Выберите действие:",
        parse_mode='Markdown',
        reply_markup=get_main_menu()
    )

# Реагируем на любое текстовое сообщение — показываем меню
@dp.message_handler()
async def any_message(message: types.Message):
    await message.reply(
        "📧 Используйте кнопки ниже:",
        reply_markup=get_main_menu()
    )

@dp.callback_query_handler(lambda c: c.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await bot.answer_callback_query(callback.id)
    await bot.send_message(
        callback.from_user.id,
        "📖 **Как пользоваться:**\n\n"
        "1. Нажмите «Создать почту» — получите email\n"
        "2. Отправляйте письма на этот адрес\n"
        "3. Нажмите «Проверить почту» — читайте письма\n"
        "4. Ящики хранятся вечно, пока вы не удалите\n"
        "5. Максимум 10 ящиков на пользователя\n\n"
        f"🌐 Домен: `{DOMAIN}`",
        parse_mode='Markdown'
    )

@dp.callback_query_handler(lambda c: c.data == "create")
async def create(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    
    if len(user_emails[user_id]) >= 10:
        await bot.answer_callback_query(callback.id, "❌ Максимум 10 ящиков", show_alert=True)
        return
    
    for _ in range(10):
        email = generate_email()
        if email not in mailboxes:
            mailboxes[email] = {'user_id': user_id, 'messages': []}
            user_emails[user_id].append(email)
            
            await bot.answer_callback_query(callback.id)
            await bot.send_message(
                user_id,
                f"✅ **Создан email:**\n`{email}`\n\n"
                f"📩 Отправляйте письма на этот адрес\n"
                f"📬 Они появятся здесь\n"
                f"🗑 Удалить можно через меню",
                parse_mode='Markdown',
                reply_markup=get_main_menu()
            )
            return
    
    await bot.answer_callback_query(callback.id, "❌ Ошибка", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "list")
async def list_emails(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = [e for e in user_emails.get(user_id, []) if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "📭 У вас нет ящиков", reply_markup=get_main_menu())
        return
    
    text = "📬 **Ваши ящики:**\n\n"
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        text += f"• `{email}` — 📨 {msg_count} писем\n"
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=get_main_menu())

@dp.callback_query_handler(lambda c: c.data == "check")
async def check(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = [e for e in user_emails.get(user_id, []) if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "📭 Нет ящиков", reply_markup=get_main_menu())
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"📨 {email} ({msg_count})",
            callback_data=f"view_{email}"
        ))
    keyboard.add(InlineKeyboardButton("🔙 Главное меню", callback_data="menu"))
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, "Выберите ящик:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("view_"))
async def view_mail(callback: types.CallbackQuery):
    email = callback.data.replace("view_", "")
    user_id = str(callback.from_user.id)
    
    if email not in mailboxes or mailboxes[email].get('user_id') != user_id:
        await bot.answer_callback_query(callback.id, "❌ Не найден", show_alert=True)
        return
    
    messages = mailboxes[email].get('messages', [])
    if not messages:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, f"📭 Писем для {email} нет", reply_markup=get_main_menu())
        return
    
    text = f"📩 **Письма для `{email}`:**\n\n"
    for i, msg in enumerate(messages[-10:][::-1], 1):
        time = msg['received_at'].strftime('%H:%M %d.%m')
        text += f"{i}. [{time}] От: {msg['sender'][:30]}\n"
        text += f"   📌 {msg['subject'][:40]}\n"
        preview = msg['body'][:80].replace('\n', ' ')
        text += f"   📝 {preview}...\n\n"
    
    if len(messages) > 10:
        text += f"... и еще {len(messages)-10} писем\n"
    
    text += f"\n📌 **Всего:** {len(messages)} писем"
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Назад к ящикам", callback_data="check"))
    keyboard.add(InlineKeyboardButton("🏠 Главное меню", callback_data="menu"))
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, text, parse_mode='Markdown', reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "delete")
async def delete_menu(callback: types.CallbackQuery):
    user_id = str(callback.from_user.id)
    emails = [e for e in user_emails.get(user_id, []) if e in mailboxes]
    
    if not emails:
        await bot.answer_callback_query(callback.id)
        await bot.send_message(user_id, "Нет ящиков", reply_markup=get_main_menu())
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    for email in emails:
        msg_count = len(mailboxes[email].get('messages', []))
        keyboard.add(InlineKeyboardButton(
            f"🗑 {email} ({msg_count})",
            callback_data=f"del_{email}"
        ))
    keyboard.add(InlineKeyboardButton("❌ Отмена", callback_data="menu"))
    
    await bot.answer_callback_query(callback.id)
    await bot.send_message(user_id, "⚠️ Выберите ящик для удаления:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_confirm(callback: types.CallbackQuery):
    email = callback.data.replace("del_", "")
    user_id = str(callback.from_user.id)
    
    if email in mailboxes and mailboxes[email].get('user_id') == user_id:
        user_emails[user_id].remove(email)
        del mailboxes[email]
        await bot.answer_callback_query(callback.id, f"✅ Удалён")
        await bot.send_message(
            user_id,
            f"🗑 {email} удалён",
            reply_markup=get_main_menu()
        )
    else:
        await bot.answer_callback_query(callback.id, "❌ Не найден", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "menu")
async def menu_callback(callback: types.CallbackQuery):
    await bot.answer_callback_query(callback.id)
    await bot.send_message(
        callback.from_user.id,
        "📧 Главное меню:",
        reply_markup=get_main_menu()
    )

# ===== ЗАПУСК =====
async def main():
    # Запускаем SMTP
    smtp = start_smtp()
    logging.info("✅ SMTP сервер запущен на порту 2525")
    
    # Запускаем Web сервер для Render
    await start_web()
    
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
