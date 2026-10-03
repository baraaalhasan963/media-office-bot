import os
from dotenv import load_dotenv

load_dotenv()
load_dotenv("env.txt")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
ADMIN_TOPIC_ID_raw = os.getenv("ADMIN_TOPIC_ID", "")
ADMIN_TOPIC_ID = int(ADMIN_TOPIC_ID_raw) if ADMIN_TOPIC_ID_raw and ADMIN_TOPIC_ID_raw != "None" else None

BORROW_TOPIC_ID_raw = os.getenv("BORROW_TOPIC_ID", "")
BORROW_TOPIC_ID = int(BORROW_TOPIC_ID_raw) if BORROW_TOPIC_ID_raw and BORROW_TOPIC_ID_raw != "None" else None

ADMIN_USERS_IDS_raw = os.getenv("ADMIN_USERS_IDS", "")
ADMIN_USERS_IDS = []
if ADMIN_USERS_IDS_raw:
    for part in ADMIN_USERS_IDS_raw.split(","):
        part = part.strip()
        if part:
            try:
                ADMIN_USERS_IDS.append(int(part))
            except ValueError:
                pass

DB_PATH = os.getenv("DB_PATH", "bot_database.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

DOSE_CHAT_ID = os.getenv("DOSE_CHAT_ID", ADMIN_CHAT_ID)
DOSE_TOPIC_ID_raw = os.getenv("DOSE_TOPIC_ID", "")
DOSE_TOPIC_ID = int(DOSE_TOPIC_ID_raw) if DOSE_TOPIC_ID_raw and DOSE_TOPIC_ID_raw != "None" else ADMIN_TOPIC_ID

SQLITE_BUSY_TIMEOUT = int(os.getenv("SQLITE_BUSY_TIMEOUT", "15000"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "60.0"))
PROXY_URL = os.getenv("PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

