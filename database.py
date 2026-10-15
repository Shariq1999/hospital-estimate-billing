"""
database.py - SQLite database management with WAL mode, soft deletes,
transactional estimate counter, and PBKDF2 authentication.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import hashlib
import hmac
import secrets
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import billing


DB_FILE = "hospital.db"
PBKDF2_ITERATIONS = 600_000


def get_connection(db_path: str = DB_FILE) -> sqlite3.Connection:
    """Return an SQLite connection configured with WAL mode and foreign keys."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    """Add a column to a table only if it doesn't already exist (safe migration helper)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Create all required tables, indexes, and initialize system counters."""
    with conn:
        conn.executescript(
            """
            -- Master Department List
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            -- Room Types
            CREATE TABLE IF NOT EXISTS room_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                daily_rate INTEGER NOT NULL,
                description TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            -- Doctors linked to Departments
            CREATE TABLE IF NOT EXISTS doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department_id INTEGER NOT NULL REFERENCES departments(id),
                consultation_fee INTEGER NOT NULL,
                surgeon_fee_pct INTEGER NOT NULL,
                surgeon_fee_type TEXT NOT NULL DEFAULT 'fixed',
                surgeon_fee_amount INTEGER NOT NULL DEFAULT 0,
                specialization TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            -- Procedures linked to Departments
            CREATE TABLE IF NOT EXISTS procedures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department_id INTEGER NOT NULL REFERENCES departments(id),
                base_cost INTEGER NOT NULL,
                ot_charge INTEGER NOT NULL,
                anaesthesia_charge INTEGER NOT NULL,
                consumables_cost INTEGER NOT NULL,
                investigations_charge INTEGER NOT NULL,
                default_stay_days INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            -- System Settings (Key-Value)
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Atomic Counters
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                current_val INTEGER NOT NULL
            );

            -- Admin Authentication (Salted PBKDF2)
            CREATE TABLE IF NOT EXISTS admin_auth (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Saved Estimates with Frozen JSON Snapshot
            CREATE TABLE IF NOT EXISTS estimates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                estimate_number TEXT UNIQUE NOT NULL,
                patient_name TEXT NOT NULL,
                room_type_id INTEGER REFERENCES room_types(id),
                department_id INTEGER REFERENCES departments(id),
                doctor_id INTEGER REFERENCES doctors(id),
                procedure_id INTEGER REFERENCES procedures(id),
                stay_days INTEGER NOT NULL,
                discount_pct TEXT NOT NULL,
                subtotal TEXT NOT NULL,
                discount_amount TEXT NOT NULL,
                contingency_amount TEXT NOT NULL,
                tax_amount TEXT NOT NULL,
                total_amount TEXT NOT NULL,
                min_estimate TEXT NOT NULL,
                max_estimate TEXT NOT NULL,
                line_items_json TEXT NOT NULL,
                remarks TEXT,
                created_at TEXT NOT NULL
            );

            -- Indexes for fast lookup
            CREATE INDEX IF NOT EXISTS idx_doctors_dept ON doctors(department_id);
            CREATE INDEX IF NOT EXISTS idx_procedures_dept ON procedures(department_id);
            CREATE INDEX IF NOT EXISTS idx_estimates_num ON estimates(estimate_number);
            CREATE INDEX IF NOT EXISTS idx_estimates_patient ON estimates(patient_name);
            CREATE INDEX IF NOT EXISTS idx_estimates_date ON estimates(created_at);
            """
        )
        # Initialize counter row if not present
        conn.execute(
            "INSERT OR IGNORE INTO counters (name, current_val) VALUES ('estimate', 0);"
        )
        # Additive column migrations (safe on existing databases)
        _add_column_if_missing(conn, "estimates", "mobile_number", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "estimates", "uhid", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "estimates", "revision_of", "TEXT DEFAULT NULL")
        _add_column_if_missing(conn, "estimates", "converted", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "estimates", "actual_amount", "TEXT DEFAULT NULL")
        _add_column_if_missing(conn, "doctors", "surgeon_fee_type", "TEXT DEFAULT 'fixed'")
        _add_column_if_missing(conn, "doctors", "surgeon_fee_amount", "INTEGER DEFAULT 0")


