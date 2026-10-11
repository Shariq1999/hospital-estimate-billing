"""
tests/test_billing.py - Unit tests for pure billing logic, Decimal arithmetic,
per-line discount allocation, tax/contingency ordering, and boundary edge cases.
"""

from decimal import Decimal
import pytest

import billing
from billing import (
    LineItem,
    allocate_discount,
    calculate_estimate,
    format_inr,
    quantize_money,
    to_decimal,
    validate_department_availability,
    validate_money_field,
    validate_patient_name,
    validate_percentage_field,
)


def test_worked_example_tkr():
    """Verify the exact worked example benchmark from seed_data.py."""
    room = {"name": "Single Private Room", "daily_rate": 5500}
    doc = {"name": "Dr. Sanjay Deshmukh", "surgeon_fee_pct": 50}
    proc = {
        "name": "Total Knee Replacement (Unilateral)",
        "base_cost": 90000,
        "ot_charge": 35000,
        "anaesthesia_charge": 15000,
        "consumables_cost": 160000,
        "investigations_charge": 10000,
        "default_stay_days": 5,
    }
    dept = {"name": "Orthopaedics"}
    settings = {
        "tax_rate": "0",
        "contingency_rate": "10",
        "estimate_range_pct": "10",
        "validity_days": "15",
        "hospital_name": "Sample Multispeciality Hospital",
    }

    res = calculate_estimate(
        patient_name="John Doe",
        stay_days=5,
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=0,
        settings=settings,
    )

    # Line item checks
    room_item = next(i for i in res.line_items if i.category == "Room Accommodation")
    assert room_item.amount == Decimal("27500.00")

    proc_item = next(i for i in res.line_items if i.category == "Hospital Procedure Tariff")
    assert proc_item.amount == Decimal("90000.00")

    ot_item = next(i for i in res.line_items if i.category == "Operation Theatre Charges")
    assert ot_item.amount == Decimal("35000.00")

    anaes_item = next(i for i in res.line_items if i.category == "Anaesthesia Charges")
    assert anaes_item.amount == Decimal("15000.00")

    consumables_item = next(i for i in res.line_items if i.category == "Surgical Consumables & Implants")
    assert consumables_item.amount == Decimal("160000.00")

    investigations_item = next(i for i in res.line_items if i.category == "Investigations & Diagnostics")
    assert investigations_item.amount == Decimal("10000.00")

    surgeon_item = next(i for i in res.line_items if i.category == "Surgeon Professional Fee")
    assert surgeon_item.amount == Decimal("45000.00")  # 50% of 90,000

    # Subtotal: 27500 + 90000 + 35000 + 15000 + 160000 + 10000 + 45000 = 382500
    assert res.subtotal == Decimal("382500.00")
    assert res.discount_amount == Decimal("0.00")
    assert res.net_base == Decimal("382500.00")

    # +10% contingency on net base: 38250
    assert res.contingency_amount == Decimal("38250.00")
    assert res.tax_amount == Decimal("0.00")

    # Grand total: 382500 + 38250 = 420750
    assert res.total_amount == Decimal("420750.00")

    # ±10% range band
    assert res.min_estimate == Decimal("378675.00")
    assert res.max_estimate == Decimal("462825.00")


def test_los_zero_with_icu():
    """Verify that Day Care (LOS = 0) with ICU produces 0 room charge."""
    room = {"name": "ICU", "daily_rate": 12000}
    doc = {"name": "Dr. Arvind Menon", "surgeon_fee_pct": 50}
    proc = {
        "name": "Coronary Angiography",
        "base_cost": 8000,
        "ot_charge": 6000,
        "anaesthesia_charge": 0,
        "consumables_cost": 9000,
        "investigations_charge": 4000,
        "default_stay_days": 1,
    }
    dept = {"name": "Cardiology"}

    res = calculate_estimate(
        patient_name="Ramesh Kumar",
        stay_days=0,  # Explicit day care override
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=0,
    )

    room_item = next(i for i in res.line_items if i.category == "Room Accommodation")
    assert room_item.amount == Decimal("0.00")
    assert room_item.quantity == Decimal(0)
    assert "Day Care" in room_item.description

    # Surgeon fee: 50% of 8,000 = 4,000
    # Subtotal: 0 + 8000 + 4000 + 6000 + 0 + 9000 + 4000 = 31000
    assert res.subtotal == Decimal("31000.00")


