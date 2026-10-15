"""
tests/test_app_test.py - UI testing of app.py using streamlit.testing.v1.AppTest.

Covers:
1. First-run administrator onboarding & setup.
2. Duplicate department error handling (friendly error, no uncaught exception).
3. Adding and editing a doctor and a procedure without exceptions.
4. Switching selected doctor reloading form values to that doctor.
5. Saving without changes leaving the record unchanged.
6. CSV upload preview and atomic import workflow.
"""

import os
import tempfile
from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest

import database
import seed_data

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def app_test_db(monkeypatch):
    """Provide an isolated temporary database for AppTest runs."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    monkeypatch.setenv("HOSPITAL_DB_PATH", db_path)
    conn = database.get_connection(db_path)
    database.init_db(conn)
    seed_data.seed(conn)
    database.set_admin_password(conn, "admin", "AdminPassword123")
    conn.close()

    yield db_path

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_app_first_run_admin_setup(monkeypatch):
    """Test first-run onboarding when no administrator account exists."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        fresh_db_path = tmp.name

    monkeypatch.setenv("HOSPITAL_DB_PATH", fresh_db_path)
    conn = database.get_connection(fresh_db_path)
    database.init_db(conn)
    seed_data.seed(conn)
    # Ensure no admin is configured
    conn.execute("DELETE FROM admin_auth")
    conn.commit()
    conn.close()

    try:
        at = AppTest.from_file(APP_PATH, default_timeout=30)
        at.run()
        at.sidebar.radio[0].set_value("Admin Portal").run()

        # Should show first-run setup form with 3 text inputs and submit button
        assert len(at.text_input) == 3
        at.text_input[0].set_value("primary_admin")
        at.text_input[1].set_value("SecurePass2026!")
        at.text_input[2].set_value("SecurePass2026!")
        at.button[0].click().run()

        assert len(at.exception) == 0
        assert at.session_state.get("admin_authenticated") is True
        assert at.session_state.get("admin_username") == "primary_admin"

        # Verify password was saved correctly in SQLite
        chk_conn = database.get_connection(fresh_db_path)
        try:
            assert database.verify_admin_credentials(chk_conn, "primary_admin", "SecurePass2026!")
        finally:
            chk_conn.close()
    finally:
        if os.path.exists(fresh_db_path):
            try:
                os.remove(fresh_db_path)
            except OSError:
                pass