def get_next_estimate_number(conn: sqlite3.Connection) -> str:
    """
    Atomically increment and return a unique estimate number inside BEGIN IMMEDIATE.
    Format: EST-YYYYMMDD-XXXX (4-digit zero-padded counter).
    """
    # Ensure any previous transaction state is committed
    conn.commit()
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        cursor.execute(
            "UPDATE counters SET current_val = current_val + 1 WHERE name = 'estimate';"
        )
        cursor.execute("SELECT current_val FROM counters WHERE name = 'estimate';")
        row = cursor.fetchone()
        if not row:
            # Fallback if row was missing
            cursor.execute(
                "INSERT INTO counters (name, current_val) VALUES ('estimate', 1);"
            )
            val = 1
        else:
            val = row[0]
        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        raise

    today = datetime.now().strftime("%Y%m%d")
    return f"EST-{today}-{val:04d}"


# ==============================================================================
# Security & Authentication (PBKDF2 with 600,000 iterations & hmac.compare_digest)
# ==============================================================================

def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Hash a password using PBKDF2-HMAC-SHA256 with 600,000 iterations."""
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return key.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    """Verify password in constant time using hmac.compare_digest."""
    computed_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(computed_hash, expected_hash)


def is_admin_configured(conn: sqlite3.Connection) -> bool:
    """Return True if at least one admin account exists in admin_auth."""
    cursor = conn.cursor()
    row = cursor.execute("SELECT COUNT(*) FROM admin_auth").fetchone()
    return bool(row and row[0] > 0)


def set_admin_password(conn: sqlite3.Connection, username: str, password: str) -> None:
    """Set or update an admin account with salted PBKDF2 hash."""
    pwd_hash, salt = hash_password(password)
    now = datetime.now().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO admin_auth (username, password_hash, salt, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                salt = excluded.salt,
                updated_at = excluded.updated_at
            """,
            (username, pwd_hash, salt, now),
        )


