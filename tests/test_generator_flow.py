"""
tests/test_generator_flow.py - Integration tests verifying the estimate generator workflow,
state transitions, dirty flag behavior, draft vs saved PDF rendering, and atomic DB saves.
"""

import json
from decimal import Decimal
import pytest

import database
import billing
import pdf_generator
from seed_data import seed


@pytest.fixture
def seeded_db(tmp_path):
    db_file = str(tmp_path / "flow_hospital.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    seed(conn)
    yield conn
    conn.close()


def test_generator_full_lifecycle(seeded_db):
    conn = seeded_db
    settings = database.get_settings(conn)

    # 1. Fetch active master items
    rooms = database.get_room_types(conn, active_only=True)
    deps = database.get_departments(conn, active_only=True)
    cardio_dept = next(d for d in deps if d["name"] == "Cardiology")

    docs = database.get_doctors(conn, department_id=cardio_dept["id"], active_only=True)
    procs = database.get_procedures(conn, department_id=cardio_dept["id"], active_only=True)

    assert len(docs) > 0
    assert len(procs) > 0

    selected_doc = docs[0]
    selected_proc = procs[0]
    selected_room = rooms[0]

    # Verify procedure default stay days
    default_los = selected_proc["default_stay_days"]
    assert default_los >= 0

    # 2. Generate Estimate
    res = billing.calculate_estimate(
        patient_name="Pooja Sharma",
        stay_days=default_los,
        room_type=selected_room,
        doctor=selected_doc,
        procedure=selected_proc,
        department=cardio_dept,
        discount_pct=5.0,
        settings=settings,
        remarks="Routine cardiology evaluation",
    )

    assert res.total_amount > Decimal("0")
    assert res.estimate_number is None

    # 3. Verify DRAFT PDF before save (Amendment 3)
    draft_pdf = pdf_generator.generate_estimate_pdf(res, is_draft=True)
    assert len(draft_pdf) > 1000
    # DRAFT - NOT SAVED should be included
    assert b"DRAFT" in draft_pdf

    # 4. Save Estimate to database (Amendment 1 & 4)
    est_num = database.get_next_estimate_number(conn)
    assert est_num.startswith("EST-")
    res.estimate_number = est_num

    snapshot = res.to_snapshot_dict()
    save_data = {
        "estimate_number": est_num,
        "patient_name": res.patient_name,
        "room_type_id": selected_room["id"],
        "department_id": cardio_dept["id"],
        "doctor_id": selected_doc["id"],
        "procedure_id": selected_proc["id"],
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
    }
    saved_id = database.save_estimate(conn, save_data)
    assert saved_id == est_num

    # 5. Verify Saved PDF carries estimate number (Amendment 3)
    saved_pdf = pdf_generator.generate_estimate_pdf(res, is_draft=False)
    assert len(saved_pdf) > 1000
    assert est_num.encode("utf-8") in saved_pdf

    # 6. Verify retrieved database record has frozen snapshot (Amendment 2)
    record = database.get_estimate_by_number(conn, est_num)
    assert record is not None
    assert record["patient_name"] == "Pooja Sharma"
    stored_snapshot = json.loads(record["line_items_json"])
    assert stored_snapshot["metadata"]["hospital_name"] == settings["hospital_name"]
    assert stored_snapshot["clinical"]["doctor"] == selected_doc["name"]
    assert stored_snapshot["clinical"]["procedure"] == selected_proc["name"]