def test_app_add_duplicate_department_shows_friendly_error(app_test_db):
    """Test adding an existing department shows a friendly error without unhandled exception."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["admin_authenticated"] = True
    at.session_state["admin_username"] = "admin"
    at.run()
    at.sidebar.radio[0].set_value("Admin Portal").run()

    # In Department tab (tab index 1)
    # Add form: text_input[0] is 'Department Name *', button[0] is '➕ Add Department'
    at.tabs[1].text_input[0].set_value("Cardiology")
    at.tabs[1].button[0].click().run()

    # Friendly error captured, no unhandled exception
    assert len(at.exception) == 0
    assert len(at.error) > 0
    assert "already exists" in at.error[0].value.lower()


def test_app_add_and_edit_doctor_and_procedure(app_test_db):
    """Test adding and editing doctor and procedure records without exception."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["admin_authenticated"] = True
    at.session_state["admin_username"] = "admin"
    at.run()
    at.sidebar.radio[0].set_value("Admin Portal").run()

    # --- 1. Add Doctor ---
    at.tabs[2].text_input[0].set_value("Dr. Test Surgeon")
    at.tabs[2].text_input[1].set_value("2500")
    at.tabs[2].number_input[0].set_value(45)
    at.tabs[2].text_input[2].set_value("Oncological Surgery")
    at.tabs[2].button[0].click().run()
    assert len(at.exception) == 0

    chk_conn = database.get_connection(app_test_db)
    try:
        docs = database.get_doctors(chk_conn, active_only=False)
        added_doc = next((d for d in docs if d["name"] == "Dr. Test Surgeon"), None)
        assert added_doc is not None
        assert added_doc["consultation_fee"] == 2500
        assert added_doc["surgeon_fee_pct"] == 45
    finally:
        chk_conn.close()

    # --- 2. Edit Doctor ---
    # Select our newly added doctor in selectbox[1]
    doc_options = at.tabs[2].selectbox[1].options
    matching_idx = next(i for i, opt in enumerate(doc_options) if "Dr. Test Surgeon" in opt)
    at.tabs[2].selectbox[1].select_index(matching_idx).run()

    # Change fee to 3200 and specialization
    at.tabs[2].text_input[4].set_value("3200")
    at.tabs[2].text_input[5].set_value("Senior Oncological Surgeon")
    at.tabs[2].button[1].click().run()
    assert len(at.exception) == 0

    chk_conn = database.get_connection(app_test_db)
    try:
        docs = database.get_doctors(chk_conn, active_only=False)
        updated_doc = next(d for d in docs if d["name"] == "Dr. Test Surgeon")
        assert updated_doc["consultation_fee"] == 3200
        assert updated_doc["specialization"] == "Senior Oncological Surgeon"
    finally:
        chk_conn.close()

    # --- 3. Add Procedure ---
    at.tabs[3].text_input[0].set_value("Minimally Invasive Nephrectomy")
    at.tabs[3].text_input[1].set_value("85000")
    at.tabs[3].text_input[2].set_value("30000")
    at.tabs[3].text_input[3].set_value("12000")
    at.tabs[3].text_input[4].set_value("90000")
    at.tabs[3].text_input[5].set_value("8000")
    at.tabs[3].number_input[0].set_value(3)
    at.tabs[3].button[0].click().run()
    assert len(at.exception) == 0

    chk_conn = database.get_connection(app_test_db)
    try:
        procs = database.get_procedures(chk_conn, active_only=False)
        added_proc = next((p for p in procs if p["name"] == "Minimally Invasive Nephrectomy"), None)
        assert added_proc is not None
        assert added_proc["base_cost"] == 85000
    finally:
        chk_conn.close()

    # --- 4. Edit Procedure ---
    proc_options = at.tabs[3].selectbox[1].options
    matching_pr_idx = next(i for i, opt in enumerate(proc_options) if "Minimally Invasive Nephrectomy" in opt)
    at.tabs[3].selectbox[1].select_index(matching_pr_idx).run()

    # Update base cost to 95000
    at.tabs[3].text_input[7].set_value("95000")
    at.tabs[3].button[1].click().run()
    assert len(at.exception) == 0

    chk_conn = database.get_connection(app_test_db)
    try:
        procs = database.get_procedures(chk_conn, active_only=False)
        updated_proc = next(p for p in procs if p["name"] == "Minimally Invasive Nephrectomy")
        assert updated_proc["base_cost"] == 95000
    finally:
        chk_conn.close()


