"""Controller/wiring layer: builds the PTB Application and owns the entry loop.

Transport stays python-telegram-bot (polling); this module only wires the
registered handlers/commands/jobs defined in `main` and runs the bot,
preserving the external entry signature (``main()`` → ``run_polling``).
"""

from datetime import timedelta
from datetime import time as dt_time
from datetime import timezone

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Handler/model references (handlers remain in main until fully split into
# controllers packages; router imports them lazily via the loaded main module
# to avoid a circular import at first import of main).
from main import (
    ForceReply,
    State,
    CaptionState,
    Update,
    ContextTypes,
    asyncio,
    config,
    db_app,
    kb,
    filters as _filters,
    track_user,
    error_handler,
    handle_admin_action,
    handle_borrow_dashboard,
    handle_settings_callbacks,
    is_supervisor,
    admin_dashboard,
    test_reminder_command,
    test_borrow_reminder_command,
    borrow_dashboard_command,
    test_dose_command,
    handle_admin_reply,
    send_daily_media_dose,
    send_daily_reminder,
    send_borrow_reminder,
    cleanup_cache,
    check_pre_event_reminders,
    cancel_command,
    start,
    handle_menu,
    handle_user_action,
    set_dept,
    set_event_name,
    set_event_type,
    set_objective,
    handle_date_callback,
    set_date,
    set_time_callback,
    set_time,
    handle_end_time_callback,
    set_end_time,
    set_location,
    handle_coverage_toggle,
    set_importance,
    set_external_media,
    set_notes,
    handle_confirmation,
    borrow_set_item,
    borrow_set_quantity,
    borrow_set_borrower,
    borrow_set_reason,
    borrow_set_phone,
    handle_borrow_date_callback,
    borrow_set_return_date,
    handle_borrow_time_callback,
    borrow_set_return_time,
    handle_borrow_confirmation,
    handle_borrow_responsibility,
    caption_command,
    caption_receive_prompt,
    caption_task_callback,
    caption_model_callback,
    caption_callback,
    caption_edit_text,
    logger,
)


# ─── Application setup ────────────────────────────────────────────────────────
async def setup_application():
    await db_app.init_db()

    # Increase network timeouts to handle slow/unreliable connections
    request_config = HTTPXRequest(connect_timeout=60.0, read_timeout=120.0)
    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    # تسجيل معالج الأخطاء العالمي لمراقبة استقرار البوت
    application.add_error_handler(error_handler)

    # Track users for username updates (runs after main handlers)
    application.add_handler(MessageHandler(filters.ALL, track_user), group=3)
    application.add_handler(CallbackQueryHandler(track_user), group=3)


    # Global admin handler (works in private + groups)
    application.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(handle_borrow_dashboard, pattern="^bdash_"))
    application.add_handler(CallbackQueryHandler(handle_settings_callbacks, pattern="^settings_"))

    # Broadcast /start command for admins
    async def broadcast_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await is_supervisor(update.effective_user.id):
            await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
            return
            
        status_msg = await update.message.reply_text("⏳ جاري البدء بعملية البث...")
        user_ids = await db_app.get_all_users()
        
        success_count = 0
        fail_count = 0
        
        for uid in user_ids:
            try:
                # We can't literally "send /start" as a command, but we can trigger the start logic
                # or send a message that invites them to click start.
                # Here we just send the main menu message directly to them.
                is_user_admin = await is_supervisor(uid)
                await context.bot.send_message(
                    chat_id=uid,
                    text="🔄 <b>تم تحديث النظام</b>\n\nتم تحديث البوت وإعادة تشغيله، يرجى الضغط على القائمة واختيار (start) لإعادة تشغيل البوت والعمل عليه.",
                    reply_markup=kb.main_menu_keyboard(is_user_admin),
                    parse_mode="HTML"
                )
                success_count += 1
                await asyncio.sleep(0.05) # Rate limiting
            except Exception:
                fail_count += 1
                
        await status_msg.edit_text(
            f"✅ <b>اكتمل البث بنجاح!</b>\n\n"
            f"▫️ تم الوصول لـ: {success_count} مستخدم\n"
            f"▫️ فشل الإرسال لـ: {fail_count} مستخدم (ربما قاموا بحظر البوت)",
            parse_mode="HTML"
        )

    application.add_handler(CommandHandler("broadcast_start", broadcast_start_command))

    # Dose refresh button
    async def handle_dose_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer("⏳ جاري توليد جرعة جديدة...")
        prev_text = context.bot_data.get("last_dose_text", "")
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        success, error = await send_daily_media_dose(context, prev_dose=prev_text)
        if not success:
            await query.message.reply_text(f"❌ تعذّر توليد جرعة جديدة:\n{error}")

    application.add_handler(CallbackQueryHandler(handle_dose_refresh, pattern="^dose_refresh"))

    # Admin commands
    application.add_handler(CommandHandler("dashboard", admin_dashboard))
    application.add_handler(CommandHandler("test_reminder", test_reminder_command))
    application.add_handler(CommandHandler("test_borrow_reminder", test_borrow_reminder_command))
    application.add_handler(CommandHandler("borrow_dashboard", borrow_dashboard_command))
    application.add_handler(CommandHandler("test_dose", test_dose_command))
    # Handler for admin ForceReply responses (rejection reasons) — must run in all states
    application.add_handler(
        MessageHandler(filters.REPLY & ~filters.COMMAND, handle_admin_reply),
        group=1
    )

    # Caption Generator Conversation Handler
    async def caption_timeout(update: object, context: ContextTypes.DEFAULT_TYPE):
        """يُنهي جلسة الكابشن تلقائياً بعد انتهاء المهلة."""
        try:
            await context.bot.send_message(
                chat_id=context._chat_id,
                text="⏰ انتهت مدة جلسة الكابشن (30 دقيقة) وتم إغلاقها تلقائياً."
            )
        except Exception:
            pass
        context.user_data.pop('caption_prompt', None)
        context.user_data.pop('last_caption', None)
        context.user_data.pop('caption_model', None)
        context.user_data.pop('awaiting_caption_edit', None)
        return ConversationHandler.END

    caption_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("caption", caption_command)],
        states={
            CaptionState.TYPING_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, caption_receive_prompt)
            ],
            CaptionState.CHOOSING_TASK: [
                CallbackQueryHandler(caption_task_callback, pattern="^captask_")
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
        conversation_timeout=timedelta(minutes=30)
    )
    application.add_handler(caption_conv_handler)


    # Main conversation
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
        fallbacks=[CommandHandler("cancel", cancel_command),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
        per_message=False,
    )
    application.add_handler(conv_handler)

    # Schedules: 7:00 AM, 14:00 PM (2:00 PM), 21:00 PM (9:00 PM) Damascus time (UTC+3)
    t1 = dt_time(hour=7, minute=0, tzinfo=timezone(timedelta(hours=3)))
    t2 = dt_time(hour=14, minute=0, tzinfo=timezone(timedelta(hours=3)))
    t3 = dt_time(hour=21, minute=0, tzinfo=timezone(timedelta(hours=3)))
    
    # الجرعة الإعلامية اليومية الساعة 11:00 صباحاً و 18:00 مساءً بتوقيت دمشق
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
        
        # تنظيف الكاش كل يومين (48 ساعة)
        application.job_queue.run_repeating(cleanup_cache, interval=timedelta(hours=48), first=10)
        
        # تذكير قبل الحدث بـ 30 دقيقة (فحص كل دقيقة)
        application.job_queue.run_repeating(check_pre_event_reminders, interval=60, first=5)
        
        logger.info("✅ Reminders, Daily Dose, and Cache Cleanup scheduled.")
    else:
        logger.warning("⚠️ job_queue is None — install python-telegram-bot[job-queue] to enable reminders")

    return application

# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    max_retries = 5
    retry_delay = 10
    for attempt in range(1, max_retries + 1):
        try:
            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            application = loop.run_until_complete(setup_application())
            print("Bot started successfully...")
            application.run_polling(allowed_updates=Update.ALL_TYPES)
            break
        except Exception as e:
            logger.exception(f"Bot crashed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                import time
                print(f"Restarting in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)
            else:
                logger.critical("All restart attempts exhausted. Bot stopped.")
                raise