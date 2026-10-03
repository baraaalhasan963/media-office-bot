"""Common helpers, filters, supervisor verification, and error handler."""

import time
import logging
import httpx
from telegram import Update
from telegram.error import TelegramError, TimedOut, NetworkError, Forbidden, BadRequest
from telegram.ext import ContextTypes

import config
from models import database as db_app
from views.formatting import escape_html

logger = logging.getLogger("bot")

# In-memory user tracking cache (5 minutes cooldown per user to prevent redundant DB writes/locks)
_USER_TRACK_CACHE: dict[int, tuple[float, str, str]] = {}
_USER_TRACK_COOLDOWN = 300  # 5 minutes

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    user_id = user.id
    username = user.username or ""
    full_name = user.full_name or ""
    now = time.monotonic()

    cached = _USER_TRACK_CACHE.get(user_id)
    if cached:
        last_time, last_uname, last_fname = cached
        if (now - last_time < _USER_TRACK_COOLDOWN) and (username == last_uname) and (full_name == last_fname):
            return

    _USER_TRACK_CACHE[user_id] = (now, username, full_name)
    try:
        await db_app.register_user(
            user_id=user_id,
            username=username,
            full_name=full_name
        )
    except Exception as e:
        logger.debug(f"User tracking error: {e}")

async def is_supervisor(user_id: int) -> bool:
    """التحقق المباشر من صلاحية المشرف بالاعتماد على معرّفات الأدمن في config.ADMIN_USERS_IDS."""
    try:
        return int(user_id) in config.ADMIN_USERS_IDS
    except (ValueError, TypeError):
        return False

# Rate-limiting cache for admin alerts (to prevent spamming the admin chat)
_ERROR_ALERT_CACHE = {}
_ERROR_ALERT_COOLDOWN = 300  # 5 minutes cooldown per distinct error

def is_transient_error(err: Exception) -> bool:
    """Returns True if the error is a transient network or transport blip."""
    if isinstance(err, (TimedOut, NetworkError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    err_str_lower = str(err).lower()
    return (
        "timed out" in err_str_lower
        or "timeout" in err_str_lower
        or "connection reset" in err_str_lower
        or "forcibly closed" in err_str_lower
        or "network is unreachable" in err_str_lower
        or "connection refused" in err_str_lower
    )


def is_benign_telegram_error(err: Exception) -> bool:
    """Returns True if the error is a standard benign Telegram API update error."""
    err_str_lower = str(err).lower()
    if isinstance(err, Forbidden) or "bot was blocked by the user" in err_str_lower:
        return True
    if isinstance(err, BadRequest) and (
        "query is too old" in err_str_lower
        or "message is not modified" in err_str_lower
        or "message to edit not found" in err_str_lower
    ):
        return True
    return False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Intelligent global error handler:
    - Logs transient network timeouts without spamming admin chat.
    - Rate-limits notifications for recurring application exceptions.
    """
    err = context.error
    if not err:
        return

    err_str = str(err)

    # Check for transient transport / network errors
    if is_transient_error(err):
        logger.warning(f"Transient network glitch (suppressed from admin chat): {err}")
        return

    # Check for client-side / benign errors (e.g. user blocked bot or query expired)
    if is_benign_telegram_error(err):
        logger.debug(f"Benign Telegram error suppressed: {err}")
        return

    # Log real application errors with full traceback
    logger.error("Exception while handling an update:", exc_info=err)

    # Rate-limit notifications to the admin chat
    now = time.monotonic()
    error_key = f"{type(err).__name__}:{err_str[:80]}"
    last_sent = _ERROR_ALERT_CACHE.get(error_key, 0)
    if now - last_sent < _ERROR_ALERT_COOLDOWN:
        return  # Suppress duplicate alert within cooldown
    _ERROR_ALERT_CACHE[error_key] = now

    try:
        err_text = escape_html(err_str[:1200])
        kwargs = {
            "chat_id": config.ADMIN_CHAT_ID,
            "text": f"⚠️ <b>خطأ تقني في البوت:</b>\n<code>{err_text}</code>",
            "parse_mode": "HTML"
        }
        if config.ADMIN_TOPIC_ID:
            kwargs["message_thread_id"] = config.ADMIN_TOPIC_ID
        await context.bot.send_message(**kwargs)
    except Exception as notify_err:
        logger.error(f"Error notifying admin about failure: {notify_err}")
