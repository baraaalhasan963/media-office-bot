import aiosqlite
import config
import logging
import json
import re
import time as pytime
import asyncio
import contextlib

logger = logging.getLogger(__name__)

# --- Connection Pool ---
_db_connection = None
_db_lock = None

async def get_db() -> aiosqlite.Connection:
    global _db_connection, _db_lock
    if _db_connection is None:
        _db_connection = await aiosqlite.connect(config.DB_PATH)
        _db_connection.row_factory = aiosqlite.Row
        await _db_connection.execute("PRAGMA journal_mode=WAL;")
        busy_timeout = getattr(config, "SQLITE_BUSY_TIMEOUT", 15000)
        await _db_connection.execute(f"PRAGMA busy_timeout = {busy_timeout};")
        await _db_connection.execute("PRAGMA synchronous = NORMAL;")
        await _db_connection.execute("PRAGMA temp_store = MEMORY;")
        await _db_connection.execute("PRAGMA cache_size = -10000;")
        await _db_connection.execute("PRAGMA mmap_size = 30000000;")
        _db_lock = asyncio.Lock()
    return _db_connection


async def close_db():
    global _db_connection
    if _db_connection:
        await _db_connection.close()
        _db_connection = None

# --- Settings cache (short TTL) ---
_SETTINGS_CACHE = {}
_SETTINGS_TTL = 60

def _cache_get(key: str):
    entry = _SETTINGS_CACHE.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if pytime.monotonic() > expires_at:
        _SETTINGS_CACHE.pop(key, None)
        return None
    return value

def _cache_set(key: str, value):
    _SETTINGS_CACHE[key] = (pytime.monotonic() + _SETTINGS_TTL, value)

def _cache_invalidate(key: str):
    _SETTINGS_CACHE.pop(key, None)


def _normalize_period(token: str):
    if not token:
        return None
    t = token.strip().lower()
    if t.startswith("ص") or t in ["am", "a.m", "a.m."]:
        return "ص"
    if t.startswith("م") or t in ["pm", "p.m", "p.m."]:
        return "م"
    return None

def time_to_24(time_str: str) -> str:
    """Convert time strings to HH:MM (24h). Returns empty string on failure."""
    if not time_str:
        return ""
    raw = str(time_str).strip()
    match = re.search(r"(\d{1,2})(?::(\d{2}))?", raw)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)

    period = None
    parts = raw.split()
    if len(parts) > 1:
        period = _normalize_period(parts[1])
    if not period:
        if re.search(r"\b(am|a\.m\.?)+\b", raw, flags=re.IGNORECASE):
            period = "ص"
        elif re.search(r"\b(pm|p\.m\.?)+\b", raw, flags=re.IGNORECASE):
            period = "م"

    if period == "م" and hour != 12:
        hour += 12
    elif period == "ص" and hour == 12:
        hour = 0

    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"

def calculate_end_time(time_str):
    """حساب وقت الانتهاء ليكون بعد ساعتين من وقت البدء"""
    try:
        from datetime import timedelta, datetime
        parts = time_str.split()
        if len(parts) < 2: return time_str
        h_m = parts[0].split(':')
        hour = int(h_m[0])
        minute = int(h_m[1]) if len(h_m) > 1 else 0
        period = parts[1]
        
        if period == 'م' and hour != 12: hour += 12
        elif period == 'ص' and hour == 12: hour = 0
        
        dt = datetime(2000, 1, 1, hour, minute) + timedelta(hours=2)
        
        out_hour = dt.hour
        out_period = 'ص' if out_hour < 12 else 'م'
        if out_hour == 0: out_hour = 12
        elif out_hour > 12: out_hour -= 12
        
        return f"{out_hour:02d}:{dt.minute:02d} {out_period}"
    except:
        return time_str

