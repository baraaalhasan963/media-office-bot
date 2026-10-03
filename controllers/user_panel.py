"""User request management, status tracking, pagination, and editing."""

import json
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from constants import State
from models import database as db_app
from views import keyboards as kb
from views.formatting import (
    escape_html,
    format_date_ar,
    get_request_state_label,
    build_admin_button_label_with_status,
)
from controllers.coverage import send_summary

logger = logging.getLogger("bot.user_panel")

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
    """بناء صفحة الطلبات السابقة للمستخدم مع الترقيم وأزرار الفلترة."""
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


async def handle_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة جميع نقرات الأزرار الخاصة بقائمة طلبات المستخدم العادي وتعديلها."""
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
                req = dict(req)

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
                can_extend = is_borrow and req['status'] == 'مقبول' and (req.get('extension_status') != 'معلق')

                if is_borrow:
                    start_d = req['start_date'] if 'start_date' in req.keys() and req['start_date'] else req['date']
                    start_t = req['start_time'] if 'start_time' in req.keys() and req['start_time'] else ""
                    start_str = format_date_ar(start_d) + (f" ({escape_html(start_t)})" if start_t else "")
                    ret_str = format_date_ar(req['date']) + (f" ({escape_html(req['time'])})" if req['time'] else "")
                    unit_str = f"• <b>القطعة المخصصة:</b> {escape_html(req['asset_unit_id'])}\n" if req.get('asset_unit_id') else ""
                    insp_str = f"• <b>نتيجة الفحص عند الإرجاع:</b> {escape_html(req['inspection_note'])}\n" if req.get('inspection_note') else ""
                    ext_status = req.get('extension_status')
                    ext_str = ""
                    if ext_status == 'معلق':
                        ext_str = f"⏳ <b>طلب تمديد قيد المراجعة إلى:</b> {format_date_ar(req.get('extension_date', ''))}\n"
                    elif ext_status == 'مقبول':
                        ext_str = "✅ <b>تم تمديد هذا الطلب سابقاً.</b>\n"

                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
                        f"{unit_str}"
                        f"• <b>العدد:</b> {escape_html(str(req['borrow_qty'] or 1))}\n"
                        f"• <b>اسم المستعير:</b> {escape_html(req['contact_name'] or '')}\n"
                        f"• <b>موعد الاستلام:</b> {start_str}\n"
                        f"• <b>موعد الإرجاع:</b> {ret_str}\n"
                        f"{insp_str}"
                        f"{ext_str}"
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
                    reply_markup=kb.user_request_action_keyboard(req['id'], req['status'], role, back_data=back_data, allow_edit=not is_borrow, is_borrow=is_borrow, can_extend=can_extend),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error viewing request detail: {e}")
                await query.edit_message_text("❌ حدث خطأ أثناء عرض تفاصيل الطلب.")
        return State.MENU

    if data.startswith("user_extend_"):
        req_id = data.replace("user_extend_", "")
        req = await db_app.get_request_by_id(req_id)
        if not req or req['status'] != 'مقبول':
            await query.answer("⚠️ لا يمكن تمديد هذا الطلب حالياً.", show_alert=True)
            return State.MENU
        req = dict(req)
        await query.edit_message_text(
            f"⏱️ <b>طلب تمديد فترة الاستعارة</b>\n\n"
            f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
            f"• <b>موعد الإرجاع الحالي:</b> {format_date_ar(req['date'])}\n\n"
            "اختر المدة التي ترغب بتمديدها:",
            reply_markup=kb.user_extension_options_keyboard(req_id),
            parse_mode="HTML"
        )
        return State.MENU

    if data.startswith("uext_quick_"):
        parts = data.split("_")
        req_id = parts[2]
        days = int(parts[3])
        req = await db_app.get_request_by_id(req_id)
        if not req:
            await query.answer("❌ لم يتم العثور على الطلب.", show_alert=True)
            return State.MENU
        req = dict(req)
        try:
            cur_dt = datetime.strptime(req['date'], "%Y-%m-%d")
            new_dt = cur_dt + timedelta(days=days)
            new_date_str = new_dt.strftime("%Y-%m-%d")
            await db_app.request_borrow_extension(req_id, new_date_str, req['time'] or "", f"تمديد سريع (+{days} أيام)")

            # Notify user
            await query.edit_message_text(
                f"✅ <b>تم إرسال طلب تمديد الاستعارة</b> بنجاح!\n\n"
                f"• الغرض: <b>{escape_html(req['event_name'])}</b>\n"
                f"• الموعد الجديد المقترح: <b>{format_date_ar(new_date_str)}</b>\n\n"
                "سيتم إشعارك بقرار الإدارة فوراً.",
                parse_mode="HTML"
            )

            # Alert admin group
            import config
            admin_msg = (
                "⏱️ <b>طلب تمديد فترة استعارة عتاد</b>\n\n"
                f"• <b>رقم الطلب:</b> <code>{req_id}</code>\n"
                f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
                f"• <b>المستعير:</b> {escape_html(req['contact_name'] or '')} (@{escape_html(query.from_user.username or 'بدون')})\n"
                f"• <b>موعد الإرجاع الحالي:</b> {format_date_ar(req['date'])}\n"
                f"• <b>الموعد المطلوب الجديد:</b> <b>{format_date_ar(new_date_str)}</b> (+{days} يوم)\n"
            )
            admin_kwargs = {
                "chat_id": config.ADMIN_CHAT_ID,
                "text": admin_msg,
                "reply_markup": kb.admin_extension_decision_keyboard(req_id),
                "parse_mode": "HTML"
            }
            topic = config.BORROW_TOPIC_ID or config.ADMIN_TOPIC_ID
            if topic is not None:
                admin_kwargs["message_thread_id"] = topic
            await context.bot.send_message(**admin_kwargs)
        except Exception as e:
            logger.error(f"Error submitting extension: {e}")
            await query.answer("❌ حدث خطأ أثناء إرسال طلب التمديد.", show_alert=True)
        return State.MENU

    if data.startswith("user_delete_"):
        req_id = data.replace("user_delete_", "")
        try:
            await db_app.delete_request(req_id, user_id=update.effective_user.id)
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
                can_extend = is_borrow and req['status'] == 'مقبول' and (req.get('extension_status') != 'معلق')

                if is_borrow:
                    start_d = req['start_date'] if 'start_date' in req.keys() and req['start_date'] else req['date']
                    start_t = req['start_time'] if 'start_time' in req.keys() and req['start_time'] else ""
                    start_str = format_date_ar(start_d) + (f" ({escape_html(start_t)})" if start_t else "")
                    ret_str = format_date_ar(req['date']) + (f" ({escape_html(req['time'])})" if req['time'] else "")
                    unit_str = f"• <b>القطعة المخصصة:</b> {escape_html(req['asset_unit_id'])}\n" if req.get('asset_unit_id') else ""
                    insp_str = f"• <b>نتيجة الفحص عند الإرجاع:</b> {escape_html(req['inspection_note'])}\n" if req.get('inspection_note') else ""
                    ext_status = req.get('extension_status')
                    ext_str = ""
                    if ext_status == 'معلق':
                        ext_str = f"⏳ <b>طلب تمديد قيد المراجعة إلى:</b> {format_date_ar(req.get('extension_date', ''))}\n"
                    elif ext_status == 'مقبول':
                        ext_str = "✅ <b>تم تمديد هذا الطلب سابقاً.</b>\n"

                    msg = (
                        f"• <b>طلب رقم:</b> <code>{req['id']}</code>\n"
                        f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
                        f"{unit_str}"
                        f"• <b>العدد:</b> {escape_html(str(req['borrow_qty'] or 1))}\n"
                        f"• <b>اسم المستعير:</b> {escape_html(req['contact_name'] or '')}\n"
                        f"• <b>موعد الاستلام:</b> {start_str}\n"
                        f"• <b>موعد الإرجاع:</b> {ret_str}\n"
                        f"{insp_str}"
                        f"{ext_str}"
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
                    reply_markup=kb.user_request_action_keyboard(req['id'], req['status'], role, allow_edit=not is_borrow, is_borrow=is_borrow, can_extend=can_extend),
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
            except Exception:
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
