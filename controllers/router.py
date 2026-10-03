"""Controller/wiring layer: builds the PTB Application and owns the entry loop.

Transport stays python-telegram-bot (polling); this module wires the
registered handlers/commands/jobs from domain controllers and runs the bot,
preserving the external entry signature (main() -> run_polling).
"""

import sys
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time as dt_time, timedelta, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.request import HTTPXRequest

import config
from constants import CaptionState, State
from models import database as db_app

# Domain controllers
from controllers.common import (
    error_handler,
    is_supervisor,
    logger,
    track_user,
)
from controllers.coverage import (
    cancel_command,
    handle_confirmation,
    handle_coverage_toggle,
    handle_date_callback,
    handle_end_time_callback,
    handle_menu,
    set_date,
    set_dept,
    set_end_time,
    set_event_name,
    set_event_type,
    set_external_media,
    set_importance,
    set_location,
    set_notes,
    set_objective,
    set_time,
    set_time_callback,
    start,
)
from controllers.borrow import (
    borrow_set_borrower,
    borrow_set_item,
    borrow_set_phone,
    borrow_set_quantity,
    borrow_set_reason,
    borrow_set_return_date,
    borrow_set_return_time,
    handle_borrow_confirmation,
    handle_borrow_date_callback,
    handle_borrow_responsibility,
    handle_borrow_time_callback,
)
from controllers.user_panel import handle_user_action
from controllers.admin import (
    admin_dashboard,
    borrow_dashboard_command,
    broadcast_start_command,
    find_command,
    handle_admin_action,
    handle_admin_reply,
    handle_borrow_dashboard,
    handle_settings_callbacks,
    health_command,
)
from controllers.jobs import (
    check_pre_event_reminders,
    cleanup_cache,
    send_borrow_reminder,
    send_daily_reminder,
    test_borrow_reminder_command,
    test_reminder_command,
)
from controllers.ai import (
    caption_callback,
    caption_command,
    caption_edit_text,
    caption_model_callback,
    caption_receive_prompt,
    caption_task_callback,
    caption_timeout,
    caption_tone_callback,
    handle_dose_refresh,
    send_daily_media_dose,
    test_dose_command,
)


