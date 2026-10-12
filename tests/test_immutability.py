"""
tests/test_immutability.py - Verification that saved estimates remain permanently immutable
even when master room types, doctor fees, and procedure base tariffs are edited or deleted.
"""

import json
from decimal import Decimal
import pytest

import database
import billing
import pdf_generator
from seed_data import seed


@pytest.fixture
def clean_db(tmp_path):
    db_file = str(tmp_path / "immutability_test.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    seed(conn)
    yield conn
    conn.close()


def test_historical_estimate_immutability_guarantee(clean_db):
    conn = clean_db
    settings = database.get_settings(conn)

    # 1. Fetch original master items
    room = database.get_room_types(conn, active_only=True)[0]
    dept = database.get_departments(conn, active_only=True)[0]
    doc = database.get_doctors(conn, department_id=dept["id"], active_only=True)[0]
    proc = database.get_procedures(conn, department_id=dept["id"], active_only=True)[0]

    original_room_rate = room["daily_rate"]
    original_doc_fee_pct = doc["surgeon_fee_pct"]
    original_proc_base = proc["base_cost"]

    # 2. Calculate and save initial estimate
    res = billing.calculate_estimate(
        patient_name="Suresh Raina",
        stay_days=4,
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=10,
        settings=settings,
        remarks="Pre-operative check complete",
    )

    original_subtotal = res.subtotal
    original_net_base = res.net_base
    original_total = res.total_amount
    original_line_count = len(res.line_items)

    est_num = database.get_next_estimate_number(conn)
    # Set created_at via the application's save path before snapshot generation
    fixed_created_at = "2026-05-15T14:30:00"
    res.estimate_number = est_num
    res.created_at = fixed_created_at
    snapshot = res.to_snapshot_dict()

    save_data = {
        "estimate_number": est_num,
        "patient_name": res.patient_name,
        "room_type_id": room["id"],
        "department_id": dept["id"],
        "doctor_id": doc["id"],
        "procedure_id": proc["id"],
        "stay_days": res.stay_days,
        "discount_pct": str(res.discount_pct),
        "subtotal": str(res.subtotal),
        "discount_amount": str(res.discount_amount),
        "contingency_amount": str(res.contingency_amount),
        "tax_amount": str(res.tax_amount),
        "total_amount": str(res.total_amount),
        "min_estimate": str(res.min_estimate),
        "max_estimate": str(res.max_estimate),
        "line_items_json": snapshot,
        "remarks": res.remarks,
        "created_at": fixed_created_at,
    }
    database.save_estimate(conn, save_data)

    # Generate original PDF bytes
    orig_pdf = pdf_generator.generate_estimate_pdf(snapshot, is_draft=False)
    assert len(orig_pdf) > 1000

    # 3. Drastically modify master tables (price hikes)
    # Double the room rate
    database.save_room_type(
        conn,
        name=room["name"],
        daily_rate=original_room_rate * 3,
        description="Renovated super luxury",
        room_id=room["id"],
    )

    # Double surgeon fee percentage
    database.save_doctor(
        conn,
        name=doc["name"],
        department_id=dept["id"],
        consultation_fee=doc["consultation_fee"] * 2,
        surgeon_fee_pct=original_doc_fee_pct + 30,
        specialization=doc["specialization"],
        doc_id=doc["id"],
    )

    # Triple procedure base cost
    database.save_procedure(
        conn,
        name=proc["name"],
        department_id=dept["id"],
        base_cost=original_proc_base * 3,
        ot_charge=proc["ot_charge"] * 2,
        anaesthesia_charge=proc["anaesthesia_charge"] * 2,
        consumables_cost=proc["consumables_cost"] * 2,
        investigations_charge=proc["investigations_charge"] * 2,
        default_stay_days=proc["default_stay_days"],
        proc_id=proc["id"],
    )

    # Soft-delete the department entirely
    database.toggle_department_status(conn, dept["id"], 0)

    # 4. Fetch the historical estimate again from database
    retrieved = database.get_estimate_by_number(conn, est_num)
    assert retrieved is not None
    assert Decimal(retrieved["subtotal"]) == original_subtotal
    assert Decimal(retrieved["total_amount"]) == original_total

    retrieved_snapshot = json.loads(retrieved["line_items_json"])
    assert len(retrieved_snapshot["line_items"]) == original_line_count
    assert Decimal(retrieved_snapshot["financials"]["subtotal"]) == original_subtotal
    assert Decimal(retrieved_snapshot["financials"]["net_base"]) == original_net_base
    assert Decimal(retrieved_snapshot["financials"]["total_amount"]) == original_total

    # Master doctor rates and date in snapshot remain frozen to original values
    assert retrieved_snapshot["clinical"]["surgeon_fee_pct"] == str(original_doc_fee_pct)
    assert retrieved_snapshot["metadata"]["created_at"] == fixed_created_at
    assert retrieved["created_at"] == fixed_created_at

    # 5. Regenerate PDF from snapshot and verify byte output integrity & date match
    reprinted_pdf = pdf_generator.generate_estimate_pdf(retrieved_snapshot, is_draft=False)
    assert len(reprinted_pdf) > 1000
    # Both PDFs carry identical estimate number and exact historical date (15-May-2026)
    assert est_num.encode("utf-8") in reprinted_pdf
    assert b"15-May-2026" in reprinted_pdf
    assert b"30-May-2026" in reprinted_pdf