async def _run_migrations(db):
    """نظام الترحيل (Migrations) — يتتبع الإصدارات المنفذة ويمنع تكرارها."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )

    async def is_applied(version: int) -> bool:
        async with db.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)) as c:
            return await c.fetchone() is not None

    async def mark_applied(version: int):
        await db.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (version,))

    # v1: Add new columns (legacy)
    if not await is_applied(1):
        for col, col_type in [("status", "TEXT DEFAULT 'معلق'"), ("admin_id", "INTEGER"),
                              ("admin_username", "TEXT"), ("external_media", "TEXT"), ("end_time", "TEXT"),
                              ("start_time_24", "TEXT"), ("end_time_24", "TEXT")]:
            try:
                await db.execute(f"ALTER TABLE requests ADD COLUMN {col} {col_type}")
            except aiosqlite.OperationalError:
                pass
        await mark_applied(1)

    # v2: Backfill end_time
    if not await is_applied(2):
        async with db.execute("SELECT id, time FROM requests WHERE end_time IS NULL OR end_time = ''") as cursor:
            for row in await cursor.fetchall():
                if row[1]:
                    await db.execute("UPDATE requests SET end_time = ? WHERE id = ?",
                                     (calculate_end_time(row[1]), row[0]))
        await mark_applied(2)

    # v3: Backfill 24h time columns
    if not await is_applied(3):
        async with db.execute("SELECT id, time, end_time, start_time_24, end_time_24 FROM requests") as cursor:
            for row in await cursor.fetchall():
                new_s24 = row[3] or time_to_24(row[1])
                new_e24 = row[4] or time_to_24(row[2])
                if new_s24 or new_e24:
                    await db.execute("UPDATE requests SET start_time_24 = ?, end_time_24 = ? WHERE id = ?",
                                     (new_s24, new_e24, row[0]))
        await mark_applied(3)

    # v4: Add request_type column to distinguish borrow requests
    if not await is_applied(4):
        try:
            await db.execute("ALTER TABLE requests ADD COLUMN request_type TEXT DEFAULT 'تغطية'")
        except aiosqlite.OperationalError:
            pass
        await mark_applied(4)

    # v5: Add budget quantity column for borrow requests (items with multiple copies)
    if not await is_applied(5):
        try:
            await db.execute("ALTER TABLE requests ADD COLUMN borrow_qty INTEGER DEFAULT 1")
        except aiosqlite.OperationalError:
            pass
        await mark_applied(5)

    # v6: Borrow lifecycle enhancements (start date/time, unit ID, inspection, extensions)
    if not await is_applied(6):
        new_cols = [
            ("start_date", "TEXT"),
            ("start_time", "TEXT"),
            ("asset_unit_id", "TEXT"),
            ("inspection_note", "TEXT"),
            ("extension_status", "TEXT"),
            ("extension_date", "TEXT"),
            ("extension_time", "TEXT"),
            ("extension_reason", "TEXT"),
        ]
        for col, col_type in new_cols:
            try:
                await db.execute(f"ALTER TABLE requests ADD COLUMN {col} {col_type}")
            except aiosqlite.OperationalError:
                pass
        await mark_applied(6)

async def init_db():
    db = await get_db()
    await db.execute("PRAGMA journal_mode=WAL;")
    
    await db.execute(
        """CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, user_id INTEGER, department TEXT, event_name TEXT,
            event_type TEXT, objective TEXT, date TEXT, time TEXT, location TEXT,
            coverage_type TEXT, importance TEXT, contact_name TEXT, phone TEXT,
            telegram TEXT, external_media TEXT, notes TEXT,
            status TEXT DEFAULT 'معلق', admin_id INTEGER, admin_username TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, end_time TEXT,
            start_time_24 TEXT, end_time_24 TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            admin_id INTEGER, admin_username TEXT, action TEXT, req_id TEXT, details TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"""
    )

    await _run_migrations(db)

    await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_user_id ON requests(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_date ON requests(date)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_department ON requests(department)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_date_start24 ON requests(date, start_time_24)")

    import constants
    default_settings = {
        "departments": json.dumps(constants.DEPARTMENTS, ensure_ascii=False),
        "event_types": json.dumps(constants.EVENT_TYPES, ensure_ascii=False),
        "coverage_options": json.dumps(constants.COVERAGE_OPTIONS, ensure_ascii=False),
        "importance_levels": json.dumps(constants.IMPORTANCE_LEVELS, ensure_ascii=False),
        "borrow_items": json.dumps(constants.BORROW_ITEMS, ensure_ascii=False),
        "borrow_item_stock": json.dumps(constants.BORROW_ITEM_STOCK, ensure_ascii=False),
    }
    for key, val in default_settings.items():
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    await db.commit()

async def save_request(data: dict, db: aiosqlite.Connection = None, commit: bool = True):
    try:
        if not data.get("end_time") and data.get("time"):
            data["end_time"] = calculate_end_time(data["time"])

        data["start_time_24"] = time_to_24(data.get("time"))
        data["end_time_24"] = time_to_24(data.get("end_time"))

        owns_db = False
        if db is None:
            db = await get_db()
            owns_db = True
        
        async with _db_lock:
            await db.execute(
                """INSERT INTO requests (
                    id, user_id, department, event_name, event_type, objective,
                    date, time, end_time, start_time_24, end_time_24, location,
                    coverage_type, importance, contact_name, phone, telegram,
                    external_media, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["id"], data["user_id"], data["department"], data["event_name"],
                    data["event_type"], data["objective"], data["date"], data["time"],
                    data.get("end_time", ""), data.get("start_time_24", ""),
                    data.get("end_time_24", ""), data["location"],
                    json.dumps(data["coverage_type"], ensure_ascii=False), data["importance"],
                    data["contact_name"], data["phone"], data["telegram"],
                    data.get("external_media", "لا"), data.get("notes", "")
                )
            )
            if commit or owns_db:
                await db.commit()

        try:
            with open("backup_requests.txt", "a", encoding="utf-8") as f:
                f.write(f"ID: {data['id']} | User: {data['user_id']} | Date: {data['date']} | Event: {data['event_name']}\n")
        except:
            pass
        return True
    except Exception as e:
        logger.error(f"Error saving request: {e}")
        return False

