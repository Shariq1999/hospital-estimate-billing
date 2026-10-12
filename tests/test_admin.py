"""
tests/test_admin.py - Comprehensive tests for Phase 6 Admin features:
PBKDF2 authentication, first-run setup, password changes,
soft delete / restore operations, duplicate-name IntegrityErrors,
formula-neutralized CSV import/export with BOM, and atomic rollback on invalid data.
"""

import os
import sqlite3
import pytest

import database
from seed_data import seed, DEPARTMENTS, ROOM_TYPES, DOCTORS, PROCEDURES


@pytest.fixture
def temp_admin_db(tmp_path):
    """Fixture providing a fresh seeded database for admin testing."""
    db_file = str(tmp_path / "test_admin.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    seed(conn)
    yield conn, db_file
    conn.close()
    for ext in ["", "-wal", "-shm"]:
        f = db_file + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


# ==============================================================================
# 1. Authentication & Security Tests
# ==============================================================================

def test_admin_first_run_setup_and_auth_workflow(temp_admin_db):
    conn, _ = temp_admin_db

    # 1. Fresh database has no admin configured initially
    assert not database.is_admin_configured(conn)

    # 2. Attempt login with unconfigured account fails
    assert not database.verify_admin_credentials(conn, "admin", "Password123")

    # 3. Create initial admin account
    database.set_admin_password(conn, "admin", "InitialSafePassword123")
    assert database.is_admin_configured(conn)

    # 4. Successful login
    assert database.verify_admin_credentials(conn, "admin", "InitialSafePassword123")

    # 5. Failed login with wrong password
    assert not database.verify_admin_credentials(conn, "admin", "WrongPassword")

    # 6. Change password requiring current password
    # Incorrect current password fails
    assert not database.verify_admin_credentials(conn, "admin", "BadCurrentPassword")

    # Correct current password allows update
    assert database.verify_admin_credentials(conn, "admin", "InitialSafePassword123")
    database.set_admin_password(conn, "admin", "NewSuperSecure2026!")

    # Verify old password no longer works and new password works
    assert not database.verify_admin_credentials(conn, "admin", "InitialSafePassword123")
    assert database.verify_admin_credentials(conn, "admin", "NewSuperSecure2026!")


def test_emergency_admin_reset_script(temp_admin_db, monkeypatch):
    """Test reset_admin.py command-line reset: old password fails, new one works, file checks."""
    import reset_admin
    conn, db_path = temp_admin_db

    # Setup initial admin password
    database.set_admin_password(conn, "admin", "OldPassword123")
    assert database.verify_admin_credentials(conn, "admin", "OldPassword123")

    # 1. Abort if database file does not exist
    non_existent_db = db_path + ".missing"
    assert not reset_admin.reset_password(username="admin", db_path=non_existent_db)

    # 2. Abort if user does not exist (never create a new account)
    assert not reset_admin.reset_password(username="ghost_user", db_path=db_path)

    # 3. Aborted reset when typed confirmation is wrong
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "NewEmergencyPwd123")
    assert not reset_admin.reset_password(username="admin", db_path=db_path)
    # Old password still intact
    assert database.verify_admin_credentials(conn, "admin", "OldPassword123")

    # 4. Aborted reset when password mismatch
    pw_sequence = iter(["NewEmergencyPwd123", "DifferentPwd123"])
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(pw_sequence))
    assert not reset_admin.reset_password(username="admin", db_path=db_path)
    assert database.verify_admin_credentials(conn, "admin", "OldPassword123")

    # 5. Successful reset with 'RESET' confirmation and matching passwords (min 8 chars)
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "BrandNewEmergency2026!")
    success = reset_admin.reset_password(username="admin", db_path=db_path)
    assert success

    # Verify: after the reset, the old password fails and the new one works
    assert not database.verify_admin_credentials(conn, "admin", "OldPassword123")
    assert database.verify_admin_credentials(conn, "admin", "BrandNewEmergency2026!")