def verify_admin_credentials(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Authenticate an administrator against salted PBKDF2 hash."""
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT password_hash, salt FROM admin_auth WHERE username = ?",
        (username,),
    ).fetchone()
    if not row:
        return False
    return verify_password(password, row["salt"], row["password_hash"])


# ==============================================================================
# Master Data Queries & Soft Deletes (is_active)
# ==============================================================================

# --- Departments ---
def get_departments(conn: sqlite3.Connection, active_only: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT id, name, is_active FROM departments"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY name ASC"
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_department_by_id(conn: sqlite3.Connection, dept_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT id, name, is_active FROM departments WHERE id = ?", (dept_id,)).fetchone()
    return dict(row) if row else None


def save_department(conn: sqlite3.Connection, name: str, dept_id: Optional[int] = None) -> int:
    clean_name = name.strip()
    dup = conn.execute(
        "SELECT id FROM departments WHERE LOWER(name) = LOWER(?) AND id != ?",
        (clean_name, dept_id or -1),
    ).fetchone()
    if dup:
        raise sqlite3.IntegrityError(f"A department named '{clean_name}' already exists.")
    with conn:
        if dept_id:
            conn.execute("UPDATE departments SET name = ? WHERE id = ?", (clean_name, dept_id))
            return dept_id
        else:
            cur = conn.execute("INSERT INTO departments (name, is_active) VALUES (?, 1)", (clean_name,))
            return cur.lastrowid


def toggle_department_status(conn: sqlite3.Connection, dept_id: int, is_active: int) -> None:
    with conn:
        conn.execute("UPDATE departments SET is_active = ? WHERE id = ?", (is_active, dept_id))


# --- Room Types ---
def get_room_types(conn: sqlite3.Connection, active_only: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT id, name, daily_rate, description, is_active FROM room_types"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY daily_rate ASC"
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_room_type_by_id(conn: sqlite3.Connection, room_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT id, name, daily_rate, description, is_active FROM room_types WHERE id = ?", (room_id,)).fetchone()
    return dict(row) if row else None


def save_room_type(
    conn: sqlite3.Connection,
    name: str,
    daily_rate: int,
    description: str,
    room_id: Optional[int] = None,
) -> int:
    clean_name = name.strip()
    dup = conn.execute(
        "SELECT id FROM room_types WHERE LOWER(name) = LOWER(?) AND id != ?",
        (clean_name, room_id or -1),
    ).fetchone()
    if dup:
        raise sqlite3.IntegrityError(f"A room category named '{clean_name}' already exists.")
    with conn:
        if room_id:
            conn.execute(
                "UPDATE room_types SET name = ?, daily_rate = ?, description = ? WHERE id = ?",
                (clean_name, daily_rate, description.strip(), room_id),
            )
            return room_id
        else:
            cur = conn.execute(
                "INSERT INTO room_types (name, daily_rate, description, is_active) VALUES (?, ?, ?, 1)",
                (clean_name, daily_rate, description.strip()),
            )
            return cur.lastrowid


def toggle_room_type_status(conn: sqlite3.Connection, room_id: int, is_active: int) -> None:
    with conn:
        conn.execute("UPDATE room_types SET is_active = ? WHERE id = ?", (is_active, room_id))


# --- Doctors ---
def get_doctors(
    conn: sqlite3.Connection,
    department_id: Optional[int] = None,
    active_only: bool = True,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT d.id, d.name, d.department_id, d.consultation_fee, d.surgeon_fee_pct,
               d.surgeon_fee_type, d.surgeon_fee_amount,
               d.specialization, d.is_active, dep.name as department_name
        FROM doctors d
        JOIN departments dep ON d.department_id = dep.id
        WHERE 1=1
    """
    params: List[Any] = []
    if active_only:
        sql += " AND d.is_active = 1 AND dep.is_active = 1"
    if department_id is not None:
        sql += " AND d.department_id = ?"
        params.append(department_id)
    sql += " ORDER BY d.name ASC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_doctor_by_id(conn: sqlite3.Connection, doc_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT d.id, d.name, d.department_id, d.consultation_fee, d.surgeon_fee_pct,
               d.surgeon_fee_type, d.surgeon_fee_amount,
               d.specialization, d.is_active, dep.name as department_name
        FROM doctors d
        JOIN departments dep ON d.department_id = dep.id
        WHERE d.id = ?
        """,
        (doc_id,),
    ).fetchone()
    return dict(row) if row else None


def save_doctor(
    conn: sqlite3.Connection,
    name: str,
    department_id: int,
    consultation_fee: int,
    surgeon_fee_pct: int,
    specialization: str,
    doc_id: Optional[int] = None,
    surgeon_fee_type: str = "fixed",
    surgeon_fee_amount: int = 0,
) -> int:
    clean_name = name.strip()
    dup = conn.execute(
        "SELECT id FROM doctors WHERE LOWER(name) = LOWER(?) AND department_id = ? AND id != ?",
        (clean_name, department_id, doc_id or -1),
    ).fetchone()
    if dup:
        raise sqlite3.IntegrityError(f"A doctor named '{clean_name}' already exists in this department.")
    with conn:
        if doc_id:
            conn.execute(
                """
                UPDATE doctors
                SET name = ?, department_id = ?, consultation_fee = ?,
                    surgeon_fee_pct = ?, surgeon_fee_type = ?, surgeon_fee_amount = ?, specialization = ?
                WHERE id = ?
                """,
                (clean_name, department_id, consultation_fee, surgeon_fee_pct, surgeon_fee_type, surgeon_fee_amount, specialization.strip(), doc_id),
            )
            return doc_id
        else:
            cur = conn.execute(
                """
                INSERT INTO doctors (name, department_id, consultation_fee, surgeon_fee_pct, surgeon_fee_type, surgeon_fee_amount, specialization, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (clean_name, department_id, consultation_fee, surgeon_fee_pct, surgeon_fee_type, surgeon_fee_amount, specialization.strip()),
            )
            return cur.lastrowid


def toggle_doctor_status(conn: sqlite3.Connection, doc_id: int, is_active: int) -> None:
    with conn:
        conn.execute("UPDATE doctors SET is_active = ? WHERE id = ?", (is_active, doc_id))


# --- Procedures ---
def get_procedures(
    conn: sqlite3.Connection,
    department_id: Optional[int] = None,
    active_only: bool = True,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT p.id, p.name, p.department_id, p.base_cost, p.ot_charge,
        anaesthesia_charge, consumables_cost, investigations_charge,
        p.default_stay_days, p.is_active, dep.name as department_name
        FROM procedures p
        JOIN departments dep ON p.department_id = dep.id
        WHERE 1=1
    """
    params: List[Any] = []
    if active_only:
        sql += " AND p.is_active = 1 AND dep.is_active = 1"
    if department_id is not None:
        sql += " AND p.department_id = ?"
        params.append(department_id)
    sql += " ORDER BY p.name ASC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_procedure_by_id(conn: sqlite3.Connection, proc_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT p.id, p.name, p.department_id, p.base_cost, p.ot_charge,
               p.anaesthesia_charge, p.consumables_cost, p.investigations_charge,
               p.default_stay_days, p.is_active, dep.name as department_name
        FROM procedures p
        JOIN departments dep ON p.department_id = dep.id
        WHERE p.id = ?
        """,
        (proc_id,),
    ).fetchone()
    return dict(row) if row else None


def save_procedure(
    conn: sqlite3.Connection,
    name: str,
    department_id: int,
    base_cost: int,
    ot_charge: int,
    anaesthesia_charge: int,
    consumables_cost: int,
    investigations_charge: int,
    default_stay_days: int,
    proc_id: Optional[int] = None,
) -> int:
    clean_name = name.strip()
    dup = conn.execute(
        "SELECT id FROM procedures WHERE LOWER(name) = LOWER(?) AND department_id = ? AND id != ?",
        (clean_name, department_id, proc_id or -1),
    ).fetchone()
    if dup:
        raise sqlite3.IntegrityError(f"A procedure named '{clean_name}' already exists in this department.")
    with conn:
        if proc_id:
            conn.execute(
                """
                UPDATE procedures
                SET name = ?, department_id = ?, base_cost = ?, ot_charge = ?,
                    anaesthesia_charge = ?, consumables_cost = ?,
                    investigations_charge = ?, default_stay_days = ?
                WHERE id = ?
                """,
                (
                    clean_name, department_id, base_cost, ot_charge,
                    anaesthesia_charge, consumables_cost,
                    investigations_charge, default_stay_days, proc_id
                ),
            )
            return proc_id
        else:
            cur = conn.execute(
                """
                INSERT INTO procedures (
                    name, department_id, base_cost, ot_charge, anaesthesia_charge,
                    consumables_cost, investigations_charge, default_stay_days, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    clean_name, department_id, base_cost, ot_charge,
                    anaesthesia_charge, consumables_cost,
                    investigations_charge, default_stay_days
                ),
            )
            return cur.lastrowid


def toggle_procedure_status(conn: sqlite3.Connection, proc_id: int, is_active: int) -> None:
    with conn:
        conn.execute("UPDATE procedures SET is_active = ? WHERE id = ?", (is_active, proc_id))


# ==============================================================================
# Settings Management
# ==============================================================================

def get_settings(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def update_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


def update_settings(conn: sqlite3.Connection, settings_dict: Dict[str, str]) -> None:
    with conn:
        for k, v in settings_dict.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))


# ==============================================================================
# Estimates & Snapshot Persistence
# ==============================================================================

def save_estimate(conn: sqlite3.Connection, est: Dict[str, Any]) -> str:
    """Save an estimate along with its frozen JSON snapshot."""
    estimate_number = est["estimate_number"]
    now = datetime.now().isoformat()
    line_items_json = (
        est["line_items_json"]
        if isinstance(est["line_items_json"], str)
        else json.dumps(est["line_items_json"])
    )

    with conn:
        conn.execute(
            """
            INSERT INTO estimates (
                estimate_number, patient_name, room_type_id, department_id,
                doctor_id, procedure_id, stay_days, discount_pct, subtotal,
                discount_amount, contingency_amount, tax_amount, total_amount,
                min_estimate, max_estimate, line_items_json, remarks, created_at,
                mobile_number, uhid, revision_of
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                estimate_number,
                est["patient_name"],
                est.get("room_type_id"),
                est.get("department_id"),
                est.get("doctor_id"),
                est.get("procedure_id"),
                est["stay_days"],
                str(est["discount_pct"]),
                str(est["subtotal"]),
                str(est["discount_amount"]),
                str(est["contingency_amount"]),
                str(est["tax_amount"]),
                str(est["total_amount"]),
                str(est["min_estimate"]),
                str(est["max_estimate"]),
                line_items_json,
                est.get("remarks", ""),
                est.get("created_at", now),
                est.get("mobile_number", ""),
                est.get("uhid", ""),
                est.get("revision_of"),
            ),
        )
    return estimate_number


def update_estimate_outcome(
    conn: sqlite3.Connection,
    estimate_number: str,
    converted: bool,
    actual_amount: Optional[str],
) -> None:
    """Update the conversion toggle and actual bill amount for an existing estimate."""
    with conn:
        conn.execute(
            """
            UPDATE estimates
            SET converted = ?, actual_amount = ?
            WHERE estimate_number = ?
            """,
            (1 if converted else 0, actual_amount or None, estimate_number),
        )


def get_next_revision_number(conn: sqlite3.Connection, base_est_num: str) -> str:
    """
    Generate the next revision estimate number for a given base estimate.
    If base_est_num is 'EST-20261015-0001' or 'EST-20261015-0001-R1',
    strips the -R suffix to find the root, then queries existing revisions
    and returns f"{root}-R{next_idx}".
    """
    import re
    root = re.sub(r"-R\d+$", "", base_est_num.strip())
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT estimate_number FROM estimates WHERE estimate_number = ? OR estimate_number LIKE ? OR revision_of = ?",
        (root, f"{root}-R%", root),
    ).fetchall()

    max_rev = 0
    for r in rows:
        num = r["estimate_number"] if isinstance(r, sqlite3.Row) else r[0]
        match = re.search(r"-R(\d+)$", num)
        if match:
            max_rev = max(max_rev, int(match.group(1)))

    next_rev = max_rev + 1
    return f"{root}-R{next_rev}"