async def get_user_requests_count(user_id: int, status: str = None):
    sql = "SELECT COUNT(*) FROM requests WHERE user_id = ?"
    params = [user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    db = await get_db()
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0

async def save_borrow_request(data: dict, db: aiosqlite.Connection = None, commit: bool = True):
    try:
        owns_db = False
        if db is None:
            db = await get_db()
            owns_db = True

        async with _db_lock:
            await db.execute(
                """INSERT INTO requests (
                    id, user_id, department, event_name, event_type, objective,
                    date, time, end_time, start_time_24, end_time_24, location,
                    coverage_type, importance, contact_name, phone, telegram,
                    external_media, notes, request_type, status, borrow_qty,
                    start_date, start_time, asset_unit_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'معلق', ?, ?, ?, ?)""",
                (
                    data["id"], data["user_id"], "", data["item"], "", data["reason"],
                    data["return_date"], data["return_time"], "",
                    time_to_24(data.get("return_time")), "", "",
                    json.dumps([], ensure_ascii=False), "", data["borrower_name"],
                    data["phone"], "", "لا", "", "استعارة",
                    int(data.get("borrow_qty") or 1),
                    data.get("start_date") or data["return_date"],
                    data.get("start_time") or "",
                    data.get("asset_unit_id") or ""
                )
            )
            if commit or owns_db:
                await db.commit()

        try:
            with open("backup_requests.txt", "a", encoding="utf-8") as f:
                f.write(f"ID: {data['id']} | User: {data['user_id']} | Borrow: {data['item']} | Qty: {data.get('borrow_qty', 1)} | Start: {data.get('start_date', '')} | Return: {data['return_date']}\n")
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"Error saving borrow request: {e}")
        return False

async def get_approved_borrow_rows(item: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE request_type = 'استعارة' AND status = 'مقبول' AND event_name = ? "
        "ORDER BY date ASC, time ASC",
        (item,)
    ) as cursor:
        return await cursor.fetchall()

