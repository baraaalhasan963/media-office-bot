"""Admin operations, dashboard, borrow board, settings, and diagnostics."""

import os
import time
import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import psutil
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.ext import ContextTypes

import config
from constants import BORROW_ITEMS, BORROW_ITEM_STOCK
from models import database as db_app
from controllers.common import is_supervisor
from controllers.reports import handle_kpi_report
from views import keyboards as kb
from views.formatting import (
    escape_html,
    format_date_ar,
    build_admin_button_label_with_status,
)
from views.messages import (
    _build_dashboard_text,
    _build_request_details_message,
    _build_borrow_request_details,
)

logger = logging.getLogger("bot.admin")

# Track when the bot process was initialized
BOT_START_TIME = datetime.now(timezone(timedelta(hours=3)))


def _format_duration(seconds: float) -> str:
    """Formats seconds into readable Arabic duration string."""
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if mins > 0:
        parts.append(f"{mins} دقيقة")
    parts.append(f"{secs} ثانية")
    return " و ".join(parts)


async def _build_health_report(context: ContextTypes.DEFAULT_TYPE) -> str:
    """بناء نص تقرير الفحص الصحي."""
    t0 = time.perf_counter()
    try:
        await context.bot.get_me()
        ping_ms = round((time.perf_counter() - t0) * 1000)
        ping_str = f"<code>{ping_ms} ms</code>"
    except Exception as ping_err:
        ping_str = f"⚠️ خطأ ({ping_err})"

    now = datetime.now(timezone(timedelta(hours=3)))
    uptime_seconds = (now - BOT_START_TIME).total_seconds()
    uptime_str = _format_duration(uptime_seconds)

    try:
        proc = psutil.Process(os.getpid())
        ram_mb = proc.memory_info().rss / (1024 * 1024)
        ram_str = f"<code>{ram_mb:.1f} MB</code>"
    except Exception:
        ram_str = "غير متوفر"

    try:
        db_size_bytes = os.path.getsize(config.DB_PATH) if os.path.exists(config.DB_PATH) else 0
        db_size_kb = db_size_bytes / 1024
        total, pending, approved, rejected = await db_app.get_dashboard_statistics()
        all_users = await db_app.get_all_users()
        users_count = len(all_users)
        db_info = (
            f"• الحجم على القرص: <code>{db_size_kb:.1f} KB</code>\n"
            f"• إجمالي الطلبات: <b>{total}</b> (⏳ {pending} معلق | ✅ {approved} مقبول | ❌ {rejected} مرفوض)\n"
            f"• عدد المستخدمين المسجلين: <b>{users_count}</b> مستخدم\n"
            f"• وضع التخزين: SQLite WAL (تزامن خفيف & Busy Timeout 15s)"
        )
    except Exception as db_err:
        db_info = f"⚠️ تعذر جلب إحصائيات القاعدة: {db_err}"

    gemini_ok = bool(config.GEMINI_API_KEY and config.GEMINI_API_KEY != "ضـع_مفتـاح_الـAPI_هنا")
    groq_ok = bool(config.GROQ_API_KEY and config.GROQ_API_KEY != "ضـع_مفتـاح_Groq_هنـا")
    gemini_status = "✅ مهيأ ومتصل" if gemini_ok else "⚠️ غير مهيأ"
    groq_status = "✅ مهيأ (احتياطي)" if groq_ok else "⚠️ غير مهيأ"

    return (
        "🩺 <b>تقرير الحالة والفحص الصحي للنظام (Health Check)</b>\n"
        f"<i>📅 الوقت: {now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+3)</i>\n\n"
        f"⏱️ <b>مدة التشغيل (Uptime):</b> {uptime_str}\n"
        f"⚡ <b>زمن استجابة الشبكة (Ping):</b> {ping_str}\n"
        f"💾 <b>استهلاك ذاكرة البوت (RAM):</b> {ram_str}\n\n"
        f"📊 <b>حالة قاعدة البيانات:</b>\n{db_info}\n\n"
        f"🤖 <b>خدمات الذكاء الاصطناعي:</b>\n"
        f"• Gemini API: {gemini_status}\n"
        f"• Groq API: {groq_status}\n\n"
        f"🌐 <b>مجمع اتصالات تيليغرام (HTTPX):</b>\n"
        f"• مجمع اتصالات نشط (Connection Pool: 100)\n"
        f"• مهلة الاتصال: {config.HTTP_TIMEOUT}s | مهلة القراءة: 120s\n"
        f"• حفظ الجلسات: PicklePersistence (مفعل)"
    )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await is_supervisor(user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    status_msg = await update.message.reply_text("⏳ جاري فحص صحة النظام وقياس زمن الاستجابة...")
    msg = await _build_health_report(context)
    await status_msg.edit_text(msg, parse_mode="HTML")


async def health_callback(query, context: ContextTypes.DEFAULT_TYPE):
    msg = await _build_health_report(context)
    await query.edit_message_text(
        msg,
        reply_markup=kb.admin_back_keyboard(),
        parse_mode="HTML"
    )


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await is_supervisor(user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "🔍 <b>البحث السريع عن الطلبات:</b>\n\n"
            "يرجى كتابة رقم الطلب أو كلمة البحث بعد الأمر، أمثلة:\n"
            "▫️ <code>/find 74</code> — للوصول لطلب محدد برقم المعرف\n"
            "▫️ <code>/find معرض</code> — للبحث عن فعالية باسم معين\n"
            "▫️ <code>/find الإعلام</code> — للبحث عن طلبات قسم معين\n"
            "▫️ <code>/find أحمد</code> — للبحث باسم صاحب الطلب\n\n"
            "<i>ملاحظة: يمكنك استخدام <code>/search</code> بنفس الطريقة.</i>",
            parse_mode="HTML"
        )
        return

    query_str = " ".join(args).strip()

    if query_str.isdigit():
        req = await db_app.get_request_by_id(query_str)
        if req:
            dmsg, dkb = await _build_request_details_message(req, "مشرف")
            await update.message.reply_text(
                dmsg,
                reply_markup=InlineKeyboardMarkup(dkb),
                parse_mode="HTML"
            )
            return

    results = await db_app.search_requests(query_str, limit=8)
    if not results:
        await update.message.reply_text(
            f"⚠️ <b>لم يتم العثور على أي نتائج!</b>\nلا توجد أي طلبات تطابق: <code>{escape_html(query_str)}</code>",
            parse_mode="HTML"
        )
        return

    if len(results) == 1:
        req = results[0]
        dmsg, dkb = await _build_request_details_message(req, "مشرف")
        await update.message.reply_text(
            dmsg,
            reply_markup=InlineKeyboardMarkup(dkb),
            parse_mode="HTML"
        )
        return

    keyboard = []
    text_lines = [f"🔍 <b>نتائج البحث عن:</b> <i>{escape_html(query_str)}</i>\n"]
    for r in results:
        status_icon = "⏳"
        if r['status'] == "مقبول":
            status_icon = "✅"
        elif r['status'] == "مرفوض":
            status_icon = "❌"
        elif r['status'] == "مُرجَع":
            status_icon = "↩️"

        type_icon = "📦" if r['request_type'] == "استعارة" else "🎥"
        btn_label = f"{type_icon} #{r['id']} - {r['event_name'][:22]} ({status_icon})"
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"admin_view_{r['id']}")])
        date_str = format_date_ar(r['date']) if r['date'] else "غير محدد"
        text_lines.append(f"• <b>#{r['id']}</b> | {escape_html(r['event_name'])} ({escape_html(r['department'])}) — {date_str}")

    text_lines.append("\n<i>اضغط على أي زر أدناه لعرض تفاصيل الطلب وخيارات الإدارة:</i>")
    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def broadcast_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إرسال إشعار تحديث ودعوة للبدء لجميع مستخدمي البوت."""
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    status_msg = await update.message.reply_text("⏳ جاري البدء بعملية البث...")
    user_ids = await db_app.get_all_users()

    success_count = 0
    fail_count = 0

    for uid in user_ids:
        try:
            is_user_admin = await is_supervisor(uid)
            await context.bot.send_message(
                chat_id=uid,
                text="🔄 <b>تم تحديث النظام</b>\n\nتم تحديث البوت وإعادة تشغيله، يرجى الضغط على القائمة واختيار (start) لإعادة تشغيل البوت والعمل عليه.",
                reply_markup=kb.main_menu_keyboard(is_user_admin),
                parse_mode="HTML"
            )
            success_count += 1
            await asyncio.sleep(0.05)  # Rate limiting
        except Exception:
            fail_count += 1

    await status_msg.edit_text(
        f"✅ <b>اكتمل البث بنجاح!</b>\n\n"
        f"▫️ تم الوصول لـ: {success_count} مستخدم\n"
        f"▫️ فشل الإرسال لـ: {fail_count} مستخدم (ربما قاموا بحظر البوت)",
        parse_mode="HTML"
    )


# ─── Borrow Board / Equipment Management ──────────────────────────────────────
async def free_item(context, item: str):
    stock_map = await db_app.get_borrow_item_stock()
    stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
    approved_qty = await db_app.get_approved_borrow_qty(item)
    available = stock - approved_qty
    pending = await db_app.get_pending_borrows(item)

    notified = []
    for req in pending:
        req_qty = req['borrow_qty'] or 1
        if req_qty <= available:
            available -= req_qty
            notified.append(req)

    if notified:
        for req in notified:
            try:
                await context.bot.send_message(
                    chat_id=req['user_id'],
                    text=f"🎉 الغرض <b>{escape_html(item)}</b> صار متاحاً الآن!\nأحضر لاستلامه من مكتب الإعلام.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error notifying borrower: {e}")

    await _sync_borrow_board(context)


async def _do_borrow_return(context, req_id: str, admin) -> bool:
    try:
        req_data = await db_app.get_request_by_id(req_id)
        if not req_data or req_data['request_type'] != 'استعارة' or req_data['status'] != 'مقبول':
            return False
        item = req_data['event_name']
        user_id = req_data['user_id']

        await db_app.mark_request_returned(req_id)
        await db_app.log_audit_action(
            admin.id,
            admin.username or str(admin.id),
            "إرجاع",
            req_id,
            f"إرجاع غرض: {item}"
        )

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"♻️ تم استلام الغرض <b>{escape_html(item)}</b> وتأكيد إرجاعه. شكراً لك!",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error notifying borrower return: {e}")

        await free_item(context, item)
        return True
    except Exception as e:
        logger.error(f"Error in _do_borrow_return: {e}")
        return False


async def _build_borrow_dashboard():
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H:%M")

    all_borrows = await db_app.get_all_borrow_requests()
    approved = [r for r in all_borrows if r['status'] == 'مقبول']
    pending = [r for r in all_borrows if r['status'] == 'معلق']
    returned = [r for r in all_borrows if r['status'] == 'مُرجَع']
    rejected = [r for r in all_borrows if r['status'] == 'مرفوض']

    def _req_key(r):
        try:
            return int(r['id'])
        except (TypeError, ValueError):
            return 0
    pending.sort(key=_req_key)

    msg = "📦 <b>لوحة الاستعارات</b>\n\n"

    if pending:
        msg += f"📋 <b>طلبات معلقة ({len(pending)}):</b>\n"
        for r in pending:
            msg += (
                f"🕐 <code>{escape_html(r['id'])}</code> — {escape_html(r['event_name'])} ×{r['borrow_qty'] or 1}"
                f" — {escape_html(r['contact_name'] or '')} — حتى {format_date_ar(r['date'])}\n"
            )
        msg += "\n"
    else:
        msg += "📋 لا توجد طلبات معلقة.\n"

    if approved:
        msg += f"🔄 <b>أغراض مستعارة ({len(approved)}):</b>\n"
        for r in approved:
            overdue = " 🚨" if r['date'] < today_str else ""
            msg += (
                f"🔄 {escape_html(r['event_name'])} ×{r['borrow_qty'] or 1}"
                f" — {escape_html(r['contact_name'] or '')} — حتى {format_date_ar(r['date'])}{overdue}\n"
            )
        msg += "\n"
    else:
        msg += "🔄 لا توجد أغراض مستعارة حالياً.\n"

    msg += "<b>📦 المتاح من كل غرض:</b>\n"
    stock_map = await db_app.get_borrow_item_stock()
    for item in await db_app.get_borrow_items():
        stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
        approved_qty = sum(r['borrow_qty'] or 1 for r in approved if r['event_name'] == item)
        free = stock - approved_qty
        msg += f"   • {escape_html(item)}: {free}/{stock}\n"
    msg += "\n"

    msg += (f"<i>إجمالي: {len(approved)} مستعار • {len(pending)} معلق • "
            f"{len(returned)} مُرجَع • {len(rejected)} مرفوض • آخر تحديث {timestamp}</i>")

    keyboard = []
    for r in pending:
        keyboard.append([
            InlineKeyboardButton(f"✅ موافقة {r['id']}", callback_data=f"admin_approve_{r['id']}"),
            InlineKeyboardButton(f"❌ رفض {r['id']}", callback_data=f"admin_reject_{r['id']}")
        ])
    for r in approved:
        label = f"↩️ إرجاع {r['event_name']}"
        if (r['borrow_qty'] or 1) > 1:
            label += f" ×{r['borrow_qty'] or 1}"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"bdash_return_{r['id']}")
        ])
    keyboard.append([
        InlineKeyboardButton("🔄 تحديث", callback_data="bdash_refresh"),
        InlineKeyboardButton("🔴 إغلاق", callback_data="bdash_close"),
    ])
    return msg, keyboard


async def _sync_borrow_board(context, notify: bool = False):
    msg, keyboard = await _build_borrow_dashboard()
    board_id = await db_app.get_setting("borrow_board_msg_id")
    if board_id and not notify:
        try:
            await context.bot.edit_message_text(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=int(board_id),
                text=msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return
        except Exception:
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=int(board_id)
                )
            except Exception:
                pass
    if board_id:
        try:
            await context.bot.delete_message(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=int(board_id)
            )
        except Exception:
            pass
        await db_app.set_setting("borrow_board_msg_id", "")
    kwargs = {
        "chat_id": config.ADMIN_CHAT_ID,
        "text": msg,
        "reply_markup": InlineKeyboardMarkup(keyboard),
        "parse_mode": "HTML"
    }
    borrow_topic = config.BORROW_TOPIC_ID or config.ADMIN_TOPIC_ID
    if borrow_topic is not None:
        kwargs["message_thread_id"] = borrow_topic
    try:
        sent = await context.bot.send_message(**kwargs)
        await db_app.set_setting("borrow_board_msg_id", str(sent.message_id))
    except Exception as e:
        logger.error(f"Error in _sync_borrow_board: {e}")


async def borrow_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    await update.message.reply_text("⏳ جاري فتح لوحة الاستعارات في مجموعة الإدارة...")
    await _sync_borrow_board(context, notify=True)


async def handle_borrow_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    role = await db_app.get_user_role(update.effective_user.id)
    if role != "مشرف":
        await query.answer("عذراً، هذا الأمر للمشرفين فقط.", show_alert=True)
        return

    if data == "bdash_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await db_app.set_setting("borrow_board_msg_id", "")
        return

    if data in ("bdash_open", "bdash_refresh"):
        await _sync_borrow_board(context)
        return

    if data.startswith("bdash_return_"):
        req_id = data.replace("bdash_return_", "")
        ok = await _do_borrow_return(context, req_id, update.effective_user)
        if not ok:
            await query.answer("⚠️ تعذّر تسجيل الإرجاع (الطلب غير صالح).", show_alert=True)
        else:
            await query.answer("✅ تم تسجيل الإرجاع")
        await _sync_borrow_board(context)


# ─── Main Admin Dashboard ─────────────────────────────────────────────────────
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send or refresh the admin dashboard."""
    user_id = update.effective_user.id
    role = await db_app.get_user_role(user_id)
    if role != "مشرف":
        if update.message:
            await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    try:
        total, pending, approved, rejected = await db_app.get_dashboard_statistics()
        stats_text = _build_dashboard_text(total, pending, approved, rejected)

        if update.message:
            dash_msg = await update.message.reply_text(
                stats_text,
                reply_markup=kb.admin_dashboard_keyboard(role),
                parse_mode="HTML"
            )
            context.chat_data["dash_msg_id"] = dash_msg.message_id

    except Exception as e:
        logger.error(f"Error fetching admin dash: {e}")
        if update.message:
            await update.message.reply_text("❌ حدث خطأ في النظام.")


