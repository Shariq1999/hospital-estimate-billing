"""
tests/test_pdf_layout_and_analytics.py - Test suite for pdf_layout.py and analytics_dashboard.py.
Validates:
1. Default initialization, updating, and factory reset of pdf_layout_config in SQLite.
2. Custom PDF layout generation with XML-unsafe characters (<, >, &, ₹) and margins.
3. Historical snapshot immutability across subsequent layout modifications.
4. Revision deduplication (-R1, -R2) logic.
5. Exact estimation accuracy variance formulas (|actual - estimated| / actual * 100%).
6. Doctor and Department KPI aggregations and volume/conversion leaders.
7. Multi-sheet Excel export generation.
"""

import io
import json
import sqlite3
import tempfile
from decimal import Decimal
import openpyxl
import pytest

import billing
import database
import pdf_generator
from pdf_layout import DEFAULT_PDF_LAYOUT, get_merged_layout, create_sample_estimate_snapshot
from analytics_dashboard import (
    get_deduplicated_estimates,
    calculate_estimate_metrics,
    aggregate_kpi_summary,
    aggregate_doctor_performance,
    aggregate_department_performance,
    create_analytics_excel_workbook,
)
from seed_data import seed
from tests.test_corrections_v3 import _extract_pdf_text


@pytest.fixture
def clean_test_db(tmp_path):
    """Provide an isolated database initialized with schema and seed data."""
    db_file = str(tmp_path / "test_pdf_analytics.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    seed(conn)
    yield conn
    conn.close()


# ==============================================================================
# 1. PDF Layout Config Database Persistence & Reset
# ==============================================================================

def test_pdf_layout_config_lifecycle(clean_test_db):
    conn = clean_test_db

    # 1. Default layout retrieval
    default_cfg = database.get_pdf_layout(conn)
    assert default_cfg["hospital_name"] == DEFAULT_PDF_LAYOUT["hospital_name"]
    assert default_cfg["page_size"] == "A4"
    assert float(default_cfg["margin_top_mm"]) == 15.0

    # 2. Update layout with custom attributes
    custom_cfg = dict(default_cfg)
    custom_cfg["hospital_name"] = "Apex Heart & Kidney Institute"
    custom_cfg["primary_color"] = "#2C5282"
    custom_cfg["margin_top_mm"] = 22.5
    custom_cfg["show_discount_row"] = False

    database.save_pdf_layout(conn, custom_cfg)

    # 3. Verify persistence
    retrieved_cfg = database.get_pdf_layout(conn)
    assert retrieved_cfg["hospital_name"] == "Apex Heart & Kidney Institute"
    assert retrieved_cfg["primary_color"] == "#2C5282"
    assert retrieved_cfg["margin_top_mm"] == 22.5
    assert retrieved_cfg["show_discount_row"] is False

    # 4. Factory reset
    reset_cfg = database.reset_pdf_layout(conn)
    assert reset_cfg["hospital_name"] == DEFAULT_PDF_LAYOUT["hospital_name"]
    after_reset = database.get_pdf_layout(conn)
    assert after_reset["hospital_name"] == DEFAULT_PDF_LAYOUT["hospital_name"]


# ==============================================================================
# 2. PDF Rendering with Custom Styles & XML Unsafe Characters
# ==============================================================================

def test_pdf_custom_rendering_with_xml_unsafe_characters(clean_test_db):
    conn = clean_test_db

    sample_snapshot = create_sample_estimate_snapshot()

    # Layout with XML-unsafe characters, custom fonts, colors, and margins
    custom_layout = {
        "hospital_name": "Care & Cure Superspecialty Hospital <NABH Accredited>",
        "hospital_subtitle": "Center for Cardiac & Vascular Surgery (Unit #1)",
        "hospital_address": "Tower A & B, Health City & Bypass Road",
        "hospital_contact": "Tel: +91 22 1111 & 2222 | Email: info@hospital.org",
        "header_alignment": "CENTER",
        "primary_color": "#2A4365",
        "secondary_color": "#2B6CB0",
        "table_header_bg": "#2B6CB0",
        "table_header_text_color": "#FFFFFF",
        "page_size": "LETTER",
        "margin_top_mm": 20.0,
        "margin_bottom_mm": 20.0,
        "margin_left_mm": 20.0,
        "margin_right_mm": 20.0,
        "title_font_size": 20,
        "body_font_size": 9.0,
        "table_font_size": 8.5,
        "show_letterhead_placeholder": False,
        "show_discount_row": True,
        "show_signature_block": True,
        "col_header_sno": "Sl No.",
        "col_header_description": "Tariff Particulars & Services",
        "col_header_category": "Tariff Group",
        "col_header_taxable": "GST Applicable",
        "col_header_amount": "Tariff (₹)",
        "draft_watermark_text": "*** CUSTOM PROVISIONAL ESTIMATE ***",
        "disclaimer_heading": "LEGAL & TARIFF DISCLAIMERS:",
        "disclaimers_text": (
            "1. Tariff subject to change & clinical verification.\n"
            "2. Non-formulary implants & consumables are billed on actuals <Standard Protocol>.\n"
            "3. Emergency OT escalation charges apply for off-hour surgeries."
        ),
        "left_signatory_title": "Counselor (Patient Accounts)",
        "left_signatory_dept": "Admissions & Tariff Cell",
        "right_signatory_title": "Patient / Guardian Signature",
        "right_signatory_dept": "Attendant Undertaking",
    }

    # Generate draft PDF
    draft_pdf = pdf_generator.generate_estimate_pdf(
        sample_snapshot,
        is_draft=True,
        layout_config=custom_layout,
    )
    assert len(draft_pdf) > 1000
    assert draft_pdf.startswith(b"%PDF-")
    draft_text = _extract_pdf_text(draft_pdf)
    assert "CUSTOM PROVISIONAL ESTIMATE" in draft_text

    # Generate finalized saved PDF
    saved_pdf = pdf_generator.generate_estimate_pdf(
        sample_snapshot,
        is_draft=False,
        layout_config=custom_layout,
    )
    assert len(saved_pdf) > 1000
    assert saved_pdf.startswith(b"%PDF-")


# ==============================================================================
# 3. Historical Snapshot Immutability Guarantee Across Layout Changes
# ==============================================================================

def test_pdf_historical_immutability_with_layout_updates(clean_test_db):
    conn = clean_test_db

    # 1. Set initial layout configuration
    initial_layout = {
        "hospital_name": "Original General Hospital",
        "hospital_address": "100 Initial Avenue, Mumbai",
        "primary_color": "#1A365D",
        "disclaimer_heading": "ORIGINAL TERMS & CONDITIONS:",
    }
    database.save_pdf_layout(conn, initial_layout)

    # 2. Create and save estimate
    room = database.get_room_types(conn, active_only=True)[0]
    dept = database.get_departments(conn, active_only=True)[0]
    doc = database.get_doctors(conn, department_id=dept["id"], active_only=True)[0]
    proc = database.get_procedures(conn, department_id=dept["id"], active_only=True)[0]
    settings = database.get_settings(conn)

    res = billing.calculate_estimate(
        patient_name="Prakash Iyer",
        stay_days=2,
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=0,
        settings=settings,
    )

    est_num = database.get_next_estimate_number(conn)
    res.estimate_number = est_num
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
    }
    database.save_estimate(conn, save_data)

    # 3. Verify initial layout is embedded in snapshot
    saved_est = database.get_estimate_by_number(conn, est_num)
    assert saved_est is not None
    saved_snapshot = json.loads(saved_est["line_items_json"])
    assert "pdf_layout_snapshot" in saved_snapshot
    assert saved_snapshot["pdf_layout_snapshot"]["hospital_name"] == "Original General Hospital"

    orig_reprint_pdf = pdf_generator.generate_estimate_pdf(saved_snapshot, is_draft=False)
    orig_text = _extract_pdf_text(orig_reprint_pdf)
    assert "Original General Hospital" in orig_text

    # 4. Now radically change hospital layout in database
    radically_new_layout = {
        "hospital_name": "Radical New Private Care Network",
        "hospital_address": "999 Cyber Boulevard, Bengaluru",
        "primary_color": "#9B2C2C",
        "disclaimer_heading": "NEW RADICAL DISCLAIMERS:",
    }
    database.save_pdf_layout(conn, radically_new_layout)

    # 5. Fetch historical estimate again and reprint
    refetched_est = database.get_estimate_by_number(conn, est_num)
    refetched_snapshot = json.loads(refetched_est["line_items_json"])

    reprinted_pdf = pdf_generator.generate_estimate_pdf(refetched_snapshot, is_draft=False)

    # Historical reprint must strictly retain original branding and NOT adopt new branding
    reprinted_text = _extract_pdf_text(reprinted_pdf)
    assert "Original General Hospital" in reprinted_text
    assert "Radical New Private Care Network" not in reprinted_text


