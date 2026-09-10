from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton

from models import database as db_app
from views.formatting import escape_html, format_coverage_type, format_date_ar


def _build_dashboard_text(total, pending, approved, rejected) -> str:
    """Build the main dashboard statistics message text."""
    now = datetime.now(timezone(timedelta(hours=3)))
    return (
        "🛠️ <b>لوحة تحكم المشرفين</b>\n"
        f"<i>آخر تحديث: {now.strftime('%H:%M')} — {format_date_ar(now.strftime('%Y-%m-%d'))}</i>\n\n"
        f"📊 <b>إحصائيات عامة:</b>\n"
        f"  • إجمالي الطلبات: <b>{total}</b>\n"
        f"  • ⏳ معلقة: <b>{pending}</b>\n"
        f"  • ✅ مقبولة: <b>{approved}</b>\n"
        f"  • ❌ مرفوضة: <b>{rejected}</b>\n\n"
        + ("⚠️ <b>يوجد طلبات معلقة تحتاج مراجعة!</b>" if pending > 0 else "✅ لا توجد طلبات معلقة.")
    )


async def _build_request_details_message(req, role: str):
    if req['request_type'] == 'استعارة':
        return await _build_borrow_request_details(req, role)

    status_label = "⏳ معلق"
    if req['status'] == 'مقبول':
        status_label = "✅ مقبول"
    elif req['status'] == 'مرفوض':
        status_label = "❌ مرفوض"

    external_media = req['external_media'] if req['external_media'] else "لا يوجد"
    notes = req['notes'] if req['notes'] else "لا يوجد"
    end_time = req['end_time'] if req['end_time'] else "غير محدد"
    sender_line = await _get_sender_line(req)

    msg = (
        "📋 <b>تفاصيل الطلب</b>\n\n"
        f"• <b>رقم الطلب:</b> <code>{req['id']}</code>\n"
        f"{sender_line}\n"
        f"• <b>القسم:</b> {escape_html(req['department'])}\n\n"
        f"• <b>الحدث:</b> {escape_html(req['event_name'])} ({escape_html(req['event_type'])})\n"
        f"• <b>الهدف:</b> {escape_html(req['objective'])}\n"
        f"• <b>التاريخ:</b> {format_date_ar(req['date'])}\n"
        f"• <b>الوقت:</b> من {escape_html(req['time'])} إلى {escape_html(end_time)}\n"
        f"• <b>المكان:</b> {escape_html(req['location'])}\n"
        f"• <b>نوع التغطية:</b> {escape_html(format_coverage_type(req['coverage_type']))}\n"
        f"• <b>الأهمية:</b> {escape_html(req['importance'])}\n"
        f"• <b>جهات خارجية:</b> {escape_html(external_media)}\n\n"
        f"• <b>ملاحظات إضافية:</b>\n{escape_html(notes)}\n\n"
        f"• <b>حالة الطلب:</b> {status_label}\n"
    )
    if req['admin_username']:
        msg += f"👤 <b>بواسطة المشرف:</b> @{req['admin_username']}\n"

    keyboard = []
    if role == "مشرف":
        if req['status'] == 'معلق':
            keyboard.append([
                InlineKeyboardButton("✅ موافقة", callback_data=f"admin_approve_{req['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{req['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🗑️ حذف", callback_data=f"admin_delete_{req['id']}")])

    return msg, keyboard


async def _build_borrow_request_details(req, role: str):
    status_label = "⏳ معلق"
    if req['status'] == 'مقبول':
        status_label = "✅ مقبول (مُستعار)"
    elif req['status'] == 'مرفوض':
        status_label = "❌ مرفوض"
    elif req['status'] == 'مُرجَع':
        status_label = "↩️ مُرجَع"

    sender_line = await _get_sender_line(req)

    msg = (
        "📦 <b>تفاصيل طلب استعارة غرض</b>\n\n"
        f"• <b>رقم الطلب:</b> <code>{req['id']}</code>\n"
        f"{sender_line}\n"
        f"• <b>الغرض:</b> {escape_html(req['event_name'])}\n"
        f"• <b>العدد:</b> {escape_html(str(req['borrow_qty'] or 1))}\n"
        f"• <b>اسم المستعير:</b> {escape_html(req['contact_name'] or '')}\n"
        f"• <b>السبب:</b> {escape_html(req['objective'] or '')}\n"
        f"• <b>رقم التواصل:</b> {escape_html(req['phone'] or '')}\n"
        f"• <b>تاريخ الإرجاع:</b> {format_date_ar(req['date'])}\n"
        f"• <b>وقت الإرجاع:</b> {escape_html(req['time'] or '')}\n"
        f"• <b>تحمل المسؤولية:</b> نعم\n\n"
        f"• <b>حالة الطلب:</b> {status_label}\n"
    )
    if req['admin_username']:
        msg += f"👤 <b>بواسطة المشرف:</b> @{req['admin_username']}\n"

    keyboard = []
    if role == "مشرف":
        if req['status'] == 'معلق':
            keyboard.append([
                InlineKeyboardButton("✅ موافقة", callback_data=f"admin_approve_{req['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{req['id']}")
            ])
        elif req['status'] == 'مقبول':
            keyboard.append([InlineKeyboardButton("↩️ تسجيل الإرجاع", callback_data=f"admin_return_{req['id']}")])
        keyboard.append([InlineKeyboardButton("🗑️ حذف", callback_data=f"admin_delete_{req['id']}")])

    return msg, keyboard


async def _get_sender_line(req):
    user_row = await db_app.get_user_by_id(req['user_id'])
    username = ""
    full_name = ""
    if user_row:
        username = user_row["username"] or ""
        full_name = user_row["full_name"] or ""

    if not full_name:
        full_name = req['contact_name'] or "غير متوفر"

    handle = username
    if not handle:
        telegram_user = req['telegram'] or ""
        if telegram_user.startswith("@"):  # normalize
            telegram_user = telegram_user[1:]
        handle = telegram_user

    handle_text = f"@{escape_html(handle)}" if handle else "لا يوجد"
    return f"• <b>المرسل:</b> {escape_html(full_name)} ({handle_text} | ID: {req['user_id']})"