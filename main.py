import logging
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
import asyncio
import os
import io
import shutil
import json
import re
import random
import aiosqlite
import google.generativeai as genai
from datetime import datetime, timezone, timedelta, time as dt_time
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Groq fallback import
try:
    from groq import Groq
    HAS_GROQ = True
except ImportError:
    HAS_GROQ = False

import config
from constants import State, CaptionState, IMPORTANCE_LEVELS, BORROW_ITEMS, BORROW_ITEM_STOCK
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from models import database as db_app
from views import keyboards as kb
from views.formatting import (
    TOTAL_STEPS,
    _progress_bar,
    _truncate_text,
    admin_short_date,
    admin_status_symbol,
    build_admin_button_label,
    build_admin_button_label_with_status,
    build_time_conflicts,
    escape_html,
    format_coverage_type,
    format_date_ar,
    get_request_state_label,
    parse_ar_time,
    step_header,
)
from views.messages import (
    _build_borrow_request_details,
    _build_dashboard_text,
    _build_request_details_message,
    _get_sender_line,
)

if config.GEMINI_API_KEY and config.GEMINI_API_KEY != "ضـع_مفتـاح_الـAPI_هنا":
    genai.configure(api_key=config.GEMINI_API_KEY)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Helpers ───────────────────────────────────────────────────────────────────
async def send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    coverage = data.get("coverage_type") or []
    coverage_text = ", ".join(coverage) if coverage else "غير محدد"
    end_time = data.get("end_time") or "غير محدد"
    external_media = data.get("external_media") or "لا"
    notes = data.get("notes") or "لا يوجد"

    summary = (
        f"{step_header(13)}"
        "<b>📋 ملخص طلب التغطية الإعلامية</b>\n\n"
        f"• <b>القسم:</b> {escape_html(data.get('department', ''))}\n"
        f"• <b>الحدث:</b> {escape_html(data.get('event_name', ''))} ({escape_html(data.get('event_type', ''))})\n"
        f"• <b>الهدف:</b> {escape_html(data.get('objective', ''))}\n"
        f"• <b>التاريخ:</b> {format_date_ar(data.get('date', ''))}\n"
        f"• <b>الوقت:</b> من {escape_html(data.get('time', ''))} إلى {escape_html(end_time)}\n"
        f"• <b>المكان:</b> {escape_html(data.get('location', ''))}\n"
        f"• <b>التغطية:</b> {escape_html(coverage_text)}\n"
        f"• <b>الأهمية:</b> {escape_html(data.get('importance', ''))}\n"
        f"• <b>جهات خارجية:</b> {escape_html(external_media)}\n"
        f"• <b>ملاحظات:</b> {escape_html(notes)}\n\n"
        "هل ترغب في التأكيد والإرسال؟"
    )

    if update.message:
        await update.message.reply_text(summary, reply_markup=kb.confirmation_edit_keyboard(), parse_mode="HTML")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary,
            reply_markup=kb.confirmation_edit_keyboard(),
            parse_mode="HTML"
        )
    context.user_data["return_to_summary"] = False

async def maybe_return_to_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("return_to_summary"):
        return None
    await send_summary(update, context)
    return State.CONFIRMATION

async def handle_edit_back_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("return_to_summary"):
        return None
    if not update.message or not update.message.text:
        return None
    text = update.message.text
    if "إلغاء" in text or "رجوع" in text:
        await send_summary(update, context)
        return State.CONFIRMATION
    return None


# ─── User requests list (pagination + filters) ───────────────────────────────
USER_REQ_PER_PAGE = 5
USER_REQ_FILTERS = {
    "all": None,
    "pending": "معلق",
    "approved": "مقبول",
    "rejected": "مرفوض",
    "returned": "مُرجَع",
}
USER_REQ_FILTER_LABELS = {
    "all": "الكل",
    "pending": "المعلقة",
    "approved": "المقبولة",
    "rejected": "المرفوضة",
    "returned": "المُرجَعة",
}