async def get_approved_borrow_qty(item: str):
    db = await get_db()
    async with db.execute(
        "SELECT COALESCE(SUM(borrow_qty), 0) FROM requests "
        "WHERE request_type = 'استعارة' AND status = 'مقبول' AND event_name = ?",
        (item,)
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

async def get_pending_borrows(item: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE request_type = 'استعارة' AND status = 'معلق' AND event_name = ? "
        "ORDER BY CAST(id AS INTEGER) ASC, timestamp ASC",
        (item,)
    ) as cursor:
        return await cursor.fetchall()

async def get_borrow_day_items(user_id: int, date: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE request_type = 'استعارة' AND user_id = ? AND date = ? "
        "AND status IN ('معلق', 'مقبول') ORDER BY CAST(id AS INTEGER) ASC",
        (user_id, date)
    ) as cursor:
        return await cursor.fetchall()

async def mark_request_returned(req_id: str):
    db = await get_db()
    async with _db_lock:
        await db.execute("UPDATE requests SET status = 'مُرجَع' WHERE id = ?", (req_id,))
        await db.commit()

async def mark_borrow_returned_with_inspection(req_id: str, inspection_note: str, admin_id: int = None, admin_username: str = None):
    db = await get_db()
    async with _db_lock:
        await db.execute(
            "UPDATE requests SET status = 'مُرجَع', inspection_note = ?, admin_id = ?, admin_username = ? WHERE id = ?",
            (inspection_note, admin_id, admin_username, req_id)
        )
        await db.commit()

async def get_occupied_units(item: str) -> set:
    db = await get_db()
    async with db.execute(
        "SELECT asset_unit_id FROM requests "
        "WHERE request_type = 'استعارة' AND status = 'مقبول' AND event_name = ? AND asset_unit_id IS NOT NULL AND asset_unit_id != ''",
        (item,)
    ) as cursor:
        rows = await cursor.fetchall()
        return {r[0] for r in rows if r[0]}

async def assign_borrow_asset_unit(req_id: str, unit_id: str):
    db = await get_db()
    async with _db_lock:
        await db.execute("UPDATE requests SET asset_unit_id = ? WHERE id = ?", (unit_id, req_id))
        await db.commit()

async def request_borrow_extension(req_id: str, new_date: str, new_time: str, reason: str):
    db = await get_db()
    async with _db_lock:
        await db.execute(
            "UPDATE requests SET extension_status = 'معلق', extension_date = ?, extension_time = ?, extension_reason = ? WHERE id = ?",
            (new_date, new_time, reason, req_id)
        )
        await db.commit()

async def resolve_borrow_extension(req_id: str, approved: bool, admin_id: int = None, admin_username: str = None):
    db = await get_db()
    async with _db_lock:
        if approved:
            async with db.execute("SELECT extension_date, extension_time FROM requests WHERE id = ?", (req_id,)) as c:
                row = await c.fetchone()
                ext_date = row[0] if row else None
                ext_time = row[1] if row else ""
            if ext_date:
                t24 = time_to_24(ext_time) if ext_time else ""
                await db.execute(
                    """UPDATE requests 
                       SET status = 'مقبول',
                           date = ?,
                           time = CASE WHEN ? != '' THEN ? ELSE time END,
                           start_time_24 = CASE WHEN ? != '' THEN ? ELSE start_time_24 END,
                           extension_status = 'مقبول',
                           admin_id = ?,
                           admin_username = ?
                       WHERE id = ?""",
                    (ext_date, ext_time, ext_time, t24, t24, admin_id, admin_username, req_id)
                )
        else:
            await db.execute(
                "UPDATE requests SET extension_status = 'مرفوض', admin_id = ?, admin_username = ? WHERE id = ?",
                (admin_id, admin_username, req_id)
            )
        await db.commit()

async def get_overdue_borrows(today_str: str, current_time_24: str = "23:59") -> list:
    db = await get_db()
    sql = """SELECT * FROM requests 
             WHERE request_type = 'استعارة' AND status = 'مقبول' 
               AND (date < ? OR (date = ? AND start_time_24 IS NOT NULL AND start_time_24 != '' AND start_time_24 < ?))
             ORDER BY date ASC, time ASC"""
    async with db.execute(sql, (today_str, today_str, current_time_24)) as cursor:
        return await cursor.fetchall()

async def get_user_requests_page(user_id: int, status: str = None, limit: int = 5, offset: int = 0):
    sql = "SELECT * FROM requests WHERE user_id = ?"
    params = [user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    db = await get_db()
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchall()

async def register_user(user_id: int, username: str, full_name: str):
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """INSERT INTO users (user_id, username, full_name, last_seen)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username, full_name=excluded.full_name,
                   last_seen=CURRENT_TIMESTAMP""",
            (user_id, username, full_name)
        )
        await db.commit()

async def get_all_users():
    db = await get_db()
    async with db.execute("SELECT user_id FROM users") as cursor:
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def get_request_by_id(req_id: str):
    db = await get_db()
    async with db.execute("SELECT * FROM requests WHERE id = ?", (req_id,)) as cursor:
        return await cursor.fetchone()


async def search_requests(query_str: str, limit: int = 10) -> list:
    """البحث في الطلبات بواسطة المعرف أو اسم الفعالية أو القسم أو اسم جهة الاتصال."""
    db = await get_db()
    clean_q = query_str.strip()
    pattern = f"%{clean_q}%"
    async with db.execute(
        """SELECT * FROM requests 
           WHERE id = ? 
              OR id LIKE ? 
              OR event_name LIKE ? 
              OR department LIKE ? 
              OR contact_name LIKE ? 
              OR location LIKE ?
           ORDER BY timestamp DESC LIMIT ?""",
        (clean_q, pattern, pattern, pattern, pattern, pattern, limit)
    ) as cursor:
        return await cursor.fetchall()

async def get_user_by_id(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT user_id, username, full_name FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        return await cursor.fetchone()

# --- Dynamic Settings ---
async def get_setting(key: str, default_val: str = None):
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
        return row['value'] if row else default_val

async def set_setting(key: str, value: str):
    db = await get_db()
    async with _db_lock:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    _cache_invalidate(key)

async def get_departments():
    cached = _cache_get("departments")
    if cached is not None:
        return list(cached)
    import constants
    val = await get_setting("departments")
    if val:
        try:
            data = json.loads(val)
            _cache_set("departments", list(data))
            return list(data)
        except:
            pass
    _cache_set("departments", list(constants.DEPARTMENTS))
    return list(constants.DEPARTMENTS)

async def save_departments(depts: list):
    await set_setting("departments", json.dumps(depts, ensure_ascii=False))
    _cache_invalidate("departments")

async def get_event_types():
    cached = _cache_get("event_types")
    if cached is not None:
        return list(cached)
    import constants
    val = await get_setting("event_types")
    if val:
        try:
            data = json.loads(val)
            _cache_set("event_types", list(data))
            return list(data)
        except:
            pass
    _cache_set("event_types", list(constants.EVENT_TYPES))
    return list(constants.EVENT_TYPES)

async def save_event_types(types: list):
    await set_setting("event_types", json.dumps(types, ensure_ascii=False))
    _cache_invalidate("event_types")

async def get_coverage_options():
    cached = _cache_get("coverage_options")
    if cached is not None:
        return list(cached)
    import constants
    val = await get_setting("coverage_options")
    if val:
        try:
            data = json.loads(val)
            _cache_set("coverage_options", list(data))
            return list(data)
        except:
            pass
    _cache_set("coverage_options", list(constants.COVERAGE_OPTIONS))
    return list(constants.COVERAGE_OPTIONS)

async def save_coverage_options(opts: list):
    await set_setting("coverage_options", json.dumps(opts, ensure_ascii=False))
    _cache_invalidate("coverage_options")

async def get_importance_levels():
    cached = _cache_get("importance_levels")
    if cached is not None:
        return list(cached)
    import constants
    val = await get_setting("importance_levels")
    if val:
        try:
            data = json.loads(val)
            _cache_set("importance_levels", list(data))
            return list(data)
        except:
            pass
    _cache_set("importance_levels", list(constants.IMPORTANCE_LEVELS))
    return list(constants.IMPORTANCE_LEVELS)

# --- Admin Roles ---
async def get_user_role(user_id: int):
    try:
        if int(user_id) in config.ADMIN_USERS_IDS:
            return "مشرف"
    except (ValueError, TypeError):
        pass
    return "مستخدم عادي"

# --- Audit Logs ---
async def log_audit_action(admin_id: int, admin_username: str, action: str, req_id: str, details: str,
                           db: aiosqlite.Connection = None, commit: bool = True):
    owns_db = False
    if db is None:
        db = await get_db()
        owns_db = True
    async with _db_lock if owns_db else contextlib.nullcontext():
        await db.execute(
            "INSERT INTO audit_logs (admin_id, admin_username, action, req_id, details) VALUES (?, ?, ?, ?, ?)",
            (admin_id, admin_username, action, req_id, details)
        )
        if commit or owns_db:
            await db.commit()

async def get_audit_logs(limit: int = 20, offset: int = 0):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset)
    ) as cursor:
        return await cursor.fetchall()

