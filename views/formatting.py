import html
import json
import logging
from datetime import datetime, time as dt_time, timezone, timedelta

logger = logging.getLogger(__name__)

TOTAL_STEPS = 13


def _progress_bar(step: int, total: int, length: int = 13) -> str:
    filled = round(step / total * length) if total else 0
    filled = max(0, min(filled, length))
    bar = "█" * filled + "░" * (length - filled)
    return f"<code>{bar}</code>"


def step_header(step: int) -> str:
    bar = _progress_bar(step, TOTAL_STEPS)
    return f"{bar} <b>{step}/{TOTAL_STEPS}</b>\n"


def escape_html(text):
    return html.escape(str(text))


def format_coverage_type(coverage_val):
    if not coverage_val:
        return ""
    try:
        data = json.loads(coverage_val)
        if isinstance(data, list):
            return ", ".join(data)
    except Exception:
        pass
    return str(coverage_val)


def format_date_ar(date_str: str) -> str:
    """Convert YYYY-MM-DD  →  '21 أبريل 2026'"""
    AR_MONTHS = [
        'يناير','فبراير','مارس','أبريل','مايو','يونيو',
        'يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'
    ]
    try:
        year, month, day = date_str.split("-")
        return f"{int(day)} {AR_MONTHS[int(month)-1]} {year}"
    except Exception:
        return date_str


def parse_ar_time(time_str):
    """تحويل الوقت العربي (10:00 ص) إلى كائن time للمقارنة"""
    try:
        if not time_str: return None
        parts = time_str.split()
        if len(parts) < 2:
            return datetime.strptime(parts[0], "%H:%M").time()

        h_m = parts[0].split(':')
        hour = int(h_m[0])
        minute = int(h_m[1]) if len(h_m) > 1 else 0
        period = parts[1]

        if period == 'م' and hour != 12: hour += 12
        elif period == 'ص' and hour == 12: hour = 0

        return dt_time(hour, minute)
    except Exception:
        return None


def get_request_state_label(req_date_str, start_time_str, end_time_str):
    """حساب حالة الطلب (لم يبدأ بعد، جاري الآن، منتهٍ)"""
    try:
        now = datetime.now(timezone(timedelta(hours=3)))
        today_str = now.strftime("%Y-%m-%d")

        if req_date_str < today_str:
            return "منتهٍ"
        elif req_date_str > today_str:
            return "لم يبدأ بعد"

        start_time = parse_ar_time(start_time_str)
        end_time = parse_ar_time(end_time_str)

        if not start_time:
            return "غير محدد"

        now_time = now.time()

        if now_time < start_time:
            return "لم يبدأ بعد"

        if end_time:
            if now_time > end_time:
                return "منتهٍ"
            else:
                return "جاري الآن"
        else:
            start_dt = datetime.combine(now.date(), start_time)
            if (datetime.combine(now.date(), now_time) - start_dt).total_seconds() > 7200:
                return "منتهٍ"
            else:
                return "جاري الآن"
    except Exception as e:
        logger.error(f"Error calculating request state: {e}")
        return "غير محدد"


def _truncate_text(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def admin_short_date(date_str: str, today_str: str, tomorrow_str: str) -> str:
    if date_str == today_str:
        return "اليوم"
    if date_str == tomorrow_str:
        return "غداً"
    try:
        year, month, day = date_str.split("-")
        return f"{day}/{month}"
    except Exception:
        return date_str


def build_admin_button_label(req, today_str: str, tomorrow_str: str) -> str:
    max_len = 60
    date_label = admin_short_date(req['date'], today_str, tomorrow_str)
    time_val = req['time'] or ""
    event_name = req['event_name'] or ""

    prefix = f"{date_label} {time_val}".strip()
    if prefix:
        prefix = f"{prefix} | "

    if len(prefix) >= max_len:
        return _truncate_text(prefix.strip(), max_len)

    event_max = max_len - len(prefix)
    return f"{prefix}{_truncate_text(event_name, event_max)}".strip()


def admin_status_symbol(status: str) -> str:
    if status == "مقبول":
        return "✅"
    if status == "مرفوض":
        return "❌"
    if status == "مُرجَع":
        return "↩️"
    return "⏳"


def build_admin_button_label_with_status(req, today_str: str, tomorrow_str: str) -> str:
    base = build_admin_button_label(req, today_str, tomorrow_str)
    try:
        status_val = req["status"]
    except (KeyError, IndexError, TypeError):
        status_val = ""
    symbol = admin_status_symbol(status_val)
    return f"{symbol} {base}".strip()


def build_time_conflicts(rows) -> set:
    counts = {}
    for r in rows:
        try:
            date_val = r["date"]
            time_val = r["time"]
        except (KeyError, IndexError, TypeError):
            continue
        if not date_val or not time_val:
            continue
        key = (date_val, time_val)
        counts[key] = counts.get(key, 0) + 1
    return {k for k, v in counts.items() if v > 1}