def test_app_select_second_doctor_reloads_values_and_saving_without_changes_leaves_record_unchanged(app_test_db):
    """Test switching doctors reloads form values and saving without changes is idempotent."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["admin_authenticated"] = True
    at.session_state["admin_username"] = "admin"
    at.run()
    at.sidebar.radio[0].set_value("Admin Portal").run()

    doc_options = at.tabs[2].selectbox[1].options
    assert len(doc_options) >= 2

    # Initial doctor
    initial_doc_name = at.tabs[2].text_input[3].value
    assert initial_doc_name in doc_options[0]

    # Select second doctor
    at.tabs[2].selectbox[1].select_index(1).run()
    second_doc_name = at.tabs[2].text_input[3].value
    assert second_doc_name in doc_options[1]
    assert second_doc_name != initial_doc_name

    # Fetch doctor details from database before save
    chk_conn = database.get_connection(app_test_db)
    try:
        docs = database.get_doctors(chk_conn, active_only=False)
        before_doc = next(d for d in docs if d["name"] == second_doc_name)
    finally:
        chk_conn.close()

    # Save without modifying any form inputs
    at.tabs[2].button[1].click().run()
    assert len(at.exception) == 0

    # Verify doctor record in database is completely unchanged
    chk_conn = database.get_connection(app_test_db)
    try:
        docs = database.get_doctors(chk_conn, active_only=False)
        after_doc = next(d for d in docs if d["name"] == second_doc_name)
        assert before_doc["name"] == after_doc["name"]
        assert before_doc["consultation_fee"] == after_doc["consultation_fee"]
        assert before_doc["surgeon_fee_pct"] == after_doc["surgeon_fee_pct"]
        assert before_doc["department_id"] == after_doc["department_id"]
        assert before_doc["specialization"] == after_doc["specialization"]
    finally:
        chk_conn.close()


def test_app_csv_upload_preview_and_import(app_test_db):
    """Test CSV file uploader preview and transactional import via AppTest."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["admin_authenticated"] = True
    at.session_state["admin_username"] = "admin"
    at.run()
    at.sidebar.radio[0].set_value("Admin Portal").run()

    # In CSV Import / Export tab (tab index 5)
    csv_bytes = b"name,is_active\nEndocrinology,1\nRheumatology,1\n"
    at.tabs[5].file_uploader[0].upload("new_depts.csv", csv_bytes)
    at.run()

    # Preview dataframe rendered
    assert len(at.tabs[5].dataframe) >= 1

    # Confirm & Import button is now rendered
    assert len(at.tabs[5].button) >= 1
    at.tabs[5].button[0].click().run()

    assert len(at.exception) == 0

    # Verify new departments imported into database
    chk_conn = database.get_connection(app_test_db)
    try:
        depts = database.get_departments(chk_conn, active_only=False)
        dept_names = {d["name"] for d in depts}
        assert "Endocrinology" in dept_names
        assert "Rheumatology" in dept_names
    finally:
        chk_conn.close()


def test_app_admin_edit_form_prevents_stale_values_across_record_selection(app_test_db):
    """Verify that unsaved form input on one record does not leak into another record when switched."""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["admin_authenticated"] = True
    at.session_state["admin_username"] = "admin"
    at.run()
    at.sidebar.radio[0].set_value("Admin Portal").run()

    # --- 1. Doctor Edit Form Stale Value Check ---
    doc_options = at.tabs[2].selectbox[1].options
    assert len(doc_options) >= 2

    # Get DB values for doctor 0 and doctor 1
    chk_conn = database.get_connection(app_test_db)
    try:
        all_docs = database.get_doctors(chk_conn, active_only=False)
        doc0 = all_docs[0]
        doc1 = all_docs[1]
    finally:
        chk_conn.close()

    # Verify doctor 0's initial fee
    assert str(at.tabs[2].text_input[4].value) == str(doc0["consultation_fee"])

    # Dirty the input for doctor 0 without saving
    at.tabs[2].text_input[4].set_value("99999")

    # Switch selectbox to Doctor 1
    at.tabs[2].selectbox[1].select_index(1).run()

    # Doctor 1's fee input must display Doctor 1's actual DB fee, NOT Doctor 0's dirty/stale '99999'
    doc1_displayed_fee = str(at.tabs[2].text_input[4].value)
    assert doc1_displayed_fee == str(doc1["consultation_fee"])
    assert doc1_displayed_fee != "99999"

    # --- 2. Procedure Edit Form Stale Value Check ---
    proc_options = at.tabs[3].selectbox[1].options
    assert len(proc_options) >= 2

    chk_conn = database.get_connection(app_test_db)
    try:
        all_procs = database.get_procedures(chk_conn, active_only=False)
        proc0 = all_procs[0]
        proc1 = all_procs[1]
    finally:
        chk_conn.close()

    # Verify procedure 0's initial base cost
    assert str(at.tabs[3].text_input[7].value) == str(proc0["base_cost"])

    # Dirty procedure 0's base cost without saving
    at.tabs[3].text_input[7].set_value("888888")

    # Switch selectbox to Procedure 1
    at.tabs[3].selectbox[1].select_index(1).run()

    # Procedure 1's base cost must display Procedure 1's actual DB cost, NOT '888888'
    proc1_displayed_base = str(at.tabs[3].text_input[7].value)
    assert proc1_displayed_base == str(proc1["base_cost"])
    assert proc1_displayed_base != "888888"

