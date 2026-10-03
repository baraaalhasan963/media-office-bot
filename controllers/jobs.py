"""Scheduled reminders and background jobs."""

import logging
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ContextTypes

import config
from models import database as db_app
from views.formatting import escape_html, format_date_ar, parse_ar_time
from controllers.common import is_supervisor

logger = logging.getLogger("bot.jobs")


async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Sends reminder for all upcoming accepted coverages and alerts about pending requests."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")

    try:
        upcoming_approved = await db_app.get_upcoming_approved_coverages(today_str)
        pending_requests = await db_app.get_pending_coverages(today_str, limit=10)
        total_pending = await db_app.get_total_pending_count()

        # Filter out events whose time has already passed today
        now_time = now.time()
        final_approved = []
        for req in upcoming_approved:
            if req['date'] == today_str:
                req_time = parse_ar_time(req['time'])
                if req_time and req_time < now_time:
                    continue
            final_approved.append(req)

        # Conflict detection
        time_groups: dict = {}
        for req in final_approved:
            key = (req['date'], req['time'])
            time_groups.setdefault(key, []).append(req)
        conflict_keys = {k for k, v in time_groups.items() if len(v) > 1}

        if final_approved:
            msg = "🔔 <b>تذكير بجدول التغطيات المعتمدة القادمة:</b>\n\n"
            for req in final_approved:
                d_str = "اليوم" if req['date'] == today_str else format_date_ar(req['date'])
                conflict_flag = " 🚨" if (req['date'], req['time']) in conflict_keys else ""
                end_time = req['end_time'] if req['end_time'] else "غير محدد"
                msg += (
                    f"▫️ <b>{d_str}</b>{conflict_flag} — <b>{escape_html(req['time'])} إلى {escape_html(end_time)}</b>\n"
                    f"   📌 {escape_html(req['event_name'])} ({escape_html(req['department'])})\n"
                    f"   📍 {escape_html(req['location'])}\n"
                    "   🔎 لمزيد من التفاصيل، راجع لوحة التحكم.\n"
                    "─────────────────\n\n"
                )

            if conflict_keys:
                msg += "🚨 <b>تحذير: يوجد تعارض في المواعيد!</b>\n"
                for (c_date, c_time), c_reqs in time_groups.items():
                    if len(c_reqs) < 2:
                        continue
                    c_d_str = "اليوم" if c_date == today_str else format_date_ar(c_date)
                    names = " &amp; ".join(f"<b>{escape_html(r['event_name'])}</b>" for r in c_reqs)
                    msg += f"   ⚠️ {c_d_str} الساعة {escape_html(c_time)}: {names}\n"
                msg += "\n"
        else:
            msg = "✅ لا توجد أي تغطيات إعلامية <b>مُعتمدة</b> في الأيام القادمة.\n\n"

        if pending_requests:
            msg += "⚠️ <b>تنبيه بطلبات معلقة تنتظر الموافقة:</b>\n"
            for req in pending_requests:
                d_str = "اليوم" if req['date'] == today_str else format_date_ar(req['date'])
                msg += (
                    f"   • {d_str} — {escape_html(req['time'])} | {escape_html(req['event_name'])}\n"
                    "   ─────────────────\n"
                )
            if total_pending > len(pending_requests):
                msg += f"   <i>...(و {total_pending - len(pending_requests)} طلبات أخرى)</i>\n"
            msg += "\n<i>يرجى مراجعتها من لوحة الإدارة (الطلبات المعلقة).</i>"

        kwargs = {"chat_id": config.ADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
        if config.ADMIN_TOPIC_ID is not None:
            kwargs["message_thread_id"] = config.ADMIN_TOPIC_ID

        # Delete previous reminder if present
        prev_msg_id = context.bot_data.get("last_reminder_msg_id")
        if prev_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=prev_msg_id
                )
            except Exception:
                pass

        sent_msg = await context.bot.send_message(**kwargs)
        context.bot_data["last_reminder_msg_id"] = sent_msg.message_id

        try:
            await context.bot.pin_chat_message(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=sent_msg.message_id
            )
            # Remove auto Telegram "pinned a message" notification
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=sent_msg.message_id + 1
                )
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Could not pin reminder: {e}")

        logger.info("Scheduled reminder sent and pinned.")
    except Exception as e:
        logger.error(f"Error in send_daily_reminder: {e}")