# --- Spam Protection (in-memory rate limiter) ---
_RATE_LIMIT_CACHE = {}
_RATE_WINDOW = 3.0

def check_rate_limit(user_id: int) -> bool:
    now = pytime.monotonic()
    last = _RATE_LIMIT_CACHE.get(user_id, 0.0)
    if now - last < _RATE_WINDOW:
        return False
    _RATE_LIMIT_CACHE[user_id] = now
    return True


# --- Centralized Query & Operation Helpers ---

async def generate_next_request_id() -> str:
    db = await get_db()
    async with _db_lock:
        async with db.execute("SELECT MAX(CAST(id AS INTEGER)) FROM requests") as cursor:
            row = await cursor.fetchone()
            max_id = row[0] if (row and row[0] is not None) else 0
            return str(max_id + 1)

async def get_dashboard_statistics() -> tuple:
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM requests") as c:
        total = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'معلق'") as c:
        pending = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'مقبول'") as c:
        approved = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'مرفوض'") as c:
        rejected = (await c.fetchone())[0]
    return total, pending, approved, rejected

async def get_upcoming_approved_coverages(today_str: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date >= ? ORDER BY date ASC, time ASC",
        (today_str,)
    ) as cursor:
        return await cursor.fetchall()

async def get_pending_coverages(today_str: str, limit: int = 10):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE status = 'معلق' AND date >= ? ORDER BY date ASC, time ASC LIMIT ?",
        (today_str, limit)
    ) as cursor:
        return await cursor.fetchall()