def get_estimates(
    conn: sqlite3.Connection,
    search_query: Optional[str] = None,
    department_id: Optional[int] = None,
    limit: Optional[int] = 100,
) -> List[Dict[str, Any]]:
    sql = """
        SELECT e.*, d.name as department_name, doc.name as doctor_name,
               p.name as procedure_name, r.name as room_type_name
        FROM estimates e
        LEFT JOIN departments d ON e.department_id = d.id
        LEFT JOIN doctors doc ON e.doctor_id = doc.id
        LEFT JOIN procedures p ON e.procedure_id = p.id
        LEFT JOIN room_types r ON e.room_type_id = r.id
        WHERE 1=1
    """
    params: List[Any] = []
    if search_query:
        sql += " AND (e.patient_name LIKE ? OR e.estimate_number LIKE ? OR e.uhid LIKE ? OR e.mobile_number LIKE ?)"
        pattern = f"%{search_query.strip()}%"
        params.extend([pattern, pattern, pattern, pattern])
    if department_id:
        sql += " AND e.department_id = ?"
        params.append(department_id)
    if limit is not None:
        sql += " ORDER BY e.id DESC LIMIT ?"
        params.append(limit)
    else:
        sql += " ORDER BY e.id DESC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_estimate_by_number(conn: sqlite3.Connection, est_num: str) -> Optional[Dict[str, Any]]:
    sql = """
        SELECT e.*, d.name as department_name, doc.name as doctor_name,
               p.name as procedure_name, r.name as room_type_name
        FROM estimates e
        LEFT JOIN departments d ON e.department_id = d.id
        LEFT JOIN doctors doc ON e.doctor_id = doc.id
        LEFT JOIN procedures p ON e.procedure_id = p.id
        LEFT JOIN room_types r ON e.room_type_id = r.id
        WHERE e.estimate_number = ?
    """
    row = conn.execute(sql, (est_num,)).fetchone()
    return dict(row) if row else None


