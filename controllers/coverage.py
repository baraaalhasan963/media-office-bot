"""Media coverage request conversation flow and menu handlers."""

import json
import re
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import config
from constants import State, IMPORTANCE_LEVELS, BORROW_ITEMS
from models import database as db_app
from views import keyboards as kb
from views.formatting import (
    step_header,
    escape_html,
    format_date_ar,
    parse_ar_time,
)
from controllers.common import is_supervisor

logger = logging.getLogger("bot.coverage")


# ─── Summary helpers ──────────────────────────────────────────────────────────
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


# ─── Start & Main Menu ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user

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
            from controllers.user_panel import build_user_requests_page
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
        borrow_items = await db_app.get_borrow_items()
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(borrow_items, with_back=False, with_cancel_only=True),
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
        from controllers.admin import admin_dashboard
        await admin_dashboard(update, context)
        return State.MENU

    return State.MENU


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = await is_supervisor(update.effective_user.id)
    await update.message.reply_text(
        "تم إلغاء العملية والعودة للقائمة الرئيسية.",
        reply_markup=kb.main_menu_keyboard(is_admin)
    )
    return State.MENU


# ─── Coverage Steps ───────────────────────────────────────────────────────────
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

    now = datetime.now()
    await update.message.reply_text(
        f"{step_header(5)}📅 <b>اختر تاريخ الحدث</b> من التقويم أدناه،\n"
        "أو اكتب التاريخ يدوياً بصيغة (يوم/شهر/سنة):",
        reply_markup=kb.generate_calendar_keyboard(now.year, now.month),
        parse_mode="HTML"
    )
    return State.DATE


async def handle_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cal_ignore":
        return State.DATE

    if data.startswith("cal_prev_") or data.startswith("cal_next_"):
        ym = data.split("_", 2)[2]
        year, month = int(ym[:4]), int(ym[5:])
        await query.edit_message_reply_markup(
            reply_markup=kb.generate_calendar_keyboard(year, month)
        )
        return State.DATE

    if data.startswith("cal_select_"):
        date_str = data.replace("cal_select_", "")
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

    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        dd, mm, yyyy = match.groups()
        date_str = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
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
        req_data = context.user_data
        req_data["status"] = "معلق"
        edit_req_id = req_data.pop("edit_req_id", None)

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
                    (req_data["department"], req_data["event_name"], req_data["event_type"], req_data["objective"],
                     req_data["date"], req_data["time"], req_data.get("end_time", ""),
                     db_app.time_to_24(req_data.get("time")), db_app.time_to_24(req_data.get("end_time")),
                     req_data["location"], json.dumps(req_data["coverage_type"], ensure_ascii=False),
                     req_data["importance"], req_data.get("external_media", "لا"), req_data.get("notes", ""),
                     req_id, req_data["user_id"])
                )
                await db_pool.commit()
            success = True
        else:
            req_id = await db_app.generate_next_request_id()
            req_data["id"] = req_id
            success = await db_app.save_request(req_data)

        if success:
            req_label = "تعديل طلب تغطية إعلامية موجود!" if edit_req_id else "📨 وصول طلب تغطية إعلامية جديد!"
            icon = "✏️" if edit_req_id else "📨"
            admin_msg = (
                f"<b>{icon} {req_label}</b>\n\n"
                f"• <b>رقم الطلب:</b> <code>{req_id}</code>\n"
                f"• <b>المرسل:</b> {escape_html(update.effective_user.full_name)} "
                f"(@{escape_html(update.effective_user.username) if update.effective_user.username else 'لا يوجد'}"
                f" | ID: {update.effective_user.id})\n"
                f"• <b>القسم:</b> {escape_html(req_data['department'])}\n\n"
                f"• <b>الحدث:</b> {escape_html(req_data['event_name'])} ({escape_html(req_data['event_type'])})\n"
                f"• <b>الهدف:</b> {escape_html(req_data['objective'])}\n"
                f"• <b>التاريخ:</b> {format_date_ar(req_data['date'])}\n"
                f"• <b>الوقت:</b> من {escape_html(req_data['time'])} إلى {escape_html(req_data['end_time'])}\n"
                f"• <b>المكان:</b> {escape_html(req_data['location'])}\n"
                f"• <b>نوع التغطية:</b> {escape_html(', '.join(req_data['coverage_type']))}\n"
                f"• <b>الأهمية:</b> {escape_html(req_data['importance'])}\n"
                f"• <b>جهات خارجية:</b> {escape_html(req_data.get('external_media', 'لا'))}\n\n"
                f"• <b>ملاحظات إضافية:</b>\n{escape_html(req_data['notes'])}\n"
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
                    text=f"طلب جديد من {req_data['department']} لحدث {req_data['event_name']}. (خطأ في التنسيق)"
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