async def get_total_pending_count() -> int:
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM requests WHERE status = 'معلق'") as c:
        row = await c.fetchone()
        return row[0] if row else 0

async def get_events_for_pre_reminder(today_str: str, window_start: str, window_end: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date = ? AND start_time_24 BETWEEN ? AND ?",
        (today_str, window_start, window_end)
    ) as cursor:
        return await cursor.fetchall()

async def get_all_borrow_requests():
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE request_type = 'استعارة' ORDER BY date ASC, time ASC"
    ) as cursor:
        return await cursor.fetchall()

async def get_requests_by_date_range(date_from: str, date_to: str):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE date BETWEEN ? AND ? ORDER BY date ASC, time ASC",
        (date_from, date_to)
    ) as cursor:
        return await cursor.fetchall()

async def get_requests_by_department(dept_name: str, limit: int = 15):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM requests WHERE department = ? ORDER BY date DESC LIMIT ?",
        (dept_name, limit)
    ) as cursor:
        return await cursor.fetchall()

async def get_admin_filtered_requests(filter_type: str, limit: int = 10, today_str: str = None):
    db = await get_db()
    if filter_type == "upcoming":
        sql = "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date >= ? ORDER BY date ASC, time ASC"
        params = (today_str,)
    elif filter_type == "today":
        sql = "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date = ? ORDER BY time ASC"
        params = (today_str,)
    elif filter_type == "tomorrow":
        sql = "SELECT * FROM requests WHERE status = 'مقبول' AND request_type = 'تغطية' AND date = ? ORDER BY time ASC"
        params = (today_str,)
    elif filter_type == "pending":
        sql = "SELECT * FROM requests WHERE status = 'معلق' ORDER BY date ASC, time ASC LIMIT ?"
        params = (limit,)
    elif filter_type == "accepted":
        sql = "SELECT * FROM requests WHERE status = 'مقبول' ORDER BY date DESC, time DESC LIMIT ?"
        params = (limit,)
    elif filter_type == "rejected":
        sql = "SELECT * FROM requests WHERE status = 'مرفوض' ORDER BY date DESC, time DESC LIMIT ?"
        params = (limit,)
    else:
        sql = "SELECT * FROM requests ORDER BY date DESC, time DESC LIMIT ?"
        params = (limit,)
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchall()

async def update_request_status(req_id: str, status: str, admin_id: int = None, admin_username: str = None):
    db = await get_db()
    async with _db_lock:
        await db.execute(
            "UPDATE requests SET status = ?, admin_id = ?, admin_username = ? WHERE id = ?",
            (status, admin_id, admin_username, req_id)
        )
        await db.commit()

