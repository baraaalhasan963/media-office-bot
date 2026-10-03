"""Thin async wrapper around python-telegram-bot bot.* calls.

Centralizes error handling / retry so controllers stay free of
try/except noise. Keeps the PTB transport unchanged (polling + httpx
connection pooling inside PTB Application).

Usage:
    from api_client import client
    await client.send_message(context, chat_id=..., text=..., ...)
"""

import logging
import asyncio
import time

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError
import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2


def _resolve_bot(context_or_bot):
    if hasattr(context_or_bot, "bot"):
        return context_or_bot.bot
    return context_or_bot


async def _run_with_retry(coro_factory):
    """Execute a coroutine factory, retrying transient Telegram errors."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_factory()
        except (TelegramError, httpx.HTTPError) as e:
            last_exc = e
            msg = str(e).lower()
            transient = (
                "timed out" in msg
                or "timeout" in msg
                or "retry after" in msg
                or "migrate" in msg
                or "flood" in msg
                or "server" in msg
                or "connection" in msg
            )
            if not transient or attempt == MAX_RETRIES - 1:
                break
            await asyncio.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
        except Exception as e:  # noqa: BLE001 - defensive barrier
            last_exc = e
            logger.error("api_client error: %s", e)
            break
    raise last_exc


async def send_message(context_or_bot, chat_id, text, *, parse_mode="HTML",
                      reply_markup=None, message_thread_id=None, **kwargs):
    bot = _resolve_bot(context_or_bot)
    return await _run_with_retry(lambda: bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        message_thread_id=message_thread_id,
        **kwargs,
    ))



async def edit_message_text(context, chat_id, message_id, text, *,
                            parse_mode="HTML", reply_markup=None, **kwargs):
    return await _run_with_retry(lambda: context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        **kwargs,
    ))


async def answer_callback_query(context, callback_query_id, text=None,
                                show_alert=False):
    return await _run_with_retry(lambda: context.bot.answer_callback_query(
        callback_query_id=callback_query_id,
        text=text,
        show_alert=show_alert,
    ))


async def delete_message(context, chat_id, message_id):
    return await _run_with_retry(lambda: context.bot.delete_message(
        chat_id=chat_id, message_id=message_id
    ))


async def send_photo(context, chat_id, photo, *, caption=None, parse_mode="HTML",
                     reply_markup=None, message_thread_id=None, **kwargs):
    return await _run_with_retry(lambda: context.bot.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=caption,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        message_thread_id=message_thread_id,
        **kwargs,
    ))


async def send_audio(context, chat_id, audio, *, caption=None,
                     reply_markup=None, **kwargs):
    return await _run_with_retry(lambda: context.bot.send_audio(
        chat_id=chat_id, audio=audio, caption=caption,
        reply_markup=reply_markup, **kwargs,
    ))


async def sleep_seconds(seconds):
    """Best-effort throttle helper (kept out of controllers tests)."""
    await asyncio.sleep(seconds)