# ==============================================================================
# 4. Revision Deduplication (-R1, -R2)
# ==============================================================================

def test_revision_deduplication_logic():
    estimates = [
        {"id": 1, "estimate_number": "EST-20261025-0001", "total_amount": "50000", "converted": 0},
        {"id": 2, "estimate_number": "EST-20261025-0001-R1", "total_amount": "55000", "converted": 0},
        {"id": 3, "estimate_number": "EST-20261025-0001-R2", "total_amount": "60000", "converted": 1},
        {"id": 4, "estimate_number": "EST-20261025-0002", "total_amount": "30000", "converted": 1},
        {"id": 5, "estimate_number": "EST-20261025-0003", "total_amount": "40000", "converted": 0},
        {"id": 6, "estimate_number": "EST-20261025-0003-R1", "total_amount": "42000", "converted": 1},
    ]

    # When exclude_superseded is True, keep only latest revision of each root
    deduped = get_deduplicated_estimates(estimates, exclude_superseded=True)
    assert len(deduped) == 3

    deduped_numbers = {e["estimate_number"] for e in deduped}
    assert "EST-20261025-0001-R2" in deduped_numbers
    assert "EST-20261025-0002" in deduped_numbers
    assert "EST-20261025-0003-R1" in deduped_numbers
    assert "EST-20261025-0001" not in deduped_numbers
    assert "EST-20261025-0001-R1" not in deduped_numbers

    # When exclude_superseded is False, keep all
    all_est = get_deduplicated_estimates(estimates, exclude_superseded=False)
    assert len(all_est) == 6