async def delete_request(req_id: str, user_id: int = None) -> bool:
    db = await get_db()
    async with _db_lock:
        if user_id:
            await db.execute("DELETE FROM requests WHERE id = ? AND user_id = ?", (req_id, user_id))
        else:
            await db.execute("DELETE FROM requests WHERE id = ?", (req_id,))
        await db.commit()
        return True

async def update_request_field(req_id: str, field: str, value: str):
    allowed_fields = {
        "department", "event_name", "event_type", "objective", "date",
        "time", "end_time", "location", "coverage_type", "importance",
        "external_media", "notes", "contact_name", "phone", "telegram",
        "start_date", "start_time", "asset_unit_id", "inspection_note"
    }
    if field not in allowed_fields:
        raise ValueError(f"Invalid field name: {field}")
    db = await get_db()
    async with _db_lock:
        await db.execute(f"UPDATE requests SET {field} = ? WHERE id = ?", (value, req_id))
        if field == "time":
            await db.execute("UPDATE requests SET start_time_24 = ? WHERE id = ?", (time_to_24(value), req_id))
        elif field == "end_time":
            await db.execute("UPDATE requests SET end_time_24 = ? WHERE id = ?", (time_to_24(value), req_id))
        await db.commit()

async def get_kpi_report_data():
    db = await get_db()
    async with db.execute("SELECT COUNT(*) FROM requests") as c:
        total_all = (await c.fetchone())[0]
    async with db.execute("SELECT status, COUNT(*) FROM requests GROUP BY status") as c:
        status_counts = dict(await c.fetchall())
    async with db.execute(
        """SELECT strftime('%Y-%m', date) as m, COUNT(*) 
           FROM requests 
           WHERE date IS NOT NULL AND date != '' 
           GROUP BY m ORDER BY m DESC LIMIT 6"""
    ) as c:
        monthly_trend = await c.fetchall()
    async with db.execute(
        """SELECT department, status, COUNT(*) as cnt 
           FROM requests 
           WHERE department != '' AND department IS NOT NULL 
           GROUP BY department, status"""
    ) as c:
        dept_status_rows = await c.fetchall()
    async with db.execute(
        """SELECT department, COUNT(*) as cnt 
           FROM requests 
           WHERE department != '' AND department IS NOT NULL 
           GROUP BY department ORDER BY cnt DESC"""
    ) as c:
        dept_total_rows = await c.fetchall()
    async with db.execute(
        """SELECT event_name as item, status, SUM(borrow_qty) as total_qty, COUNT(*) as cnt 
           FROM requests 
           WHERE request_type = 'استعارة' 
           GROUP BY event_name, status"""
    ) as c:
        borrow_rows = await c.fetchall()
    return {
        "total_all": total_all,
        "status_counts": status_counts,
        "monthly_trend": monthly_trend,
        "dept_status_rows": dept_status_rows,
        "dept_total_rows": dept_total_rows,
        "borrow_rows": borrow_rows,
    }

async def get_borrow_items() -> list:
    cached = _cache_get("borrow_items")
    if cached is not None:
        return list(cached)
    import constants
    val = await get_setting("borrow_items")
    if val:
        try:
            data = json.loads(val)
            _cache_set("borrow_items", list(data))
            return list(data)
        except Exception:
            pass
    _cache_set("borrow_items", list(constants.BORROW_ITEMS))
    return list(constants.BORROW_ITEMS)

async def save_borrow_items(items: list):
    await set_setting("borrow_items", json.dumps(items, ensure_ascii=False))
    _cache_invalidate("borrow_items")

async def get_borrow_item_stock() -> dict:
    cached = _cache_get("borrow_item_stock")
    if cached is not None:
        return dict(cached)
    import constants
    val = await get_setting("borrow_item_stock")
    if val:
        try:
            data = json.loads(val)
            _cache_set("borrow_item_stock", dict(data))
            return dict(data)
        except Exception:
            pass
    _cache_set("borrow_item_stock", dict(constants.BORROW_ITEM_STOCK))
    return dict(constants.BORROW_ITEM_STOCK)

async def save_borrow_item_stock(stock: dict):
    await set_setting("borrow_item_stock", json.dumps(stock, ensure_ascii=False))
    _cache_invalidate("borrow_item_stock")