async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة جميع نقرات المشرفين في لوحة التحكم."""
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "admin_close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("تم إغلاق لوحة التحكم.")
        return

    if data == "admin_details_close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("تم إغلاق التفاصيل.")
        context.chat_data.pop("admin_details_msg_id", None)
        return

    if data == "admin_filter_dept_menu":
        depts = await db_app.get_departments()
        await query.edit_message_text(
            "📁 <b>استعرض الطلبات حسب القسم</b>\nاختر القسم المطلوب:",
            reply_markup=kb.admin_dept_filter_keyboard(depts),
            parse_mode="HTML"
        )
        return

    if data == "admin_quick_filters":
        await query.edit_message_text(
            "⚡ <b>الفلاتر السريعة</b>\nاختر المدة المطلوبة:",
            reply_markup=kb.admin_quick_filters_keyboard(),
            parse_mode="HTML"
        )
        return

    if data in ["admin_filter_today", "admin_filter_tomorrow", "admin_filter_week", "admin_filter_month"]:
        now = datetime.now(timezone(timedelta(hours=3)))
        today = now.date()

        if data == "admin_filter_today":
            date_from = today
            date_to = today
            title = "📅 طلبات اليوم"
        elif data == "admin_filter_tomorrow":
            date_from = today + timedelta(days=1)
            date_to = date_from
            title = "📅 طلبات الغد"
        elif data == "admin_filter_week":
            date_from = today
            date_to = today + timedelta(days=6)
            title = "📆 طلبات هذا الأسبوع"
        else:
            date_from = today.replace(day=1)
            next_month = (date_from.replace(day=28) + timedelta(days=4)).replace(day=1)
            date_to = next_month - timedelta(days=1)
            title = "🗓️ طلبات هذا الشهر"

        try:
            rows = await db_app.get_requests_by_date_range(date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d"))

            if not rows:
                await query.edit_message_text(
                    f"✅ لا توجد طلبات ضمن {escape_html(title)}.",
                    reply_markup=kb.admin_back_keyboard(),
                    parse_mode="HTML"
                )
                return

            msg = f"<b>{title}</b>\n<i>عدد النتائج: {len(rows)}</i>\n\n"
            msg += "اختر حدثاً لعرض التفاصيل والإجراءات.\n"
            buttons = []
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

            for req in rows[:20]:
                buttons.append([
                    InlineKeyboardButton(
                        build_admin_button_label_with_status(req, today_str, tomorrow_str),
                        callback_data=f"admin_view_{req['id']}"
                    )
                ])

            if len(rows) > 20:
                msg += f"<i>... تم إخفاء {len(rows) - 20} طلبات إضافية لطول الرسالة.</i>"

            buttons.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")])

            await query.edit_message_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error in quick filters: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء تطبيق الفلتر.")
        return

    if data in ["admin_pending", "admin_upcoming", "admin_past"]:
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            filter_type = data.replace("admin_", "")
            rows = await db_app.get_admin_filtered_requests(filter_type, limit=20, today_str=today_str)

            title_map = {
                "pending": "⏳ الطلبات المعلقة",
                "upcoming": "🔜 الطلبات القادمة",
                "past": "⏪ الطلبات الفائتة",
            }
            title = title_map.get(filter_type, "الطلبات")

            if not rows:
                await query.edit_message_text(
                    f"✅ لا توجد أي {escape_html(title)} حالياً.",
                    reply_markup=kb.admin_back_keyboard(),
                    parse_mode="HTML"
                )
                return

            msg = f"<b>{title} (آخر {len(rows)}):</b>\n\n"
            msg += "اختر حدثاً لعرض التفاصيل والإجراءات.\n"
            buttons = []

            for req in rows:
                buttons.append([
                    InlineKeyboardButton(
                        build_admin_button_label_with_status(req, today_str, tomorrow_str),
                        callback_data=f"admin_view_{req['id']}"
                    )
                ])

            buttons.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")])

            await query.edit_message_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error filtering requests: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء تصفية الطلبات.")
        return

    if data == "admin_month_stats":
        now = datetime.now(timezone(timedelta(hours=3)))
        month_start = now.replace(day=1).strftime("%Y-%m-%d")

        total = await db_app.get_requests_count_since(month_start)
        approved = await db_app.get_requests_count_by_status_since("مقبول", month_start)
        rejected = await db_app.get_requests_count_by_status_since("مرفوض", month_start)
        pending = await db_app.get_requests_count_by_status_since("معلق", month_start)

        ar_months = ['يناير','فبراير','مارس','أبريل','مايو','يونيو',
                     'يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر']
        month_name = ar_months[now.month - 1]

        stats_msg = (
            f"📊 <b>إحصائيات شهر {month_name} {now.year}:</b>\n\n"
            f"• إجمالي طلبات هذا الشهر: <b>{total}</b>\n"
            f"• ✅ مقبولة: <b>{approved}</b>\n"
            f"• ❌ مرفوضة: <b>{rejected}</b>\n"
            f"• ⏳ معلقة: <b>{pending}</b>\n"
        )
        await query.edit_message_text(
            stats_msg, reply_markup=kb.admin_back_keyboard(), parse_mode="HTML"
        )
        return

    if data in ["admin_dash_back", "admin_dash_refresh"]:
        try:
            role = await db_app.get_user_role(query.from_user.id)
            total, pending, approved, rejected = await db_app.get_dashboard_statistics()
            stats_text = _build_dashboard_text(total, pending, approved, rejected)
            await query.edit_message_text(
                stats_text, reply_markup=kb.admin_dashboard_keyboard(role), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"admin_dash_back error: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء تحديث لوحة التحكم.")
        return

    if data.startswith("admin_view_"):
        req_id = data.replace("admin_view_", "")
        try:
            req = await db_app.get_request_by_id(req_id)
            if not req:
                await query.edit_message_text("❌ لم يتم العثور على الطلب المحدد.")
                return

            role = await db_app.get_user_role(query.from_user.id)
            msg, keyboard = await _build_request_details_message(req, role)
            keyboard.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")])

            details_msg_id = context.chat_data.get("admin_details_msg_id")
            if details_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=details_msg_id,
                        text=msg,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="HTML"
                    )
                    return
                except Exception:
                    context.chat_data.pop("admin_details_msg_id", None)

            kwargs = {
                "chat_id": query.message.chat_id,
                "text": msg,
                "reply_markup": InlineKeyboardMarkup(keyboard),
                "parse_mode": "HTML"
            }
            if query.message.message_thread_id:
                kwargs["message_thread_id"] = query.message.message_thread_id
            details_msg = await context.bot.send_message(**kwargs)
            context.chat_data["admin_details_msg_id"] = details_msg.message_id
        except Exception as e:
            logger.error(f"Error showing request details: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء عرض تفاصيل الطلب.")
        return

    if data.startswith("admin_vd_"):
        dept_idx = int(data.replace("admin_vd_", ""))
        depts = await db_app.get_departments()
        if dept_idx >= len(depts):
            await query.edit_message_text("❌ قسم غير صالح.")
            return
        dept_name = depts[dept_idx]
        try:
            dept_requests = await db_app.get_requests_by_department(dept_name, 15)
            back_row = [[InlineKeyboardButton("🔙 رجوع للأقسام", callback_data="admin_filter_dept_menu")]]

            if not dept_requests:
                await query.edit_message_text(
                    f"📁 لا توجد أي طلبات في قسم <b>{escape_html(dept_name)}</b>.",
                    reply_markup=InlineKeyboardMarkup(back_row),
                    parse_mode="HTML"
                )
                return

            msg = f"<b>📁 طلبات قسم {escape_html(dept_name)} (آخر 15):</b>\n\n"
            msg += "اختر حدثاً لعرض التفاصيل والإجراءات.\n"
            buttons = []
            now = datetime.now(timezone(timedelta(hours=3)))
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

            for req in dept_requests:
                buttons.append([
                    InlineKeyboardButton(
                        build_admin_button_label_with_status(req, today_str, tomorrow_str),
                        callback_data=f"admin_view_{req['id']}"
                    )
                ])

            buttons.append(back_row[0])
            await query.edit_message_text(
                msg, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error fetching dept requests: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء جلب طلبات القسم.")
        return

    if data.startswith("admin_delete_"):
        req_id = data.replace("admin_delete_", "")
        try:
            req_data = await db_app.get_request_by_id(req_id)
            await db_app.delete_request(req_id)
            event_name = req_data['event_name'] if req_data else "غير معروف"
            req_type = req_data['request_type'] if req_data and req_data['request_type'] else 'تغطية'
            type_desc = "طلب استعارة غرض" if req_type == 'استعارة' else "طلب تغطية حدث"
            await db_app.log_audit_action(
                query.from_user.id,
                query.from_user.username or str(query.from_user.id),
                "حذف",
                req_id,
                f"حذف {type_desc}: {event_name}"
            )

            if req_type == 'استعارة':
                await query.answer(f"✅ تم حذف الطلب <code>{req_id}</code> نهائياً.")
                if (query.message.text or "").startswith("📦 <b>تفاصيل طلب استعارة"):
                    try:
                        await query.edit_message_text(
                            f"🗑️ <b>تم حذف طلب الاستعارة <code>{req_id}</code> نهائياً.</b>",
                            reply_markup=InlineKeyboardMarkup([]),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                await _sync_borrow_board(context)
            else:
                await query.edit_message_text(f"✅ تم حذف الطلب <code>{req_id}</code> نهائياً.", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error deleting: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء الحذف.")
        return

    if data.startswith("admin_return_"):
        req_id = data.replace("admin_return_", "")
        try:
            req_data = await db_app.get_request_by_id(req_id)
            if not req_data:
                await query.answer("❌ لم يتم العثور على طلب الاستعارة.", show_alert=True)
                return
            if req_data['request_type'] != 'استعارة':
                await query.answer("❌ هذا الطلب ليس طلب استعارة.", show_alert=True)
                return
            if req_data['status'] != 'مقبول':
                await query.answer("⚠️ هذا الغرض غير مستعار حالياً.", show_alert=True)
                return

            item = req_data['event_name']
            ok = await _do_borrow_return(context, req_id, update.effective_user)
            await query.answer("✅ تم تسجيل الإرجاع" if ok else "⚠️ تعذّر تسجيل الإرجاع")
            if not ok:
                return
            try:
                await query.edit_message_text(
                    f"✅ تم تسجيل إرجاع الغرض <b>{escape_html(item)}</b> (طلب <code>{req_id}</code>).",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error in admin return: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء معالجة الإرجاع.")
        return

    if data.startswith("admin_reject_"):
        req_id = data.replace("admin_reject_", "")
        try:
            req_data = await db_app.get_request_by_id(req_id)
            if not req_data:
                await query.answer("❌ لم يتم العثور على الطلب.", show_alert=True)
                await query.message.delete()
                return

            if req_data['status'] != 'معلق':
                admin_name = req_data['admin_username'] or "مشرف آخر"
                status_str = "مقبول" if req_data['status'] == 'مقبول' else "مرفوض"
                await query.answer(f"⚠️ تمت معالجة هذا الطلب سابقاً ({status_str}) بواسطة @{admin_name}.", show_alert=True)
                if (req_data['request_type'] or 'تغطية') == 'استعارة':
                    await _sync_borrow_board(context, notify=True)
                else:
                    try:
                        icon = "✅" if req_data['status'] == 'مقبول' else "❌"
                        new_text = query.message.text
                        if "تم القبول بواسطة" not in new_text and "تم الرفض بواسطة" not in new_text:
                            new_text += f"\n\n{icon} الطلب <b>{status_str}</b> بالفعل بواسطة: @{admin_name}"
                        await query.edit_message_text(new_text, parse_mode="HTML")
                    except Exception:
                        pass
                return
        except Exception as e:
            logger.error(f"Error checking status before reject: {e}")

        if req_data and (req_data['request_type'] or 'تغطية') == 'استعارة' and \
                (query.message.text or "").startswith("📦 <b>تفاصيل طلب استعارة"):
            await query.edit_message_text(
                f"📦 لرفض طلب استعارة <code>{req_id}</code>، يرجى الرد على هذه الرسالة واكتب سبب الرفض:\n\n"
                "(ستصل هذه الملاحظات كرسالة للمستخدم)",
                parse_mode="HTML"
            )
            await query.answer()
            return

        kwargs = {
            "chat_id": query.message.chat_id,
            "text": f"❌ لرفض الطلب <code>{req_id}</code>، يرجى الرد على هذه الرسالة واكتب سبب الرفض:\n\n"
                    "(ستصل هذه الملاحظات كرسالة للمستخدم)",
            "reply_markup": ForceReply(selective=True),
            "parse_mode": "HTML"
        }
        if query.message.message_thread_id:
            kwargs["message_thread_id"] = query.message.message_thread_id

        await context.bot.send_message(**kwargs)
        await query.answer()
        return

    if data.startswith("admin_approve_"):
        req_id = data.replace("admin_approve_", "")
        status = "مقبول"
        admin = update.effective_user
        requester_id = None
        event_name = ""

        try:
            row = await db_app.get_request_by_id(req_id)
            if not row:
                await query.answer("❌ لم يتم العثور على الطلب.", show_alert=True)
                await query.message.delete()
                return

            if row['status'] != 'معلق':
                admin_name = row['admin_username'] or "مشرف آخر"
                status_str = {"مقبول": "مقبول", "مرفوض": "مرفوض", "مُرجَع": "مُرجَع"}.get(row['status'], "محلول")
                await query.answer(f"⚠️ تمت معالجة هذا الطلب سابقاً ({status_str}) بواسطة @{admin_name}.", show_alert=True)
                if (row['request_type'] or 'تغطية') == 'استعارة':
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    await _sync_borrow_board(context, notify=True)
                else:
                    try:
                        icon = {"مقبول": "✅", "مرفوض": "❌", "مُرجَع": "↩️"}.get(row['status'], "✅")
                        new_text = query.message.text
                        if "تم القبول بواسطة" not in new_text and "تم الرفض بواسطة" not in new_text:
                            new_text += f"\n\n{icon} الطلب <b>{status_str}</b> بالفعل بواسطة: @{admin_name}"
                        await query.edit_message_text(new_text, parse_mode="HTML")
                    except Exception:
                        pass
                return

            requester_id = row['user_id']
            event_name = row['event_name']
            request_type = row['request_type'] or 'تغطية'

            await db_app.update_request_status(req_id, status, admin_id=admin.id, admin_username=admin.username)
            action_desc = f"قبول طلب استعارة غرض: {event_name}" if request_type == 'استعارة' else f"قبول طلب تغطية حدث: {event_name}"
            await db_app.log_audit_action(
                admin.id,
                admin.username or str(admin.id),
                "قبول",
                req_id,
                action_desc
            )

            if request_type == 'استعارة':
                try:
                    await query.message.delete()
                except Exception as e:
                    logger.error(f"Error deleting borrow details after approve: {e}")
                await _sync_borrow_board(context, notify=True)
            else:
                icon = "✅"
                original_text = query.message.text or ""
                new_text = original_text + f"\n\n{icon} تم <b>القبول</b> بواسطة: @{admin.username or admin.first_name}"
                await query.edit_message_text(new_text, parse_mode="HTML")

            if requester_id:
                if request_type == 'استعارة':
                    user_msg = (
                        f"✅ <b>تحديث بخصوص طلبك رقم <code>{req_id}</code></b>\n\n"
                        f"تم <b>قبول</b> طلب استعارة الغرض (<b>{escape_html(event_name)}</b>).\n\n"
                        "يمكنك مراجعة مكتب الإعلام لاستلامه ومراعاة تاريخ الإرجاع."
                    )
                else:
                    user_msg = (
                        f"✅ <b>تحديث بخصوص طلبك رقم <code>{req_id}</code></b>\n\n"
                        f"تم <b>قبول</b> طلب التغطية لحدث (<b>{escape_html(event_name)}</b>).\n\n"
                        "شكراً لتعاونكم."
                    )
                await context.bot.send_message(chat_id=requester_id, text=user_msg, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Error in admin action: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء معالجة الطلب.")

    if data == "admin_audit_logs":
        user_id = query.from_user.id
        role = await db_app.get_user_role(user_id)
        if role != "مشرف":
            await query.answer("عذراً، تحتاج لصلاحيات مشرف للاطلاع على سجل العمليات.", show_alert=True)
            return
        logs = await db_app.get_audit_logs(limit=15)
        if not logs:
            msg = "📜 <b>سجل العمليات (Audit Logs):</b>\nلا توجد عمليات مسجلة حتى الآن."
        else:
            msg = "📜 <b>سجل العمليات الأخير (آخر 15 عملية):</b>\n\n"
            for log in logs:
                msg += (
                    f"• <b>المشرف:</b> @{escape_html(log['admin_username'])}\n"
                    f"  <b>العملية:</b> {escape_html(log['action'])}\n"
                    f"  <b>التفاصيل:</b> {escape_html(log['details'])}\n"
                    f"  📅 {escape_html(log['timestamp'])}\n"
                    f"─────────────────\n"
                )
        await query.edit_message_text(
            msg,
            reply_markup=kb.admin_back_keyboard(),
            parse_mode="HTML"
        )
        return

    if data == "admin_health_check":
        user_id = query.from_user.id
        role = await db_app.get_user_role(user_id)
        if role != "مشرف":
            await query.answer("عذراً، تحتاج لصلاحيات مشرف للاطلاع على فحص النظام.", show_alert=True)
            return
        await query.answer("⏳ جاري فحص النظام...")
        await health_callback(query, context)
        return

    if data == "admin_kpi_report":
        await handle_kpi_report(update, context)
        return

    if data == "admin_settings_menu":
        user_id = query.from_user.id
        role = await db_app.get_user_role(user_id)
        if role != "مشرف":
            await query.answer("عذراً، تحتاج لصلاحيات مشرف للوصول للإعدادات.", show_alert=True)
            return
        await query.edit_message_text(
            "⚙️ <b>لوحة إدارة النظام:</b>\n"
            "اختر ما تريد تعديله من القوائم أدناه (إضافة/حذف عناصر ديناميكياً):",
            reply_markup=kb.admin_settings_main_keyboard(),
            parse_mode="HTML"
        )
        return


async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ردود المشرفين (مثل إدخال سبب الرفض أو إضافة عناصر الإعدادات)."""
    if not update.message or not update.message.reply_to_message:
        return
    admin_id = update.effective_user.id
    role = await db_app.get_user_role(admin_id)
    if role != "مشرف":
        return

    replied_msg = update.message.reply_to_message
    orig_text = replied_msg.text or ""

    # رفض طلب استعارة
    if "📦 لرفض طلب استعارة" in orig_text:
        match = re.search(r"📦 لرفض طلب استعارة <code>([\w\-]+)</code>", orig_text)
        if not match:
            match = re.search(r"📦 لرفض طلب استعارة ([\w\-]+)", orig_text)
        if not match:
            await update.message.reply_text("❌ تعذّر استخراج معرف الطلب للرفض.")
            return

        req_id = match.group(1)
        reason = (update.message.text or "").strip()
        if not reason:
            await update.message.reply_text("❌ يرجى كتابة سبب الرفض.")
            return

        req_data = await db_app.get_request_by_id(req_id)
        if not req_data:
            await update.message.reply_text("❌ لم يتم العثور على الطلب المحدد.")
            return

        if req_data['status'] != 'معلق':
            admin_name = req_data['admin_username'] or "مشرف آخر"
            status_str = {"مقبول": "مقبول", "مرفوض": "مرفوض", "مُرجَع": "مُرجَع"}.get(req_data['status'], "محلول")
            await update.message.reply_text(
                f"⚠️ هذا الطلب تمت معالجته مسبقاً ({status_str}) بواسطة @{admin_name}."
            )
            return

        await db_app.update_request_status(req_id, "مرفوض", admin_id=admin_id, admin_username=update.effective_user.username or str(admin_id))
        await db_app.log_audit_action(
            admin_id,
            update.effective_user.username or str(admin_id),
            "رفض",
            req_id,
            f"رفض الطلب بسبب: {reason}"
        )

        try:
            await context.bot.send_message(
                chat_id=req_data['user_id'],
                text=f"❌ <b>تم رفض طلبك رقم <code>{req_id}</code></b>\n\nسبب الرفض:\n{escape_html(reason)}",
                parse_mode="HTML"
            )
        except Exception:
            pass

        try:
            await update.message.reply_to_message.delete()
        except Exception as e:
            logger.error(f"Error deleting borrow details after reject: {e}")

        await update.message.reply_text(
            f"✅ تم رفض الطلب <code>{req_id}</code> وإرسال السبب للمستخدم.",
            parse_mode="HTML"
        )
        await _sync_borrow_board(context, notify=True)
        return

    # رفض طلب تغطية
    if "لرفض الطلب" in orig_text:
        match = re.search(r"لرفض الطلب <code>([\w\-]+)</code>", orig_text)
        if not match:
            match = re.search(r"لرفض الطلب ([\w\-]+)", orig_text)
        if not match:
            await update.message.reply_text("❌ تعذّر استخراج معرف الطلب للرفض.")
            return

        req_id = match.group(1)
        reason = (update.message.text or "").strip()
        if not reason:
            await update.message.reply_text("❌ يرجى كتابة سبب الرفض.")
            return

        req_data = await db_app.get_request_by_id(req_id)
        if not req_data:
            await update.message.reply_text("❌ لم يتم العثور على الطلب المحدد.")
            return

        if req_data['status'] != 'معلق':
            admin_name = req_data['admin_username'] or "مشرف آخر"
            status_str = {"مقبول": "مقبول", "مرفوض": "مرفوض", "مُرجَع": "مُرجَع"}.get(req_data['status'], "محلول")
            await update.message.reply_text(
                f"⚠️ هذا الطلب تمت معالجته مسبقاً ({status_str}) بواسطة @{admin_name}."
            )
            return

        await db_app.update_request_status(req_id, "مرفوض", admin_id=admin_id, admin_username=update.effective_user.username or str(admin_id))
        await db_app.log_audit_action(
            admin_id,
            update.effective_user.username or str(admin_id),
            "رفض",
            req_id,
            f"رفض الطلب بسبب: {reason}"
        )

        try:
            user_msg = (
                f"❌ <b>تم رفض طلبك رقم <code>{req_id}</code></b>\n\n"
                f"سبب الرفض:\n{escape_html(reason)}"
            )
            await context.bot.send_message(
                chat_id=req_data['user_id'],
                text=user_msg,
                parse_mode="HTML"
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ تم رفض الطلب <code>{req_id}</code> وإرسال السبب للمستخدم.",
            parse_mode="HTML"
        )
        if (req_data['request_type'] or 'تغطية') == 'استعارة':
            await _sync_borrow_board(context)
        return

    # إضافة إعدادات ديناميكياً
    if "➕ إضافة" in orig_text:
        prefix = None
        type_ar = ""
        if "القسم الجديد" in orig_text:
            prefix = "dept"
            type_ar = "قسم"
        elif "نوع الحدث الجديد" in orig_text:
            prefix = "type"
            type_ar = "نوع حدث"
        elif "خيار التغطية الجديد" in orig_text:
            prefix = "coverage"
            type_ar = "خيار تغطية"

        if not prefix:
            return

        new_val = update.message.text.strip()
        if not new_val:
            await update.message.reply_text("❌ لا يمكن إضافة قيمة فارغة.")
            return

        if prefix == "dept":
            items = await db_app.get_departments()
            if new_val in items:
                await update.message.reply_text("❌ هذا القسم موجود بالفعل.")
                return
            items.append(new_val)
            await db_app.save_departments(items)
        elif prefix == "type":
            items = await db_app.get_event_types()
            if new_val in items:
                await update.message.reply_text("❌ هذا النوع موجود بالفعل.")
                return
            items.append(new_val)
            await db_app.save_event_types(items)
        elif prefix == "coverage":
            items = await db_app.get_coverage_options()
            if new_val in items:
                await update.message.reply_text("❌ هذا الخيار موجود بالفعل.")
                return
            items.append(new_val)
            await db_app.save_coverage_options(items)

        await db_app.log_audit_action(admin_id, update.effective_user.username or str(admin_id), "إضافة إعدادات", "", f"إضافة {type_ar}: {new_val}")
        await update.message.reply_text(f"✅ تم إضافة {type_ar} <b>{escape_html(new_val)}</b> بنجاح!", parse_mode="HTML")
        return


