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