# ==============================================================================
# Database Hot Backup
# ==============================================================================

def backup_database(conn: sqlite3.Connection) -> bytes:
    """Create a real binary SQLite database file backup and return its bytes."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        temp_path = tmp.name

    try:
        dest_conn = sqlite3.connect(temp_path)
        conn.backup(dest_conn)
        dest_conn.close()

        with open(temp_path, "rb") as f:
            data = f.read()
        return data
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ==============================================================================
# CSV Import / Export Helpers (Formula neutralization & All-or-Nothing Transactions)
# ==============================================================================

def sanitize_csv_cell(val: Any) -> str:
    """Neutralize formula injection by prefixing with ' if string starts with =, +, -, @."""
    s = str(val) if val is not None else ""
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def unsanitize_csv_cell(val: Any) -> str:
    """Remove leading single quote added to neutralize formula injection."""
    s = str(val).strip() if val is not None else ""
    if len(s) > 1 and s[0] == "'" and s[1] in ("=", "+", "-", "@"):
        return s[1:]
    return s


def parse_is_active(val: Any) -> int:
    """Parse is_active boolean or integer value."""
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "active"):
        return 1
    elif s in ("0", "false", "no", "n", "inactive"):
        return 0
    return 1


# --- Departments CSV ---

def export_departments_csv(conn: sqlite3.Connection) -> bytes:
    depts = get_departments(conn, active_only=False)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "is_active"])
    for d in depts:
        writer.writerow([sanitize_csv_cell(d["name"]), d["is_active"]])
    return out.getvalue().encode("utf-8-sig")