def test_seed_flag_prevents_reseeding_after_table_emptied(temp_admin_db):
    """Verify seed() runs only once: emptying departments table does not bring sample data back."""
    conn, _ = temp_admin_db

    # Database was seeded in fixture
    assert conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] > 0
    flag = conn.execute("SELECT value FROM settings WHERE key = 'seeded'").fetchone()
    assert flag and flag[0] == "1"

    # Admin removes all departments (e.g. to replace with custom hospital tariffs)
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("DELETE FROM procedures;")
    conn.execute("DELETE FROM doctors;")
    conn.execute("DELETE FROM departments;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0

    # Call seed again (simulating application restart)
    seed(conn)

    # Verify seed does NOT bring back sample data because 'seeded' flag is preserved
    assert conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0


# ==============================================================================
# 2. Soft-Delete and Restore Tests (All Master Tables)
# ==============================================================================

def test_soft_delete_and_restore_all_master_tables(temp_admin_db):
    conn, _ = temp_admin_db

    # --- A. Departments ---
    depts = database.get_departments(conn, active_only=True)
    orig_dept_count = len(depts)
    target_dept = depts[0]
    dept_id = target_dept["id"]

    # Deactivate (Soft delete)
    database.toggle_department_status(conn, dept_id, 0)
    active_depts = database.get_departments(conn, active_only=True)
    assert len(active_depts) == orig_dept_count - 1
    assert not any(d["id"] == dept_id for d in active_depts)

    # Still present in master list
    all_depts = database.get_departments(conn, active_only=False)
    assert len(all_depts) == orig_dept_count
    assert any(d["id"] == dept_id and d["is_active"] == 0 for d in all_depts)

    # Reactivate (Restore)
    database.toggle_department_status(conn, dept_id, 1)
    restored_depts = database.get_departments(conn, active_only=True)
    assert len(restored_depts) == orig_dept_count

    # --- B. Room Types ---
    rooms = database.get_room_types(conn, active_only=True)
    orig_room_count = len(rooms)
    target_room = rooms[0]
    room_id = target_room["id"]

    # Deactivate
    database.toggle_room_type_status(conn, room_id, 0)
    active_rooms = database.get_room_types(conn, active_only=True)
    assert len(active_rooms) == orig_room_count - 1
    assert not any(r["id"] == room_id for r in active_rooms)

    # Restore
    database.toggle_room_type_status(conn, room_id, 1)
    restored_rooms = database.get_room_types(conn, active_only=True)
    assert len(restored_rooms) == orig_room_count

    # --- C. Doctors ---
    docs = database.get_doctors(conn, active_only=True)
    orig_doc_count = len(docs)
    target_doc = docs[0]
    doc_id = target_doc["id"]

    # Deactivate
    database.toggle_doctor_status(conn, doc_id, 0)
    active_docs = database.get_doctors(conn, active_only=True)
    assert len(active_docs) == orig_doc_count - 1
    assert not any(d["id"] == doc_id for d in active_docs)

    # Restore
    database.toggle_doctor_status(conn, doc_id, 1)
    restored_docs = database.get_doctors(conn, active_only=True)
    assert len(restored_docs) == orig_doc_count

    # --- D. Procedures ---
    procs = database.get_procedures(conn, active_only=True)
    orig_proc_count = len(procs)
    target_proc = procs[0]
    proc_id = target_proc["id"]

    # Deactivate
    database.toggle_procedure_status(conn, proc_id, 0)
    active_procs = database.get_procedures(conn, active_only=True)
    assert len(active_procs) == orig_proc_count - 1
    assert not any(p["id"] == proc_id for p in active_procs)

    # Restore
    database.toggle_procedure_status(conn, proc_id, 1)
    restored_procs = database.get_procedures(conn, active_only=True)
    assert len(restored_procs) == orig_proc_count


# ==============================================================================
# 3. Duplicate Name IntegrityError Tests
# ==============================================================================

def test_duplicate_name_integrity_errors(temp_admin_db):
    conn, _ = temp_admin_db

    # 1. Duplicate Department Name
    with pytest.raises(sqlite3.IntegrityError):
        database.save_department(conn, "Cardiology")  # Already in seed data

    # Case-insensitive duplicate check
    with pytest.raises(sqlite3.IntegrityError):
        database.save_department(conn, "cardiology")

    # 2. Duplicate Room Type Name
    with pytest.raises(sqlite3.IntegrityError):
        database.save_room_type(conn, "Single Private Room", 6000, "Duplicate room")

    with pytest.raises(sqlite3.IntegrityError):
        database.save_room_type(conn, "single private room", 6000, "Duplicate room")

    # 3. Duplicate Doctor in same department
    dept = database.get_departments(conn)[0]
    with pytest.raises(sqlite3.IntegrityError):
        database.save_doctor(
            conn,
            name="Dr. Arvind Menon",  # Already in Cardiology
            department_id=dept["id"],
            consultation_fee=1500,
            surgeon_fee_pct=50,
            specialization="Interventional Cardiologist",
        )

    # 4. Duplicate Procedure in same department
    with pytest.raises(sqlite3.IntegrityError):
        database.save_procedure(
            conn,
            name="Coronary Angiography",  # Already in Cardiology
            department_id=dept["id"],
            base_cost=25000,
            ot_charge=12000,
            anaesthesia_charge=5000,
            consumables_cost=8000,
            investigations_charge=4000,
            default_stay_days=1,
        )


# ==============================================================================
# 4. CSV Export Tests (BOM, Formula Neutralization, Reference by Name)
# ==============================================================================

def test_csv_export_neutralizes_formulas_and_includes_bom(temp_admin_db):
    conn, _ = temp_admin_db

    # Insert test items starting with formula symbols: =, +, -, @
    database.save_room_type(conn, "=SUM(1+2)", 3000, "-Special Discount Room")

    csv_bytes = database.export_room_types_csv(conn)

    # Verify UTF-8 with BOM prefix
    assert csv_bytes.startswith(b"\xef\xbb\xbf")

    csv_text = csv_bytes.decode("utf-8-sig")
    assert "'=SUM(1+2)" in csv_text
    assert "'-Special Discount Room" in csv_text


def test_csv_export_departments_referenced_by_name(temp_admin_db):
    conn, _ = temp_admin_db

    doc_csv = database.export_doctors_csv(conn).decode("utf-8-sig")
    assert "Cardiology" in doc_csv
    assert "department_id" not in doc_csv.lower()

    proc_csv = database.export_procedures_csv(conn).decode("utf-8-sig")
    assert "Orthopaedics" in proc_csv
    assert "department_id" not in proc_csv.lower()


# ==============================================================================
# 5. CSV Round-Trip Tests (Add / Update Only, No Delete-All)
# ==============================================================================

def test_csv_round_trip_success(temp_admin_db):
    conn, _ = temp_admin_db

    # Export all 4 categories
    dep_bytes = database.export_departments_csv(conn)
    room_bytes = database.export_room_types_csv(conn)
    doc_bytes = database.export_doctors_csv(conn)
    proc_bytes = database.export_procedures_csv(conn)

    # Re-import identical files: all records should match and be updated without errors
    ins_dep, upd_dep = database.import_departments_csv(conn, dep_bytes)
    assert ins_dep == 0
    assert upd_dep == len(DEPARTMENTS)

    ins_r, upd_r = database.import_room_types_csv(conn, room_bytes)
    assert ins_r == 0
    assert upd_r == len(ROOM_TYPES)

    ins_doc, upd_doc = database.import_doctors_csv(conn, doc_bytes)
    assert ins_doc == 0
    assert upd_doc == len(DOCTORS)

    ins_p, upd_p = database.import_procedures_csv(conn, proc_bytes)
    assert ins_p == 0
    assert upd_p == len(PROCEDURES)


def test_csv_import_add_and_update_behavior(temp_admin_db):
    conn, _ = temp_admin_db

    # CSV with 1 existing department (to update) and 1 new department (to insert)
    csv_content = "name,is_active\nCardiology,0\nPaediatrics,1\n"
    ins, upd = database.import_departments_csv(conn, csv_content)
    assert ins == 1
    assert upd == 1

    # Verify Cardiology was updated to inactive (not duplicated or deleted)
    cardio = conn.execute("SELECT is_active FROM departments WHERE name = 'Cardiology'").fetchone()
    assert cardio["is_active"] == 0

    # Verify Paediatrics was added
    paed = conn.execute("SELECT is_active FROM departments WHERE name = 'Paediatrics'").fetchone()
    assert paed is not None
    assert paed["is_active"] == 1


# ==============================================================================
# 6. CSV Rejection of Bad Rows with Atomic Transaction Rollback
# ==============================================================================

def test_csv_import_rejection_invalid_money_rolls_back_atomically(temp_admin_db):
    conn, _ = temp_admin_db

    initial_room_count = len(database.get_room_types(conn, active_only=False))

    # Row 1 is valid, Row 2 has non-numeric "abc" for daily_rate
    bad_csv = (
        "name,daily_rate,description,is_active\n"
        "Valid Suite,9500,Luxury,1\n"
        "Invalid Room,abc,Bad Rate,1\n"
    )

    with pytest.raises(ValueError) as excinfo:
        database.import_room_types_csv(conn, bad_csv)

    assert "Invalid number format" in str(excinfo.value)

    # Assert atomic rollback: 'Valid Suite' was NOT inserted
    current_room_count = len(database.get_room_types(conn, active_only=False))
    assert current_room_count == initial_room_count
    assert not conn.execute("SELECT id FROM room_types WHERE name = 'Valid Suite'").fetchone()


def test_csv_import_rejection_missing_department_rolls_back_atomically(temp_admin_db):
    conn, _ = temp_admin_db

    initial_doc_count = len(database.get_doctors(conn, active_only=False))

    # Doctor references department 'SpaceMedicine' which does not exist
    bad_csv = (
        "name,department,consultation_fee,surgeon_fee_pct,specialization,is_active\n"
        "Dr. Test Surgeon,SpaceMedicine,2000,50,Cosmonaut,1\n"
    )

    with pytest.raises(ValueError) as excinfo:
        database.import_doctors_csv(conn, bad_csv)

    assert "does not exist" in str(excinfo.value)

    # Assert no change in doctors
    assert len(database.get_doctors(conn, active_only=False)) == initial_doc_count


def test_csv_import_rejection_invalid_percentage_rolls_back(temp_admin_db):
    conn, _ = temp_admin_db

    initial_doc_count = len(database.get_doctors(conn, active_only=False))

    # surgeon_fee_pct = 150 (must be between 0 and 100)
    bad_csv = (
        "name,department,consultation_fee,surgeon_fee_pct,specialization,is_active\n"
        "Dr. Greedy,Cardiology,1500,150,Specialist,1\n"
    )

    with pytest.raises(ValueError) as excinfo:
        database.import_doctors_csv(conn, bad_csv)

    assert "must be between" in str(excinfo.value)
    assert len(database.get_doctors(conn, active_only=False)) == initial_doc_count


def test_csv_import_empty_raises_error(temp_admin_db):
    conn, _ = temp_admin_db

    with pytest.raises(ValueError) as excinfo:
        database.import_departments_csv(conn, "")

    assert "missing headers" in str(excinfo.value)


def test_csv_import_rejection_non_whole_rupees_and_fractional_percentages(temp_admin_db):
    """Test rejection of non-whole rupee amounts (paise/decimals) and fractional surgeon percentages."""
    conn, _ = temp_admin_db

    # 1. Non-whole rupee rate in room import
    bad_room_csv = (
        "name,daily_rate,description,is_active\n"
        "Fractional Room,5500.50,Decimal room,1\n"
    )
    with pytest.raises(ValueError) as exc1:
        database.import_room_types_csv(conn, bad_room_csv)
    assert "must be a whole rupee amount (columns are whole rupees)" in str(exc1.value)

    # 2. Fractional surgeon fee percentage in doctor import
    bad_doc_csv = (
        "name,department,consultation_fee,surgeon_fee_pct,specialization,is_active\n"
        "Dr. Fractional,Cardiology,1500,42.5,Cardiologist,1\n"
    )
    with pytest.raises(ValueError) as exc2:
        database.import_doctors_csv(conn, bad_doc_csv)
    assert "fractional percentages not allowed" in str(exc2.value)

    # 3. Non-whole rupee base cost in procedure import
    bad_proc_csv = (
        "name,department,base_cost,ot_charge,anaesthesia_charge,consumables_cost,investigations_charge,default_stay_days,is_active\n"
        "Decimal Proc,Cardiology,90000.75,35000,15000,160000,10000,3,1\n"
    )
    with pytest.raises(ValueError) as exc3:
        database.import_procedures_csv(conn, bad_proc_csv)
    assert "must be a whole rupee amount (columns are whole rupees)" in str(exc3.value)