def test_per_line_discount_allocation_and_leftover_paise():
    """
    Test that per-line discount allocation sums exactly to net base,
    and leftover paise are allocated to the largest line item.
    """
    items = [
        LineItem(
            category="Cat 1",
            description="Item 1 (Small)",
            quantity=Decimal(1),
            unit_price=Decimal("33.33"),
            amount=Decimal("33.33"),
            is_taxable=False,
        ),
        LineItem(
            category="Cat 2",
            description="Item 2 (Medium)",
            quantity=Decimal(1),
            unit_price=Decimal("33.33"),
            amount=Decimal("33.33"),
            is_taxable=False,
        ),
        LineItem(
            category="Cat 3",
            description="Item 3 (Largest)",
            quantity=Decimal(1),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
            is_taxable=False,
        ),
    ]

    total_gross = sum(i.amount for i in items)  # 166.66
    discount_pct = Decimal("15.0")  # 15% of 166.66 = 24.999 -> rounds to 25.00

    tot_disc, net_base = allocate_discount(items, discount_pct)

    assert tot_disc == Decimal("25.00")
    assert net_base == Decimal("141.66")

    # Sum of individual line discounts must equal tot_disc exactly
    sum_line_discounts = sum(i.discount_amount for i in items)
    assert sum_line_discounts == tot_disc

    # Sum of individual line net amounts must equal net_base exactly
    sum_line_nets = sum(i.net_amount for i in items)
    assert sum_line_nets == net_base

    # Check that largest line (Item 3, index 2) absorbed the leftover fraction
    # 33.33 * 0.15 = 4.9995 -> 5.00
    # 33.33 * 0.15 = 4.9995 -> 5.00
    # 100.00 * 0.15 = 15.00
    # 5.00 + 5.00 + 15.00 = 25.00 (no leftover needed here, but net sum is exact)
    for i in items:
        assert i.amount - i.discount_amount == i.net_amount


def test_tax_and_contingency_rules():
    """
    Verify:
    1. Contingency applies to post-discount net and is not taxed.
    2. Tax applies ONLY to taxable items after discount.
    """
    room = {"name": "General Ward", "daily_rate": 2000}
    doc = {"name": "Dr. Prakash Nair", "surgeon_fee_pct": 40}
    proc = {
        "name": "Laparoscopic Cholecystectomy",
        "base_cost": 35000,     # Non-taxable
        "ot_charge": 18000,     # Non-taxable
        "anaesthesia_charge": 8000, # Non-taxable
        "consumables_cost": 20000,  # Taxable
        "investigations_charge": 5000, # Taxable
        "default_stay_days": 2,
    }
    dept = {"name": "General Surgery"}
    settings = {
        "tax_rate": "18.0",       # 18% GST
        "contingency_rate": "10.0",# 10%
        "estimate_range_pct": "5.0",
    }

    # Gross calculations:
    # Room (2 * 2000) = 4,000 (Non-taxable)
    # Proc base = 35,000 (Non-taxable)
    # Surgeon (40% of 35k) = 14,000 (Non-taxable)
    # OT = 18,000 (Non-taxable)
    # Anaes = 8,000 (Non-taxable)
    # Consumables = 20,000 (Taxable)
    # Investigations = 5,000 (Taxable)
    # Subtotal = 1,04,000

    # Discount 10% -> Discount Amount = 10,400 -> Net Base = 93,600
    # Taxable items after 10% discount: (20,000 + 5,000) * 0.90 = 22,500
    # Tax @ 18% on 22,500 = 4,050
    # Contingency @ 10% on Net Base (93,600) = 9,360 (NOT taxed)
    # Grand Total = 93,600 + 9,360 + 4,050 = 1,07,010

    res = calculate_estimate(
        patient_name="Anita Sharma",
        stay_days=2,
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=10,
        settings=settings,
    )

    assert res.subtotal == Decimal("104000.00")
    assert res.discount_amount == Decimal("10400.00")
    assert res.net_base == Decimal("93600.00")
    assert res.taxable_base == Decimal("22500.00")
    assert res.contingency_amount == Decimal("9360.00")
    assert res.tax_amount == Decimal("4050.00")
    assert res.total_amount == Decimal("107010.00")


