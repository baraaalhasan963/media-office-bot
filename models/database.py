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
        "importance_levels": json.dumps(constants.IMPORTANCE_LEVELS, ensure_ascii=False)
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
                    external_media, notes, request_type, status, borrow_qty
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'معلق', ?)""",
                (
                    data["id"], data["user_id"], "", data["item"], "", data["reason"],
                    data["return_date"], data["return_time"], "",
                    time_to_24(data.get("return_time")), "", "",
                    json.dumps([], ensure_ascii=False), "", data["borrower_name"],
                    data["phone"], "", "لا", "", "استعارة",
                    int(data.get("borrow_qty") or 1)
                )
            )
            if commit or owns_db:
                await db.commit()

        try:
            with open("backup_requests.txt", "a", encoding="utf-8") as f:
                f.write(f"ID: {data['id']} | User: {data['user_id']} | Borrow: {data['item']} | Qty: {data.get('borrow_qty', 1)} | Return: {data['return_date']}\n")
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
    if user_id in config.ADMIN_USERS_IDS:
        return "مشرف"
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
