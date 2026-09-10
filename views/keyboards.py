from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from constants import DEPARTMENTS, IMPORTANCE_LEVELS
from datetime import datetime
import calendar as cal_module

# Arabic month names
AR_MONTHS = [
    'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'
]

def main_menu_keyboard(is_admin=False):
    keyboard = [
        ["📋 طلباتي السابقة", "📝 طلب تغطية إعلامية"],
        ["📦 استعارة أغراض"]
    ]
    if is_admin:
        keyboard.append(["🛠️ لوحة تحكم المشرفين"])
    keyboard.append(["📖 تعليمات الاستخدام"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def get_reply_keyboard(options, with_back=True, with_cancel_only=False):
    keyboard = []
    if options == IMPORTANCE_LEVELS:
        keyboard = [[opt] for opt in options]
    else:
        for i in range(0, len(options), 2):
            if i + 1 < len(options):
                keyboard.append([options[i+1], options[i]])
            else:
                keyboard.append([options[i]])
    if with_back:
        keyboard.append(["❌ إلغاء", "◀️ رجوع"])
    elif with_cancel_only:
        keyboard.append(["❌ إلغاء"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

async def coverage_selection_menu(selected_list):
    from models import database as db_app
    options = await db_app.get_coverage_options()
    keyboard = []
    for option in options:
        status = "✅ " if option in selected_list else "⬜ "
        keyboard.append([InlineKeyboardButton(f"{status}{option}", callback_data=f"toggle_{option}")])
    keyboard.append([InlineKeyboardButton("✔️ تم الاختيار - متابعة", callback_data="coverage_done")])
    return InlineKeyboardMarkup(keyboard)

def confirmation_edit_keyboard():
    keyboard = [
        [InlineKeyboardButton("✏️ القسم", callback_data="edit_department"), InlineKeyboardButton("✏️ الحدث", callback_data="edit_event_name")],
        [InlineKeyboardButton("✏️ نوع الحدث", callback_data="edit_event_type"), InlineKeyboardButton("✏️ الهدف", callback_data="edit_objective")],
        [InlineKeyboardButton("✏️ التاريخ", callback_data="edit_date"), InlineKeyboardButton("✏️ وقت البدء", callback_data="edit_time")],
        [InlineKeyboardButton("✏️ وقت الانتهاء", callback_data="edit_end_time"), InlineKeyboardButton("✏️ المكان", callback_data="edit_location")],
        [InlineKeyboardButton("✏️ التغطية", callback_data="edit_coverage"), InlineKeyboardButton("✏️ الأهمية", callback_data="edit_importance")],
        [InlineKeyboardButton("✏️ جهات خارجية", callback_data="edit_external_media"), InlineKeyboardButton("✏️ الملاحظات", callback_data="edit_notes")],
        [InlineKeyboardButton("✅ تأكيد وإرسال", callback_data="confirm_request")],
        [InlineKeyboardButton("❌ إلغاء الطلب", callback_data="cancel_request")]
    ]
    return InlineKeyboardMarkup(keyboard)

def user_request_action_keyboard(req_id, status, role="مستخدم عادي", back_data: str = None, allow_edit: bool = True):
    keyboard = []
    if status == 'معلق' and allow_edit:
        keyboard.append([InlineKeyboardButton("✏️ تعديل الطلب", callback_data=f"user_edit_{req_id}")])
    keyboard.append([InlineKeyboardButton("🗑️ حذف الطلب", callback_data=f"user_delete_{req_id}")])
    if back_data:
        keyboard.append([InlineKeyboardButton("🔙 رجوع للقائمة", callback_data=back_data)])
    return InlineKeyboardMarkup(keyboard)

def admin_approval_keyboard(req_id):
    keyboard = [[
        InlineKeyboardButton("🗑️ حذف", callback_data=f"admin_delete_{req_id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{req_id}"),
        InlineKeyboardButton("✅ موافقة", callback_data=f"admin_approve_{req_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

def admin_dashboard_keyboard(role="مستخدم عادي"):
    keyboard = []
    keyboard.append([
        InlineKeyboardButton("⏳ الطلبات المعلقة", callback_data="admin_pending"),
        InlineKeyboardButton("🔜 الطلبات القادمة", callback_data="admin_upcoming")
    ])
    keyboard.append([
        InlineKeyboardButton("⏪ الطلبات الفائتة", callback_data="admin_past"),
        InlineKeyboardButton("📁 حسب القسم", callback_data="admin_filter_dept_menu")
    ])
    keyboard.append([
        InlineKeyboardButton("⚡ فلاتر سريعة", callback_data="admin_quick_filters"),
        InlineKeyboardButton("📊 إحصائيات الشهر", callback_data="admin_month_stats")
    ])
    
    if role == "مشرف":
        keyboard.append([
            InlineKeyboardButton("📜 سجل العمليات", callback_data="admin_audit_logs")
        ])
        keyboard.append([
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="admin_settings_menu")
        ])
    
    keyboard.append([InlineKeyboardButton("🔴 إغلاق لوحة التحكم", callback_data="admin_close")])
    return InlineKeyboardMarkup(keyboard)

def admin_quick_filters_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 اليوم", callback_data="admin_filter_today")],
        [InlineKeyboardButton("📅 الغد", callback_data="admin_filter_tomorrow")],
        [InlineKeyboardButton("📆 هذا الأسبوع", callback_data="admin_filter_week")],
        [InlineKeyboardButton("🗓️ هذا الشهر", callback_data="admin_filter_month")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")]
    ])

def admin_back_keyboard():
    """Single back button to return to dashboard stats message."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")]])

def admin_dept_filter_keyboard(depts=None):
    if depts is None:
        depts = DEPARTMENTS
    keyboard = []
    for i in range(0, len(depts), 2):
        row = []
        if i + 1 < len(depts):
            row.append(InlineKeyboardButton(depts[i+1], callback_data=f"admin_vd_{i+1}"))
        row.append(InlineKeyboardButton(depts[i], callback_data=f"admin_vd_{i}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_dash_back")])
    return InlineKeyboardMarkup(keyboard)



# ─────────────────────────────────────────
# 📅 Inline Calendar
# ─────────────────────────────────────────
def generate_calendar_keyboard(year: int, month: int, prefix: str = "cal_") -> InlineKeyboardMarkup:
    """Generate a full month inline calendar keyboard."""
    # Navigation months
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    keyboard = []

    # Header: ◀ Month Year ▶
    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"{prefix}prev_{prev_year}-{prev_month:02d}"),
        InlineKeyboardButton(f"📅  {AR_MONTHS[month-1]}  {year}", callback_data=f"{prefix}ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}next_{next_year}-{next_month:02d}"),
    ])

    # Weekday headers: Sun → Sat (Right-to-left for Arabic)
    day_names = ["أح", "إث", "ثل", "أر", "خم", "جم", "سب"]
    keyboard.append([InlineKeyboardButton(d, callback_data=f"{prefix}ignore") for d in day_names])

    # Days grid  (Python calendar: Mon=0, Sun=6 → display Sun=0)
    first_weekday = cal_module.weekday(year, month, 1)   # Mon=0..Sun=6
    start_col = (first_weekday + 1) % 7                  # shift so Sun=0
    days_in_month = cal_module.monthrange(year, month)[1]
    today = datetime.now()

    row = [InlineKeyboardButton(" ", callback_data=f"{prefix}ignore")] * start_col
    for day in range(1, days_in_month + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        is_today = (day == today.day and month == today.month and year == today.year)
        label = f"[{day}]" if is_today else str(day)
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}select_{date_str}"))
        if len(row) == 7:
            keyboard.append(row)
            row = []
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}ignore"))
        keyboard.append(row)

    # Footer buttons
    keyboard.append([
        InlineKeyboardButton("◀️ رجوع", callback_data=f"{prefix}back"),
        InlineKeyboardButton("❌ إلغاء", callback_data=f"{prefix}cancel"),
    ])
    return InlineKeyboardMarkup(keyboard)

# ─────────────────────────────────────────
# ⏰ Inline Time Picker
# ─────────────────────────────────────────
def generate_time_picker_keyboard(prefix="time_pick_") -> InlineKeyboardMarkup:
    """Generate an inline time picker keyboard."""
    slot_rows = [
        ["07:00 ص", "08:00 ص", "09:00 ص"],
        ["10:00 ص", "11:00 ص", "12:00 م"],
        ["01:00 م", "02:00 م", "03:00 م"],
        ["04:00 م", "05:00 م", "06:00 م"],
        ["07:00 م", "08:00 م", "09:00 م"],
    ]
    keyboard = []
    for row_times in slot_rows:
        keyboard.append([
            InlineKeyboardButton(t, callback_data=f"{prefix}{t}") for t in row_times
        ])
    keyboard.append([InlineKeyboardButton("✏️ إدخال وقت مخصص", callback_data=f"{prefix}custom")])
    keyboard.append([
        InlineKeyboardButton("◀️ رجوع", callback_data=f"{prefix}back"),
        InlineKeyboardButton("❌ إلغاء", callback_data=f"{prefix}cancel"),
    ])
    return InlineKeyboardMarkup(keyboard)

# --- Dynamic Settings Keyboards ---
def settings_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🏢 إدارة الأقسام", callback_data="settings_depts")],
        [InlineKeyboardButton("📌 إدارة أنواع الأحداث", callback_data="settings_types")],
        [InlineKeyboardButton("🎥 إدارة خيارات التغطية", callback_data="settings_coverage")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_dash_back")]
    ]
    return InlineKeyboardMarkup(keyboard)

def settings_items_keyboard(items: list, prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for idx, item in enumerate(items):
        keyboard.append([
            InlineKeyboardButton(item, callback_data="ignore"),
            InlineKeyboardButton("🗑️ حذف", callback_data=f"settings_del_{prefix}_{idx}")
        ])
    keyboard.append([InlineKeyboardButton("➕ إضافة جديد", callback_data=f"settings_add_{prefix}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_settings_menu")])
    return InlineKeyboardMarkup(keyboard)

def borrow_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("✏️ الغرض", callback_data="bedit_item"), InlineKeyboardButton("✏️ اسم المستعير", callback_data="bedit_borrower")],
        [InlineKeyboardButton("✏️ العدد", callback_data="bedit_quantity"), InlineKeyboardButton("✏️ السبب", callback_data="bedit_reason")],
        [InlineKeyboardButton("✏️ رقم التواصل", callback_data="bedit_phone")],
        [InlineKeyboardButton("✏️ تاريخ الإرجاع", callback_data="bedit_return_date"), InlineKeyboardButton("✏️ وقت الإرجاع", callback_data="bedit_return_time")],
        [InlineKeyboardButton("✅ تأكيد وإرسال", callback_data="bconfirm_request")],
        [InlineKeyboardButton("❌ إلغاء الطلب", callback_data="bcancel_request")]
    ]
    return InlineKeyboardMarkup(keyboard)

def borrow_quantity_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["📦 قطعة واحدة", "📦 قطعتان"]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def borrow_responsibility_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، أتحمل المسؤولية", callback_data="bresp_yes")],
        [InlineKeyboardButton("❌ لا", callback_data="bresp_no")],
        [InlineKeyboardButton("🔙 رجوع للملخص", callback_data="bresp_back")],
    ])