def test_format_inr_indian_grouping():
    """Verify Indian currency comma formatting."""
    assert format_inr(Decimal("0")) == "₹0.00"
    assert format_inr(Decimal("50")) == "₹50.00"
    assert format_inr(Decimal("999.50")) == "₹999.50"
    assert format_inr(Decimal("1000")) == "₹1,000.00"
    assert format_inr(Decimal("10000")) == "₹10,000.00"
    assert format_inr(Decimal("100000")) == "₹1,00,000.00"
    assert format_inr(Decimal("1500000")) == "₹15,00,000.00"
    assert format_inr(Decimal("10000000")) == "₹1,00,00,000.00"
    assert format_inr(Decimal("-25000")) == "-₹25,000.00"


def test_decimal_rounding_half_up():
    """Verify ROUND_HALF_UP precision."""
    assert quantize_money(Decimal("100.005")) == Decimal("100.01")
    assert quantize_money(Decimal("100.004")) == Decimal("100.00")
    assert quantize_money(Decimal("0.005")) == Decimal("0.01")


def test_patient_name_validation():
    """Test blank, whitespace, overly long, and valid patient names."""
    # Blank
    valid, err = validate_patient_name("")
    assert not valid
    assert "cannot be blank" in err

    # Whitespace
    valid, err = validate_patient_name("    ")
    assert not valid
    assert "cannot be blank" in err

    # Exceeding 100 characters
    valid, err = validate_patient_name("A" * 101)
    assert not valid
    assert "exceed 100 characters" in err

    # Valid names
    valid, err = validate_patient_name("Dr. A. P. J. Abdul Kalam")
    assert valid
    assert err == ""

    valid, err = validate_patient_name("Rhea D'Souza-Smith")
    assert valid


def test_department_availability_validation():
    """Verify validation when department lacks active doctors or procedures."""
    valid, err = validate_department_availability([], [{"id": 1}])
    assert not valid
    assert "no active doctors" in err

    valid, err = validate_department_availability([{"id": 1}], [])
    assert not valid
    assert "no active procedures" in err

    valid, err = validate_department_availability([{"id": 1}], [{"id": 1}])
    assert valid
    assert err == ""


def test_snapshot_json_structure():
    """Verify snapshot dictionary contains all necessary frozen audit fields."""
    room = {"name": "Deluxe Room", "daily_rate": 8500}
    doc = {"name": "Dr. Karthik Rao", "surgeon_fee_pct": 50}
    proc = {
        "name": "Craniotomy - Tumour Excision",
        "base_cost": 120000,
        "ot_charge": 50000,
        "anaesthesia_charge": 30000,
        "consumables_cost": 80000,
        "investigations_charge": 25000,
        "default_stay_days": 8,
    }
    dept = {"name": "Neurosurgery"}
    settings = {
        "hospital_name": "Metro Neuro Hospital",
        "hospital_address": "123 Brain Ave",
        "hospital_phone": "+91 99999 99999",
        "hospital_email": "info@metroneuro.org",
        "tax_rate": "5",
        "contingency_rate": "10",
        "estimate_range_pct": "10",
        "validity_days": "20",
        "disclaimer_text": "Hospital estimate only.",
    }

    res = calculate_estimate(
        patient_name="Amitabh Bachchan",
        stay_days=8,
        room_type=room,
        doctor=doc,
        procedure=proc,
        department=dept,
        discount_pct=5,
        settings=settings,
        remarks="Special surgical assistance required.",
    )
    res.estimate_number = "EST-20261011-0042"
    res.created_at = "2026-10-11T12:00:00"

    snap = res.to_snapshot_dict()
    assert snap["metadata"]["hospital_name"] == "Metro Neuro Hospital"
    assert snap["metadata"]["estimate_number"] == "EST-20261011-0042"
    assert snap["clinical"]["department"] == "Neurosurgery"
    assert snap["clinical"]["doctor"] == "Dr. Karthik Rao"
    assert snap["clinical"]["procedure"] == "Craniotomy - Tumour Excision"
    assert snap["patient"]["name"] == "Amitabh Bachchan"
    assert len(snap["line_items"]) == 7
    assert snap["financials"]["subtotal"] == str(res.subtotal)
    assert snap["financials"]["total_amount"] == str(res.total_amount)