def import_departments_csv(conn: sqlite3.Connection, csv_data: str | bytes) -> Tuple[int, int]:
    text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is empty or missing headers.")
    col_map = {k.strip().lower(): k for k in reader.fieldnames if k}
    if "name" not in col_map:
        raise ValueError("CSV must contain a 'name' column.")

    rows = list(reader)
    if not rows:
        raise ValueError("CSV contains no data rows.")

    cleaned_rows: List[Tuple[str, int]] = []
    for idx, r in enumerate(rows, start=1):
        raw_name = unsanitize_csv_cell(r.get(col_map["name"], ""))
        if not raw_name.strip():
            raise ValueError(f"Row {idx}: Department name cannot be blank.")
        active_val = 1
        if "is_active" in col_map:
            active_val = parse_is_active(unsanitize_csv_cell(r.get(col_map["is_active"], "1")))
        cleaned_rows.append((raw_name.strip(), active_val))

    inserted = 0
    updated = 0
    with conn:
        for name, active_val in cleaned_rows:
            existing = conn.execute(
                "SELECT id FROM departments WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE departments SET is_active = ? WHERE id = ?",
                    (active_val, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO departments (name, is_active) VALUES (?, ?)",
                    (name, active_val),
                )
                inserted += 1
    return inserted, updated


# --- Room Types CSV ---

def export_room_types_csv(conn: sqlite3.Connection) -> bytes:
    rooms = get_room_types(conn, active_only=False)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "daily_rate", "description", "is_active"])
    for r in rooms:
        writer.writerow([
            sanitize_csv_cell(r["name"]),
            r["daily_rate"],
            sanitize_csv_cell(r.get("description", "")),
            r["is_active"],
        ])
    return out.getvalue().encode("utf-8-sig")


def import_room_types_csv(conn: sqlite3.Connection, csv_data: str | bytes) -> Tuple[int, int]:
    text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is empty or missing headers.")
    col_map = {k.strip().lower(): k for k in reader.fieldnames if k}
    for req in ("name", "daily_rate"):
        if req not in col_map:
            raise ValueError(f"CSV must contain a '{req}' column.")

    rows = list(reader)
    if not rows:
        raise ValueError("CSV contains no data rows.")

    cleaned_rows: List[Tuple[str, int, str, int]] = []
    for idx, r in enumerate(rows, start=1):
        raw_name = unsanitize_csv_cell(r.get(col_map["name"], ""))
        if not raw_name.strip():
            raise ValueError(f"Row {idx}: Room category name cannot be blank.")

        raw_rate = unsanitize_csv_cell(r.get(col_map["daily_rate"], ""))
        valid, rate_dec, err = billing.validate_whole_rupee_field(raw_rate, "daily_rate", allow_zero=True)
        if not valid:
            raise ValueError(f"Row {idx}: {err}")

        desc = ""
        if "description" in col_map:
            desc = unsanitize_csv_cell(r.get(col_map["description"], ""))

        active_val = 1
        if "is_active" in col_map:
            active_val = parse_is_active(unsanitize_csv_cell(r.get(col_map["is_active"], "1")))

        cleaned_rows.append((raw_name.strip(), int(rate_dec), desc.strip(), active_val))

    inserted = 0
    updated = 0
    with conn:
        for name, rate, desc, active_val in cleaned_rows:
            existing = conn.execute(
                "SELECT id FROM room_types WHERE LOWER(name) = LOWER(?)", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE room_types SET daily_rate = ?, description = ?, is_active = ? WHERE id = ?",
                    (rate, desc, active_val, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO room_types (name, daily_rate, description, is_active) VALUES (?, ?, ?, ?)",
                    (name, rate, desc, active_val),
                )
                inserted += 1
    return inserted, updated


# --- Doctors CSV ---

def export_doctors_csv(conn: sqlite3.Connection) -> bytes:
    docs = get_doctors(conn, active_only=False)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["name", "department", "consultation_fee", "surgeon_fee_pct", "specialization", "is_active"])
    for d in docs:
        writer.writerow([
            sanitize_csv_cell(d["name"]),
            sanitize_csv_cell(d["department_name"]),
            d["consultation_fee"],
            d["surgeon_fee_pct"],
            sanitize_csv_cell(d.get("specialization", "")),
            d["is_active"],
        ])
    return out.getvalue().encode("utf-8-sig")


