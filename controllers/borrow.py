"""Equipment borrow request conversation flow and validation."""

import re
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from constants import State, BORROW_ITEMS, BORROW_ITEM_STOCK
from models import database as db_app
from views import keyboards as kb
from views.formatting import (
    escape_html,
    format_date_ar,
    is_valid_phone,
    clean_phone_number,
)
from views.messages import _build_borrow_request_details
from controllers.common import is_supervisor

logger = logging.getLogger("bot.borrow")


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
    start_d = data.get('start_date') or data.get('return_date', '')
    start_t = data.get('start_time', '')
    start_disp = format_date_ar(start_d) + (f" ({escape_html(start_t)})" if start_t else "")
    ret_disp = format_date_ar(data.get('return_date', '')) + (f" ({escape_html(data.get('return_time', ''))})" if data.get('return_time') else "")

    msg = (
        "📦 <b>ملخص طلب استعارة غرض</b>\n\n"
        f"• <b>الغرض:</b> {escape_html(data.get('item', ''))}\n"
        f"• <b>العدد:</b> {escape_html(str(data.get('borrow_qty') or 1))}\n"
        f"• <b>اسم المستعير:</b> {escape_html(data.get('borrower_name', ''))}\n"
        f"• <b>السبب:</b> {escape_html(data.get('reason', ''))}\n"
        f"• <b>رقم التواصل:</b> {escape_html(data.get('phone', ''))}\n"
        f"• <b>موعد الاستلام:</b> {start_disp}\n"
        f"• <b>موعد الإرجاع:</b> {ret_disp}\n\n"
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


async def _check_borrow_availability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    item = context.user_data.get("item", "")
    qty = int(context.user_data.get("borrow_qty") or 1)
    stock_map = await db_app.get_borrow_item_stock()
    stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
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


async def borrow_set_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        from controllers.coverage import start
        return await start(update, context)

    borrow_items = await db_app.get_borrow_items()
    if text in borrow_items or text in BORROW_ITEMS:
        item = text
    else:
        await update.message.reply_text(
            "⚠️ الرجاء اختيار غرض من القائمة أدناه:",
            reply_markup=kb.get_reply_keyboard(borrow_items, with_back=False, with_cancel_only=True),
            parse_mode="HTML"
        )
        return State.BORROW_ITEM

    context.user_data["item"] = item
    context.user_data["user_id"] = update.effective_user.id
    context.user_data.pop("borrow_qty", None)

    stock_map = await db_app.get_borrow_item_stock()
    stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
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


async def borrow_set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        borrow_items = await db_app.get_borrow_items()
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(borrow_items, with_back=False, with_cancel_only=True),
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
    stock_map = await db_app.get_borrow_item_stock()
    stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
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
        borrow_items = await db_app.get_borrow_items()
        await update.message.reply_text(
            "📦 <b>استعارة أغراض</b>\n\nاختر الغرض الذي تريد استعارته:",
            reply_markup=kb.get_reply_keyboard(borrow_items, with_back=False, with_cancel_only=True),
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

    if not is_valid_phone(text):
        await update.message.reply_text(
            "⚠️ <b>رقم التواصل غير صحيح!</b>\nيرجى إدخال رقم هاتف صالح يتكون من 8 إلى 15 رقماً (مثال: <code>0912345678</code> أو مع الرمز الدولي):",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_PHONE

    context.user_data["phone"] = clean_phone_number(text)
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION

    await update.message.reply_text(
        "📅 <b>متى ترغب في استلام الغرض؟</b>\n\n"
        "اختر الاستلام اليوم، أو حدد موعداً آخر من التقويم:",
        reply_markup=kb.borrow_pickup_choice_keyboard(),
        parse_mode="HTML"
    )
    return State.BORROW_START_DATE


async def handle_borrow_start_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    now = datetime.now(timezone(timedelta(hours=3)))
    today_str = now.strftime("%Y-%m-%d")

    if data == "bpick_today":
        context.user_data["start_date"] = today_str
        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        await query.edit_message_text(
            f"✅ <b>تاريخ الاستلام:</b> {format_date_ar(today_str)} (اليوم)\n\n"
            "⏰ اختر <b>وقت الاستلام</b> التقريبي:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="bstart_time_pick_"),
            parse_mode="HTML"
        )
        return State.BORROW_START_TIME

    if data == "bpick_custom":
        await query.edit_message_text(
            "📅 <b>اختر تاريخ الاستلام</b> من التقويم أدناه:\n"
            "أو اكتبه يدوياً بصيغة (يوم/شهر/سنة):",
            reply_markup=kb.generate_calendar_keyboard(now.year, now.month, prefix="bstart_cal_"),
            parse_mode="HTML"
        )
        return State.BORROW_START_DATE

    if data == "bpick_back":
        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        await query.edit_message_text(
            "يرجى إدخال <b>رقم التواصل</b>:",
            reply_markup=kb.get_reply_keyboard([], with_back=True),
            parse_mode="HTML"
        )
        return State.BORROW_PHONE

    if data == "bpick_cancel":
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء العملية.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="القائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    if data == "bstart_cal_ignore":
        return State.BORROW_START_DATE

    if data.startswith("bstart_cal_prev_") or data.startswith("bstart_cal_next_"):
        ym = data.split("_", 3)[3]
        year, month = int(ym[:4]), int(ym[5:])
        await query.edit_message_reply_markup(
            reply_markup=kb.generate_calendar_keyboard(year, month, prefix="bstart_cal_")
        )
        return State.BORROW_START_DATE

    if data.startswith("bstart_cal_select_"):
        date_str = data.replace("bstart_cal_select_", "")
        if date_str < today_str:
            await query.answer("⚠️ لا يمكن اختيار تاريخ استلام في الماضي!", show_alert=True)
            return State.BORROW_START_DATE
        context.user_data["start_date"] = date_str
        display = format_date_ar(date_str)
        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION
        await query.edit_message_text(
            f"✅ <b>تم اختيار تاريخ الاستلام:</b> {display}\n\n"
            "⏰ الآن اختر <b>وقت الاستلام</b> التقريبي:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="bstart_time_pick_"),
            parse_mode="HTML"
        )
        return State.BORROW_START_TIME

    if data == "bstart_cal_back":
        await query.edit_message_text(
            "📅 <b>متى ترغب في استلام الغرض؟</b>",
            reply_markup=kb.borrow_pickup_choice_keyboard(),
            parse_mode="HTML"
        )
        return State.BORROW_START_DATE

    if data == "bstart_cal_cancel":
        is_admin = await is_supervisor(update.effective_user.id)
        await query.edit_message_text("تم إلغاء العملية.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="القائمة الرئيسية:",
            reply_markup=kb.main_menu_keyboard(is_admin)
        )
        return State.MENU

    return State.BORROW_START_DATE


async def borrow_set_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                "⚠️ لا يمكن اختيار تاريخ استلام في الماضي.\n"
                "يرجى إدخال تاريخ اليوم أو تاريخ مستقبلي:",
                parse_mode="HTML"
            )
            return State.BORROW_START_DATE
        display = format_date_ar(date_str)
    else:
        date_str = text
        display = text

    context.user_data["start_date"] = date_str
    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION

    await update.message.reply_text(
        f"✅ <b>تم تسجيل تاريخ الاستلام:</b> {display}\n\n"
        "⏰ الآن اختر <b>وقت الاستلام</b> التقريبي:",
        reply_markup=kb.generate_time_picker_keyboard(prefix="bstart_time_pick_"),
        parse_mode="HTML"
    )
    return State.BORROW_START_TIME


async def handle_borrow_start_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("bstart_time_pick_"):
        time_val = data.replace("bstart_time_pick_", "")
        if time_val == "custom":
            await query.edit_message_text(
                "✏️ يرجى إدخال <b>وقت الاستلام</b> يدوياً\n"
                "مثال: <code>10:00 صباحاً</code> أو <code>14:30</code>",
                parse_mode="HTML"
            )
            context.user_data["awaiting_borrow_custom_start_time"] = True
            return State.BORROW_START_TIME
        elif time_val == "back":
            await query.edit_message_text(
                "📅 <b>متى ترغب في استلام الغرض؟</b>",
                reply_markup=kb.borrow_pickup_choice_keyboard(),
                parse_mode="HTML"
            )
            return State.BORROW_START_DATE
        elif time_val == "cancel":
            return await borrow_cancel(update, context)

        context.user_data["start_time"] = time_val
        context.user_data.pop("awaiting_borrow_custom_start_time", None)

        if context.user_data.get("borrow_return_to_summary"):
            await send_borrow_summary(update, context)
            return State.BORROW_CONFIRMATION

        now = datetime.now()
        start_d = context.user_data.get("start_date", "")
        start_disp = format_date_ar(start_d) if start_d else "اليوم"

        await query.edit_message_text(
            f"✅ <b>موعد الاستلام:</b> {start_disp} الساعة {escape_html(time_val)}\n\n"
            "📅 الآن <b>اختر تاريخ الإرجاع</b> من التقويم أدناه:\n"
            "أو اكتب التاريخ يدوياً بصيغة (يوم/شهر/سنة):",
            reply_markup=kb.generate_calendar_keyboard(now.year, now.month, prefix="bcal_"),
            parse_mode="HTML"
        )
        return State.BORROW_RETURN_DATE

    return State.BORROW_START_TIME


async def borrow_set_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back = await borrow_edit_back_cancel(update, context)
    if back is not None:
        return back
    text = update.message.text
    if "إلغاء" in text:
        return await borrow_cancel(update, context)
    if "رجوع" in text:
        await update.message.reply_text(
            "📅 <b>متى ترغب في استلام الغرض؟</b>",
            reply_markup=kb.borrow_pickup_choice_keyboard(),
            parse_mode="HTML"
        )
        return State.BORROW_START_DATE

    context.user_data["start_time"] = text
    context.user_data.pop("awaiting_borrow_custom_start_time", None)

    if context.user_data.get("borrow_return_to_summary"):
        await send_borrow_summary(update, context)
        return State.BORROW_CONFIRMATION

    now = datetime.now()
    start_d = context.user_data.get("start_date", "")
    start_disp = format_date_ar(start_d) if start_d else "اليوم"

    await update.message.reply_text(
        f"✅ <b>موعد الاستلام:</b> {start_disp} الساعة {escape_html(text)}\n\n"
        "📅 الآن <b>اختر تاريخ الإرجاع</b> من التقويم أدناه:\n"
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
        start_date = context.user_data.get("start_date") or today_str
        if date_str < today_str:
            await query.answer("⚠️ لا يمكن اختيار تاريخ إرجاع في الماضي!", show_alert=True)
            return State.BORROW_RETURN_DATE
        if date_str < start_date:
            await query.answer(f"⚠️ تاريخ الإرجاع لا يمكن أن يكون قبل تاريخ الاستلام ({format_date_ar(start_date)})!", show_alert=True)
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
            "⏰ اختر <b>وقت الاستلام</b>:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="bstart_time_pick_"),
            parse_mode="HTML"
        )
        return State.BORROW_START_TIME

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
            "⏰ اختر <b>وقت الاستلام</b>:",
            reply_markup=kb.generate_time_picker_keyboard(prefix="bstart_time_pick_"),
            parse_mode="HTML"
        )
        return State.BORROW_START_TIME

    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        dd, mm, yyyy = match.groups()
        date_str = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")
        start_date = context.user_data.get("start_date") or today_str
        if date_str < today_str:
            await update.message.reply_text(
                "⚠️ لا يمكن اختيار تاريخ إرجاع في الماضي.\n"
                "يرجى إدخال تاريخ اليوم أو تاريخ مستقبلي:",
                parse_mode="HTML"
            )
            return State.BORROW_RETURN_DATE
        if date_str < start_date:
            await update.message.reply_text(
                f"⚠️ تاريخ الإرجاع لا يمكن أن يكون قبل تاريخ الاستلام (<b>{escape_html(format_date_ar(start_date))}</b>).\n"
                "يرجى إدخال تاريخ إرجاع صالح:",
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
            borrow_items = await db_app.get_borrow_items()
            await context.bot.send_message(
                chat_id, "📦 اختر الغرض:",
                reply_markup=kb.get_reply_keyboard(borrow_items, with_back=False, with_cancel_only=True),
                parse_mode="HTML"
            )
            return State.BORROW_ITEM
        if field == "quantity":
            item = context.user_data.get("item", "")
            stock_map = await db_app.get_borrow_item_stock()
            stock = stock_map.get(item, BORROW_ITEM_STOCK.get(item, 1))
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
        if field == "start_date":
            await context.bot.send_message(
                chat_id, "📅 <b>اختر موعد الاستلام:</b>",
                reply_markup=kb.borrow_pickup_choice_keyboard(),
                parse_mode="HTML"
            )
            return State.BORROW_START_DATE
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
        req_id = await db_app.generate_next_request_id()
        borrow_data = {
            "id": req_id,
            "user_id": d.get("user_id", update.effective_user.id),
            "item": d.get("item", ""),
            "borrow_qty": int(d.get("borrow_qty") or 1),
            "reason": d.get("reason", ""),
            "borrower_name": d.get("borrower_name", ""),
            "phone": d.get("phone", ""),
            "start_date": d.get("start_date") or d.get("return_date", ""),
            "start_time": d.get("start_time", ""),
            "return_date": d.get("return_date", ""),
            "return_time": d.get("return_time", ""),
        }
        success = await db_app.save_borrow_request(borrow_data)
    except Exception as e:
        logger.error(f"Error saving borrow request: {e}")

    if success:
        # حذف اللوحة القديمة قبل إرسال رسالة التفاصيل
        old_board_id = await db_app.get_setting("borrow_board_msg_id")
        if old_board_id:
            try:
                await context.bot.delete_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    message_id=int(old_board_id)
                )
            except Exception:
                pass
            await db_app.set_setting("borrow_board_msg_id", "")
        try:
            fresh = await db_app.get_request_by_id(req_id)
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
