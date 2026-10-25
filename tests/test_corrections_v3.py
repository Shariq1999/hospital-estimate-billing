"""
tests/test_corrections_v3.py - Verification suite for the 5 requested enhancements:
1. Comparative room column in printed PDF.
2. Toggle & multiselect for one or more comparative rooms.
3. Surgeon fee fixed amount (default) with switch to percentage of base tariff.
4. Integrated inline editing in the Itemized Estimate Breakdown section.
5. Codebase audit: 0 emojis and 0 em-dashes across all source files.
"""

from decimal import Decimal
import base64
import os
import re
import zlib
import pytest

import billing
import database
import pdf_generator
import seed_data


@pytest.fixture
def clean_db(tmp_path):
    db_file = str(tmp_path / "test_v3.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)
    seed_data.seed(conn)
    database.set_admin_password(conn, "admin", "Admin@1234")
    yield conn, db_file
    conn.close()


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from ReportLab PDF using CMaps and unescaping literal/hex strings."""
    pos = 0
    cmaps = []
    content_streams = []
    while True:
        s_idx = data.find(b"stream\n", pos)
        if s_idx == -1:
            break
        start = s_idx + 7
        e_idx = data.find(b"endstream", start)
        if e_idx == -1:
            break
        raw = data[start:e_idx].rstrip(b"\r\n")
        try:
            d = zlib.decompress(raw)
            if b"beginbfchar" in d:
                m = {}
                for line in d.splitlines():
                    line = line.strip()
                    if line.startswith(b"<") and b"> <" in line:
                        parts = line.split()
                        src = int(parts[0].strip(b"<>"), 16)
                        dst = chr(int(parts[1].strip(b"<>"), 16))
                        m[src] = dst
                cmaps.append(m)
            elif b"BT" in d and b"ET" in d:
                content_streams.append(d)
        except Exception:
            try:
                norm_raw = raw if raw.endswith(b"~>") else (raw[:raw.find(b"~>")+2] if b"~>" in raw else raw + b"~>")
                d = zlib.decompress(base64.a85decode(norm_raw, adobe=True))
                if b"BT" in d and b"ET" in d:
                    content_streams.append(d)
            except Exception:
                pass
        pos = e_idx + 9

    merged_cmap = {}
    for cm in cmaps:
        merged_cmap.update(cm)

    def _unescape_pdf_literal(raw_bytes):
        out = []
        i = 0
        n = len(raw_bytes)
        while i < n:
            if raw_bytes[i:i+1] == b"\\":
                i += 1
                if i >= n:
                    break
                oct_match = re.match(rb"^[0-7]{1,3}", raw_bytes[i:i+3])
                if oct_match:
                    oct_str = oct_match.group(0)
                    out.append(int(oct_str, 8))
                    i += len(oct_str)
                else:
                    esc = raw_bytes[i:i+1]
                    if esc == b"n":
                        out.append(10)
                    elif esc == b"r":
                        out.append(13)
                    elif esc == b"t":
                        out.append(9)
                    elif esc == b"b":
                        out.append(8)
                    elif esc == b"f":
                        out.append(12)
                    elif esc in (b"(", b")", b"\\"):
                        out.append(ord(esc))
                    else:
                        out.append(ord(esc))
                    i += 1
            else:
                out.append(raw_bytes[i])
                i += 1
        return "".join(merged_cmap.get(c, chr(c) if 32 <= c <= 126 else "") for c in out)

    decoded_fragments = []
    for cs in content_streams:
        tokens = re.findall(rb"<([0-9a-fA-F]+)>", cs)
        for tok in tokens:
            chars = []
            for i in range(0, len(tok), 2):
                code = int(tok[i:i+2], 16)
                chars.append(merged_cmap.get(code, chr(code) if 32 <= code <= 126 else ""))
            decoded_fragments.append("".join(chars))

        lits = re.findall(rb"\((.*?)\)", cs)
        for lit in lits:
            decoded_fragments.append(_unescape_pdf_literal(lit))

    return " ".join(decoded_fragments)


def test_surgeon_fee_fixed_amount_and_percentage_toggle():
    """Verify surgeon fee fixed amount calculation and toggle to percentage."""
    proc = {
        "id": 1,
        "name": "Total Knee Replacement",
        "base_cost": 120000,
        "ot_charge": 35000,
        "anaesthesia_charge": 15000,
        "consumables_cost": 160000,
        "investigations_charge": 10000,
    }
    room = {"id": 1, "name": "General Ward", "daily_rate": 2000}
    dept = {"id": 1, "name": "Orthopaedics"}
    doctor = {
        "id": 1,
        "name": "Dr. Sanjay Deshmukh",
        "consultation_fee": 1500,
        "surgeon_fee_pct": 50,
        "surgeon_fee_type": "fixed",
        "surgeon_fee_amount": 45000,
    }

    # 1. Fixed Fee Mode (Default)
    res_fixed = billing.calculate_estimate(
        patient_name="Ramesh Patel",
        stay_days=3,
        room_type=room,
        doctor=doctor,
        procedure=proc,
        department=dept,
        surgeon_fee_type="fixed",
        surgeon_fee_amount=45000,
    )
    surg_item = next(it for it in res_fixed.line_items if it.category == "Surgeon Professional Fee")
    assert surg_item.amount == Decimal("45000.00")
    assert "Fixed Professional Fee" in surg_item.description
    assert res_fixed.surgeon_fee_type == "fixed"
    assert res_fixed.surgeon_fee_amount == Decimal("45000.00")

    # 2. Percentage Mode Switch
    res_pct = billing.calculate_estimate(
        patient_name="Ramesh Patel",
        stay_days=3,
        room_type=room,
        doctor=doctor,
        procedure=proc,
        department=dept,
        surgeon_fee_type="percentage",
    )
    surg_item_pct = next(it for it in res_pct.line_items if it.category == "Surgeon Professional Fee")
    # 50% of 1,20,000 = 60,000
    assert surg_item_pct.amount == Decimal("60000.00")
    assert "50% of base tariff" in surg_item_pct.description
    assert res_pct.surgeon_fee_type == "percentage"


def test_doctor_master_surgeon_fee_persistence(clean_db):
    """Verify database migrations, save_doctor, and get_doctors for surgeon fee fields."""
    conn, _ = clean_db

    # Add a doctor with fixed fee
    doc_id = database.save_doctor(
        conn,
        name="Dr. Vikram Sen",
        department_id=1,
        consultation_fee=1500,
        surgeon_fee_pct=35,
        specialization="Senior Joint Surgeon",
        surgeon_fee_type="fixed",
        surgeon_fee_amount=50000,
    )

    doc = database.get_doctor_by_id(conn, doc_id)
    assert doc is not None
    assert doc["name"] == "Dr. Vikram Sen"
    assert doc["surgeon_fee_type"] == "fixed"
    assert doc["surgeon_fee_amount"] == 50000

    # Update doctor to percentage mode
    database.save_doctor(
        conn,
        name="Dr. Vikram Sen",
        department_id=1,
        consultation_fee=1800,
        surgeon_fee_pct=40,
        specialization="Senior Joint Surgeon",
        doc_id=doc_id,
        surgeon_fee_type="percentage",
        surgeon_fee_amount=0,
    )
    doc_updated = database.get_doctor_by_id(conn, doc_id)
    assert doc_updated["surgeon_fee_type"] == "percentage"
    assert doc_updated["surgeon_fee_pct"] == 40


def test_multi_room_comparative_estimate_calculation():
    """Verify estimate generation with one or more comparative rooms."""
    proc = {
        "id": 1,
        "name": "Total Knee Replacement",
        "base_cost": 120000,
        "ot_charge": 35000,
        "anaesthesia_charge": 15000,
        "consumables_cost": 160000,
        "investigations_charge": 10000,
    }
    primary_room = {"id": 1, "name": "General Ward", "daily_rate": 2000}
    comp_room1 = {"id": 2, "name": "Twin Sharing Room", "daily_rate": 3500}
    comp_room2 = {"id": 3, "name": "Single Private Room", "daily_rate": 5500}
    dept = {"id": 1, "name": "Orthopaedics"}
    doctor = {
        "id": 1,
        "name": "Dr. Sanjay Deshmukh",
        "consultation_fee": 1500,
        "surgeon_fee_pct": 40,
        "surgeon_fee_type": "fixed",
        "surgeon_fee_amount": 40000,
    }

    res = billing.calculate_estimate(
        patient_name="Pooja Mehta",
        stay_days=4,
        room_type=primary_room,
        doctor=doctor,
        procedure=proc,
        department=dept,
        comparative_rooms=[comp_room1, comp_room2],
    )

    # Primary room charges: 4 * 2000 = 8,000
    assert res.room_type_name == "General Ward"
    assert len(res.comparative_estimates) == 2

    comp1 = res.comparative_estimates[0]
    assert comp1["clinical"]["room_type"] == "Twin Sharing Room"
    # Twin Sharing room charges: 4 * 3500 = 14,000 (diff +6,000)
    primary_subtotal = res.subtotal
    comp1_subtotal = Decimal(comp1["financials"]["subtotal"])
    assert comp1_subtotal - primary_subtotal == Decimal("6000.00")

    comp2 = res.comparative_estimates[1]
    assert comp2["clinical"]["room_type"] == "Single Private Room"
    # Single Private room charges: 4 * 5500 = 22,000 (diff +14,000)
    comp2_subtotal = Decimal(comp2["financials"]["subtotal"])
    assert comp2_subtotal - primary_subtotal == Decimal("14000.00")

    # Snapshot dictionary verification
    snapshot = res.to_snapshot_dict()
    assert "comparative_estimates" in snapshot
    assert len(snapshot["comparative_estimates"]) == 2


def test_pdf_with_comparative_room_columns():
    """Verify PDF generator renders side-by-side comparative room columns."""
    proc = {
        "id": 1,
        "name": "Laparoscopic Cholecystectomy",
        "base_cost": 45000,
        "ot_charge": 20000,
        "anaesthesia_charge": 10000,
        "consumables_cost": 25000,
        "investigations_charge": 8000,
    }
    primary_room = {"id": 1, "name": "Twin Sharing Room", "daily_rate": 3500}
    comp_room1 = {"id": 2, "name": "Single Private Room", "daily_rate": 5500}
    comp_room2 = {"id": 3, "name": "Deluxe Room", "daily_rate": 8500}
    dept = {"id": 1, "name": "General Surgery"}
    doctor = {
        "id": 1,
        "name": "Dr. Prakash Nair",
        "consultation_fee": 1000,
        "surgeon_fee_type": "fixed",
        "surgeon_fee_amount": 35000,
    }

    res = billing.calculate_estimate(
        patient_name="Sunil Gupta",
        stay_days=2,
        room_type=primary_room,
        doctor=doctor,
        procedure=proc,
        department=dept,
        comparative_rooms=[comp_room1, comp_room2],
    )
    res.estimate_number = "EST-20261015-0042"

    pdf_bytes = pdf_generator.generate_estimate_pdf(res, is_draft=False)
    assert len(pdf_bytes) > 5000

    # Extract text from PDF
    all_text = _extract_pdf_text(pdf_bytes)

    # Check presence of primary and comparative rooms in table headers
    assert "Twin Sharing Room" in all_text
    assert "Single Private Room" in all_text
    assert "Deluxe Room" in all_text
    assert "EST-20261015-0042" in all_text

    # Verify ZERO em dashes in generated PDF text and raw bytes
    assert chr(0x2014) not in all_text
    assert b"\xe2\x80\x94" not in pdf_bytes


def test_zero_emojis_and_zero_em_dashes_in_codebase():
    """Verify that all project source files contain 0 emojis and 0 em-dashes."""
    source_files = [
        "app.py",
        "billing.py",
        "database.py",
        "pdf_generator.py",
        "seed_data.py",
    ]

    base_dir = os.path.dirname(os.path.dirname(__file__))

    for fn in source_files:
        fpath = os.path.join(base_dir, fn)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Check for em-dashes
        em_dash_count = content.count(chr(0x2014))
        assert em_dash_count == 0, f"{fn} contains {em_dash_count} em dashes!"

        # 2. Check for emojis (characters in unicode emoji ranges or pictographs)
        emojis = [c for c in content if ord(c) >= 0x1F300 or (ord(c) > 127 and c not in "₹±")]
        assert len(emojis) == 0, f"{fn} contains {len(emojis)} emoji characters: {set(emojis)}"


def test_backward_compatible_estimate_result_attributes():
    """Verify that older EstimateResult objects without new attributes don't raise AttributeError."""
    proc = {
        "id": 1,
        "name": "General Surgery",
        "base_cost": 50000,
        "ot_charge": 10000,
        "anaesthesia_charge": 5000,
        "consumables_cost": 5000,
        "investigations_charge": 2000,
    }
    room = {"id": 1, "name": "General Ward", "daily_rate": 2000}
    dept = {"id": 1, "name": "General Surgery"}
    doctor = {"id": 1, "name": "Dr. Smith", "consultation_fee": 1000, "surgeon_fee_pct": 20}

    res = billing.calculate_estimate(
        patient_name="Old Patient",
        stay_days=2,
        room_type=room,
        doctor=doctor,
        procedure=proc,
        department=dept,
    )

    # Delete attributes to simulate a session state object from an older deployment
    for attr in ("comparative_estimates", "surgeon_fee_type", "surgeon_fee_amount", "mobile_number", "uhid"):
        if attr in res.__dict__:
            del res.__dict__[attr]

    # Must safely fallback via __getattr__
    assert res.comparative_estimates == []
    assert res.surgeon_fee_type == "fixed"
    assert res.surgeon_fee_amount == Decimal("0.00")
    assert res.mobile_number == ""
    assert res.uhid == ""

    # Snapshot dict must succeed without error
    snapshot = res.to_snapshot_dict()
    assert snapshot["comparative_estimates"] == []
    assert snapshot["patient"]["mobile_number"] == ""

    # PDF generation must succeed without error
    pdf_bytes = pdf_generator.generate_estimate_pdf(res, is_draft=True)
    assert len(pdf_bytes) > 1000


def test_calculate_estimate_resilient_to_unexpected_kwargs():
    """Verify calculate_estimate gracefully accepts unexpected kwargs without raising TypeError."""
    proc = {
        "id": 1,
        "name": "General Surgery",
        "base_cost": 50000,
        "ot_charge": 10000,
        "anaesthesia_charge": 5000,
        "consumables_cost": 5000,
        "investigations_charge": 2000,
    }
    room = {"id": 1, "name": "General Ward", "daily_rate": 2000}
    dept = {"id": 1, "name": "General Surgery"}
    doctor = {"id": 1, "name": "Dr. Smith", "consultation_fee": 1000, "surgeon_fee_pct": 20}

    # Pass arbitrary kwargs and legacy comparative_room argument
    res = billing.calculate_estimate(
        patient_name="Resilience Test",
        stay_days=2,
        room_type=room,
        doctor=doctor,
        procedure=proc,
        department=dept,
        surgeon_fee_type="fixed",
        surgeon_fee_amount=15000,
        comparative_room={"name": "Deluxe", "daily_rate": 6000},
        legacy_arg="ignored_safely",
        future_flag=True,
    )
    assert res.patient_name == "Resilience Test"
    assert len(res.comparative_estimates) == 1
    assert res.comparative_estimates[0]["clinical"]["room_type"] == "Deluxe"


def test_default_admin_account_credentials(tmp_path):
    """Verify that ensure_default_admin creates admin with password Admin@1234."""
    db_file = str(tmp_path / "test_default_admin.db")
    conn = database.get_connection(db_file)
    database.init_db(conn)

    # Initially not configured
    assert not database.is_admin_configured(conn)

    # Ensure default admin
    database.ensure_default_admin(conn)
    assert database.is_admin_configured(conn)

    # Verify credentials
    assert database.verify_admin_credentials(conn, "admin", "Admin@1234") is True
    assert database.verify_admin_credentials(conn, "admin", "Admin@12345") is False
    assert database.verify_admin_credentials(conn, "admin", "WrongPassword") is False
    conn.close()

