from .config import logger
from . import db
from .db import ensure_list, extract_value

user_accounts_cache = {}
bot_messages = {}


async def load_all_users_to_cache():
    sql = 'SELECT user_id, email, token, account_id, messages, read_ids FROM users'
    result = await db.turso.execute(sql)
    rows = result.get('result', {}).get('rows', [])
    for row in rows:
        if isinstance(row, (list, tuple)):
            if len(row) >= 6:
                user_id = str(extract_value(row[0]))
                user_accounts_cache[user_id] = {
                    'email': str(extract_value(row[1])) if row[1] else '',
                    'token': str(extract_value(row[2])) if row[2] else '',
                    'account_id': str(extract_value(row[3])) if row[3] else '',
                    'messages': ensure_list(row[4]),
                    'read_ids': ensure_list(row[5])
                }
        elif isinstance(row, dict):
            user_id = str(extract_value(row.get('user_id', '')))
            user_accounts_cache[user_id] = {
                'email': str(extract_value(row.get('email', ''))),
                'token': str(extract_value(row.get('token', ''))),
                'account_id': str(extract_value(row.get('account_id', ''))),
                'messages': ensure_list(row.get('messages', [])),
                'read_ids': ensure_list(row.get('read_ids', []))
            }
    if rows:
        logger.info(f"✅ Loaded {len(rows)} users from Turso")
