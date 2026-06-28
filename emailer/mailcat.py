from datetime import datetime

import aiohttp

from .config import MAILCAT_API
from .parsing import clean_html_fallback, decode_header_value, extract_code, extract_links_from_text, find_confirmation_link


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
    if 'read_ids' not in account or not isinstance(account['read_ids'], list):
        account['read_ids'] = []

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
            if msg_id not in account['read_ids']:
                account['read_ids'].append(msg_id)
                async with session.get(f"{MAILCAT_API}/emails/{msg_id}", headers=headers) as resp2:
                    if resp2.status in [200, 201]:
                        full = await resp2.json()
                        email_data = full.get('data', {})

                        raw_subject = email_data.get('email', {}).get('subject', '(no subject)')
                        subject = decode_header_value(raw_subject)
                        raw_from = email_data.get('email', {}).get('from', 'unknown')
                        sender = decode_header_value(raw_from)

                        clean_text = email_data.get('email', {}).get('text', '')
                        if not clean_text:
                            html = email_data.get('email', {}).get('html', '')
                            clean_text = clean_html_fallback(html)

                        all_links = extract_links_from_text(clean_text)
                        confirm_link = find_confirmation_link(all_links)
                        code = extract_code(clean_text)

                        new_messages.append({
                            'sender': sender,
                            'subject': subject,
                            'links': [confirm_link] if confirm_link else [],
                            'code': code,
                            'received_at': datetime.now().isoformat()
                        })
        return new_messages