async def build_user_requests_page(user_id: int, filter_key: str, page: int):
    status = USER_REQ_FILTERS.get(filter_key)
    total = await db_app.get_user_requests_count(user_id, status)
    total_pages = max(1, (total + USER_REQ_PER_PAGE - 1) // USER_REQ_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    offset = page * USER_REQ_PER_PAGE

    rows = []
    if total > 0:
        rows = await db_app.get_user_requests_page(user_id, status, USER_REQ_PER_PAGE, offset)

    label = USER_REQ_FILTER_LABELS.get(filter_key, "الكل")
    if not rows:
        msg = f"📋 <b>طلباتي السابقة</b> — <b>{label}</b>\n\nلا توجد طلبات لعرضها."
    else:
        msg = (
            f"📋 <b>طلباتي السابقة</b> — <b>{label}</b>\n"
            f"<i>عرض {offset + 1} إلى {min(offset + USER_REQ_PER_PAGE, total)} من أصل {total}</i>\n"
            "اختر طلباً لعرض التفاصيل والإجراءات.\n"
        )

    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    filter_row = []
    for key in ["all", "pending", "approved", "rejected", "returned"]:
        label_text = USER_REQ_FILTER_LABELS[key]
        if key == filter_key:
            label_text = f"✅ {label_text}"
        filter_row.append(InlineKeyboardButton(label_text, callback_data=f"user_list_{key}_0"))

    keyboard = [filter_row]

    for req in rows:
        keyboard.append([
            InlineKeyboardButton(
                build_admin_button_label_with_status(req, today_str, tomorrow_str),
                callback_data=f"user_view_{req['id']}_{filter_key}_{page}"
            )
        ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=f"user_list_{filter_key}_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=f"user_list_{filter_key}_{page + 1}"))
        keyboard.append(nav_row)

    return msg, InlineKeyboardMarkup(keyboard)

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await db_app.register_user(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name
    )

async def is_supervisor(user_id: int) -> bool:
    role = await db_app.get_user_role(user_id)
    return role == "مشرف"

async def get_ai_response(prompt: str) -> str:
    """محاولة توليد محتوى باستخدام Gemini أولاً، ثم Groq كبديل"""
    # 1. محاولة Gemini
    if config.GEMINI_API_KEY and config.GEMINI_API_KEY != "ضـع_مفتـاح_الـAPI_هنا":
        try:
            # محاولة عدة موديلات مجانية في حال كان أحدهم مقيداً في منطقتك أو حصته منتهية
            models_to_try = ['gemini-flash-latest', 'gemini-1.5-flash-8b', 'gemini-1.5-pro']
            for model_name in models_to_try:
                try:
                    # تعطيل فلاتر الأمان لضمان عدم حظر الردود البريئة
                    safety_settings = [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                    ]
                    model = genai.GenerativeModel(model_name, safety_settings=safety_settings)
                    response = await asyncio.to_thread(model.generate_content, prompt)
                    if response and response.text:
                        return response.text.strip()
                except Exception as inner_e:
                    logger.warning(f"Model {model_name} failed: {inner_e}")
                    continue
        except Exception as e:
            logger.error(f"All Gemini models failed: {e}")

    # 2. محاولة Groq كبديل (Fallback)
    if HAS_GROQ and config.GROQ_API_KEY and config.GROQ_API_KEY != "ضـع_مفتـاح_Groq_هنـا":
        try:
            client = Groq(api_key=config.GROQ_API_KEY)
            def call_groq():
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                )
                return completion.choices[0].message.content
            response_text = await asyncio.to_thread(call_groq)
            if response_text:
                return response_text.strip()
        except Exception as e:
            logger.error(f"Groq error: {e}")
    elif not HAS_GROQ and config.GROQ_API_KEY != "ضـع_مفتـاح_Groq_هنـا":
        logger.warning("Groq library is not installed. Fallback skipped.")

    raise Exception("عذراً، جميع خدمات الذكاء الاصطناعي (Gemini & Groq) غير متاحة حالياً بسبب ضغط الطلبات أو انتهاء الحصة.")

# ─── Daily reminder ────────────────────────────────────────────────────────────
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Sends reminder for all upcoming accepted coverages and alerts about pending requests."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            
            # Fetch APPROVED requests for today and upcoming
            async with db.execute(
                "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date >= ? ORDER BY date ASC, time ASC",
                (today_str,)
            ) as cursor:
                upcoming_approved = await cursor.fetchall()

            # Fetch PENDING requests for today and upcoming to alert admins
            async with db.execute(
                "SELECT * FROM requests WHERE status = 'معلق' AND date >= ? ORDER BY date ASC, time ASC LIMIT 10",
                (today_str,)
            ) as cursor:
                pending_requests = await cursor.fetchall()
                
            async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'معلق'") as c:
                total_pending = (await c.fetchone())[0]
            
            logger.info(f"Reminder Debug: Found {len(upcoming_approved)} approved and {total_pending} pending in DB.")

        # تصفية الأحداث التي انتهى وقتها اليوم
        now_time = now.time()
        final_approved = []
        for req in upcoming_approved:
            if req['date'] == today_str:
                req_time = parse_ar_time(req['time'])
                if req_time and req_time < now_time:
                    continue
            final_approved.append(req)
        
        logger.info(f"Reminder Debug: After filtering, {len(final_approved)} events remain.")

        # Build Accepted list msg
        # ─── Conflict detection: أحداث بنفس التاريخ والوقت ───────────────
        conflict_keys = set()
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

            # إضافة ملخص التعارضات إن وُجدت
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

        # Build Pending alert msg
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

        # ─── حذف التذكير السابق إن وُجد ────────────────────────────────────
        prev_msg_id = context.bot_data.get("last_reminder_msg_id")
        if prev_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=prev_msg_id
                )
            except Exception:
                pass  # ربما انحذفت يدوياً أو انتهت صلاحيتها

        # ─── إرسال التذكير الجديد ────────────────────────────────────────────
        sent_msg = await context.bot.send_message(**kwargs)
        context.bot_data["last_reminder_msg_id"] = sent_msg.message_id

        # ─── تثبيت الرسالة الجديدة وإخفاء إشعار التثبيت ──────────────────
        try:
            await context.bot.pin_chat_message(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=sent_msg.message_id
                # بدون disable_notification → يُرسل الإشعار الصوتي
            )
            # حذف رسالة الخدمة "قام بتثبيت رسالة" التي يرسلها تليغرام تلقائياً
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=sent_msg.message_id + 1
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Could not pin reminder: {e}")

        logger.info("Scheduled reminder sent and pinned.")
        
    except Exception as e:
        logger.error(f"Error in send_daily_reminder: {e}")

async def send_borrow_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Sends reminder about currently-borrowed items, due/overdue returns, and pending borrow requests."""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")

    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM requests WHERE request_type = 'استعارة' AND status = 'مقبول' "
                "ORDER BY date ASC, time ASC"
            ) as cursor:
                active_borrows = await cursor.fetchall()
            async with db.execute(
                "SELECT * FROM requests WHERE request_type = 'استعارة' AND status = 'معلق' "
                "ORDER BY date ASC, time ASC LIMIT 10"
            ) as cursor:
                pending_borrows = await cursor.fetchall()
            async with db.execute(
                "SELECT COUNT(*) FROM requests WHERE request_type = 'استعارة' AND status = 'معلق'"
            ) as c:
                total_pending = (await c.fetchone())[0]
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
                msg += (
                    f"   • {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
                    f" — كان يجب إرجاعه {format_date_ar(r['date'])}\n"
                )
            msg += "\n"
        if today_due:
            msg += "📌 <b>أغراض يجب إرجاعها اليوم:</b>\n"
            for r in today_due:
                msg += (
                    f"   • {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
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
            for r in pending_borrows:
                msg += (
                    f"   • {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
                    f" — إرجاع {format_date_ar(r['date'])}\n"
                )
            if total_pending > len(pending_borrows):
                msg += f"   <i>...(و {total_pending - len(pending_borrows)} طلبات أخرى)</i>\n"
            msg += "\n<i>يرجى مراجعتها من قائمة الطلبات المعلقة.</i>"

    kwargs = {"chat_id": config.ADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    borrow_topic = config.BORROW_TOPIC_ID or config.ADMIN_TOPIC_ID
    if borrow_topic is not None:
        kwargs["message_thread_id"] = borrow_topic
    kwargs["reply_markup"] = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 لوحة الاستعارات", callback_data="bdash_open")]
    ])
    try:
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Error in send_borrow_reminder: {e}")

async def borrow_dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    await _sync_borrow_board(context)
    await update.message.reply_text("✅ تم تحديث لوحة الاستعارات.")

# ─── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    
    # Register or update user info in DB
    await db_app.register_user(
        user_id=user.id, 
        username=user.username or "", 
        full_name=user.full_name
    )

    is_admin = await is_supervisor(user.id)
    await update.message.reply_text(
        f"أهلاً بك يا <b>{escape_html(user.first_name)}</b> في نظام إدارة طلبات التغطية الإعلامية 📡\n\n"
        "يرجى اختيار أحد الخيارات من القائمة أدناه:",
        reply_markup=kb.main_menu_keyboard(is_admin),
        parse_mode="HTML"
    )
    return State.MENU

# ─── Main menu handler ─────────────────────────────────────────────────────────
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if "طلب تغطية إعلامية" in text:
        if not db_app.check_rate_limit(update.effective_user.id):
            await update.message.reply_text("⚠️ تمهل قليلاً... أنت سريع جداً! انتظر 3 ثوانٍ بين الطلبات.", parse_mode="HTML")
            return State.MENU
        depts = await db_app.get_departments()
        await update.message.reply_text(
            f"{step_header(1)}يرجى اختيار القسم:",
            reply_markup=kb.get_reply_keyboard(depts, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.CHOOSING_DEPT

    elif "طلباتي السابقة" in text:
        try:
            msg, reply_markup = await build_user_requests_page(update.effective_user.id, "all", 0)
            await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error viewing requests: {e}")
            await update.message.reply_text("حدث خطأ أثناء جلب الطلبات.")
        return State.MENU

    elif "استعارة أغراض" in text:
        if not db_app.check_rate_limit(update.effective_user.id):
            await update.message.reply_text("⚠️ تمهل قليلاً... أنت سريع جداً! انتظر 3 ثوانٍ بين الطلبات.", parse_mode="HTML")
            return State.MENU
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(BORROW_ITEMS, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.BORROW_ITEM

    elif "تعليمات الاستخدام" in text:
        instr = (
            "<b>📖 تعليمات الاستخدام:</b>\n\n"
            "- ابدأ بطلب التغطية عبر '📝 طلب تغطية إعلامية'\n"
            "- اختر التاريخ من التقويم والوقت من اللوحة التفاعلية\n"
            "- لاستعارة غرض (كاميرا، ميكروفون...) استخدم '📦 استعارة أغراض'\n"
            "- سيصلك تنبيه إذا كان الغرض محجوزاً وتوفر لاحقاً\n"
            "- راجع ملخص الطلب قبل التأكيد\n"
            "- سيصل طلبك فوراً لمكتب الإعلام للمراجعة\n"
            "- يمكنك متابعة طلباتك عبر قسم '📋 طلباتي السابقة'"
        )
        await update.message.reply_text(instr, parse_mode="HTML")
        return State.MENU

    elif "لوحة تحكم المشرفين" in text:
        await admin_dashboard(update, context)
        return State.MENU

    return State.MENU

# ─── Generic back/cancel helper ────────────────────────────────────────────────
async def handle_back_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              prev_state, prev_msg, prev_kb_func, prev_kb_args=None):
    if not update.message or not update.message.text:
        return None
    text = update.message.text
    if "إلغاء" in text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in text:
        markup = prev_kb_func(prev_kb_args) if prev_kb_args is not None else prev_kb_func()
        await update.message.reply_text(prev_msg, reply_markup=markup, parse_mode="HTML")
        return prev_state
    return None

# ─── Conversation steps ────────────────────────────────────────────────────────
async def set_dept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    if "إلغاء" in update.message.text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in update.message.text:
        return await start(update, context)

    context.user_data["department"] = update.message.text
    context.user_data["user_id"] = update.effective_user.id
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"{step_header(2)}يرجى إدخال <b>اسم الحدث</b>:",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.EVENT_NAME

async def set_event_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    depts = await db_app.get_departments()
    back = await handle_back_cancel(update, context, State.CHOOSING_DEPT,
                                    f"{step_header(1)}يرجى اختيار <b>القسم</b>:", kb.get_reply_keyboard, depts)
    if back is not None: return back
    context.user_data["event_name"] = update.message.text
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    types = await db_app.get_event_types()
    await update.message.reply_text(
        f"{step_header(3)}يرجى اختيار <b>نوع الحدث</b>:",
        reply_markup=kb.get_reply_keyboard(types),
        parse_mode="HTML"
    )
    return State.EVENT_TYPE

async def set_event_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    back = await handle_back_cancel(update, context, State.EVENT_NAME,
                                    f"{step_header(2)}يرجى إدخال اسم الحدث بالتفصيل:", kb.get_reply_keyboard, [])
    if back is not None: return back
    context.user_data["event_type"] = update.message.text
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"{step_header(4)}يرجى كتابة <b>الهدف أو الغاية</b> من الحدث (بحال وجود تفاصيل كاملة يرجى كتابتها أيضاً):",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.OBJECTIVE

async def set_objective(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    types = await db_app.get_event_types()
    back = await handle_back_cancel(update, context, State.EVENT_TYPE,
                                    f"{step_header(3)}يرجى اختيار نوع الحدث:", kb.get_reply_keyboard, types)
    if back is not None: return back
    context.user_data["objective"] = update.message.text
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back

    # Show inline calendar
    now = datetime.now()
    await update.message.reply_text(
        f"{step_header(5)}📅 <b>اختر تاريخ الحدث</b> من التقويم أدناه،\n"
        "أو اكتب التاريخ يدوياً بصيغة (يوم/شهر/سنة):",
        reply_markup=kb.generate_calendar_keyboard(now.year, now.month),
        parse_mode="HTML"
    )
    return State.DATE

# ─── Date: inline calendar callbacks ──────────────────────────────────────────
async def handle_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cal_ignore":
        return State.DATE

    # ◀ / ▶ navigation
    if data.startswith("cal_prev_") or data.startswith("cal_next_"):
        ym = data.split("_", 2)[2]          # "YYYY-MM"
        year, month = int(ym[:4]), int(ym[5:])
        await query.edit_message_reply_markup(
            reply_markup=kb.generate_calendar_keyboard(year, month)
        )
        return State.DATE

    # Day selected
    if data.startswith("cal_select_"):
        date_str = data.replace("cal_select_", "")   # YYYY-MM-DD
        
        # ─── تحقق من أن التاريخ ليس في الماضي ───
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        if date_str < today_str:
            await query.answer("⚠️ لا يمكن اختيار تاريخ قديم!", show_alert=True)
            return State.DATE
            
        context.user_data["date"] = date_str
        display = format_date_ar(date_str)
        back = await maybe_return_to_summary(update, context)
        if back is not None:
            return back
        await query.edit_message_text(
            f"✅ <b>تم اختيار التاريخ:</b> {display}\n\n"
            f"{step_header(6)}⏰ الآن اختر <b>توقيت الحدث</b>:",
            reply_markup=kb.generate_time_picker_keyboard(),
            parse_mode="HTML"
        )
        return State.TIME

    # Back → go to OBJECTIVE
    if data == "cal_back":
        back = await maybe_return_to_summary(update, context)
        if back is not None:
            return back
        await query.edit_message_text(
            f"{step_header(4)}يرجى كتابة <b>الهدف أو الغاية</b> من الحدث (بحال وجود تفاصيل كاملة يرجى كتابتها أيضاً):",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.OBJECTIVE

    # Cancel
    if data == "cal_cancel":
        back = await maybe_return_to_summary(update, context)
        if back is not None:
            return back
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء العملية.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="القائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    return State.DATE

# ─── Date: manual text fallback ───────────────────────────────────────────────
async def set_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in text:
        await update.message.reply_text(
            f"{step_header(4)}يرجى كتابة <b>الهدف أو الغاية</b> من الحدث:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.OBJECTIVE

    # Parse manual date input
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        dd, mm, yyyy = match.groups()
        date_str = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        
        # ─── تحقق من أن التاريخ ليس في الماضي ───
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        if date_str < today_str:
            await update.message.reply_text(
                "⚠️ نعتذر، لا يمكن تسجيل طلب لتاريخ قديم.\nيرجى إدخال تاريخ اليوم أو تاريخ مستقبلي:",
                parse_mode="HTML"
            )
            return State.DATE
            
        context.user_data["date"] = date_str
        display = format_date_ar(date_str)
    else:
        # إذا كان نصاً غير مفهوم كـ تاريخ، نعتبره كما هو (قد يسبب أخطاء لاحقاً لكن نحافظ على السلوك الحالي)
        context.user_data["date"] = text
        display = text

    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"✅ <b>تم تسجيل التاريخ:</b> {display}\n\n"
        f"{step_header(6)}⏰ الآن اختر <b>توقيت الحدث</b>:",
        reply_markup=kb.generate_time_picker_keyboard(prefix="time_pick_"),
        parse_mode="HTML"
    )
    return State.TIME

# ─── Time selection handlers ───────────────────────────────────────────────────
async def set_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("time_pick_"):
        time_val = data.replace("time_pick_", "")
        if time_val == "custom":
            await query.edit_message_text(
                f"{step_header(6)}✏️ يرجى إدخال <b>وقت البدء</b> يدوياً\n"
                "مثال: <code>10:30 صباحاً</code> أو <code>14:00</code>",
                parse_mode="HTML"
            )
            context.user_data["awaiting_custom_time"] = True
            return State.TIME
        elif time_val == "back":
            now = datetime.now()
            saved_date = context.user_data.get("date", "")
            if saved_date and len(saved_date) == 10:
                year, month = int(saved_date[:4]), int(saved_date[5:7])
            else:
                year, month = now.year, now.month
            await query.edit_message_text(
                f"{step_header(5)}📅 <b>اختر تاريخ الحدث</b> من التقويم:",
                reply_markup=kb.generate_calendar_keyboard(year, month),
                parse_mode="HTML"
            )
            return State.DATE
        elif time_val == "cancel":
            back = await maybe_return_to_summary(update, context)
            if back is not None:
                return back
            is_admin = await is_supervisor(update.effective_user.id)
            await query.edit_message_text("تم إلغاء العملية.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="القائمة الرئيسية:",
                reply_markup=kb.main_menu_keyboard(is_admin)
            )
            return State.MENU
            
        context.user_data["time"] = time_val
        await query.edit_message_text(
            f"✅ <b>تم اختيار وقت البدء:</b> {escape_html(time_val)}\n\n"
            f"{step_header(7)}⏰ <b>يرجى اختيار وقت انتهاء الحدث:</b>",
            reply_markup=kb.generate_time_picker_keyboard(prefix="endtime_pick_"),
            parse_mode="HTML"
        )
        return State.END_TIME

async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in text:
        now = datetime.now()
        saved_date = context.user_data.get("date", "")
        if saved_date and len(saved_date) == 10:
            year, month = int(saved_date[:4]), int(saved_date[5:7])
        else:
            year, month = now.year, now.month
        await update.message.reply_text(
            f"{step_header(5)}📅 <b>اختر تاريخ الحدث</b> من التقويم:",
            reply_markup=kb.generate_calendar_keyboard(year, month),
            parse_mode="HTML"
        )
        return State.DATE

    context.user_data["time"] = text
    context.user_data.pop("awaiting_custom_time", None)
    await update.message.reply_text(
        f"✅ <b>تم تسجيل وقت البدء:</b> {escape_html(text)}\n\n"
        f"{step_header(7)}⏰ <b>يرجى اختيار وقت انتهاء الحدث:</b>",
        reply_markup=kb.generate_time_picker_keyboard(prefix="endtime_pick_"),
        parse_mode="HTML"
    )
    return State.END_TIME

async def handle_end_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("endtime_pick_"):
        time_val = data.replace("endtime_pick_", "")
        if time_val == "custom":
            await query.edit_message_text(
                f"{step_header(7)}✏️ يرجى إدخال وقت الانتهاء يدوياً\n"
                "مثال: <code>12:30 مساءً</code> أو <code>16:30</code>",
                parse_mode="HTML"
            )
            context.user_data["awaiting_custom_end_time"] = True
            return State.END_TIME
        elif time_val == "back":
            await query.edit_message_text(
                f"{step_header(6)}⏰ <b>اختر توقيت بدء الحدث</b>:",
                reply_markup=kb.generate_time_picker_keyboard(prefix="time_pick_"),
                parse_mode="HTML"
            )
            return State.TIME
        elif time_val == "cancel":
            back = await maybe_return_to_summary(update, context)
            if back is not None:
                return back
            is_admin = await is_supervisor(update.effective_user.id)
            await query.edit_message_text("تم إلغاء العملية.")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="القائمة الرئيسية:",
                reply_markup=kb.main_menu_keyboard(is_admin)
            )
            return State.MENU

        # ─── Validate: end time must be after start time ─────────────
        start_time_str = context.user_data.get("time", "")
        
        t_start = parse_ar_time(start_time_str)
        t_end = parse_ar_time(time_val)
        
        if t_start and t_end:
            if t_end <= t_start:
                await query.answer(
                    "⚠️ وقت الانتهاء لا يمكن أن يكون قبل أو نفس وقت البدء!\nيرجى اختيار وقت لاحق.",
                    show_alert=True
                )
                return State.END_TIME
        elif time_val == start_time_str:
            # Fallback if parsing fails but strings are identical
            await query.answer(
                "⚠️ وقت الانتهاء لا يمكن أن يكون نفس وقت البدء!",
                show_alert=True
            )
            return State.END_TIME

        context.user_data["end_time"] = time_val
        await query.edit_message_text(
            f"✅ <b>تم اختيار وقت الانتهاء:</b> {escape_html(time_val)}",
            parse_mode="HTML"
        )
        back = await maybe_return_to_summary(update, context)
        if back is not None:
            return back
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{step_header(8)}📍 يرجى إدخال <b>مكان الحدث</b>:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.LOCATION

async def set_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in text:
        await update.message.reply_text(
            f"{step_header(6)}⏰ <b>اختر توقيت بدء الحدث</b>:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="time_pick_"),
            parse_mode="HTML"
        )
        return State.TIME

    # ─── Validate: end time must be after start time ─────────────────
    start_time_str = context.user_data.get("time", "")
    
    t_start = parse_ar_time(start_time_str)
    t_end = parse_ar_time(text)
    
    if t_start and t_end:
        if t_end <= t_start:
            await update.message.reply_text(
                "⚠️ وقت الانتهاء لا يمكن أن يكون قبل أو نفس وقت البدء!\n"
                "يرجى إدخال وقت انتهاء لاحق ومختلف:",
                reply_markup=kb.generate_time_picker_keyboard(prefix="endtime_pick_"),
                parse_mode="HTML"
            )
            return State.END_TIME
    elif text.strip() == start_time_str.strip():
        await update.message.reply_text(
            "⚠️ وقت الانتهاء لا يمكن أن يكون نفس وقت البدء!\n"
            "يرجى إدخال وقت انتهاء مختلف:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="endtime_pick_"),
            parse_mode="HTML"
        )
        return State.END_TIME

    context.user_data["end_time"] = text
    context.user_data.pop("awaiting_custom_end_time", None)
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"✅ <b>تم تسجيل وقت الانتهاء:</b> {escape_html(text)}\n\n"
        f"{step_header(8)}📍 يرجى إدخال <b>مكان الحدث</b>:",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.LOCATION

# ─── Location ─────────────────────────────────────────────────────────────────
async def set_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
        return State.MENU
    if "رجوع" in text:
        await update.message.reply_text(
            f"{step_header(7)}⏳ <b>اختر وقت انتهاء الحدث</b>:",
            reply_markup=kb.generate_time_picker_keyboard(),
            parse_mode="HTML"
        )
        return State.END_TIME

    context.user_data["location"] = text
    if not context.user_data.get("return_to_summary"):
        context.user_data["coverage_type"] = []
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"{step_header(9)}يرجى اختيار <b>نوع التغطية المطلوبة</b> (يمكنك اختيار أكثر من بند):",
        reply_markup=await kb.coverage_selection_menu([]),
        parse_mode="HTML"
    )
    return State.COVERAGE_TYPE

# ─── Coverage toggle ──────────────────────────────────────────────────────────
async def handle_coverage_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        back = await handle_edit_back_cancel(update, context)
        if back is not None:
            return back
        text = update.message.text
        if "إلغاء" in text:
            is_admin = await is_supervisor(update.effective_user.id)
            await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
            return State.MENU
        if "رجوع" in text:
            await update.message.reply_text(
                f"{step_header(8)}📍 يرجى إدخال <b>مكان الحدث</b>:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.LOCATION
        return State.COVERAGE_TYPE

    query = update.callback_query
    await query.answer()
    data = query.data
    selected = context.user_data.get("coverage_type", [])

    if data.startswith("toggle_"):
        option = data.replace("toggle_", "")
        if option in selected:
            selected.remove(option)
        else:
            selected.append(option)
        context.user_data["coverage_type"] = selected
        await query.edit_message_reply_markup(reply_markup=await kb.coverage_selection_menu(selected))
        return State.COVERAGE_TYPE

    elif data == "coverage_done":
        if not selected:
            await query.answer("⚠️ يرجى اختيار نوع واحد على الأقل!", show_alert=True)
            return State.COVERAGE_TYPE
        await query.message.delete()
        back = await maybe_return_to_summary(update, context)
        if back is not None:
            return back
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{step_header(10)}حدد <b>مستوى أهمية الحدث</b>:",
            reply_markup=kb.get_reply_keyboard(IMPORTANCE_LEVELS),
            parse_mode="HTML"
        )
        return State.IMPORTANCE

# ─── Importance ───────────────────────────────────────────────────────────────
async def set_importance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    back = await handle_back_cancel(update, context, State.LOCATION,
                                    f"{step_header(8)}📍 يرجى إدخال مكان الحدث:", kb.get_reply_keyboard, [])
    if back is not None: return back
    context.user_data["importance"] = update.message.text
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"{step_header(11)}هل هناك حاجة لوجود جهات إعلام خارجية؟:",
        reply_markup=kb.get_reply_keyboard(["نعم", "لا"]),
        parse_mode="HTML"
    )
    return State.EXTERNAL_MEDIA

# ─── External Media ───────────────────────────────────────────────────────────
async def set_external_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    back = await handle_back_cancel(update, context, State.IMPORTANCE,
                                    f"{step_header(10)}حدد مستوى أهمية الحدث:", kb.get_reply_keyboard, IMPORTANCE_LEVELS)
    if back is not None: return back
    context.user_data["external_media"] = update.message.text
    back = await maybe_return_to_summary(update, context)
    if back is not None:
        return back
    await update.message.reply_text(
        f"{step_header(12)}💬 هل لديك أي <b>ملاحظات إضافية</b> بما يتعلق بالتغطية؟\n(أمور يجب التركيز عليها أو تجنبها):",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.NOTES

# ─── Notes → Summary ──────────────────────────────────────────────────────────
async def set_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await handle_edit_back_cancel(update, context)
    if back is not None:
        return back
    back = await handle_back_cancel(update, context, State.EXTERNAL_MEDIA,
                                    f"{step_header(11)}هل هناك حاجة لوجود جهات إعلام خارجية؟", kb.get_reply_keyboard, ["نعم", "لا"])
    if back is not None: return back

    context.user_data["notes"] = update.message.text
    context.user_data["contact_name"] = ""
    context.user_data["phone"] = ""
    context.user_data["telegram"] = ""
    await send_summary(update, context)
    return State.CONFIRMATION

# ─── Confirmation ─────────────────────────────────────────────────────────────
async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("edit_"):
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        context.user_data["return_to_summary"] = True
        field = data.replace("edit_", "")

        if field == "department":
            depts = await db_app.get_departments()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(1)}يرجى اختيار <b>القسم</b>:",
                reply_markup=kb.get_reply_keyboard(depts, with_back=False, with_cancel_only=True),
                parse_mode="HTML"
            )
            return State.CHOOSING_DEPT

        if field == "event_name":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(2)}يرجى إدخال <b>اسم الحدث</b>:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.EVENT_NAME

        if field == "event_type":
            types = await db_app.get_event_types()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(3)}يرجى اختيار <b>نوع الحدث</b>:",
                reply_markup=kb.get_reply_keyboard(types),
                parse_mode="HTML"
            )
            return State.EVENT_TYPE

        if field == "objective":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(4)}يرجى كتابة <b>الهدف أو الغاية</b> من الحدث (نص قصير):",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.OBJECTIVE

        if field == "date":
            now = datetime.now()
            saved_date = context.user_data.get("date", "")
            if saved_date and len(saved_date) == 10:
                year, month = int(saved_date[:4]), int(saved_date[5:7])
            else:
                year, month = now.year, now.month
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(5)}📅 <b>اختر تاريخ الحدث</b> من التقويم أدناه:",
                reply_markup=kb.generate_calendar_keyboard(year, month),
                parse_mode="HTML"
            )
            return State.DATE

        if field == "time":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(6)}⏰ <b>اختر توقيت بدء الحدث</b>:",
                reply_markup=kb.generate_time_picker_keyboard(prefix="time_pick_"),
                parse_mode="HTML"
            )
            return State.TIME

        if field == "end_time":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(7)}⏰ <b>اختر توقيت انتهاء الحدث</b>:",
                reply_markup=kb.generate_time_picker_keyboard(prefix="endtime_pick_"),
                parse_mode="HTML"
            )
            return State.END_TIME

        if field == "location":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(8)}📍 يرجى إدخال <b>مكان الحدث</b>:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.LOCATION

        if field == "coverage":
            selected = context.user_data.get("coverage_type", [])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(9)}يرجى اختيار <b>نوع التغطية المطلوبة</b>:",
                reply_markup=await kb.coverage_selection_menu(selected),
                parse_mode="HTML"
            )
            return State.COVERAGE_TYPE

        if field == "importance":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(10)}حدد <b>مستوى أهمية الحدث</b>:",
                reply_markup=kb.get_reply_keyboard(IMPORTANCE_LEVELS),
                parse_mode="HTML"
            )
            return State.IMPORTANCE

        if field == "external_media":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(11)}هل هناك حاجة لوجود جهات إعلام خارجية؟:",
                reply_markup=kb.get_reply_keyboard(["نعم", "لا"]),
                parse_mode="HTML"
            )
            return State.EXTERNAL_MEDIA

        if field == "notes":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{step_header(12)}💬 هل لديك أي <b>ملاحظات إضافية</b> بما يتعلق بالتغطية؟\n(أمور يجب التركيز عليها أو تجنبها):",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.NOTES

        await query.message.reply_text("❌ خيار غير صالح.")
        return State.CONFIRMATION

    if data == "confirm_request":
        context.user_data.pop("return_to_summary", None)
        data = context.user_data
        data["status"] = "معلق"
        edit_req_id = data.pop("edit_req_id", None)

        if edit_req_id:
            req_id = edit_req_id
            db_pool = await db_app.get_db()
            async with db_app._db_lock:
                await db_pool.execute(
                    """UPDATE requests SET department=?, event_name=?, event_type=?, objective=?,
                       date=?, time=?, end_time=?, start_time_24=?, end_time_24=?, location=?,
                       coverage_type=?, importance=?, external_media=?, notes=?, status='معلق',
                       admin_id=NULL, admin_username=NULL
                       WHERE id=? AND user_id=? AND status='معلق'""",
                    (data["department"], data["event_name"], data["event_type"], data["objective"],
                     data["date"], data["time"], data.get("end_time", ""),
                     db_app.time_to_24(data.get("time")), db_app.time_to_24(data.get("end_time")),
                     data["location"], json.dumps(data["coverage_type"], ensure_ascii=False),
                     data["importance"], data.get("external_media", "لا"), data.get("notes", ""),
                     req_id, data["user_id"])
                )
                await db_pool.commit()
            success = True
        else:
            async with aiosqlite.connect(config.DB_PATH) as db:
                async with db.execute("SELECT MAX(CAST(id AS INTEGER)) FROM requests") as cursor:
                    row = await cursor.fetchone()
                    max_id = row[0] if (row and row[0] is not None) else 0
                    req_id = str(max_id + 1)
                data["id"] = req_id
                success = await db_app.save_request(data, db=db, commit=False)
                await db.commit()

        if success:
            req_label = "تعديل طلب تغطية إعلامية موجود!" if edit_req_id else "📨 وصول طلب تغطية إعلامية جديد!"
            icon = "✏️" if edit_req_id else "📨"
            admin_msg = (
                f"<b>{icon} {req_label}</b>\n\n"
                f"• <b>رقم الطلب:</b> <code>{req_id}</code>\n"
                f"• <b>المرسل:</b> {escape_html(update.effective_user.full_name)} "
                f"(@{escape_html(update.effective_user.username) if update.effective_user.username else 'لا يوجد'}"
                f" | ID: {update.effective_user.id})\n"
                f"• <b>القسم:</b> {escape_html(data['department'])}\n\n"
                f"• <b>الحدث:</b> {escape_html(data['event_name'])} ({escape_html(data['event_type'])})\n"
                f"• <b>الهدف:</b> {escape_html(data['objective'])}\n"
                f"• <b>التاريخ:</b> {format_date_ar(data['date'])}\n"
                f"• <b>الوقت:</b> من {escape_html(data['time'])} إلى {escape_html(data['end_time'])}\n"
                f"• <b>المكان:</b> {escape_html(data['location'])}\n"
                f"• <b>نوع التغطية:</b> {escape_html(', '.join(data['coverage_type']))}\n"
                f"• <b>الأهمية:</b> {escape_html(data['importance'])}\n"
                f"• <b>جهات خارجية:</b> {escape_html(data.get('external_media', 'لا'))}\n\n"
                f"• <b>ملاحظات إضافية:</b>\n{escape_html(data['notes'])}\n"
            )
            try:
                kwargs = {
                    "chat_id": config.ADMIN_CHAT_ID,
                    "text": admin_msg,
                    "reply_markup": kb.admin_approval_keyboard(req_id),
                    "parse_mode": "HTML"
                }
                if config.ADMIN_TOPIC_ID is not None:
                    kwargs["message_thread_id"] = config.ADMIN_TOPIC_ID
                await context.bot.send_message(**kwargs)
            except Exception as e:
                logger.error(f"Error sending to admin: {e}")
                await context.bot.send_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    text=f"طلب جديد من {data['department']} لحدث {data['event_name']}. (خطأ في التنسيق)"
                )

            await query.edit_message_text("✅ تم إرسال طلبك بنجاح! بانتظار موافقة الإدارة.", parse_mode="HTML")
        else:
            await query.edit_message_text("❌ حدث خطأ أثناء حفظ الطلب. يرجى المحاولة لاحقاً.")

        is_admin = await is_supervisor(update.effective_user.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="العودة للقائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    elif data == "cancel_request":
        context.user_data.pop("return_to_summary", None)
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء الطلب.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="يمكنك البدء من جديد:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    return State.CONFIRMATION

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = await is_supervisor(update.effective_user.id)
    await update.message.reply_text(
        "تم إلغاء العملية والعودة للقائمة الرئيسية.",
        reply_markup=kb.main_menu_keyboard(is_admin)
    )
    return State.MENU

# ─── Borrow (استعارة الأغراض) ─────────────────────────────────────────────────
async def borrow_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = await is_supervisor(update.effective_user.id)
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=kb.main_menu_keyboard(is_admin))
    return State.MENU

async def borrow_edit_back_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("borrow_return_to_summary"):
        return None
    if not update.message or not update.message.text:
        return None
    text = update.message.text
    if "إلغاء" in text or "رجوع" in text:
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    return None

async def send_borrow_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    msg = (
        "📦 <b>ملخص طلب استعارة غرض</b>\n\n"
        f"• <b>الغرض:</b> {escape_html(data.get('item', ''))}\n"
        f"• <b>العدد:</b> {escape_html(str(data.get('borrow_qty') or 1))}\n"
        f"• <b>اسم المستعير:</b> {escape_html(data.get('borrower_name', ''))}\n"
        f"• <b>السبب:</b> {escape_html(data.get('reason', ''))}\n"
        f"• <b>رقم التواصل:</b> {escape_html(data.get('phone', ''))}\n"
        f"• <b>تاريخ الإرجاع:</b> {format_date_ar(data.get('return_date', ''))}\n"
        f"• <b>وقت الإرجاع:</b> {escape_html(data.get('return_time', ''))}\n\n"
        "هل ترغب في التأكيد والإرسال؟"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb.borrow_confirmation_keyboard(), parse_mode="HTML")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            reply_markup=kb.borrow_confirmation_keyboard(),
            parse_mode="HTML"
        )
    context.user_data["borrow_return_to_summary"] = False

async def borrow_set_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        return await start(update, context)

    if text in BORROW_ITEMS:
        item = text
    else:
        await update.message.reply_text(
            "⚠️ الرجاء اختيار غرض من القائمة أدناه:",
            reply_markup=kb.get_reply_keyboard(BORROW_ITEMS, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.BORROW_ITEM

    context.user_data["item"] = item
    context.user_data["user_id"] = update.effective_user.id
    context.user_data.pop("borrow_qty", None)

    stock = BORROW_ITEM_STOCK.get(item, 1)
    if stock > 1:
        await update.message.reply_text(
            f"📦 <b>{escape_html(item)}</b> يتوفر منه <b>{stock}</b> قطع.\n\n"
            "كم عدد القطع التي تريد استعارتها؟",
            reply_markup=kb.borrow_quantity_keyboard(),
            parse_mode="HTML"
        )
        return State.BORROW_QUANTITY

    context.user_data["borrow_qty"] = 1
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    return await _check_borrow_availability(update, context)

async def _check_borrow_availability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    item = context.user_data.get("item", "")
    qty = int(context.user_data.get("borrow_qty") or 1)
    stock = BORROW_ITEM_STOCK.get(item, 1)
    approved_qty = await db_app.get_approved_borrow_qty(item)
    available = stock - approved_qty

    if available < qty:
        if available <= 0:
            reservations = await db_app.get_approved_borrow_rows(item)
            ret = ""
            if reservations:
                ret = " حتى <b>" + escape_html(format_date_ar(max(r['date'] for r in reservations))) + "</b>"
            msg = (
                f"⚠️ <b>{escape_html(item)}</b> محجوز بالكامل حالياً{ret}.\n\n"
                "لا يمكنك تقديم طلب استعارة له حالياً.\n"
                "أعد المحاولة لاحقاً عندما يتوفر الغرض."
            )
        else:
            msg = (
                f"⚠️ يتوفر الآن فقط <b>{available}</b> من أصل <b>{stock}</b> قطع، "
                f"وأنت تريد استعارة <b>{qty}</b>.\n\n"
                "لا يمكنك تقديم طلب بهذا العدد حالياً.\n"
                "أعد المحاولة لاحقاً أو اختر عدداً متاحاً."
            )
        await update.message.reply_text(msg, parse_mode="HTML")
        is_admin = await is_supervisor(update.effective_user.id)
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته، أو عد إلى القائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin),
            parse_mode="HTML"
        )
        return State.MENU

    avail_text = f" ({available} قطع متاحة)" if stock > 1 else ""
    await update.message.reply_text(
        f"✅ <b>{escape_html(item)}</b> متاح حالياً{avail_text}.\n\n"
        "يرجى إدخال <b>اسم المستعير</b>:",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.BORROW_BORROWER

async def borrow_set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(BORROW_ITEMS, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.BORROW_ITEM

    if "واحدة" in text:
        qty = 1
    elif "قطعتان" in text or "قطعتين" in text:
        qty = 2
    else:
        try:
            qty = int(text)
        except Exception:
            qty = 0

    item = context.user_data.get("item", "")
    stock = BORROW_ITEM_STOCK.get(item, 1)
    if qty not in (1, 2) or qty > stock:
        await update.message.reply_text(
            f"⚠️ أقصى عدد متاح من <b>{escape_html(item)}</b> هو <b>{stock}</b> قطع.\n"
            "يرجى اختيار العدد المطلوب:",
            reply_markup=kb.borrow_quantity_keyboard(),
            parse_mode="HTML"
        )
        return State.BORROW_QUANTITY

    context.user_data["borrow_qty"] = qty
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    return await _check_borrow_availability(update, context)

async def borrow_set_borrower(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(BORROW_ITEMS, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.BORROW_ITEM
    context.user_data["borrower_name"] = text
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    await update.message.reply_text(
        "يرجى إدخال <b>السبب</b> للاستعارة:",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.BORROW_REASON

async def borrow_set_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "يرجى إدخال <b>اسم المستعير</b>:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_BORROWER
    context.user_data["reason"] = text
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    await update.message.reply_text(
        "يرجى إدخال <b>رقم التواصل</b>:",
        reply_markup=kb.get_reply_keyboard([], with_back=True),
        parse_mode="HTML"
    )
    return State.BORROW_PHONE

async def borrow_set_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "يرجى إدخال <b>السبب</b> للاستعارة:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_REASON
    if not re.search(r"\d", text):
        await update.message.reply_text(
            "⚠️ يرجى إدخال رقم تواصل صحيح يتضمن أرقاماً:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_PHONE
    context.user_data["phone"] = text
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    now = datetime.now()
    await update.message.reply_text(
        f"📅 <b>اختر تاريخ الإرجاع</b> من التقويم أدناه،\n"
        "أو اكتب التاريخ يدوياً بصيغة (يوم/شهر/سنة):",
        reply_markup=kb.generate_calendar_keyboard(now.year, now.month, prefix="bcal_"),
        parse_mode="HTML"
    )
    return State.BORROW_RETURN_DATE

async def borrow_day_items(update: Update, context: ContextTypes.DEFAULT_TYPE, date: str):
    user_id = context.user_data.get("user_id") or update.effective_user.id
    return await db_app.get_borrow_day_items(user_id, date)

async def borrow_day_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE, date: str) -> bool:
    items = await borrow_day_items(update, context, date)
    current_qty = int(context.user_data.get("borrow_qty") or 1)
    occupied = sum((r["borrow_qty"] or 1) for r in items)
    return occupied + current_qty > 2

async def borrow_day_block_lines(update: Update, context: ContextTypes.DEFAULT_TYPE, date: str):
    items = await borrow_day_items(update, context, date)
    return "\n".join(
        f"• {escape_html(r['event_name'])} (العدد {r['borrow_qty'] or 1})"
        for r in items
    )

async def handle_borrow_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "bcal_ignore":
        return State.BORROW_RETURN_DATE

    if data.startswith("bcal_prev_") or data.startswith("bcal_next_"):
        ym = data.split("_", 2)[2]
        year, month = int(ym[:4]), int(ym[5:])
        await query.edit_message_reply_markup(
            reply_markup=kb.generate_calendar_keyboard(year, month, prefix="bcal_")
        )
        return State.BORROW_RETURN_DATE

    if data.startswith("bcal_select_"):
        date_str = data.replace("bcal_select_", "")
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        if date_str < today_str:
            await query.answer("⚠️ لا يمكن اختيار تاريخ إرجاع في الماضي!", show_alert=True)
            return State.BORROW_RETURN_DATE
        context.user_data["return_date"] = date_str
        display = format_date_ar(date_str)
        if await borrow_day_blocked(update, context, date_str):
            lines = await borrow_day_block_lines(update, context, date_str)
            await query.edit_message_text(
                f"⚠️ <b>لا يمكنك استعارة أكثر من غرضين في نفس اليوم.</b>\n\n"
                f"لديك حالياً في {display}:\n{lines}\n"
                "اختر تاريخاً آخر:",
                reply_markup=kb.generate_calendar_keyboard(
                    int(date_str[:4]), int(date_str[5:7]), prefix="bcal_"
                ),
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_DATE
        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        await query.edit_message_text(
            f"✅ <b>تم اختيار تاريخ الإرجاع:</b> {display}\n\n"
            "⏰ الآن اختر <b>وقت الإرجاع</b>:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="btime_pick_"),
            parse_mode="HTML"
        )
        return State.BORROW_RETURN_TIME

    if data == "bcal_back":
        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        await query.edit_message_text(
            "يرجى إدخال <b>رقم التواصل</b>:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_PHONE

    if data == "bcal_cancel":
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء العملية.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="القائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    return State.BORROW_RETURN_DATE

async def borrow_set_return_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "يرجى إدخال <b>رقم التواصل</b>:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_PHONE

    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        dd, mm, yyyy = match.groups()
        date_str = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        if date_str < today_str:
            await update.message.reply_text(
                "⚠️ لا يمكن اختيار تاريخ إرجاع في الماضي.\n"
                "يرجى إدخال تاريخ اليوم أو تاريخ مستقبلي:",
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_DATE
        display = format_date_ar(date_str)
    else:
        date_str = text
        display = text

    if await borrow_day_blocked(update, context, date_str):
        lines = await borrow_day_block_lines(update, context, date_str)
        await update.message.reply_text(
            f"⚠️ <b>لا يمكنك استعارة أكثر من غرضين في نفس اليوم.</b>\n\n"
            f"لديك حالياً في {escape_html(display)}:\n{lines}\n\n"
            "يرجى إدخال <b>تاريخ إرجاع آخر</b>:",
            parse_mode="HTML"
        )
        return State.BORROW_RETURN_DATE

    context.user_data["return_date"] = date_str

    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION
    await update.message.reply_text(
        f"✅ <b>تم تسجيل تاريخ الإرجاع:</b> {display}\n\n"
        "⏰ الآن اختر <b>وقت الإرجاع</b>:",
        reply_markup=kb.generate_time_picker_keyboard(prefix="btime_pick_"),
        parse_mode="HTML"
    )
    return State.BORROW_RETURN_TIME

async def handle_borrow_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("btime_pick_"):
        time_val = data.replace("btime_pick_", "")
        if time_val == "custom":
            await query.edit_message_text(
                "✏️ يرجى إدخال <b>وقت الإرجاع</b> يدوياً\n"
                "مثال: <code>10:30 مساءً</code> أو <code>16:30</code>",
                parse_mode="HTML"
            )
            context.user_data["awaiting_borrow_custom_time"] = True
            return State.BORROW_RETURN_TIME
        elif time_val == "back":
            if context.user_data.get("borrow_return_to_summary"):
                await send_borrow_summary(update, context)
                return State.BORROW_CONFIRMATION
            now = datetime.now()
            saved = context.user_data.get("return_date", "")
            if saved and len(saved) == 10:
                year, month = int(saved[:4]), int(saved[5:7])
            else:
                year, month = now.year, now.month
            await query.edit_message_text(
                "📅 <b>اختر تاريخ الإرجاع</b>:",
                reply_markup=kb.generate_calendar_keyboard(year, month, prefix="bcal_"),
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_DATE
        elif time_val == "cancel":
            if context.user_data.get("borrow_return_to_summary"):
                await send_borrow_summary(update, context)
                return State.BORROW_CONFIRMATION
            return await borrow_cancel(update, context)

        context.user_data["return_time"] = time_val
        context.user_data.pop("awaiting_borrow_custom_time", None)
        await query.edit_message_text(
            f"✅ <b>تم اختيار وقت الإرجاع:</b> {escape_html(time_val)}",
            parse_mode="HTML"
        )
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION

    return State.BORROW_RETURN_TIME

async def borrow_set_return_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "📅 <b>اختر تاريخ الإرجاع</b>:",
            reply_markup=kb.generate_calendar_keyboard(datetime.now().year, datetime.now().month, prefix="bcal_"),
            parse_mode="HTML"
        )
        return State.BORROW_RETURN_DATE

    context.user_data["return_time"] = text
    context.user_data.pop("awaiting_borrow_custom_time", None)
    await send_borrow_summary(update, context)
    return State.BORROW_CONFIRMATION

async def handle_borrow_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("bedit_"):
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        context.user_data["borrow_return_to_summary"] = True
        field = data.replace("bedit_", "")
        chat_id = update.effective_chat.id

        if field == "item":
            await context.bot.send_message(
                chat_id, "📦 اختر الغرض:", 
                reply_markup=kb.get_reply_keyboard(BORROW_ITEMS, with_back=False, with_cancel_only=True),
                parse_mode="HTML"
            )
            return State.BORROW_ITEM
        if field == "quantity":
            item = context.user_data.get("item", "")
            stock = BORROW_ITEM_STOCK.get(item, 1)
            if stock > 1:
                await context.bot.send_message(
                    chat_id, "📦 <b>كم عدد القطع التي تريد استعارتها؟</b>",
                    reply_markup=kb.borrow_quantity_keyboard(),
                    parse_mode="HTML"
                )
                return State.BORROW_QUANTITY
            context.user_data["borrow_qty"] = 1
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        if field == "borrower":
            await context.bot.send_message(
                chat_id, "يرجى إدخال <b>اسم المستعير</b>:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.BORROW_BORROWER
        if field == "reason":
            await context.bot.send_message(
                chat_id, "يرجى إدخال <b>السبب</b> للاستعارة:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.BORROW_REASON
        if field == "phone":
            await context.bot.send_message(
                chat_id, "يرجى إدخال <b>رقم التواصل</b>:",
                reply_markup=kb.get_reply_keyboard([], with_back=True),
                parse_mode="HTML"
            )
            return State.BORROW_PHONE
        if field == "return_date":
            now = datetime.now()
            saved = context.user_data.get("return_date", "")
            if saved and len(saved) == 10:
                year, month = int(saved[:4]), int(saved[5:7])
            else:
                year, month = now.year, now.month
            await context.bot.send_message(
                chat_id, "📅 <b>اختر تاريخ الإرجاع</b>:",
                reply_markup=kb.generate_calendar_keyboard(year, month, prefix="bcal_"),
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_DATE
        if field == "return_time":
            await context.bot.send_message(
                chat_id, "⏰ <b>اختر وقت الإرجاع</b>:",
                reply_markup=kb.generate_time_picker_keyboard(prefix="btime_pick_"),
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_TIME

        return State.BORROW_CONFIRMATION

    if data == "bconfirm_request":
        context.user_data.pop("borrow_return_to_summary", None)
        item = context.user_data.get("item", "")
        qty = context.user_data.get("borrow_qty") or 1
        await query.edit_message_text(
            f"📦 <b>الغرض:</b> {escape_html(item)} (العدد: {qty})\n\n"
            "⚠️ <b>هل تتحمل مسؤولية هذا الغرض وتكاليف ضياعه أو إلحاق الضرر به؟</b>",
            reply_markup=kb.borrow_responsibility_keyboard(),
            parse_mode="HTML"
        )
        return State.BORROW_RESPONSIBILITY

    elif data == "bcancel_request":
        context.user_data.pop("borrow_return_to_summary", None)
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء الطلب.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="يمكنك البدء من جديد:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    return State.BORROW_CONFIRMATION

async def _submit_borrow_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    d = context.user_data
    return_date = d.get("return_date", "")
    if await borrow_day_blocked(update, context, return_date):
        lines = await borrow_day_block_lines(update, context, return_date)
        await query.answer("⚠️ تجاوزت حد الاستعارة اليومي!", show_alert=True)
        await query.edit_message_text(
            f"⚠️ <b>تم إلغاء الطلب.</b> لا يمكنك استعارة أكثر من غرضين في نفس اليوم.\n\n"
            f"لديك حالياً في {escape_html(format_date_ar(return_date))}:\n{lines}\n"
            "أعد إرسال الطلب بتاريخ مختلف.",
            parse_mode="HTML"
        )
        is_admin = await is_supervisor(update.effective_user.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="العودة للقائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU
    success = False
    req_id = ""
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute("SELECT MAX(CAST(id AS INTEGER)) FROM requests") as cursor:
                row = await cursor.fetchone()
                max_id = row[0] if (row and row[0] is not None) else 0
                req_id = str(max_id + 1)
            borrow_data = {
                "id": req_id,
                "user_id": d.get("user_id", update.effective_user.id),
                "item": d.get("item", ""),
                "borrow_qty": int(d.get("borrow_qty") or 1),
                "reason": d.get("reason", ""),
                "borrower_name": d.get("borrower_name", ""),
                "phone": d.get("phone", ""),
                "return_date": d.get("return_date", ""),
                "return_time": d.get("return_time", ""),
            }
            success = await db_app.save_borrow_request(borrow_data, db=db, commit=False)
            await db.commit()
    except Exception as e:
        logger.error(f"Error saving borrow request: {e}")

    if success:
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM requests WHERE id = ?", (req_id,)
                ) as cursor:
                    fresh = await cursor.fetchone()
            if fresh:
                dmsg, dkb = await _build_borrow_request_details(fresh, "مشرف")
                kwargs = {
                    "chat_id": config.ADMIN_CHAT_ID,
                    "text": dmsg,
                    "reply_markup": InlineKeyboardMarkup(dkb),
                    "parse_mode": "HTML"
                }
                borrow_topic = config.BORROW_TOPIC_ID or config.ADMIN_TOPIC_ID
                if borrow_topic is not None:
                    kwargs["message_thread_id"] = borrow_topic
                await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.error(f"Error sending borrow details to admin: {e}")
        await _sync_borrow_board(context)

        await query.edit_message_text("✅ تم إرسال طلبك بنجاح! بانتظار موافقة الإدارة.", parse_mode="HTML")
    else:
        await query.edit_message_text("❌ حدث خطأ أثناء حفظ الطلب. يرجى المحاولة لاحقاً.")

    is_admin = await is_supervisor(update.effective_user.id)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="العودة للقائمة الرئيسية:",
        reply_markup=kb.main_menu_keyboard(is_admin)
    )
    return State.MENU

async def handle_borrow_responsibility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "bresp_yes":
        return await _submit_borrow_request(update, context)

    if data == "bresp_no":
        context.user_data.pop("borrow_return_to_summary", None)
        is_admin = await is_supervisor(update.effective_user.id)
        try:
            await query.edit_message_text("❌ تم إلغاء الطلب لأنك لا تتحمل مسؤولية الغرض.")
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="يمكنك البدء من جديد:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    if data == "bresp_back":
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        context.user_data["borrow_return_to_summary"] = True
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION

    return State.BORROW_RESPONSIBILITY

# ─── Admin dashboard ──────────────────────────────────────────────────────────
async def _get_dashboard_stats(db) -> tuple:
    async with db.execute("SELECT COUNT(*) FROM requests") as c:
        total = (await c.fetchone())[0]
    async with db.execute("SELECT status, COUNT(*) FROM requests GROUP BY status") as c:
        stats = await c.fetchall()
    pending = approved = rejected = 0
    for row in stats:
        if row[0] == 'معلق':    pending  = row[1]
        elif row[0] == 'مقبول': approved = row[1]
        elif row[0] == 'مرفوض': rejected = row[1]
    return total, pending, approved, rejected

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send or refresh the admin dashboard."""
    user_id = update.effective_user.id
    role = await db_app.get_user_role(user_id)
    if role != "مشرف":
        if update.message:
            await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return

    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            total, pending, approved, rejected = await _get_dashboard_stats(db)

        stats_text = _build_dashboard_text(total, pending, approved, rejected)

        # Send dashboard summary (single message with inline keyboard)
        if update.message:
            dash_msg = await update.message.reply_text(
                stats_text,
                reply_markup=kb.admin_dashboard_keyboard(role),
                parse_mode="HTML"
            )
            # Store the dashboard message ID so we can edit it later
            context.chat_data["dash_msg_id"] = dash_msg.message_id

    except Exception as e:
        logger.error(f"Error fetching admin dash: {e}")
        if update.message:
            await update.message.reply_text("❌ حدث خطأ في النظام.")

# ─── User actions (delete/edit own request) ───────────────────────────────────
async def handle_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("user_list_"):
        parts = data.split("_")
        if len(parts) >= 4:
            filter_key = parts[2]
            try:
                page = int(parts[3])
            except Exception:
                page = 0
            msg, reply_markup = await build_user_requests_page(query.from_user.id, filter_key, page)
            try:
                await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="HTML")
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        return State.MENU

    if data.startswith("user_view_"):
        parts = data.split("_")
        if len(parts) >= 5:
            req_id = parts[2]
            filter_key = parts[3]
            try:
                page = int(parts[4])
            except Exception:
                page = 0
            try:
                req = await db_app.get_request_by_id(req_id)
                if not req:
                    await query.edit_message_text("❌ لم يتم العثور على الطلب المحدد.")
                    return State.MENU

                status_label = "⏳ معلق"
                if req['status'] == 'مقبول':
                    status_label = "✅ مقبول"
                elif req['status'] == 'مرفوض':
                    status_label = "❌ مرفوض"
                elif req['status'] == 'مُرجَع':
                    status_label = "↩️ مُرجَع"

                role = await db_app.get_user_role(query.from_user.id)
                back_data = f"user_list_{filter_key}_{page}"
                is_borrow = req['request_type'] == 'استعارة'

                if is_borrow:
                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
                        f"• <b>العدد:</b> {escape_html(str(req['borrow_qty'] or 1))}\n"
                        f"• <b>اسم المستعير:</b> {escape_html(req['contact_name'] or '')}\n"
                        f"• <b>تاريخ الإرجاع:</b> {format_date_ar(req['date'])}\n"
                        f"• <b>وقت الإرجاع:</b> {escape_html(req['time'] or '')}\n"
                        f"• <b>تحمل المسؤولية:</b> نعم\n"
                        f"• <b>وضع الطلب:</b> {status_label}"
                    )
                else:
                    state_label = get_request_state_label(req['date'], req['time'], req['end_time'])
                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>القسم:</b> {escape_html(req['department'])}\n"
                        f"• <b>الحدث:</b> {escape_html(req['event_name'])}\n"
                        f"• <b>التاريخ:</b> {format_date_ar(req['date'])}\n"
                        f"• <b>المكان:</b> {escape_html(req['location'])}\n"
                        f"• <b>التوقيت:</b> من {escape_html(req['time'])} إلى {escape_html(req['end_time'] if req['end_time'] else 'غير محدد')}\n"
                        f"• <b>وضع الطلب:</b> {status_label}\n"
                        f"• <b>حالة الطلب:</b> {state_label}"
                    )
                await query.edit_message_text(
                    msg,
                    reply_markup=kb.user_request_action_keyboard(req['id'], req['status'], role, back_data=back_data, allow_edit=not is_borrow),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error viewing request detail: {e}")
                await query.edit_message_text("❌ حدث خطأ أثناء عرض تفاصيل الطلب.")
        return State.MENU

    if data.startswith("user_delete_"):
        req_id = data.replace("user_delete_", "")
        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                await db.execute("DELETE FROM requests WHERE id = ? AND user_id = ?",
                                 (req_id, update.effective_user.id))
                await db.commit()

            await query.edit_message_text(f"✅ تم حذف الطلب رقم <code>{req_id}</code> بنجاح.", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error deleting: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء الحذف.")

    elif data.startswith("user_req_back_"):
        req_id = data.replace("user_req_back_", "")
        try:
            req = await db_app.get_request_by_id(req_id)
            if req:
                status_label = "⏳ معلق"
                if req['status'] == 'مقبول':
                    status_label = "✅ مقبول"
                elif req['status'] == 'مرفوض':
                    status_label = "❌ مرفوض"
                elif req['status'] == 'مُرجَع':
                    status_label = "↩️ مُرجَع"

                role = await db_app.get_user_role(query.from_user.id)
                is_borrow = req['request_type'] == 'استعارة'

                if is_borrow:
                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
                        f"• <b>العدد:</b> {escape_html(str(req['borrow_qty'] or 1))}\n"
                        f"• <b>اسم المستعير:</b> {escape_html(req['contact_name'] or '')}\n"
                        f"• <b>تاريخ الإرجاع:</b> {format_date_ar(req['date'])}\n"
                        f"• <b>وقت الإرجاع:</b> {escape_html(req['time'] or '')}\n"
                        f"• <b>تحمل المسؤولية:</b> نعم\n"
                        f"• <b>وضع الطلب:</b> {status_label}"
                    )
                else:
                    state_label = get_request_state_label(req['date'], req['time'], req['end_time'])
                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>القسم:</b> {escape_html(req['department'])}\n"
                        f"• <b>الحدث:</b> {escape_html(req['event_name'])}\n"
                        f"• <b>التاريخ:</b> {format_date_ar(req['date'])}\n"
                        f"• <b>المكان:</b> {escape_html(req['location'])}\n"
                        f"• <b>التوقيت:</b> من {escape_html(req['time'])} إلى {escape_html(req['end_time'] if req['end_time'] else 'غير محدد')}\n"
                        f"• <b>وضع الطلب:</b> {status_label}\n"
                        f"• <b>حالة الطلب:</b> {state_label}"
                    )
                await query.edit_message_text(
                    msg,
                    reply_markup=kb.user_request_action_keyboard(req['id'], req['status'], role, allow_edit=not is_borrow),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error handling back: {e}")

    elif data.startswith("user_edit_"):
        req_id = data.replace("user_edit_", "")
        try:
            req = await db_app.get_request_by_id(req_id)
            if not req:
                await query.edit_message_text("❌ لم يتم العثور على الطلب.")
                return State.MENU
            if req['user_id'] != update.effective_user.id:
                await query.answer("❌ هذا ليس طلبك!", show_alert=True)
                return State.MENU
            if req['request_type'] == 'استعارة':
                await query.answer("⚠️ تعديل طلبات الاستعارة يتم عبر تقديم طلب جديد.", show_alert=True)
                return State.MENU
            if req['status'] != 'معلق':
                await query.answer("⚠️ لا يمكن تعديل طلب تمت معالجته بالفعل.", show_alert=True)
                return State.MENU

            context.user_data.clear()
            context.user_data["user_id"] = update.effective_user.id
            for field in ["department", "event_name", "event_type", "objective", "date",
                          "time", "end_time", "location", "importance", "external_media", "notes"]:
                context.user_data[field] = req[field] or ""
            try:
                cov = json.loads(req['coverage_type']) if req['coverage_type'] and req['coverage_type'].startswith('[') else (req['coverage_type'] or "").split(", ")
                context.user_data["coverage_type"] = cov if isinstance(cov, list) else [cov]
            except:
                context.user_data["coverage_type"] = []
            context.user_data["edit_req_id"] = req_id

            await query.edit_message_text("✅ تم تحميل بيانات الطلب. راجع الملخص وعدّل ما تريد:")
            await send_summary(update, context)
            return State.CONFIRMATION
        except Exception as e:
            logger.error(f"Error loading request for edit: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء تحميل الطلب للتعديل.")
            return State.MENU
    return State.MENU

# ─── 30-Min Pre-event Reminder ───────────────────────────────────────────────
async def check_pre_event_reminders(context: ContextTypes.DEFAULT_TYPE):
    """التحقق من التغطيات التي ستبدأ بعد 30 دقيقة وإرسال تنبيه"""
    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    window_start = (now + timedelta(minutes=25)).strftime("%H:%M")
    window_end = (now + timedelta(minutes=35)).strftime("%H:%M")
    
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date = ? AND start_time_24 BETWEEN ? AND ?",
                (today_str, window_start, window_end)
            ) as cursor:
                todays_events = await cursor.fetchall()

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
            
            # إذا كان الوقت المتبقي بين 25 و 35 دقيقة (لتجنب التكرار ولضمان التقاط الحدث)
            # نستخدم الـ job_data لتجنب إرسال نفس التنبيه مرتين
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

