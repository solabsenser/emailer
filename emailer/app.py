import asyncio

from .bot_app import bot, dp
from .config import TURSO_TOKEN, TURSO_URL, logger
from .db import configure_db, get_all_users, get_user, init_db, save_user
from .mailcat import check_mailcat
from .storage import load_all_users_to_cache, user_accounts_cache
from .turso import TursoClient
from .webapp import start_web

configure_db(TursoClient(TURSO_URL, TURSO_TOKEN))


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
                            account['messages'] = []
                        if not isinstance(account.get('read_ids'), list):
                            account['read_ids'] = []
                        new = await check_mailcat(account)
                        if new:
                            account['messages'].extend(new)
                            await save_user(user_id, account)
                            msg = new[0]
                            text = f"📨 **Новое письмо!**\n\nОт: {msg['sender'][:35]}\nТема: {msg['subject'][:40]}"
                            if msg.get('code'):
                                text += f"\n🔑 Код: `{msg['code']}`"
                            if msg.get('links') and msg['links'][0]:
                                text += f"\n🔗 {msg['links'][0]}"
                            await bot.send_message(int(user_id), text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Background error for {user_id}: {e}")
        except Exception as e:
            logger.error(f"Background error: {e}")
        await asyncio.sleep(30)


async def main():
    try:
        await init_db()
        logger.info("✅ Database initialized")
        await load_all_users_to_cache()
        await start_web()
        asyncio.create_task(background_check())
        for attempt in range(5):
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                logger.info("✅ Webhook deleted")
                break
            except Exception as e:
                logger.warning(f"Webhook delete attempt {attempt+1} failed: {e}")
                await asyncio.sleep(2)
        logger.info("🚀 Bot started")
        while True:
            try:
                await dp.start_polling(bot)
                break
            except Exception as e:
                if "ConflictError" in str(e) or "TerminatedByOtherGetUpdates" in str(e):
                    logger.warning("⚠️ Conflict, waiting 10 seconds...")
                    await asyncio.sleep(10)
                else:
                    logger.error(f"Polling error: {e}")
                    await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