# ==============================================================================
# 5. Estimation Accuracy & Signed Drift Formulas
# ==============================================================================

def test_estimation_accuracy_variance_formulas():
    # Case 1: Under-estimated / Bill Overrun (actual > estimated)
    # Estimated: 100,000; Actual: 125,000
    # Variance % = |125,000 - 100,000| / 125,000 * 100 = 20.0%
    # Accuracy % = 100 - 20 = 80.0%
    # Drift % = +20.0%
    # Within 10% band: False
    e1 = {
        "estimate_number": "EST-001",
        "total_amount": "100000.00",
        "actual_amount": "125000.00",
        "converted": 1,
    }
    m1 = calculate_estimate_metrics(e1)
    assert pytest.approx(m1["metrics_variance_pct"], 0.01) == 20.0
    assert pytest.approx(m1["metrics_accuracy_pct"], 0.01) == 80.0
    assert pytest.approx(m1["metrics_drift_pct"], 0.01) == 20.0
    assert m1["metrics_within_10_band"] is False

    # Case 2: Within +/- 10% clinical range buffer
    # Estimated: 100,000; Actual: 95,000
    # Variance % = |95,000 - 100,000| / 95,000 * 100 = 5000 / 95000 * 100 = 5.263%
    # Accuracy % = 94.737%
    # Drift % = -5.263%
    # Within 10% band: True
    e2 = {
        "estimate_number": "EST-002",
        "total_amount": "100000.00",
        "actual_amount": "95000.00",
        "converted": 1,
    }
    m2 = calculate_estimate_metrics(e2)
    assert pytest.approx(m2["metrics_variance_pct"], 0.01) == 5.263
    assert pytest.approx(m2["metrics_accuracy_pct"], 0.01) == 94.737
    assert pytest.approx(m2["metrics_drift_pct"], 0.01) == -5.263
    assert m2["metrics_within_10_band"] is True

    # Case 3: Unbilled case (actual_amount missing or blank)
    e3 = {
        "estimate_number": "EST-003",
        "total_amount": "100000.00",
        "actual_amount": None,
        "converted": 0,
    }
    m3 = calculate_estimate_metrics(e3)
    assert m3["metrics_variance_pct"] is None
    assert m3["metrics_accuracy_pct"] is None
    assert m3["metrics_within_10_band"] is None


