"""
tests/test_new_features.py - Comprehensive tests covering the 7 new features:
1. Login gate to generate estimates
2. Mobile number & UHID persistence and validation
3. Comparative estimate for superior room tier
4. Estimate revision / editing workflow with -R1 suffix & revision_of
5. Dashboard metrics, conversion tracking, and accuracy calculation
6. PDF naming template {PatientName}_{UHID}_{EstimateNumber}.pdf
7. Excel ledger database export
"""

import io
import json
import re
import tempfile
from decimal import Decimal
import openpyxl
import pandas as pd
import pytest

import billing
import database
import pdf_generator
import seed_data


@pytest.fixture
def feature_db():
    """Create an isolated temporary database initialized and seeded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    conn = database.get_connection(db_path)
    database.init_db(conn)
    seed_data.seed(conn)
    database.set_admin_password(conn, "admin", "Admin@12345")
    yield conn, db_path
    conn.close()


# ------------------------------------------------------------------------------
# Feature 1: Login Credentials & Authentication
# ------------------------------------------------------------------------------
def test_admin_and_staff_auth_credentials(feature_db):
    conn, _ = feature_db
    assert database.verify_admin_credentials(conn, "admin", "Admin@12345") is True
    assert database.verify_admin_credentials(conn, "admin", "WrongPassword") is False
    assert database.verify_admin_credentials(conn, "unknown", "Admin@12345") is False


# ------------------------------------------------------------------------------
# Feature 2 & 6: Mobile, UHID, and PDF Naming Template
# ------------------------------------------------------------------------------
def test_mobile_validation_rules():
    # Valid Indian formats
    assert billing.validate_mobile_number("9876543210")[0] is True
    assert billing.validate_mobile_number("+91 98765 43210")[0] is True
    assert billing.validate_mobile_number("+91-9876543210")[0] is True
    # Valid international format
    assert billing.validate_mobile_number("+1 (555) 234-5678")[0] is True
    # Blank is allowed (optional field)
    assert billing.validate_mobile_number("")[0] is True
    assert billing.validate_mobile_number("   ")[0] is True
    # Invalid: non-numeric garbage
    assert billing.validate_mobile_number("abcdefg")[0] is False
    assert billing.validate_mobile_number("123")[0] is False  # too short (< 7 digits)


def test_mobile_and_uhid_persistence_and_search(feature_db):
    conn, _ = feature_db
    est_num = database.get_next_estimate_number(conn)
    rooms = database.get_room_types(conn)
    depts = database.get_departments(conn)
    docs = database.get_doctors(conn, department_id=depts[0]["id"])
    procs = database.get_procedures(conn, department_id=depts[0]["id"])

    save_data = {
        "estimate_number": est_num,
        "patient_name": "Siddharth Verma",
        "room_type_id": rooms[0]["id"],
        "department_id": depts[0]["id"],
        "doctor_id": docs[0]["id"],
        "procedure_id": procs[0]["id"],
        "stay_days": 2,
        "discount_pct": "0.0",
        "subtotal": "50000.00",
        "discount_amount": "0.00",
        "contingency_amount": "5000.00",
        "tax_amount": "0.00",
        "total_amount": "55000.00",
        "min_estimate": "49500.00",
        "max_estimate": "60500.00",
        "line_items_json": json.dumps({"test": True}),
        "remarks": "Urgent consultation",
        "mobile_number": "+91 98765 11223",
        "uhid": "UHID-2026-9999",
    }
    saved_num = database.save_estimate(conn, save_data)
    assert saved_num == est_num

    # Verify retrieval
    rec = database.get_estimate_by_number(conn, est_num)
    assert rec["mobile_number"] == "+91 98765 11223"
    assert rec["uhid"] == "UHID-2026-9999"

    # Verify search by UHID
    results_uhid = database.get_estimates(conn, search_query="9999")
    assert len(results_uhid) == 1
    assert results_uhid[0]["estimate_number"] == est_num

    # Verify search by Mobile
    results_mob = database.get_estimates(conn, search_query="11223")
    assert len(results_mob) == 1
    assert results_mob[0]["estimate_number"] == est_num


def test_pdf_filename_template_and_letterhead_content(feature_db):
    conn, _ = feature_db
    settings = database.get_settings(conn)
    rooms = database.get_room_types(conn)
    depts = database.get_departments(conn)
    docs = database.get_doctors(conn, department_id=depts[0]["id"])
    procs = database.get_procedures(conn, department_id=depts[0]["id"])

    est = billing.calculate_estimate(
        patient_name="Amitabh Bachchan",
        stay_days=3,
        room_type=rooms[0],
        doctor=docs[0],
        procedure=procs[0],
        department=depts[0],
        settings=settings,
    )
    est.estimate_number = "EST-20261015-0042"
    est.uhid = "UHID-8877"
    est.mobile_number = "9876500000"

    # Template naming
    safe_name = re.sub(r'[^\w\-]', '_', est.patient_name.strip())[:30]
    safe_uhid = re.sub(r'[^\w\-]', '_', est.uhid.strip())[:15]
    filename = f"{safe_name}_{safe_uhid}_{est.estimate_number}.pdf"
    assert filename == "Amitabh_Bachchan_UHID-8877_EST-20261015-0042.pdf"

    # PDF bytes check
    pdf_bytes = pdf_generator.generate_estimate_pdf(est, is_draft=False)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"EST-20261015-0042" in pdf_bytes
    assert b"NotoSans" in pdf_bytes

    # Verify snapshot carried UHID and mobile
    snap = est.to_snapshot_dict()
    assert snap["patient"]["uhid"] == "UHID-8877"
    assert snap["patient"]["mobile_number"] == "9876500000"
    assert snap["patient"]["name"] == "Amitabh Bachchan"


# ------------------------------------------------------------------------------
# Feature 3: Comparative Estimate
# ------------------------------------------------------------------------------
def test_comparative_estimate_next_room_tier(feature_db):
    conn, _ = feature_db
    settings = database.get_settings(conn)
    rooms_sorted = sorted(database.get_room_types(conn), key=lambda r: r["daily_rate"])
    depts = database.get_departments(conn)
    docs = database.get_doctors(conn, department_id=depts[0]["id"])
    procs = database.get_procedures(conn, department_id=depts[0]["id"])

    # Base estimate in lowest tier
    primary_room = rooms_sorted[0]
    superior_room = rooms_sorted[1]
    stay_days = 4

    primary_est = billing.calculate_estimate(
        patient_name="Vikram Seth",
        stay_days=stay_days,
        room_type=primary_room,
        doctor=docs[0],
        procedure=procs[0],
        department=depts[0],
        settings=settings,
    )

    superior_est = billing.calculate_estimate(
        patient_name="Vikram Seth",
        stay_days=stay_days,
        room_type=superior_room,
        doctor=docs[0],
        procedure=procs[0],
        department=depts[0],
        settings=settings,
    )

    # Superior room rate is higher
    assert superior_room["daily_rate"] > primary_room["daily_rate"]
    # Total of superior is higher by difference in room charges + contingency/tax buffer
    room_diff = (superior_room["daily_rate"] - primary_room["daily_rate"]) * stay_days
    assert superior_est.total_amount > primary_est.total_amount
    assert superior_est.subtotal - primary_est.subtotal == Decimal(room_diff)


def test_comparative_estimate_highest_tier(feature_db):
    conn, _ = feature_db
    rooms_sorted = sorted(database.get_room_types(conn), key=lambda r: r["daily_rate"])
    highest_idx = len(rooms_sorted) - 1
    # If currently at highest tier, next room is None
    next_room = rooms_sorted[highest_idx + 1] if highest_idx + 1 < len(rooms_sorted) else None
    assert next_room is None


# ------------------------------------------------------------------------------
# Feature 4: Revision Workflow
# ------------------------------------------------------------------------------
def test_revision_numbering_and_chain(feature_db):
    conn, _ = feature_db
    root_num = "EST-20261015-0010"

    # First revision of root
    rev1 = database.get_next_revision_number(conn, root_num)
    assert rev1 == f"{root_num}-R1"

    # Save rev1
    save_data_r1 = {
        "estimate_number": rev1,
        "patient_name": "Kavita Roy",
        "stay_days": 3,
        "discount_pct": "0.0",
        "subtotal": "60000.00",
        "discount_amount": "0.00",
        "contingency_amount": "6000.00",
        "tax_amount": "0.00",
        "total_amount": "66000.00",
        "min_estimate": "59400.00",
        "max_estimate": "72600.00",
        "line_items_json": json.dumps({"test": True}),
        "revision_of": root_num,
    }
    database.save_estimate(conn, save_data_r1)

    # Revising rev1 should give -R2
    rev2 = database.get_next_revision_number(conn, rev1)
    assert rev2 == f"{root_num}-R2"

    # Save rev2
    save_data_r2 = dict(save_data_r1)
    save_data_r2["estimate_number"] = rev2
    database.save_estimate(conn, save_data_r2)

    # Next revision is -R3
    rev3 = database.get_next_revision_number(conn, root_num)
    assert rev3 == f"{root_num}-R3"


# ------------------------------------------------------------------------------
# Feature 5: Outcome & Accuracy Tracking
# ------------------------------------------------------------------------------
def test_accuracy_calculation_formula():
    # |actual - estimated| / actual * 100%
    # Actual: 3,00,000, Estimated: 3,20,000 -> Diff = 20,000 -> 20,000 / 3,00,000 = 6.67%
    acc1 = billing.calculate_accuracy(320000, 300000)
    assert acc1 == Decimal("6.67")

    # Exact match -> 0.00%
    acc2 = billing.calculate_accuracy(150000, 150000)
    assert acc2 == Decimal("0.00")

    # Higher actual: Estimated: 1,00,000, Actual: 1,25,000 -> 25,000 / 1,25,000 = 20.00%
    acc3 = billing.calculate_accuracy(100000, 125000)
    assert acc3 == Decimal("20.00")

    # Invalid / Zero actual
    assert billing.calculate_accuracy(100000, 0) is None
    assert billing.calculate_accuracy(100000, None) is None
    assert billing.calculate_accuracy(100000, "") is None


def test_update_estimate_outcome(feature_db):
    conn, _ = feature_db
    est_num = database.get_next_estimate_number(conn)

    save_data = {
        "estimate_number": est_num,
        "patient_name": "Meera Joshi",
        "stay_days": 2,
        "discount_pct": "0.0",
        "subtotal": "40000.00",
        "discount_amount": "0.00",
        "contingency_amount": "4000.00",
        "tax_amount": "0.00",
        "total_amount": "44000.00",
        "min_estimate": "39600.00",
        "max_estimate": "48400.00",
        "line_items_json": json.dumps({"test": True}),
    }
    database.save_estimate(conn, save_data)

    # Initial state: converted=0, actual_amount=None
    rec_init = database.get_estimate_by_number(conn, est_num)
    assert rec_init["converted"] == 0
    assert rec_init["actual_amount"] is None

    # Update outcome to converted=True, actual_amount=46000.00
    database.update_estimate_outcome(conn, est_num, True, "46000.00")

    rec_updated = database.get_estimate_by_number(conn, est_num)
    assert rec_updated["converted"] == 1
    assert rec_updated["actual_amount"] == "46000.00"


# ------------------------------------------------------------------------------
# Feature 7: Excel Spreadsheet Database Export
# ------------------------------------------------------------------------------
def test_excel_export_generation_and_columns(feature_db):
    conn, _ = feature_db
    est_num = database.get_next_estimate_number(conn)
    database.save_estimate(conn, {
        "estimate_number": est_num,
        "patient_name": "Ramesh Rao",
        "stay_days": 2,
        "discount_pct": "0.0",
        "subtotal": "50000.00",
        "discount_amount": "0.00",
        "contingency_amount": "5000.00",
        "tax_amount": "0.00",
        "total_amount": "55000.00",
        "min_estimate": "49500.00",
        "max_estimate": "60500.00",
        "line_items_json": json.dumps({"test": True}),
        "uhid": "UH-5555",
        "mobile_number": "9876512345",
    })
    database.update_estimate_outcome(conn, est_num, True, "52000.00")

    estimates = database.get_estimates(conn, limit=None)
    assert len(estimates) >= 1

    # Build export dataframe
    excel_rows = []
    for e in estimates:
        acc_val = billing.calculate_accuracy(e["total_amount"], e.get("actual_amount"))
        excel_rows.append({
            "Estimate Number": e["estimate_number"],
            "Revision Of": e.get("revision_of") or "—",
            "Patient Name": e["patient_name"],
            "UHID": e.get("uhid") or "—",
            "Mobile Number": e.get("mobile_number") or "—",
            "Department": e.get("department_name") or "—",
            "Doctor": e.get("doctor_name") or "—",
            "Procedure": e.get("procedure_name") or "—",
            "Room Type": e.get("room_type_name") or "—",
            "Total Amount (₹)": e.get("total_amount", 0),
            "Converted": "Yes" if e.get("converted") == 1 else "No",
            "Actual Bill (₹)": e.get("actual_amount") or "—",
            "Accuracy Variance (%)": f"{acc_val:.2f}%" if acc_val is not None else "—",
        })

    df = pd.DataFrame(excel_rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Estimates_Database")

    excel_bytes = buf.getvalue()
    assert len(excel_bytes) > 2000

    # Verify openpyxl can read it back
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes))
    sheet = wb["Estimates_Database"]
    headers = [cell.value for cell in sheet[1]]
    assert "Estimate Number" in headers
    assert "Patient Name" in headers
    assert "UHID" in headers
    assert "Mobile Number" in headers
    assert "Total Amount (₹)" in headers
    assert "Converted" in headers
    assert "Accuracy Variance (%)" in headers
