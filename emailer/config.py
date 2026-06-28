import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")
MAILCAT_API = "https://api.mailcat.ai"

if not TURSO_URL or not TURSO_TOKEN:
    logger.error("❌ TURSO_URL or TURSO_TOKEN not set")
    exit(1)

if TURSO_URL.startswith("libsql://"):
    TURSO_URL = TURSO_URL.replace("libsql://", "https://")
    logger.info(f"✅ Converted to HTTPS: {TURSO_URL}")

TURSO_URL = TURSO_URL.replace(":443", "").rstrip("/")