# ─── Main Execution ──────────────────────────────────────────────────────────

async def _get_requester_name(req) -> str:
    user_row = await db_app.get_user_by_id(req['user_id'])
    if user_row:
        if user_row["full_name"]:
            return user_row["full_name"]
        if user_row["username"]:
            return f"@{user_row['username']}"
    return req['contact_name'] or str(req['user_id'])

async def free_item(context, item: str):
    stock = BORROW_ITEM_STOCK.get(item, 1)
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

        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("UPDATE requests SET status = 'مُرجَع' WHERE id = ?", (req_id,))
            await db_app.log_audit_action(
                admin.id,
                admin.username or str(admin.id),
                "إرجاع",
                req_id,
                f"إرجاع غرض: {item}",
                db=db,
                commit=False
            )
            await db.commit()

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

    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM requests WHERE request_type = 'استعارة' ORDER BY date ASC, time ASC"
        ) as cursor:
            all_borrows = await cursor.fetchall()

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
    for item, stock in BORROW_ITEM_STOCK.items():
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

async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    # ── Close dashboard ──
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
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM requests WHERE date BETWEEN ? AND ? ORDER BY date ASC, time ASC",
                    (date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d"))
                ) as cursor:
                    rows = await cursor.fetchall()

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
            now = datetime.now(timezone(timedelta(hours=3)))
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
                msg,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Quick filter error: {e}")
            await query.edit_message_text("❌ حدث خطأ في النظام.", reply_markup=kb.admin_back_keyboard())
        return

    if data == "admin_settings_menu":
        user_id = query.from_user.id
        role = await db_app.get_user_role(user_id)
        if role != "مشرف":
            await query.answer("عذراً، تحتاج لصلاحيات مشرف للتحكم بالإعدادات.", show_alert=True)
            return
        await query.edit_message_text(
            "⚙️ <b>لوحة التحكم بالإعدادات الديناميكية:</b>\n"
            "اختر البند الذي ترغب بتعديله (إضافة/حذف) من الخيارات أدناه:",
            reply_markup=kb.settings_main_keyboard(),
            parse_mode="HTML"
        )
        return


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

    if data == "admin_kpi_report":
        # Forward to the dedicated handler
        await handle_kpi_report(update, context)
        return

    if data == "admin_pending" or data.startswith("admin_pending_"):
        try:
            page = 0
            if data.startswith("admin_pending_"):
                try:
                    page = int(data.split("_")[-1])
                except Exception:
                    page = 0

            per_page = 5
            offset = page * per_page

            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'معلق'") as c:
                    total_pending = (await c.fetchone())[0]

                async with db.execute(
                    "SELECT * FROM requests WHERE status = 'معلق' ORDER BY date ASC, timestamp ASC LIMIT ? OFFSET ?",
                    (per_page, offset)
                ) as cursor:
                    pending_requests = await cursor.fetchall()

            if not pending_requests:
                await query.edit_message_text(
                    "✅ لا توجد طلبات معلقة بانتظار الموافقة في الوقت الحالي.",
                    reply_markup=kb.admin_back_keyboard()
                )
                return

            total_pages = (total_pending + per_page - 1) // per_page
            now = datetime.now(timezone(timedelta(hours=3)))
            today_str = now.strftime("%Y-%m-%d")
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            conflict_keys = build_time_conflicts(pending_requests)

            msg = (
                "⏳ <b>الطلبات المعلقة بانتظار الموافقة</b>\n"
                f"<i>عرض {offset + 1} إلى {min(offset + per_page, total_pending)} من أصل {total_pending}</i>\n"
            )

            if conflict_keys:
                msg += "🚨 يوجد تعارض في بعض المواعيد المعروضة.\n"
            msg += "\nاختر طلباً لعرض التفاصيل والإجراءات.\n"

            buttons = []
            for req in pending_requests:
                buttons.append([
                    InlineKeyboardButton(
                        build_admin_button_label_with_status(req, today_str, tomorrow_str),
                        callback_data=f"admin_view_{req['id']}"
                    )
                ])

            # Navigation row
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=f"admin_pending_{page-1}"))
            if total_pages > 1:
                nav_row.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="ignore"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=f"admin_pending_{page+1}"))
            if nav_row:
                buttons.append(nav_row)

            buttons.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")])

            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error fetching pending requests: {e}")
            await query.edit_message_text("❌ حدث خطأ في النظام.", reply_markup=kb.admin_back_keyboard())
        return

    if data.startswith("admin_upcoming") or data.startswith("admin_past"):
        try:
            page = 0
            if "_" in data.replace("admin_upcoming", "").replace("admin_past", ""):
                try:
                    page = int(data.split("_")[-1])
                except:
                    page = 0
            
            is_upcoming = "upcoming" in data
            now = datetime.now(timezone(timedelta(hours=3)))
            today_str = now.strftime("%Y-%m-%d")
            
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                if is_upcoming:
                    query_sql = "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date >= ? ORDER BY date ASC, time ASC"
                    prefix = "admin_upcoming_"
                    title = "🔜 التغطيات المعتمدة القادمة"
                    empty_msg = "✅ لا توجد تغطيات معتمدة قادمة في الجدول."
                else:
                    query_sql = "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date < ? ORDER BY date DESC, time DESC"
                    prefix = "admin_past_"
                    title = "⏪ التغطيات المعتمدة السابقة (الأرشيف)"
                    empty_msg = "✅ لا توجد تغطيات معتمدة سابقة في الأرشيف."
                
                async with db.execute(query_sql, (today_str,)) as cursor:
                    all_rows_db = await cursor.fetchall()
                
                # ─── تصفية الأحداث التي انتهت اليوم (لزر القادمة) ───
                now_time = now.time()
                all_rows = []
                
                if is_upcoming:
                    for req in all_rows_db:
                        if req['date'] == today_str:
                            t_to_check = req['end_time'] if req['end_time'] else req['time']
                            req_time = parse_ar_time(t_to_check)
                            if req_time and req_time < now_time:
                                continue
                        all_rows.append(req)
                else:
                    for req in all_rows_db:
                        all_rows.append(req)
                    
                    async with db.execute(
                        "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date = ? ORDER BY time DESC", (today_str,)
                    ) as cursor:
                        today_rows = await cursor.fetchall()
                        for r in today_rows:
                            t_to_check = r['end_time'] if r['end_time'] else r['time']
                            r_time = parse_ar_time(t_to_check)
                            if r_time and r_time < now_time:
                                if not any(x['id'] == r['id'] for x in all_rows):
                                    all_rows.append(r)
                    all_rows.sort(key=lambda x: (x['date'], parse_ar_time(x['time']) or now_time), reverse=True)
                
                pending_future_count = 0
                if is_upcoming and not all_rows:
                    async with db.execute(
                        "SELECT COUNT(*) FROM requests WHERE status = 'معلق' AND date >= ?", (today_str,)
                    ) as c:
                        pending_future_count = (await c.fetchone())[0]
            
            if not all_rows:
                if pending_future_count > 0:
                    final_msg = (
                        f"{empty_msg}\n\n"
                        f"⚠️ <b>ملاحظة:</b> يوجد <b>{pending_future_count}</b> طلبات معلقة لتواريخ مستقبلية بانتظار موافقتك.\n"
                        "يرجى مراجعة 'الطلبات المعلقة' لقبولها لتظهر هنا."
                    )
                else:
                    final_msg = empty_msg
                    
                await query.edit_message_text(final_msg, reply_markup=kb.admin_back_keyboard(), parse_mode="HTML")
                return

            per_page = 5
            total_pages = (len(all_rows) + per_page - 1) // per_page
            start_idx = page * per_page
            end_idx = start_idx + per_page
            page_rows = all_rows[start_idx:end_idx]
            
            msg = f"<b>{title}</b>\n"
            msg += f"<i>عرض {start_idx + 1} إلى {min(end_idx, len(all_rows))} من أصل {len(all_rows)}</i>\n\n"
            
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            msg += "اختر حدثاً لعرض التفاصيل والإجراءات.\n"

            buttons = []
            for req in page_rows:
                buttons.append([
                    InlineKeyboardButton(
                        build_admin_button_label_with_status(req, today_str, tomorrow_str),
                        callback_data=f"admin_view_{req['id']}"
                    )
                ])

            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=f"{prefix}{page-1}"))
            if total_pages > 1:
                nav_row.append(InlineKeyboardButton(f"صفحة {page+1}/{total_pages}", callback_data="ignore"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=f"{prefix}{page+1}"))
            if nav_row:
                buttons.append(nav_row)

            buttons.append([InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")])

            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error in upcoming/past requests: {e}")
            await query.edit_message_text("❌ حدث خطأ في النظام.", reply_markup=kb.admin_back_keyboard())
        return

    if data == "admin_month_stats":
        now = datetime.now(timezone(timedelta(hours=3)))
        month_start = now.replace(day=1).strftime("%Y-%m-%d")
        month_end = now.strftime("%Y-%m-%d")
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM requests WHERE date BETWEEN ? AND ?", (month_start, month_end)
            ) as cursor:
                total = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT status, COUNT(*) FROM requests WHERE date BETWEEN ? AND ? GROUP BY status",
                (month_start, month_end)
            ) as cursor:
                month_stats = await cursor.fetchall()

        m_pending = m_approved = m_rejected = 0
        for row in month_stats:
            if row[0] == 'معلق':    m_pending  = row[1]
            elif row[0] == 'مقبول': m_approved = row[1]
            elif row[0] == 'مرفوض': m_rejected = row[1]

        stats_msg = (
            f"<b>📊 إحصائيات شهر {kb.AR_MONTHS[now.month-1]} {now.year}</b>\n\n"
            f"• إجمالي الطلبات: <b>{total}</b>\n"
            f"• ⏳ المعلقة: <b>{m_pending}</b>\n"
            f"• ✅ المقبولة: <b>{m_approved}</b>\n"
            f"• ❌ المرفوضة: <b>{m_rejected}</b>\n"
        )
        await query.edit_message_text(
            stats_msg, reply_markup=kb.admin_back_keyboard(), parse_mode="HTML"
        )
        return

    if data in ["admin_dash_back", "admin_dash_refresh"]:
        try:
            role = await db_app.get_user_role(query.from_user.id)
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                total, pending, approved, rejected = await _get_dashboard_stats(db)
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
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM requests WHERE department = ? ORDER BY date DESC LIMIT 15", (dept_name,)
                ) as cursor:
                    dept_requests = await cursor.fetchall()

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
            async with aiosqlite.connect(config.DB_PATH) as db:
                await db.execute("DELETE FROM requests WHERE id = ?", (req_id,))
                
                # سجل العمليات (Audit Logs)
                event_name = req_data['event_name'] if req_data else "غير معروف"
                req_type = req_data['request_type'] if req_data and req_data['request_type'] else 'تغطية'
                type_desc = "طلب استعارة غرض" if req_type == 'استعارة' else "طلب تغطية حدث"
                await db_app.log_audit_action(
                    query.from_user.id,
                    query.from_user.username or str(query.from_user.id),
                    "حذف",
                    req_id,
                    f"حذف {type_desc}: {event_name}",
                    db=db,
                    commit=False
                )
                await db.commit()

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
                    await _sync_borrow_board(context)
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
        icon = "✅"
        admin = update.effective_user
        requester_id = None
        event_name = ""

        try:
            async with aiosqlite.connect(config.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM requests WHERE id = ?", (req_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    
                if not row:
                    await query.answer("❌ لم يتم العثور على الطلب.", show_alert=True)
                    await query.message.delete()
                    return
                    
                # منع الموافقة في حال تمت معالجة الطلب مسبقاً (مقبول أو مرفوض)
                if row['status'] != 'معلق':
                    admin_name = row['admin_username'] or "مشرف آخر"
                    status_str = {"مقبول": "مقبول", "مرفوض": "مرفوض", "مُرجَع": "مُرجَع"}.get(row['status'], "محلول")
                    await query.answer(f"⚠️ تمت معالجة هذا الطلب سابقاً ({status_str}) بواسطة @{admin_name}.", show_alert=True)
                    if (row['request_type'] or 'تغطية') == 'استعارة':
                        if (query.message.text or "").startswith("📦 <b>تفاصيل طلب استعارة"):
                            try:
                                await query.message.delete()
                            except Exception:
                                pass
                        await _sync_borrow_board(context)
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
                
                await db.execute(
                    "UPDATE requests SET status = ?, admin_id = ?, admin_username = ? WHERE id = ?",
                    (status, admin.id, admin.username, req_id)
                )
                
                # سجل العمليات (Audit Logs)
                action_desc = f"قبول طلب استعارة غرض: {event_name}" if request_type == 'استعارة' else f"قبول طلب تغطية حدث: {event_name}"
                await db_app.log_audit_action(
                    admin.id,
                    admin.username or str(admin.id),
                    "قبول",
                    req_id,
                    action_desc,
                    db=db,
                    commit=False
                )
                await db.commit()

            if request_type == 'استعارة':
                if (query.message.text or "").startswith("📦 <b>تفاصيل طلب استعارة"):
                    try:
                        await query.message.delete()
                    except Exception as e:
                        logger.error(f"Error deleting borrow details after approve: {e}")
                await _sync_borrow_board(context)
            else:
                icon = "✅"
                original_text = query.message.text or ""
                new_text = original_text + f"\n\n{icon} تم <b>القبول</b> بواسطة: @{admin.username or admin.first_name}"
                await query.edit_message_text(new_text, parse_mode="HTML")

            if requester_id:
                if request_type == 'استعارة':
                    user_msg = (
                        f"{icon} <b>تحديث بخصوص طلبك رقم <code>{req_id}</code></b>\n\n"
                        f"تم <b>قبول</b> طلب استعارة الغرض (<b>{escape_html(event_name)}</b>).\n\n"
                        "يمكنك مراجعة مكتب الإعلام لاستلامه ومراعاة تاريخ الإرجاع."
                    )
                else:
                    user_msg = (
                        f"{icon} <b>تحديث بخصوص طلبك رقم <code>{req_id}</code></b>\n\n"
                        f"تم <b>قبول</b> طلب التغطية لحدث (<b>{escape_html(event_name)}</b>).\n\n"
                        "شكراً لتعاونكم."
                    )
                await context.bot.send_message(chat_id=requester_id, text=user_msg, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Error in admin action: {e}")
            await query.edit_message_text("❌ حدث خطأ أثناء معالجة الطلب.")

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin text replies (e.g. providing rejection reasons via ForceReply)."""
    if not update.message or not update.message.reply_to_message:
        return
    admin_id = update.effective_user.id
    role = await db_app.get_user_role(admin_id)
    if role != "مشرف":
        return

    replied_msg = update.message.reply_to_message
    orig_text = replied_msg.text or ""

    # 0. التحقق من سبب رفض الطلب
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

        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE requests SET status = ?, admin_id = ?, admin_username = ? WHERE id = ?",
                ("مرفوض", admin_id, update.effective_user.username or str(admin_id), req_id)
            )
            await db_app.log_audit_action(
                admin_id,
                update.effective_user.username or str(admin_id),
                "رفض",
                req_id,
                f"رفض الطلب بسبب: {reason}",
                db=db,
                commit=False
            )
            await db.commit()

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
        await _sync_borrow_board(context)
        return

    # 0. التحقق من سبب رفض الطلب
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

        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE requests SET status = ?, admin_id = ?, admin_username = ? WHERE id = ?",
                ("مرفوض", admin_id, update.effective_user.username or str(admin_id), req_id)
            )
            await db_app.log_audit_action(
                admin_id,
                update.effective_user.username or str(admin_id),
                "رفض",
                req_id,
                f"رفض الطلب بسبب: {reason}",
                db=db,
                commit=False
            )
            await db.commit()

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

    # 1. التحقق من إضافة الإعدادات ديناميكياً
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

    return

# ─── KPI Report Generator ──────────────────────────────────────────────────────
async def generate_kpi_excel() -> io.BytesIO:
    """
    يولد تقرير Excel احترافي يحتوي على KPIs ومؤشرات أداء شاملة.
    يُعيد BytesIO يمكن إرساله مباشرةً عبر Telegram.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (
            PatternFill, Font, Alignment, Border, Side, GradientFill
        )
        from openpyxl.utils import get_column_letter
        from openpyxl.chart import BarChart, Reference
        from openpyxl.chart.label import DataLabelList
    except ImportError:
        raise ImportError("openpyxl غير مثبت. قم بتشغيل: pip install openpyxl")

    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    month_name = kb.AR_MONTHS[now.month - 1] if hasattr(kb, 'AR_MONTHS') else now.strftime('%B')

    # ─── جلب البيانات من قاعدة البيانات ───────────────────────────────────────
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. إحصائيات عامة
        async with db.execute("SELECT COUNT(*) FROM requests") as c:
            total_all = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM requests WHERE status='مقبول'") as c:
            total_approved = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM requests WHERE status='مرفوض'") as c:
            total_rejected = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM requests WHERE status='معلق'") as c:
            total_pending = (await c.fetchone())[0]

        # 2. إحصائيات هذا الشهر
        async with db.execute(
            "SELECT COUNT(*) FROM requests WHERE timestamp >= ?", (month_start,)
        ) as c:
            month_total = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM requests WHERE status='مقبول' AND timestamp >= ?", (month_start,)
        ) as c:
            month_approved = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM requests WHERE status='مرفوض' AND timestamp >= ?", (month_start,)
        ) as c:
            month_rejected = (await c.fetchone())[0]

        # 3. أكثر الأقسام طلباً (كل الوقت)
        async with db.execute(
            "SELECT department, COUNT(*) as cnt FROM requests GROUP BY department ORDER BY cnt DESC LIMIT 10"
        ) as c:
            dept_stats = await c.fetchall()

        # 4. توزيع حالات الطلبات حسب القسم
        async with db.execute(
            "SELECT department, status, COUNT(*) as cnt FROM requests GROUP BY department, status"
        ) as c:
            dept_status_rows = await c.fetchall()

        # 5. الاتجاه الشهري (آخر 6 أشهر)
        monthly_trend = []
        for i in range(5, -1, -1):
            # حساب الشهر بطريقة آمنة
            if now.month - i <= 0:
                m_month = now.month - i + 12
                m_year = now.year - 1
            else:
                m_month = now.month - i
                m_year = now.year
            m_start = f"{m_year}-{m_month:02d}-01"
            if m_month == 12:
                m_end = f"{m_year + 1}-01-01"
            else:
                m_end = f"{m_year}-{m_month + 1:02d}-01"
            async with db.execute(
                "SELECT COUNT(*) FROM requests WHERE timestamp >= ? AND timestamp < ?",
                (m_start, m_end)
            ) as c:
                cnt = (await c.fetchone())[0]
            ar_months = ['يناير','فبراير','مارس','أبريل','مايو','يونيو',
                         'يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر']
            monthly_trend.append((ar_months[m_month - 1], cnt))

        # 6. أكثر أنواع التغطية طلباً
        async with db.execute(
            "SELECT coverage_type FROM requests WHERE coverage_type IS NOT NULL AND coverage_type != ''"
        ) as c:
            coverage_rows = await c.fetchall()
        coverage_counter: dict = {}
        for row in coverage_rows:
            coverage_val = row['coverage_type'] or ''
            items = []
            if coverage_val.startswith('['):
                try:
                    parsed = json.loads(coverage_val)
                    if isinstance(parsed, list):
                        items = parsed
                except:
                    pass
            if not items:
                items = [x.strip() for x in coverage_val.split(',') if x.strip()]
            for item in items:
                if item:
                    coverage_counter[item] = coverage_counter.get(item, 0) + 1
        coverage_sorted = sorted(coverage_counter.items(), key=lambda x: x[1], reverse=True)[:8]

        # 7. آخر 10 تغطيات مقبولة
        async with db.execute(
            "SELECT event_name, department, date, time FROM requests "
            "WHERE status='مقبول' ORDER BY timestamp DESC LIMIT 10"
        ) as c:
            recent_approved = await c.fetchall()

        # 8. معدل الموافقة
        approval_rate = round((total_approved / total_all * 100), 1) if total_all > 0 else 0
        rejection_rate = round((total_rejected / total_all * 100), 1) if total_all > 0 else 0

    # ─── بناء ملف Excel ────────────────────────────────────────────────────────
    wb = Workbook()

    # ألوان ثابتة
    CLR_HEADER   = "1A3C5E"   # أزرق داكن
    CLR_SUBHEAD  = "2E86C1"   # أزرق متوسط
    CLR_ACCENT   = "E8F4FD"   # أزرق فاتح جداً
    CLR_GREEN    = "27AE60"
    CLR_RED      = "E74C3C"
    CLR_ORANGE   = "F39C12"
    CLR_WHITE    = "FFFFFF"
    CLR_GRAY     = "F2F3F4"
    CLR_GOLD     = "D4AC0D"

    thin = Side(style='thin', color="CCCCCC")
    thick = Side(style='medium', color=CLR_SUBHEAD)
    thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    thick_border = Border(left=thick, right=thick, top=thick, bottom=thick)

    def style_header(cell, text, bg=CLR_HEADER, fg=CLR_WHITE, size=12, bold=True):
        cell.value = text
        cell.font = Font(name='Arial', bold=bold, color=fg, size=size)
        cell.fill = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = thin_border

    def style_cell(cell, text, bg=None, fg="000000", bold=False, align='center'):
        cell.value = text
        cell.font = Font(name='Arial', bold=bold, color=fg, size=10)
        if bg:
            cell.fill = PatternFill("solid", fgColor=bg)
        cell.alignment = Alignment(horizontal=align, vertical='center', wrap_text=True)
        cell.border = thin_border

    # ══════════════════════════════════════════════════════
    # ورقة 1: ملخص تنفيذي (KPI Overview)
    # ══════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "الملخص التنفيذي"
    ws1.sheet_view.rightToLeft = True
    ws1.column_dimensions['A'].width = 28
    ws1.column_dimensions['B'].width = 18
    ws1.column_dimensions['C'].width = 28
    ws1.column_dimensions['D'].width = 18

    # عنوان رئيسي
    ws1.merge_cells('A1:D1')
    title_cell = ws1['A1']
    style_header(title_cell, f"📊 تقرير الأداء الإعلامي — {month_name} {now.year}",
                 bg=CLR_HEADER, size=14)
    ws1.row_dimensions[1].height = 35

    ws1.merge_cells('A2:D2')
    sub_cell = ws1['A2']
    style_header(sub_cell, f"تاريخ الإصدار: {now.strftime('%Y/%m/%d')} — الساعة {now.strftime('%H:%M')}",
                 bg=CLR_SUBHEAD, size=10, bold=False)
    ws1.row_dimensions[2].height = 22

    # مؤشرات KPI الرئيسية
    ws1.merge_cells('A3:D3')
    style_header(ws1['A3'], "🎯 مؤشرات الأداء الرئيسية (KPIs)", bg=CLR_SUBHEAD, size=11)
    ws1.row_dimensions[3].height = 26

    kpis = [
        ("📋 إجمالي الطلبات (كل الوقت)", total_all, "✅ طلبات مقبولة (كل الوقت)", total_approved),
        ("🗓️ طلبات هذا الشهر", month_total, "✅ مقبولة هذا الشهر", month_approved),
        ("❌ مرفوضة (كل الوقت)", total_rejected, "⏳ معلقة حالياً", total_pending),
        (f"📈 معدل الموافقة", f"{approval_rate}%", f"📉 معدل الرفض", f"{rejection_rate}%"),
    ]

    for i, (lbl1, val1, lbl2, val2) in enumerate(kpis, start=4):
        ws1.row_dimensions[i].height = 30
        bg = CLR_GRAY if i % 2 == 0 else CLR_ACCENT
        style_cell(ws1.cell(i, 1), lbl1, bg=bg, bold=True, align='right')
        c = ws1.cell(i, 2)
        c.value = val1
        c.font = Font(name='Arial', bold=True, size=13,
                      color=CLR_GREEN if 'مقبول' in str(lbl1) or 'معدل الموافقة' in str(lbl1) else CLR_HEADER)
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border
        style_cell(ws1.cell(i, 3), lbl2, bg=bg, bold=True, align='right')
        c2 = ws1.cell(i, 4)
        c2.value = val2
        c2.font = Font(name='Arial', bold=True, size=13,
                       color=CLR_RED if 'مرفوض' in str(lbl2) or 'معلق' in str(lbl2) or 'معدل الرفض' in str(lbl2)
                       else CLR_GREEN)
        c2.fill = PatternFill("solid", fgColor=bg)
        c2.alignment = Alignment(horizontal='center', vertical='center')
        c2.border = thin_border

    # فاصل
    row_offset = len(kpis) + 5
    ws1.merge_cells(f'A{row_offset}:D{row_offset}')
    style_header(ws1[f'A{row_offset}'], "📅 الاتجاه الشهري (آخر 6 أشهر)", bg=CLR_SUBHEAD, size=11)
    ws1.row_dimensions[row_offset].height = 26
    row_offset += 1

    style_header(ws1.cell(row_offset, 1), "الشهر", bg=CLR_HEADER, size=10)
    style_header(ws1.cell(row_offset, 2), "عدد الطلبات", bg=CLR_HEADER, size=10)
    ws1.merge_cells(f'C{row_offset}:D{row_offset}')
    style_header(ws1.cell(row_offset, 3), "ملاحظة", bg=CLR_HEADER, size=10)
    row_offset += 1

    trend_data_start = row_offset
    for month_label, cnt in monthly_trend:
        bg = CLR_GRAY if row_offset % 2 == 0 else CLR_ACCENT
        style_cell(ws1.cell(row_offset, 1), month_label, bg=bg, bold=True)
        c = ws1.cell(row_offset, 2)
        c.value = cnt
        c.font = Font(name='Arial', bold=True, size=11, color=CLR_HEADER)
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border
        ws1.merge_cells(f'C{row_offset}:D{row_offset}')
        note = "الشهر الحالي" if month_label == month_name else ""
        style_cell(ws1.cell(row_offset, 3), note, bg=bg,
                   fg=CLR_GOLD if note else "888888")
        row_offset += 1
    trend_data_end = row_offset - 1

    # إضافة مخطط بياني للاتجاه الشهري
    chart1 = BarChart()
    chart1.type = "col"
    chart1.grouping = "clustered"
    chart1.title = "الاتجاه الشهري للطلبات"
    chart1.y_axis.title = "عدد الطلبات"
    chart1.style = 10
    chart1.width = 16
    chart1.height = 10
    data_ref = Reference(ws1, min_col=2, min_row=trend_data_start - 1, max_row=trend_data_end)
    cats_ref = Reference(ws1, min_col=1, min_row=trend_data_start, max_row=trend_data_end)
    chart1.add_data(data_ref, titles_from_data=True)
    chart1.set_categories(cats_ref)
    ws1.add_chart(chart1, f"A{row_offset + 1}")

    # ══════════════════════════════════════════════════════
    # ورقة 2: تحليل الأقسام
    # ══════════════════════════════════════════════════════
    ws2 = wb.create_sheet("تحليل الأقسام")
    ws2.sheet_view.rightToLeft = True
    for col, w in zip(['A','B','C','D','E'], [30, 15, 15, 15, 15]):
        ws2.column_dimensions[col].width = w

    ws2.merge_cells('A1:E1')
    style_header(ws2['A1'], "📁 تحليل الطلبات حسب القسم", bg=CLR_HEADER, size=13)
    ws2.row_dimensions[1].height = 32

    headers2 = ["القسم", "إجمالي الطلبات", "مقبولة ✅", "مرفوضة ❌", "معلقة ⏳"]
    for col_i, h in enumerate(headers2, start=1):
        style_header(ws2.cell(2, col_i), h, bg=CLR_SUBHEAD, size=10)
    ws2.row_dimensions[2].height = 26

    # بناء dict للحالات
    dept_map: dict = {}
    for row in dept_status_rows:
        dept = row['department']
        status = row['status']
        cnt = row['cnt']
        if dept not in dept_map:
            dept_map[dept] = {'مقبول': 0, 'مرفوض': 0, 'معلق': 0, 'total': 0}
        dept_map[dept][status] = cnt
        dept_map[dept]['total'] += cnt

    dept_sorted = sorted(dept_map.items(), key=lambda x: x[1]['total'], reverse=True)
    for i, (dept, vals) in enumerate(dept_sorted, start=3):
        bg = CLR_GRAY if i % 2 == 0 else CLR_ACCENT
        ws2.row_dimensions[i].height = 24
        style_cell(ws2.cell(i, 1), dept, bg=bg, bold=True, align='right')
        style_cell(ws2.cell(i, 2), vals['total'], bg=bg,
                   fg=CLR_HEADER, bold=True)
        c_app = ws2.cell(i, 3)
        c_app.value = vals['مقبول']
        c_app.font = Font(name='Arial', bold=True, color=CLR_GREEN, size=10)
        c_app.fill = PatternFill("solid", fgColor=bg)
        c_app.alignment = Alignment(horizontal='center', vertical='center')
        c_app.border = thin_border
        c_rej = ws2.cell(i, 4)
        c_rej.value = vals['مرفوض']
        c_rej.font = Font(name='Arial', bold=True, color=CLR_RED, size=10)
        c_rej.fill = PatternFill("solid", fgColor=bg)
        c_rej.alignment = Alignment(horizontal='center', vertical='center')
        c_rej.border = thin_border
        c_pnd = ws2.cell(i, 5)
        c_pnd.value = vals['معلق']
        c_pnd.font = Font(name='Arial', bold=True, color=CLR_ORANGE, size=10)
        c_pnd.fill = PatternFill("solid", fgColor=bg)
        c_pnd.alignment = Alignment(horizontal='center', vertical='center')
        c_pnd.border = thin_border

    # ══════════════════════════════════════════════════════
    # ورقة 3: أنواع التغطية وآخر المقبولات
    # ══════════════════════════════════════════════════════
    ws3 = wb.create_sheet("التغطيات والأنواع")
    ws3.sheet_view.rightToLeft = True
    for col, w in zip(['A','B','C','D','E'], [30, 14, 26, 20, 16]):
        ws3.column_dimensions[col].width = w

    ws3.merge_cells('A1:B1')
    style_header(ws3['A1'], "🎬 أنواع التغطية الأكثر طلباً", bg=CLR_HEADER, size=12)
    ws3.row_dimensions[1].height = 30

    style_header(ws3.cell(2, 1), "نوع التغطية", bg=CLR_SUBHEAD, size=10)
    style_header(ws3.cell(2, 2), "عدد مرات الطلب", bg=CLR_SUBHEAD, size=10)

    for i, (ctype, cnt) in enumerate(coverage_sorted, start=3):
        bg = CLR_GRAY if i % 2 == 0 else CLR_ACCENT
        ws3.row_dimensions[i].height = 22
        style_cell(ws3.cell(i, 1), ctype, bg=bg, bold=True, align='right')
        c = ws3.cell(i, 2)
        c.value = cnt
        c.font = Font(name='Arial', bold=True, size=11, color=CLR_SUBHEAD)
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border

    # آخر 10 تغطيات مقبولة
    ws3.merge_cells('C1:E1')
    style_header(ws3['C1'], "✅ آخر 10 تغطيات مقبولة", bg=CLR_HEADER, size=12)
    headers3b = ["اسم الحدث", "القسم", "التاريخ"]
    for col_i, h in enumerate(headers3b, start=3):
        style_header(ws3.cell(2, col_i), h, bg=CLR_SUBHEAD, size=10)

    for i, req in enumerate(recent_approved, start=3):
        bg = CLR_GRAY if i % 2 == 0 else CLR_ACCENT
        ws3.row_dimensions[i].height = 22
        style_cell(ws3.cell(i, 3), req['event_name'], bg=bg, align='right')
        style_cell(ws3.cell(i, 4), req['department'], bg=bg)
        style_cell(ws3.cell(i, 5), format_date_ar(req['date']), bg=bg)

    # ─── حفظ في BytesIO ────────────────────────────────────────────────────────
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