def import_doctors_csv(conn: sqlite3.Connection, csv_data: str | bytes) -> Tuple[int, int]:
    text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is empty or missing headers.")
    col_map = {k.strip().lower(): k for k in reader.fieldnames if k}
    for req in ("name", "department", "consultation_fee", "surgeon_fee_pct"):
        if req not in col_map:
            raise ValueError(f"CSV must contain a '{req}' column.")

    rows = list(reader)
    if not rows:
        raise ValueError("CSV contains no data rows.")

    cleaned_rows: List[Tuple[str, int, int, int, str, int]] = []
    for idx, r in enumerate(rows, start=1):
        raw_name = unsanitize_csv_cell(r.get(col_map["name"], ""))
        if not raw_name.strip():
            raise ValueError(f"Row {idx}: Doctor name cannot be blank.")

        raw_dept = unsanitize_csv_cell(r.get(col_map["department"], ""))
        if not raw_dept.strip():
            raise ValueError(f"Row {idx}: Department cannot be blank.")

        dept_row = conn.execute(
            "SELECT id FROM departments WHERE LOWER(name) = LOWER(?)", (raw_dept.strip(),)
        ).fetchone()
        if not dept_row:
            raise ValueError(f"Row {idx}: Department '{raw_dept.strip()}' does not exist.")
        dept_id = dept_row["id"]

        raw_fee = unsanitize_csv_cell(r.get(col_map["consultation_fee"], ""))
        valid, fee_dec, err = billing.validate_whole_rupee_field(raw_fee, "consultation_fee", allow_zero=True)
        if not valid:
            raise ValueError(f"Row {idx}: {err}")

        raw_pct = unsanitize_csv_cell(r.get(col_map["surgeon_fee_pct"], ""))
        valid, pct_dec, err = billing.validate_whole_percentage_field(raw_pct, "surgeon_fee_pct")
        if not valid:
            raise ValueError(f"Row {idx}: {err}")

        spec = ""
        if "specialization" in col_map:
            spec = unsanitize_csv_cell(r.get(col_map["specialization"], ""))

        active_val = 1
        if "is_active" in col_map:
            active_val = parse_is_active(unsanitize_csv_cell(r.get(col_map["is_active"], "1")))

        cleaned_rows.append((raw_name.strip(), dept_id, int(fee_dec), int(pct_dec), spec.strip(), active_val))

    inserted = 0
    updated = 0
    with conn:
        for name, dept_id, fee, pct, spec, active_val in cleaned_rows:
            existing = conn.execute(
                "SELECT id FROM doctors WHERE LOWER(name) = LOWER(?) AND department_id = ?",
                (name, dept_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE doctors
                    SET consultation_fee = ?, surgeon_fee_pct = ?, specialization = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (fee, pct, spec, active_val, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO doctors (name, department_id, consultation_fee, surgeon_fee_pct, specialization, is_active)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, dept_id, fee, pct, spec, active_val),
                )
                inserted += 1
    return inserted, updated


# --- Procedures CSV ---

def export_procedures_csv(conn: sqlite3.Connection) -> bytes:
    procs = get_procedures(conn, active_only=False)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "name", "department", "base_cost", "ot_charge", "anaesthesia_charge",
        "consumables_cost", "investigations_charge", "default_stay_days", "is_active"
    ])
    for p in procs:
        writer.writerow([
            sanitize_csv_cell(p["name"]),
            sanitize_csv_cell(p["department_name"]),
            p["base_cost"],
            p["ot_charge"],
            p["anaesthesia_charge"],
            p["consumables_cost"],
            p["investigations_charge"],
            p["default_stay_days"],
            p["is_active"],
        ])
    return out.getvalue().encode("utf-8-sig")