async def handle_settings_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إدارة الإعدادات (الأقسام، الأنواع، خيارات التغطية)."""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    role = await db_app.get_user_role(user_id)
    if role != "مشرف":
        await query.answer("عذراً، هذا الأمر مخصص للمشرفين فقط.", show_alert=True)
        return

    if data == "settings_depts":
        depts = await db_app.get_departments()
        await query.edit_message_text(
            "🏢 <b>إدارة الأقسام:</b>\n"
            "تظهر أدناه الأقسام الحالية. يمكنك حذف أي قسم أو إضافة قسم جديد.",
            reply_markup=kb.settings_items_keyboard(depts, "dept"),
            parse_mode="HTML"
        )
        await query.answer()
        return

    if data == "settings_types":
        types = await db_app.get_event_types()
        await query.edit_message_text(
            "📌 <b>إدارة أنواع الأحداث:</b>\n"
            "تظهر أدناه أنواع الأحداث الحالية. يمكنك حذف أي نوع أو إضافة نوع جديد.",
            reply_markup=kb.settings_items_keyboard(types, "type"),
            parse_mode="HTML"
        )
        await query.answer()
        return

    if data == "settings_coverage":
        opts = await db_app.get_coverage_options()
        await query.edit_message_text(
            "🎥 <b>إدارة خيارات التغطية:</b>\n"
            "تظهر أدناه خيارات التغطية الحالية. يمكنك حذف أي خيار أو إضافة خيار جديد.",
            reply_markup=kb.settings_items_keyboard(opts, "coverage"),
            parse_mode="HTML"
        )
        await query.answer()
        return

    if data.startswith("settings_del_"):
        parts = data.replace("settings_del_", "").split("_")
        prefix = parts[0]
        idx = int(parts[1])
        if prefix == "dept":
            items = await db_app.get_departments()
            removed = items.pop(idx)
            await db_app.save_departments(items)
            await db_app.log_audit_action(user_id, query.from_user.username or str(user_id), "تعديل إعدادات", "", f"حذف قسم: {removed}")
            await query.answer(f"✅ تم حذف القسم: {removed}")
            await query.edit_message_text(
                "🏢 <b>إدارة الأقسام:</b>\nتم تحديث القائمة بنجاح.",
                reply_markup=kb.settings_items_keyboard(items, "dept"),
                parse_mode="HTML"
            )
        elif prefix == "type":
            items = await db_app.get_event_types()
            removed = items.pop(idx)
            await db_app.save_event_types(items)
            await db_app.log_audit_action(user_id, query.from_user.username or str(user_id), "تعديل إعدادات", "", f"حذف نوع حدث: {removed}")
            await query.answer(f"✅ تم حذف نوع الحدث: {removed}")
            await query.edit_message_text(
                "📌 <b>إدارة أنواع الأحداث:</b>\nتم تحديث القائمة بنجاح.",
                reply_markup=kb.settings_items_keyboard(items, "type"),
                parse_mode="HTML"
            )
        elif prefix == "coverage":
            items = await db_app.get_coverage_options()
            removed = items.pop(idx)
            await db_app.save_coverage_options(items)
            await db_app.log_audit_action(user_id, query.from_user.username or str(user_id), "تعديل إعدادات", "", f"حذف خيار تغطية: {removed}")
            await query.answer(f"✅ تم حذف خيار التغطية: {removed}")
            await query.edit_message_text(
                "🎥 <b>إدارة خيارات التغطية:</b>\nتم تحديث القائمة بنجاح.",
                reply_markup=kb.settings_items_keyboard(items, "coverage"),
                parse_mode="HTML"
            )
        return

    if data.startswith("settings_add_"):
        prefix = data.replace("settings_add_", "")
        type_ar = ""
        if prefix == "dept": type_ar = "القسم الجديد"
        elif prefix == "type": type_ar = "نوع الحدث الجديد"
        elif prefix == "coverage": type_ar = "خيار التغطية الجديد"

        kwargs = {
            "chat_id": query.message.chat_id,
            "text": f"➕ <b>إضافة {type_ar}:</b>\nيرجى الرد على هذه الرسالة بكتابة الاسم الجديد المراد إضافته:",
            "reply_markup": ForceReply(selective=True),
            "parse_mode": "HTML"
        }
        if query.message.message_thread_id:
            kwargs["message_thread_id"] = query.message.message_thread_id

        await context.bot.send_message(**kwargs)
        await query.answer()
        return