async def handle_kpi_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler لزر تقرير الأداء — يولد Excel ويرسله للأدمن."""
    query = update.callback_query
    await query.answer()

    if not await is_supervisor(update.effective_user.id):
        await query.answer("عذراً، هذا الأمر للمشرفين فقط.", show_alert=True)
        return

    await query.edit_message_text(
        "⏳ <b>جاري توليد تقرير الأداء...</b>\nسيصل ملف Excel خلال ثوانٍ.",
        parse_mode="HTML"
    )

    excel_bytes = await generate_kpi_excel()

    now = datetime.now(timezone(timedelta(hours=3)))
    filename = f"KPI_Report_{now.strftime('%Y_%m_%d_%H%M')}.xlsx"

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=excel_bytes,
        filename=filename,
        caption=(
            f"📈 <b>تقرير الأداء الإعلامي</b>\n"
            f"📅 {format_date_ar(now.strftime('%Y-%m-%d'))} — الساعة {now.strftime('%H:%M')}\n\n"
            "يحتوي التقرير على:\n"
            "• ملخص KPIs الرئيسية\n"
            "• تحليل الأقسام\n"
            "• الاتجاه الشهري (آخر 6 أشهر)\n"
            "• أنواع التغطية وآخر المقبولات"
        ),
        parse_mode="HTML"
    )

    # إعادة لوحة التحكم
    try:
        async with aiosqlite.connect(config.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            total, pending, approved, rejected = await _get_dashboard_stats(db)
        stats_text = _build_dashboard_text(total, pending, approved, rejected)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=stats_text,
            reply_markup=kb.admin_dashboard_keyboard("مشرف"),
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Admin: test reminder command ─────────────────────────────────────────────
async def test_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    await update.message.reply_text("🔔 يتم الآن إرسال إشعار تجريبي (تغطيات + استعارات)...")
    await send_daily_reminder(context)
    await send_borrow_reminder(context)
    await update.message.reply_text("✅ تم إرسال الإشعارين التجريبيين.")

async def test_borrow_reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    await update.message.reply_text("📦 يتم الآن إرسال إشعار تجريبي للاستعارات...")
    await send_borrow_reminder(context)
    await update.message.reply_text("✅ تم إرسال إشعار الاستعارات التجريبي.")

async def send_daily_media_dose(context: ContextTypes.DEFAULT_TYPE, prev_dose: str = ""):
    """إرسال جرعة إعلامية يومية. prev_dose: نص الجرعة السابقة لتجنب التكرار."""
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "ضـع_مفتـاح_الـAPI_هنا":
        logger.warning("Gemini API key is not configured. Daily dose skipped.")
        return False, "مفتاح API الخاص بـ Gemini غير مهيأ."

    categories = ["نصيحة تقنية أو احترافية", "خطأ إعلامي شائع وكيفية تجنبه", "فكرة إبداعية ومبتكرة للتغطيات", "تجربة شخصية أو موقف طريف", "اتجاه جديد في الإعلام"]
    weights = [35, 20, 20, 15, 10]
    chosen_category = random.choices(categories, weights=weights, k=1)[0]
    topics = [
        "التصوير الفوتوغرافي", "تصوير الفيديو والمونتاج", "إجراء المقابلات",
        "هندسة الصوت الميدانية", "صياغة العناوين والكابشن", "توزيع الإضاءة",
        "إدارة الوقت الميداني", "التعامل مع الضيوف", "تحرير الفيديو",
        "التصوير الجوي بالدرون", "البث المباشر", "كتابة السيناريو",
        "تصوير المؤتمرات والفعاليات الكبرى", "التعامل مع الكاميرات المختلفة",
        "أخلاقيات العمل الإعلامي", "تصوير المنتجات", "الإخراج التلفزيوني",
        "المونتاج السينمائي", "تصحيح الألوان", "التعليق الصوتي",
        "الإنفوجرافيك والتصميم", "صناعة المحتوى الرقمي", "تحرير الصور",
        "إدارة حسابات التواصل الاجتماعي", "التحليل الإعلامي",
        "البودكاست وإعداده", "التغطيات الرياضية", "الترجمة الإعلامية",
        "الأمان الميداني للفرق الإعلامية", "إعداد التقارير الإخبارية",
    ]
    random_topic = random.choice(topics)

    avoid_block = (
        f"\nمهم: الجرعة السابقة كانت: '{prev_dose[:120]}...'\nاحرص أن تكون مختلفة تماماً بفكرة وأسلوب مختلف."
        if prev_dose else ""
    )

    try:
        prompt = (
            f"اكتب رسالة قصيرة لفريق إعلامي في سوريا حول '{chosen_category}' في مجال {random_topic}."
            f" اللغة سورية طبيعية، الأسلوب مباشر وعملي بدون مبالغة أو تزويق. الجمل قصيرة ومختصرة."
            f" 2-3 أسطر بدون مقدمة طويلة، الفكرة قابلة للتطبيق ضمن الإمكانيات المتاحة في سوريا (ضعف التجهيزات، تقطع الكهرباء والنت، الحصار والعقوبات)."
            f" لا تذكر شغلات مثالية أو مكلفة أو مستحيلة التطبيق محلياً. لا تستخدم ألقاب أو مديح زائد."
            f" يمكن استخدام إيموجي واحد أو اثنين."
            f"{avoid_block}"
        )

        ai_text = await get_ai_response(prompt)

        emojis = {"نصيحة تقنية أو احترافية": "💡", "خطأ إعلامي شائع وكيفية تجنبه": "⚠️", "فكرة إبداعية ومبتكرة للتغطيات": "🌟", "تجربة شخصية أو موقف طريف": "🎙️", "اتجاه جديد في الإعلام": "📈"}
        header = f"{emojis[chosen_category]} <b>{random_topic}</b>"
        msg = f"{header}\n\n{ai_text}"

        # حفظ نص الجرعة في bot_data لاستخدامه عند طلب جرعة ثانية
        context.bot_data["last_dose_text"] = ai_text
        kwargs = {"chat_id": config.DOSE_CHAT_ID, "text": msg, "parse_mode": "HTML",
                  "reply_markup": InlineKeyboardMarkup([[InlineKeyboardButton("🔄 جرعة ثانية", callback_data="dose_refresh")]])}

        await context.bot.send_message(**kwargs)
        logger.info(f"Daily dose ({chosen_category}) sent.")
        return True, None

    except Exception as e:
        logger.error(f"Error generating content via Gemini: {e}")
        return False, str(e)

async def cleanup_cache(context: ContextTypes.DEFAULT_TYPE = None):
    """حذف مجلدات __pycache__ لتوفير المساحة كل يومين"""
    try:
        count = 0
        for root, dirs, files in os.walk("."):
            if "__pycache__" in dirs:
                shutil.rmtree(os.path.join(root, "__pycache__"))
                count += 1
        logger.info(f"🗑️ Cache cleanup: Removed {count} __pycache__ directories.")
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")

async def test_dose_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_supervisor(update.effective_user.id):
        await update.message.reply_text("عذراً، هذا الأمر مخصص للمشرفين فقط.")
        return
    status_msg = await update.message.reply_text("⏳ جاري إنشاء الرسالة من الذكاء الاصطناعي... يرجى الانتظار.")
    
    success, error = await send_daily_media_dose(context)
    if success:
        await status_msg.edit_text("✅ تم توليد الرسالة وإرسالها إلى مجموعة الإشراف بنجاح.")
    else:
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التوليد:\n{error}")


# ─── Caption / Media Writing Assistant ────────────────────────────────────────

async def get_ai_response_by_model(prompt: str, model_key: str) -> str:
    if model_key == "gemini":
        models_to_try = ['gemini-flash-latest', 'gemini-1.5-flash-latest', 'gemini-1.5-flash-8b']
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        for model_name in models_to_try:
            try:
                model = genai.GenerativeModel(model_name, safety_settings=safety_settings)
                response = await asyncio.to_thread(model.generate_content, prompt)
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                logger.warning(f"Gemini model {model_name} failed: {e}")
                continue
        raise Exception("جميع نماذج Gemini غير متاحة حالياً")
    elif model_key == "groq":
        if not HAS_GROQ:
            raise Exception("مكتبة Groq غير مثبتة")
        if not config.GROQ_API_KEY or config.GROQ_API_KEY == "ضـع_مفتـاح_Groq_هنـا":
            raise Exception("مفتاح Groq غير مهيأ")
        client = Groq(api_key=config.GROQ_API_KEY)
        def call_groq():
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
            )
            return completion.choices[0].message.content
        result = await asyncio.to_thread(call_groq)
        return result.strip()
    raise Exception(f"نموذج غير معروف: {model_key}")


CAPTION_TASKS = {
    "reels":    ("🎬", "كابشن ريلز/شورتس",    "اكتب كابشن احترافي لفيديو ريلز على إنستغرام وتيك توك حول الموضوع التالي. الأسلوب جذاب وسريع ومثير للفضول، ابدأ بجملة صادمة أو سؤال قوي، أضف 5-7 هاشتاغات مناسبة في النهاية:\n\n"),
    "news":     ("📰", "خبر صحفي رسمي",        "اكتب خبراً صحفياً رسمياً بأسلوب الصحافة المحترفة (الهرم المقلوب) حول الموضوع التالي. ابدأ بسطر ملخص قوي، ثم التفاصيل، ثم السياق. اللغة عربية فصحى رصينة:\n\n"),
    "three":    ("📋", "3 صيغ مختلفة",         "اكتب 3 صيغ مختلفة تماماً لكابشن حول الموضوع التالي:\nالصيغة 1 - رسمية ومؤسسية\nالصيغة 2 - تفاعلية وشعبية\nالصيغة 3 - قصيرة وصاعقة (أقل من 15 كلمة)\n\nالموضوع:\n"),
    "hashtags": ("🏷️", "هاشتاغات ذكية",         "ولّد 15-20 هاشتاغاً عربياً وإنجليزياً متنوعاً ومناسباً للمنصات لهذا الموضوع. قسّمها: هاشتاغات عامة، متخصصة، ومحلية:\n\n"),
    "proofread":("✅", "تدقيق وتحسين",          "دقق النص التالي إملائياً ونحوياً وأسلوبياً. أعطني:\n1. النص المصحح كاملاً\n2. قائمة بالأخطاء التي وجدتها\n3. 2-3 مقترحات لتحسين الأسلوب\n\nالنص:\n"),
    "platform": ("📱", "صيغة منصة محددة",       "سأعطيك نصاً أو فكرة، أعد صياغتها بما يناسب إنستغرام وتويتر/X وتيليغرام بشكل منفصل مع مراعاة حد الأحرف وطبيعة كل منصة:\n\n"),
    "headline": ("📣", "عنوان جذاب",            "اقترح 5 عناوين جذابة ومختلفة في الأسلوب لهذا الموضوع أو الخبر. كل عنوان لا يتجاوز 10 كلمات:\n\n"),
}


def _task_keyboard():
    buttons = []
    items = list(CAPTION_TASKS.items())
    for i in range(0, len(items), 2):
        row = []
        for key, (emoji, label, _) in items[i:i+2]:
            row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"captask_{key}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ إلغاء", callback_data="captask_cancel")])
    return InlineKeyboardMarkup(buttons)


def _model_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✨ Gemini (أقوى)", callback_data="capmodel_gemini"),
        InlineKeyboardButton("⚡ Groq (أسرع)",  callback_data="capmodel_groq"),
    ]])


def _action_keyboard(has_variants=False):
    rows = [[
        InlineKeyboardButton("📝 تعديل",       callback_data="caption_edit"),
        InlineKeyboardButton("🔄 مهمة أخرى",   callback_data="caption_restart"),
        InlineKeyboardButton("✅ إنهاء",        callback_data="caption_done"),
    ]]
    return InlineKeyboardMarkup(rows)


# ─── Step 1: /caption ──────────────────────────────────────────────────────────
async def caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✍️ <b>المساعد الإعلامي للكتابة والمحتوى</b>\n\n"
        "أرسل لي النص أو الفكرة أو الموضوع الذي تريد العمل عليه:",
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True)
    )
    return CaptionState.TYPING_PROMPT


# ─── Step 2: Receive text → Show task menu ─────────────────────────────────────
async def caption_receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("⚠️ النص فارغ، أرسله مجدداً.")
        return CaptionState.TYPING_PROMPT

    context.user_data['caption_prompt'] = text
    await update.message.reply_text(
        "🎯 <b>اختر المهمة المطلوبة:</b>",
        parse_mode="HTML",
        reply_markup=_task_keyboard()
    )
    return CaptionState.CHOOSING_TASK


# ─── Step 3: Task chosen → Show model selection ────────────────────────────────
async def caption_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "captask_cancel":
        await query.edit_message_text("✅ تم إلغاء الجلسة.")
        context.user_data.clear()
        return ConversationHandler.END

    task_key = query.data.replace("captask_", "")
    if task_key not in CAPTION_TASKS:
        return CaptionState.CHOOSING_TASK

    emoji, label, _ = CAPTION_TASKS[task_key]
    context.user_data['caption_task'] = task_key

    await query.edit_message_text(
        f"{emoji} <b>{label}</b>\n\n🧠 اختر نموذج الذكاء الاصطناعي:",
        parse_mode="HTML",
        reply_markup=_model_keyboard()
    )
    return CaptionState.CHOOSING_MODEL


# ─── Step 4: Model chosen → Generate ──────────────────────────────────────────
async def caption_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    model_key = query.data.replace("capmodel_", "")
    model_labels = {"gemini": "Gemini ✨", "groq": "Groq ⚡"}
    label = model_labels.get(model_key, model_key)

    context.user_data['caption_model'] = model_key
    task_key = context.user_data.get('caption_task', 'reels')
    user_text = context.user_data.get('caption_prompt', '')

    task_emoji, task_label, task_instruction = CAPTION_TASKS[task_key]
    full_prompt = f"{task_instruction}{user_text}"

    await query.edit_message_text(f"⏳ جاري المعالجة بواسطة <b>{label}</b>...", parse_mode="HTML")

    try:
        ai_text = await get_ai_response_by_model(full_prompt, model_key)
        context.user_data['last_caption'] = ai_text

        reply = (
            f"{task_emoji} <b>{task_label} — ({label})</b>\n"
            f"{'─' * 20}\n\n"
            f"{ai_text}"
        )
        await query.edit_message_text(reply, parse_mode="HTML", reply_markup=_action_keyboard())
        return CaptionState.EDITING

    except Exception as e:
        logger.error(f"Caption generation error ({model_key}): {e}")
        await query.edit_message_text(
            f"❌ حدث خطأ:\n<code>{str(e)}</code>\n\nاختر نموذجاً آخر:",
            parse_mode="HTML",
            reply_markup=_model_keyboard()
        )
        return CaptionState.CHOOSING_MODEL


# ─── Edit / Done / Restart callbacks ──────────────────────────────────────────
async def caption_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "caption_done":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("✅ تم اعتماد المحتوى وإنهاء الجلسة.")
        context.user_data.clear()
        return ConversationHandler.END

    elif query.data == "caption_restart":
        context.user_data.pop('last_caption', None)
        context.user_data.pop('caption_task', None)
        await query.edit_message_text(
            "🎯 <b>اختر المهمة التالية:</b>",
            parse_mode="HTML",
            reply_markup=_task_keyboard()
        )
        return CaptionState.CHOOSING_TASK

    elif query.data == "caption_edit":
        await query.message.reply_text(
            "✏️ أرسل طلب التعديل (مثال: اجعله أقصر، أضف حماس أكثر، غير الأسلوب...):",
            reply_markup=ForceReply(selective=True)
        )
        context.user_data['awaiting_caption_edit'] = True
        return CaptionState.EDITING


# ─── Edit text received ────────────────────────────────────────────────────────
async def caption_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_caption_edit'):
        return CaptionState.EDITING

    user_edit = update.message.text
    context.user_data['awaiting_caption_edit'] = False

    original_text  = context.user_data.get('caption_prompt', '')
    old_caption    = context.user_data.get('last_caption', '')
    model_key      = context.user_data.get('caption_model', 'gemini')
    task_key       = context.user_data.get('caption_task', 'reels')
    task_emoji, task_label, _ = CAPTION_TASKS.get(task_key, ("🎨", "محتوى", ""))
    model_labels   = {"gemini": "Gemini ✨", "groq": "Groq ⚡"}
    label          = model_labels.get(model_key, model_key)

    status_msg = await update.message.reply_text("⏳ جاري التعديل...")

    edit_prompt = (
        f"الموضوع الأصلي: {original_text}\n\n"
        f"المحتوى الحالي:\n{old_caption}\n\n"
        f"طلب التعديل: {user_edit}\n\n"
        f"أعد الصياغة بناءً على طلب التعديل مع الحفاظ على نفس نوع المحتوى وأسلوبه العام."
    )

    try:
        ai_text = await get_ai_response_by_model(edit_prompt, model_key)
        context.user_data['last_caption'] = ai_text

        reply = (
            f"{task_emoji} <b>{task_label} — معدّل ({label})</b>\n"
            f"{'─' * 20}\n\n"
            f"{ai_text}"
        )
        await status_msg.edit_text(reply, parse_mode="HTML", reply_markup=_action_keyboard())

    except Exception as e:
        logger.error(f"Caption edit error: {e}")
        await status_msg.edit_text("❌ حدث خطأ أثناء التعديل. حاول مجدداً.")

    return CaptionState.EDITING

# ─── Entry point ──────────────────────────────────────────────────────────────────────
async def handle_settings_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        err_text = escape_html(str(context.error)[:1500]) if context.error else "خطأ غير معروف"
        kwargs = {
            "chat_id": config.ADMIN_CHAT_ID,
            "text": f"⚠️ <b>خطأ تقني في البوت:</b>\n<code>{err_text}</code>",
            "parse_mode": "HTML"
        }
        if config.ADMIN_TOPIC_ID:
            kwargs["message_thread_id"] = config.ADMIN_TOPIC_ID
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Error notifying admin about failure: {e}")


if __name__ == "__main__":
    from controllers.router import main

    main()