def import_procedures_csv(conn: sqlite3.Connection, csv_data: str | bytes) -> Tuple[int, int]:
    text = csv_data.decode("utf-8-sig") if isinstance(csv_data, bytes) else csv_data
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV is empty or missing headers.")
    col_map = {k.strip().lower(): k for k in reader.fieldnames if k}
    required_cols = [
        "name", "department", "base_cost", "ot_charge",
        "anaesthesia_charge", "consumables_cost", "investigations_charge"
    ]
    for req in required_cols:
        if req not in col_map:
            raise ValueError(f"CSV must contain a '{req}' column.")

    rows = list(reader)
    if not rows:
        raise ValueError("CSV contains no data rows.")

    cleaned_rows = []
    for idx, r in enumerate(rows, start=1):
        raw_name = unsanitize_csv_cell(r.get(col_map["name"], ""))
        if not raw_name.strip():
            raise ValueError(f"Row {idx}: Procedure name cannot be blank.")

        raw_dept = unsanitize_csv_cell(r.get(col_map["department"], ""))
        if not raw_dept.strip():
            raise ValueError(f"Row {idx}: Department cannot be blank.")

        dept_row = conn.execute(
            "SELECT id FROM departments WHERE LOWER(name) = LOWER(?)", (raw_dept.strip(),)
        ).fetchone()
        if not dept_row:
            raise ValueError(f"Row {idx}: Department '{raw_dept.strip()}' does not exist.")
        dept_id = dept_row["id"]

        costs = {}
        for c_field in ("base_cost", "ot_charge", "anaesthesia_charge", "consumables_cost", "investigations_charge"):
            raw_c = unsanitize_csv_cell(r.get(col_map[c_field], ""))
            valid, c_dec, err = billing.validate_whole_rupee_field(raw_c, c_field, allow_zero=True)
            if not valid:
                raise ValueError(f"Row {idx}: {err}")
            costs[c_field] = int(c_dec)

        stay_val = 1
        if "default_stay_days" in col_map:
            raw_stay = unsanitize_csv_cell(r.get(col_map["default_stay_days"], "1"))
            try:
                stay_val = int(raw_stay)
                if stay_val < 0:
                    raise ValueError(f"Row {idx}: default_stay_days cannot be negative.")
            except ValueError:
                raise ValueError(f"Row {idx}: Invalid number format for default_stay_days.")

        active_val = 1
        if "is_active" in col_map:
            active_val = parse_is_active(unsanitize_csv_cell(r.get(col_map["is_active"], "1")))

        cleaned_rows.append((
            raw_name.strip(), dept_id, costs["base_cost"], costs["ot_charge"],
            costs["anaesthesia_charge"], costs["consumables_cost"],
            costs["investigations_charge"], stay_val, active_val
        ))

    inserted = 0
    updated = 0
    with conn:
        for (name, dept_id, base, ot, anaes, cons, inv, stay, active_val) in cleaned_rows:
            existing = conn.execute(
                "SELECT id FROM procedures WHERE LOWER(name) = LOWER(?) AND department_id = ?",
                (name, dept_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE procedures
                    SET base_cost = ?, ot_charge = ?, anaesthesia_charge = ?,
                        consumables_cost = ?, investigations_charge = ?,
                        default_stay_days = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (base, ot, anaes, cons, inv, stay, active_val, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO procedures (
                        name, department_id, base_cost, ot_charge, anaesthesia_charge,
                        consumables_cost, investigations_charge, default_stay_days, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, dept_id, base, ot, anaes, cons, inv, stay, active_val),
                )
                inserted += 1
    return inserted, updated
