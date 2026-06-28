import json

from .config import logger


turso = None


def configure_db(client):
    global turso
    turso = client


def extract_value(data):
    if isinstance(data, dict) and 'value' in data:
        return data['value']
    return data


def ensure_list(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                return parsed
            return [parsed] if parsed else []
        except:
            return []
    if isinstance(data, dict):
        return [data] if data else []
    return [data] if data else []


def serialize_messages(messages):
    if not messages:
        return '[]'
    return json.dumps(messages, ensure_ascii=False)


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
                'messages': ensure_list(row[3]),
                'read_ids': ensure_list(row[4])
            }
        elif isinstance(row, dict):
            user_data = {
                'email': str(extract_value(row.get('email', ''))),
                'token': str(extract_value(row.get('token', ''))),
                'account_id': str(extract_value(row.get('account_id', ''))),
                'messages': ensure_list(row.get('messages', [])),
                'read_ids': ensure_list(row.get('read_ids', []))
            }
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
    read_ids = account.get('read_ids', [])
    if not isinstance(read_ids, list):
        read_ids = []
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
        json.dumps(read_ids)
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