# ==============================================================================
# 6. Doctor and Department Performance Aggregations
# ==============================================================================

def test_doctor_and_department_analytics_aggregation():
    raw_data = [
        # Dr. A (Orthopaedics): 3 estimates, 2 converted, 1 audited with 5% var
        {
            "id": 1, "estimate_number": "EST-01", "total_amount": "100000",
            "converted": 1, "actual_amount": "105000",
            "doctor_name": "Dr. A", "department_name": "Orthopaedics",
        },
        {
            "id": 2, "estimate_number": "EST-02", "total_amount": "100000",
            "converted": 1, "actual_amount": None,
            "doctor_name": "Dr. A", "department_name": "Orthopaedics",
        },
        {
            "id": 3, "estimate_number": "EST-03", "total_amount": "100000",
            "converted": 0, "actual_amount": None,
            "doctor_name": "Dr. A", "department_name": "Orthopaedics",
        },
        # Dr. B (Cardiology): 2 estimates, 2 converted (100% conversion), 1 audited with 15% var
        {
            "id": 4, "estimate_number": "EST-04", "total_amount": "200000",
            "converted": 1, "actual_amount": "230000",
            "doctor_name": "Dr. B", "department_name": "Cardiology",
        },
        {
            "id": 5, "estimate_number": "EST-05", "total_amount": "200000",
            "converted": 1, "actual_amount": None,
            "doctor_name": "Dr. B", "department_name": "Cardiology",
        },
    ]

    metrics_list = [calculate_estimate_metrics(e) for e in raw_data]

    # KPI Summary check
    kpi = aggregate_kpi_summary(metrics_list)
    assert kpi["total_count"] == 5
    assert kpi["converted_count"] == 4
    assert pytest.approx(kpi["conversion_rate_pct"], 0.1) == 80.0
    assert kpi["total_pipeline_val"] == Decimal("700000")
    assert kpi["audited_count"] == 2
    assert kpi["within_10_count"] == 1
    assert pytest.approx(kpi["within_10_rate_pct"], 0.1) == 50.0

    # Doctor Performance check
    doc_stats = aggregate_doctor_performance(metrics_list)
    assert len(doc_stats) == 2

    dr_a = next(d for d in doc_stats if d["doctor_name"] == "Dr. A")
    assert dr_a["estimates_given"] == 3
    assert dr_a["converted_count"] == 2
    assert pytest.approx(dr_a["conversion_rate_pct"], 0.1) == 66.7

    dr_b = next(d for d in doc_stats if d["doctor_name"] == "Dr. B")
    assert dr_b["estimates_given"] == 2
    assert dr_b["converted_count"] == 2
    assert pytest.approx(dr_b["conversion_rate_pct"], 0.1) == 100.0

    # Volume leader: Dr. A (3 estimates)
    volume_leader = max(doc_stats, key=lambda x: x["estimates_given"])
    assert volume_leader["doctor_name"] == "Dr. A"

    # Conversion Rate champion: Dr. B (100% conversion)
    conv_champ = max(doc_stats, key=lambda x: x["conversion_rate_pct"])
    assert conv_champ["doctor_name"] == "Dr. B"

    # Department Performance check
    dept_stats = aggregate_department_performance(metrics_list)
    assert len(dept_stats) == 2

    cardio = next(d for d in dept_stats if d["department"] == "Cardiology")
    assert cardio["estimates_given"] == 2
    assert cardio["converted_count"] == 2
    assert cardio["conversion_rate_pct"] == 100.0

    ortho = next(d for d in dept_stats if d["department"] == "Orthopaedics")
    assert ortho["estimates_given"] == 3
    assert ortho["converted_count"] == 2
    assert pytest.approx(ortho["conversion_rate_pct"], 0.1) == 66.7