async def send_borrow_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Sends reminder about currently-borrowed items, due/overdue returns, and pending borrow requests."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")

    try:
        all_borrows = await db_app.get_all_borrow_requests()
        active_borrows = [r for r in all_borrows if r['status'] == 'مقبول']
        pending_borrows = [r for r in all_borrows if r['status'] == 'معلق']
    except Exception as e:
        logger.error(f"Error in send_borrow_reminder: {e}")
        return

    overdue = [r for r in active_borrows if r['date'] < today_str]
    today_due = [r for r in active_borrows if r['date'] == today_str]
    upcoming = [r for r in active_borrows if r['date'] > today_str]

    msg = "📦 <b>تذكير بطلبات الاستعارة:</b>\n\n"
    if not active_borrows and not pending_borrows:
        msg += "✅ لا توجد أغراض مستعارة حالياً ولا طلبات استعارة معلقة."
    else:
        if overdue:
            msg += "🚨 <b>أغراض تأخر إرجاعها:</b>\n"
            for r in overdue:
                unit_text = f" ({r['asset_unit_id']})" if 'asset_unit_id' in r.keys() and r['asset_unit_id'] else ""
                msg += (
                    f"   • {escape_html(r['event_name'])}{unit_text} (العدد {r['borrow_qty'] or 1})"
                    f" — كان يجب إرجاعه {format_date_ar(r['date'])}\n"
                )
            msg += "\n"
        if today_due:
            msg += "📌 <b>أغراض يجب إرجاعها اليوم:</b>\n"
            for r in today_due:
                unit_text = f" ({r['asset_unit_id']})" if 'asset_unit_id' in r.keys() and r['asset_unit_id'] else ""
                msg += (
                    f"   • {escape_html(r['event_name'])}{unit_text} (العدد {r['borrow_qty'] or 1})"
                    f" — {escape_html(r['time'] or '')}\n"
                )
            msg += "\n"
        if upcoming:
            msg += "📆 <b>أغراض مستعارة حالياً (إرجاع قادم):</b>\n"
            for r in upcoming[:5]:
                msg += (
                    f"   • {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
                    f" — حتى {format_date_ar(r['date'])}\n"
                )
            if len(upcoming) > 5:
                msg += f"   <i>...(و {len(upcoming) - 5} أخرى)</i>\n"
            msg += "\n"
        if pending_borrows:
            msg += "⚠️ <b>طلبات استعارة معلقة تنتظر الموافقة:</b>\n"
            for r in pending_borrows[:10]:
                msg += (
                    f"   • {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
                    f" — {escape_html(r['contact_name'] or '')} — إرجاع: {format_date_ar(r['date'])}\n"
                )
            if len(pending_borrows) > 10:
                msg += f"   <i>...(و {len(pending_borrows) - 10} طلبات أخرى)</i>\n"
            msg += "\n<i>يرجى مراجعتها من لوحة الاستعارات.</i>"

    kwargs = {"chat_id": config.ADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    topic_id = config.BORROW_TOPIC_ID if config.BORROW_TOPIC_ID else config.ADMIN_TOPIC_ID
    if topic_id is not None:
        kwargs["message_thread_id"] = topic_id

    try:
        await context.bot.send_message(**kwargs)
        logger.info("Borrow reminder sent.")
    except Exception as e:
        logger.error(f"Error sending borrow reminder: {e}")


async def check_pre_event_reminders(context: ContextTypes.DEFAULT_TYPE):
    """فحص كل دقيقة لتذكير المشرفين قبل 30 دقيقة من موعد بدء أي تغطية."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    window_start = (now + timedelta(minutes=25)).strftime("%H:%M")
    window_end = (now + timedelta(minutes=35)).strftime("%H:%M")

    try:
        todays_events = await db_app.get_events_for_pre_reminder(today_str, window_start, window_end)
        for req in todays_events:
            event_time_24 = req["start_time_24"]
            if not event_time_24:
                continue
            try:
                event_time = datetime.strptime(event_time_24, "%H:%M").time()
            except Exception:
                continue

            event_dt = datetime.combine(now.date(), event_time).replace(tzinfo=timezone(timedelta(hours=3)))
            diff = (event_dt - now).total_seconds() / 60

            reminder_key = f"remind_30m_{req['id']}"
            if 25 <= diff <= 35 and reminder_key not in context.bot_data:
                msg = (
                    f"⏰ <b>تذكير ببدء تغطية وشيكة (بعد 30 دقيقة):</b>\n\n"
                    f"📌 <b>الحدث:</b> {escape_html(req['event_name'])}\n"
                    f"🏢 <b>القسم:</b> {escape_html(req['department'])}\n"
                    f"📍 <b>المكان:</b> {escape_html(req['location'])}\n"
                    f"🕒 <b>الوقت:</b> {escape_html(req['time'])} إلى {escape_html(req['end_time'] if req['end_time'] else '')}\n"
                    "🔎 <b>لمزيد من التفاصيل، راجع لوحة التحكم.</b>\n"
                )

                kwargs = {"chat_id": config.ADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
                if config.ADMIN_TOPIC_ID:
                    kwargs["message_thread_id"] = config.ADMIN_TOPIC_ID

                await context.bot.send_message(**kwargs)
                context.bot_data[reminder_key] = True
                logger.info(f"30-min reminder sent for request {req['id']}")
    except Exception as e:
        logger.error(f"Error in check_pre_event_reminders: {e}")


async def cleanup_cache(context: ContextTypes.DEFAULT_TYPE = None):
    """تنظيف آمن للذاكرة المؤقتة ومفاتيح التنبيه القديمة المنتهية."""
    try:
        # تنظيف مفاتيح التنبيه القديمة من bot_data
        if context and context.bot_data:
            keys_to_remove = [k for k in list(context.bot_data.keys()) if k.startswith("remind_30m_")]
            # نحتفظ بآخر 50 مفتاحاً فقط لتفادي تضخم الذاكرة
            if len(keys_to_remove) > 50:
                for k in keys_to_remove[:-50]:
                    context.bot_data.pop(k, None)
        logger.info("🧹 Cache & memory cleanup executed safely.")
    except Exception as e:
        logger.error(f"Safe cleanup error: {e}")


async def test_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختبار يدوي لإرسال تذكير التغطيات القادمة (للمشرفين فقط)."""
    user = update.effective_user
    if not user or not await is_supervisor(user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    status_msg = await update.message.reply_text("⏳ جاري تشغيل تذكير التغطيات وإرساله لمجموعة الإدارة...")
    try:
        await send_daily_reminder(context)
        await status_msg.edit_text("✅ تم تشغيل تذكير التغطيات الإعلامية بنجاح.")
    except Exception as e:
        logger.error(f"Error in test_reminder_command: {e}")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء تشغيل التذكير:\n<code>{escape_html(str(e))}</code>", parse_mode="HTML")


async def test_borrow_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اختبار يدوي لإرسال تذكير الاستعارات (للمشرفين فقط)."""
    user = update.effective_user
    if not user or not await is_supervisor(user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    status_msg = await update.message.reply_text("⏳ جاري تشغيل تذكير الاستعارات وإرساله لمجموعة الإدارة...")
    try:
        await send_borrow_reminder(context)
        await status_msg.edit_text("✅ تم تشغيل تذكير الاستعارات بنجاح.")
    except Exception as e:
        logger.error(f"Error in test_borrow_reminder_command: {e}")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء تشغيل تذكير الاستعارات:\n<code>{escape_html(str(e))}</code>", parse_mode="HTML")


async def check_overdue_borrows(context: ContextTypes.DEFAULT_TYPE):
    """فحص دوري لتنبيه المستعيرين الذين تجاوزوا موعد الإرجاع مع زر طلب تمديد."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    current_time_24 = now.strftime("%H:%M")

    try:
        overdue_items = await db_app.get_overdue_borrows(today_str, current_time_24)
        for row in overdue_items:
            req_id = row['id']
            alert_key = f"overdue_alert_{req_id}_{today_str}"
            if context.bot_data.get(alert_key):
                continue

            user_id = row['user_id']
            item = row['event_name']
            unit_desc = f" ({row['asset_unit_id']})" if 'asset_unit_id' in row.keys() and row['asset_unit_id'] else ""
            ret_str = format_date_ar(row['date']) + (f" الساعة {row['time']}" if row['time'] else "")

            overdue_msg = (
                "🚨 <b>تنبيه: لقد تجاوزت موعد إرجاع العتاد المستعار!</b>\n\n"
                f"• <b>الغرض:</b> <b>{escape_html(item)}</b>{escape_html(unit_desc)}\n"
                f"• <b>رقم الطلب:</b> <code>{req_id}</code>\n"
                f"• <b>موعد الإرجاع المحدد:</b> {ret_str}\n\n"
                "يرجى إعادة الغرض لمكتب الإعلام في أقرب وقت لإتاحته لباقي الزملاء، أو طلب تمديد بالضغط على الزر أدناه:"
            )
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton
            kb_ext = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏱️ طلب تمديد الاستعارة", callback_data=f"user_extend_{req_id}")]
            ])
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=overdue_msg,
                    reply_markup=kb_ext,
                    parse_mode="HTML"
                )
                context.bot_data[alert_key] = True
            except Exception as e:
                logger.debug(f"Could not send overdue notice to user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in check_overdue_borrows: {e}")


