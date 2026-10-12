"""
tests/test_database.py - Verification tests for database schema, WAL mode,
transactional counter, PBKDF2 authentication, and soft deletes.
"""

import os
import pytest
from datetime import datetime

import database
from seed_data import seed, DEPARTMENTS, ROOM_TYPES, DOCTORS, PROCEDURES, SETTINGS


@pytest.fixture
def temp_db(tmp_path):
    """Fixture providing a fresh SQLite database file for testing."""
    db_file = str(tmp_path / "test_hospital.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    yield conn, db_file
    conn.close()
    # Clean up wal/shm if present
    for ext in ["", "-wal", "-shm"]:
        f = db_file + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


def test_init_db_and_wal_mode(temp_db):
    conn, _ = temp_db
    # Check WAL mode
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"

    # Check foreign keys
    fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1

    # Check tables exist
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()
    ]
    expected_tables = [
        "departments",
        "room_types",
        "doctors",
        "procedures",
        "settings",
        "counters",
        "admin_auth",
        "estimates",
    ]
    for tbl in expected_tables:
        assert tbl in tables, f"Expected table '{tbl}' was not created"

    # Check counter row was initialized
    row = conn.execute("SELECT current_val FROM counters WHERE name = 'estimate';").fetchone()
    assert row is not None
    assert row[0] == 0


def test_transactional_estimate_number_generation(temp_db):
    conn, _ = temp_db
    today = datetime.now().strftime("%Y%m%d")

    num1 = database.get_next_estimate_number(conn)
    assert num1 == f"EST-{today}-0001"

    num2 = database.get_next_estimate_number(conn)
    assert num2 == f"EST-{today}-0002"

    num3 = database.get_next_estimate_number(conn)
    assert num3 == f"EST-{today}-0003"

    # Counter in table should now be 3
    row = conn.execute("SELECT current_val FROM counters WHERE name = 'estimate';").fetchone()
    assert row[0] == 3


def test_seed_data_execution_and_idempotency(temp_db):
    conn, _ = temp_db
    seed(conn)

    # Verify counts
    dep_count = conn.execute("SELECT COUNT(*) FROM departments;").fetchone()[0]
    assert dep_count == len(DEPARTMENTS)

    room_count = conn.execute("SELECT COUNT(*) FROM room_types;").fetchone()[0]
    assert room_count == len(ROOM_TYPES)

    doc_count = conn.execute("SELECT COUNT(*) FROM doctors;").fetchone()[0]
    assert doc_count == len(DOCTORS)

    proc_count = conn.execute("SELECT COUNT(*) FROM procedures;").fetchone()[0]
    assert proc_count == len(PROCEDURES)

    # Settings verification
    for k, v in SETTINGS.items():
        assert database.get_setting(conn, k) == str(v)

    # Idempotency check: running seed again should not duplicate rows
    seed(conn)
    assert conn.execute("SELECT COUNT(*) FROM departments;").fetchone()[0] == len(DEPARTMENTS)
    assert conn.execute("SELECT COUNT(*) FROM doctors;").fetchone()[0] == len(DOCTORS)


def test_soft_deletes_on_master_tables(temp_db):
    conn, _ = temp_db
    seed(conn)

    # Test Department soft delete
    active_deps = database.get_departments(conn, active_only=True)
    target_dep = active_deps[0]
    assert target_dep["is_active"] == 1

    database.toggle_department_status(conn, target_dep["id"], 0)

    # Should disappear from active departments
    active_deps_after = database.get_departments(conn, active_only=True)
    assert not any(d["id"] == target_dep["id"] for d in active_deps_after)

    # Still accessible in all departments and by ID
    all_deps = database.get_departments(conn, active_only=False)
    assert any(d["id"] == target_dep["id"] for d in all_deps)
    fetched = database.get_department_by_id(conn, target_dep["id"])
    assert fetched["is_active"] == 0

    # Reactivate
    database.toggle_department_status(conn, target_dep["id"], 1)
    active_deps_restored = database.get_departments(conn, active_only=True)
    assert any(d["id"] == target_dep["id"] for d in active_deps_restored)


def test_admin_auth_pbkdf2_and_security(temp_db):
    conn, _ = temp_db

    # Initially no admin configured
    assert not database.is_admin_configured(conn)
    assert not database.verify_admin_credentials(conn, "admin", "admin123")

    # Set password
    database.set_admin_password(conn, "admin", "Hospital@Safe#2026")
    assert database.is_admin_configured(conn)

    # Check correct password
    assert database.verify_admin_credentials(conn, "admin", "Hospital@Safe#2026")

    # Check wrong password
    assert not database.verify_admin_credentials(conn, "admin", "WrongPassword")

    # Verify iteration count and hashing properties
    row = conn.execute("SELECT password_hash, salt FROM admin_auth WHERE username = 'admin';").fetchone()
    assert row is not None
    assert len(row["salt"]) == 32  # 16 bytes in hex = 32 chars
    assert len(row["password_hash"]) == 64  # sha256 hex = 64 chars


def test_database_backup(temp_db):
    conn, _ = temp_db
    seed(conn)

    backup_bytes = database.backup_database(conn)
    assert len(backup_bytes) > 0
    # Must be a real binary SQLite 3 database file
    assert backup_bytes.startswith(b"SQLite format 3\x00")


def test_update_doctor(temp_db):
    conn, _ = temp_db
    seed(conn)

    doctors = database.get_doctors(conn, active_only=True)
    assert len(doctors) > 0
    target_doc = doctors[0]
    doc_id = target_doc["id"]

    updated_name = "Dr. Arvind Menon Senior"
    updated_fee = 2500
    updated_pct = 55
    updated_spec = "Senior Interventional Cardiologist"

    res_id = database.save_doctor(
        conn,
        name=updated_name,
        department_id=target_doc["department_id"],
        consultation_fee=updated_fee,
        surgeon_fee_pct=updated_pct,
        specialization=updated_spec,
        doc_id=doc_id,
    )
    assert res_id == doc_id

    # Verify updated values in database
    fetched = database.get_doctor_by_id(conn, doc_id)
    assert fetched is not None
    assert fetched["name"] == updated_name
    assert fetched["consultation_fee"] == updated_fee
    assert fetched["surgeon_fee_pct"] == updated_pct
    assert fetched["specialization"] == updated_spec