# ==============================================================================
# 7. Multi-Sheet Excel Workbook Export
# ==============================================================================

def test_analytics_excel_export_generation():
    raw_data = [
        {
            "id": 1, "estimate_number": "EST-20261025-0001", "total_amount": "120000",
            "converted": 1, "actual_amount": "125000",
            "doctor_name": "Dr. Suresh", "department_name": "General Surgery",
            "patient_name": "Anita Roy", "created_at": "2026-10-25T11:00:00",
            "stay_days": 2, "subtotal": "110000", "discount_pct": "0",
            "discount_amount": "0", "contingency_amount": "10000", "tax_amount": "0",
            "min_estimate": "108000", "max_estimate": "132000", "remarks": "Elective",
        }
    ]
    metrics_list = [calculate_estimate_metrics(e) for e in raw_data]
    doc_stats = aggregate_doctor_performance(metrics_list)
    dept_stats = aggregate_department_performance(metrics_list)

    xlsx_bytes = create_analytics_excel_workbook(raw_data, doc_stats, dept_stats)
    assert len(xlsx_bytes) > 1000

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    sheet_names = wb.sheetnames
    assert "1_All_Estimates_Ledger" in sheet_names
    assert "2_Doctor_Leaderboard" in sheet_names
    assert "3_Department_Summary" in sheet_names

    ws1 = wb["1_All_Estimates_Ledger"]
    assert ws1.cell(row=1, column=1).value == "Estimate Number"
    assert ws1.cell(row=2, column=1).value == "EST-20261025-0001"
    assert ws1.cell(row=2, column=4).value == "Anita Roy"

    ws2 = wb["2_Doctor_Leaderboard"]
    assert ws2.cell(row=1, column=2).value == "Doctor Name"
    assert ws2.cell(row=2, column=2).value == "Dr. Suresh"

    ws3 = wb["3_Department_Summary"]
    assert ws3.cell(row=1, column=1).value == "Department"
    assert ws3.cell(row=2, column=1).value == "General Surgery"


def test_pdf_preprinted_letterhead_blank_space_and_date_line():
    """Verify that default PDF layout leaves letterhead blank for pre-printed stationary."""
    sample_snapshot = create_sample_estimate_snapshot()

    # Generate with default blank layout
    pdf_bytes = pdf_generator.generate_estimate_pdf(
        sample_snapshot,
        is_draft=False,
        layout_config=DEFAULT_PDF_LAYOUT,
    )
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF-")

    pdf_text = _extract_pdf_text(pdf_bytes)
    # Hospital branding must be absent
    assert "Sample Multispeciality Hospital" not in pdf_text
    assert "Plot 12, Health City Road" not in pdf_text
    # Essential proposal title and date line must be present
    assert "ESTIMATED HOSPITAL TARIFF & BILLING PROPOSAL" in pdf_text
    assert "ESTIMATE DATE:" in pdf_text
    assert "VALID UNTIL:" in pdf_text
    assert "Rajesh Kumar" in pdf_text
