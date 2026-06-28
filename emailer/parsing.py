import email.header
import re


def decode_header_value(value):
    if not value:
        return ''
    try:
        decoded_parts = []
        for part, encoding in email.header.decode_header(value):
            if isinstance(part, bytes):
                try:
                    if encoding:
                        part = part.decode(encoding, errors='ignore')
                    else:
                        part = part.decode('utf-8', errors='ignore')
                except:
                    part = part.decode('utf-8', errors='ignore')
            decoded_parts.append(str(part))
        return ' '.join(decoded_parts)
    except:
        return value


def extract_links_from_text(text):
    if not text:
        return []
    url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    links = re.findall(url_pattern, text)
    clean = []
    for link in links:
        link = link.strip('.,;:!?()[]{}"\'')
        if link.startswith('http') or link.startswith('www'):
            clean.append(link)
    return clean


def clean_html_fallback(html):
    if not html:
        return ''
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
    html = re.sub(r'<[^>]+>', ' ', html)
    html = re.sub(r'\{[^}]*\}', '', html)
    html = re.sub(r'\s+', ' ', html)
    return html.strip()


def find_confirmation_link(links):
    if not links:
        return None
    for link in links:
        if 'confirmemail' in link.lower() or 'verify' in link.lower() or 'confirmation' in link.lower():
            return link
    for link in links:
        if '?' in link and len(link) > 30:
            return link
    return links[0] if links else None


def extract_code(text):
    if not text:
        return None
    patterns = [
        r'\b(\d{4,8})\b',
        r'код[:\s]*([A-Z0-9]{4,8})',
        r'code[:\s]*([A-Z0-9]{4,8})',
        r'verification code[:\s]*([A-Z0-9]{4,8})',
        r'подтверждения[:\s]*([A-Z0-9]{4,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            code = match.group(1) if match.groups() else match.group(0)
            if len(code) >= 4:
                return code
    return None


def escape_markdown(text):
    """Экранирует только спецсимволы для Markdown"""
    if not text:
        return ''
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars:
        text = text.replace(char, f'\\{char}')
    return text