def setup_logging():
    """Configure dual logging: stdout + rotating bot.log (5MB max, 2 backups)."""
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        try:
            file_handler = RotatingFileHandler(
                "bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.INFO)
            root_logger.addHandler(file_handler)
        except Exception:
            pass

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(logging.INFO)
        root_logger.addHandler(stream_handler)

    # Mute noisy internal HTTP polling logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.request").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# ─── Application setup ────────────────────────────────────────────────────────
async def setup_application():
    await db_app.init_db()

    # Robust network timeouts to handle slow or unstable connections
    timeout = getattr(config, "HTTP_TIMEOUT", 60.0)
    request_kwargs = {
        "connection_pool_size": 100,
        "connect_timeout": timeout,
        "read_timeout": 120.0,
        "write_timeout": 60.0,
        "pool_timeout": 60.0,
        "media_write_timeout": 60.0,
    }
    proxy_url = getattr(config, "PROXY_URL", None)
    if proxy_url:
        request_kwargs["proxy"] = proxy_url
        logger.info(f"Using proxy for Telegram requests: {proxy_url}")

    request_config = HTTPXRequest(**request_kwargs)
    persistence = PicklePersistence(
        filepath="bot_persistence.pickle",
        update_interval=30
    )

    async def post_shutdown(app: Application):
        try:
            if app.persistence:
                await app.persistence.flush()
        except Exception:
            pass
        await db_app.close_db()
        logger.info("Database connection and persistence closed cleanly on shutdown.")

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .concurrent_updates(True)
        .persistence(persistence)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Global error handler
    application.add_error_handler(error_handler)

    # Track users for username updates (group 3 runs after main handlers)
    application.add_handler(MessageHandler(filters.ALL, track_user), group=3)
    application.add_handler(CallbackQueryHandler(track_user), group=3)

    # Global admin callbacks
    application.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(handle_borrow_dashboard, pattern="^bdash_"))
    application.add_handler(CallbackQueryHandler(handle_settings_callbacks, pattern="^settings_"))

    # Admin commands
    application.add_handler(CommandHandler("broadcast_start", broadcast_start_command))
    application.add_handler(CommandHandler("dashboard", admin_dashboard))
    application.add_handler(CommandHandler("test_reminder", test_reminder_command))
    application.add_handler(CommandHandler("test_borrow_reminder", test_borrow_reminder_command))
    application.add_handler(CommandHandler("borrow_dashboard", borrow_dashboard_command))
    application.add_handler(CommandHandler("test_dose", test_dose_command))

    # Diagnostics & Quick Search commands
    application.add_handler(CommandHandler(["health", "status", "ping"], health_command))
    application.add_handler(CommandHandler(["find", "search"], find_command))

    # Admin ForceReply handler (group 1 runs across all states)
    application.add_handler(
        MessageHandler(filters.REPLY & ~filters.COMMAND, handle_admin_reply),
        group=1
    )

    # AI Daily Dose refresh callback
    application.add_handler(CallbackQueryHandler(handle_dose_refresh, pattern="^dose_refresh"))

    # Caption Generator Conversation Handler
    caption_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("caption", caption_command)],
        states={
            CaptionState.TYPING_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_receive_prompt)
            ],
            CaptionState.CHOOSING_TASK: [
                CallbackQueryHandler(caption_task_callback, pattern="^captask_")
            ],
            CaptionState.CHOOSING_TONE: [
                CallbackQueryHandler(caption_tone_callback, pattern="^captone_")
            ],
            CaptionState.CHOOSING_MODEL: [
                CallbackQueryHandler(caption_model_callback, pattern="^capmodel_")
            ],
            CaptionState.EDITING: [
                CallbackQueryHandler(caption_callback, pattern="^caption_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_edit_text)
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, caption_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        per_user=True,
        per_chat=True,
        name="caption_conversation",
        persistent=True,
        conversation_timeout=timedelta(minutes=30)
    )
    application.add_handler(caption_conv_handler)

    # Main Conversation Handler (Media Coverage + Equipment Borrow)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            State.MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu),
                CallbackQueryHandler(handle_user_action)
            ],
            State.CHOOSING_DEPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_dept)],
            State.EVENT_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, set_event_name)],
            State.EVENT_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, set_event_type)],
            State.OBJECTIVE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, set_objective)],
            State.DATE: [
                CallbackQueryHandler(handle_date_callback, pattern="^cal_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_date),
            ],
            State.TIME: [
                CallbackQueryHandler(set_time_callback, pattern="^time_pick_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_time),
            ],
            State.END_TIME: [
                CallbackQueryHandler(handle_end_time_callback, pattern="^endtime_pick_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_end_time),
            ],
            State.LOCATION:    [MessageHandler(filters.TEXT & ~filters.COMMAND, set_location)],
            State.COVERAGE_TYPE: [
                CallbackQueryHandler(handle_coverage_toggle),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coverage_toggle)
            ],
            State.IMPORTANCE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, set_importance)],
            State.EXTERNAL_MEDIA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_external_media)],
            State.NOTES:       [MessageHandler(filters.TEXT & ~filters.COMMAND, set_notes)],
            State.CONFIRMATION:[CallbackQueryHandler(handle_confirmation)],
            State.BORROW_ITEM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_item)
            ],
            State.BORROW_QUANTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_quantity)
            ],
            State.BORROW_BORROWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_borrower)],
            State.BORROW_REASON:   [MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_reason)],
            State.BORROW_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_phone)],
            State.BORROW_RETURN_DATE: [
                CallbackQueryHandler(handle_borrow_date_callback, pattern="^bcal_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_return_date),
            ],
            State.BORROW_RETURN_TIME: [
                CallbackQueryHandler(handle_borrow_time_callback, pattern="^btime_pick_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, borrow_set_return_time),
            ],
            State.BORROW_CONFIRMATION: [CallbackQueryHandler(handle_borrow_confirmation)],
            State.BORROW_RESPONSIBILITY: [CallbackQueryHandler(handle_borrow_responsibility, pattern="^bresp_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start)
        ],
        name="main_conversation",
        persistent=True,
        per_message=False,
    )
    application.add_handler(conv_handler)

    # Schedules: 7:00 AM, 14:00 PM (2:00 PM), 21:00 PM (9:00 PM) Damascus time (UTC+3)
    t1 = dt_time(hour=7, minute=0, tzinfo=timezone(timedelta(hours=3)))
    t2 = dt_time(hour=14, minute=0, tzinfo=timezone(timedelta(hours=3)))
    t3 = dt_time(hour=21, minute=0, tzinfo=timezone(timedelta(hours=3)))

    # Daily Media Dose: 11:00 AM and 18:00 PM Damascus time
    dose_morning = dt_time(hour=11, minute=0, tzinfo=timezone(timedelta(hours=3)))
    dose_evening = dt_time(hour=18, minute=0, tzinfo=timezone(timedelta(hours=3)))

    if application.job_queue:
        application.job_queue.run_daily(send_daily_reminder, t1)
        application.job_queue.run_daily(send_daily_reminder, t2)
        application.job_queue.run_daily(send_daily_reminder, t3)
        application.job_queue.run_daily(send_borrow_reminder, t1)
        application.job_queue.run_daily(send_borrow_reminder, t2)
        application.job_queue.run_daily(send_borrow_reminder, t3)
        application.job_queue.run_daily(send_daily_media_dose, dose_morning)
        application.job_queue.run_daily(send_daily_media_dose, dose_evening)

        # Cache cleanup every 48 hours
        application.job_queue.run_repeating(cleanup_cache, interval=timedelta(hours=48), first=10)

        # Pre-event reminders (30 min prior, check every 60s)
        application.job_queue.run_repeating(check_pre_event_reminders, interval=60, first=5)

        logger.info("✅ Reminders, Daily Dose, and Cache Cleanup scheduled.")
    else:
        logger.warning("⚠️ job_queue is None — install python-telegram-bot[job-queue] to enable reminders")

    return application


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    setup_logging()
    retry_delay = 5
    max_delay = 60
    attempt = 0

    while True:
        attempt += 1
        start_time = time.monotonic()
        try:
            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            application = loop.run_until_complete(setup_application())
            logger.info("Bot started successfully...")
            application.run_polling(
                allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
                drop_pending_updates=False
            )
            break
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped by user or system signal.")
            break
        except Exception as e:
            run_duration = time.monotonic() - start_time
            if run_duration > 120:
                retry_delay = 5  # Reset backoff if bot was running stably before crash

            logger.exception(f"Bot encountered an error (incident #{attempt}): {e}")
            logger.info(f"Auto-reconnecting in {retry_delay} seconds...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)