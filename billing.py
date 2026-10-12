"""
billing.py - Pure financial calculation engine for hospital billing estimates.
Uses Python Decimal for all monetary math, implements exact per-line discount allocation,
taxable item segregation, non-taxed contingency, and Indian Rupee (INR) formatting.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple


CENTS = Decimal("0.01")
ROUNDING = ROUND_HALF_UP


def to_decimal(val: Any) -> Decimal:
    """
    Safely convert numeric, string or int value to Decimal.
    Raises ValueError or decimal.InvalidOperation on null, empty, or non-numeric input.
    """
    if isinstance(val, Decimal):
        return val
    if val is None or val == "":
        raise ValueError("Value cannot be null or empty.")
    # Strip any existing commas or currency symbols
    cleaned = str(val).replace(",", "").replace("₹", "").strip()
    return Decimal(cleaned)


def quantize_money(val: Decimal) -> Decimal:
    """Round a Decimal amount to 2 decimal places using ROUND_HALF_UP."""
    return val.quantize(CENTS, rounding=ROUNDING)


def format_inr(val: Any, show_symbol: bool = True, whole_rupees: bool = False) -> str:
    """
    Format a Decimal amount into the Indian numbering system (e.g., ₹1,50,000.00 or ₹1,50,000).
    The last three digits are grouped together, and preceding digits in pairs of two.
    """
    try:
        dec = quantize_money(to_decimal(val))
    except Exception:
        dec = Decimal("0.00")
    s = str(dec)
    is_neg = s.startswith("-")
    if is_neg:
        s = s[1:]

    int_part, dec_part = s.split(".") if "." in s else (s, "00")
    if len(int_part) <= 3:
        formatted_int = int_part
    else:
        last3 = int_part[-3:]
        rem = int_part[:-3]
        groups = []
        while len(rem) > 2:
            groups.insert(0, rem[-2:])
            rem = rem[:-2]
        if rem:
            groups.insert(0, rem)
        formatted_int = ",".join(groups) + "," + last3

    symbol = "₹" if show_symbol else ""
    if whole_rupees:
        res = f"{symbol}{formatted_int}"
    else:
        res = f"{symbol}{formatted_int}.{dec_part}"
    return f"-{res}" if is_neg else res


@dataclass
class LineItem:
    """A single itemized billing entry."""
    category: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    is_taxable: bool
    discount_amount: Decimal = Decimal("0.00")
    net_amount: Decimal = Decimal("0.00")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "quantity": str(self.quantity),
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
            "is_taxable": self.is_taxable,
            "discount_amount": str(self.discount_amount),
            "net_amount": str(self.net_amount),
        }


@dataclass
class EstimateResult:
    """Complete financial breakdown and snapshot for an estimate."""
    patient_name: str
    stay_days: int
    room_type_name: str
    room_daily_rate: Decimal
    department_name: str
    doctor_name: str
    doctor_fee_pct: Decimal
    procedure_name: str
    line_items: List[LineItem]

    subtotal: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    net_base: Decimal
    taxable_base: Decimal
    tax_pct: Decimal
    tax_amount: Decimal
    contingency_pct: Decimal
    contingency_amount: Decimal
    total_amount: Decimal

    estimate_range_pct: Decimal
    min_estimate: Decimal
    max_estimate: Decimal

    # Snapshot metadata
    hospital_name: str = ""
    hospital_address: str = ""
    hospital_phone: str = ""
    hospital_email: str = ""
    validity_days: int = 15
    disclaimer_text: str = ""
    remarks: str = ""
    estimate_number: Optional[str] = None
    created_at: Optional[str] = None

    def to_snapshot_dict(self) -> Dict[str, Any]:
        """Produce the comprehensive immutable snapshot dictionary."""
        return {
            "metadata": {
                "hospital_name": self.hospital_name,
                "hospital_address": self.hospital_address,
                "hospital_phone": self.hospital_phone,
                "hospital_email": self.hospital_email,
                "validity_days": self.validity_days,
                "disclaimer_text": self.disclaimer_text,
                "currency_symbol": "₹",
                "estimate_number": self.estimate_number,
                "created_at": self.created_at,
            },
            "patient": {
                "name": self.patient_name,
                "remarks": self.remarks,
                "stay_days": self.stay_days,
            },
            "clinical": {
                "department": self.department_name,
                "doctor": self.doctor_name,
                "surgeon_fee_pct": str(self.doctor_fee_pct),
                "procedure": self.procedure_name,
                "room_type": self.room_type_name,
                "room_daily_rate": str(self.room_daily_rate),
            },
            "financials": {
                "subtotal": str(self.subtotal),
                "discount_pct": str(self.discount_pct),
                "discount_amount": str(self.discount_amount),
                "net_base": str(self.net_base),
                "taxable_base": str(self.taxable_base),
                "contingency_pct": str(self.contingency_pct),
                "contingency_amount": str(self.contingency_amount),
                "tax_pct": str(self.tax_pct),
                "tax_amount": str(self.tax_amount),
                "total_amount": str(self.total_amount),
                "estimate_range_pct": str(self.estimate_range_pct),
                "min_estimate": str(self.min_estimate),
                "max_estimate": str(self.max_estimate),
            },
            "line_items": [item.to_dict() for item in self.line_items],
        }


def allocate_discount(
    items: List[LineItem],
    discount_pct: Decimal,
) -> Tuple[Decimal, Decimal]:
    """
    Proportionally allocate discount across line items so that the sum of line
    net amounts equals the net base exactly. Any leftover paise due to rounding
    is placed on the largest line item.

    Returns:
        (total_discount, net_base)
    """
    total_gross = sum((item.amount for item in items), Decimal("0.00"))
    if total_gross <= Decimal("0.00") or discount_pct <= Decimal("0.00"):
        for item in items:
            item.discount_amount = Decimal("0.00")
            item.net_amount = item.amount
        return Decimal("0.00"), total_gross

    discount_ratio = discount_pct / Decimal("100.0")
    total_discount = quantize_money(total_gross * discount_ratio)
    net_base = total_gross - total_discount

    # Calculate preliminary rounded discount per line
    raw_discounts: List[Decimal] = []
    for item in items:
        disc = quantize_money(item.amount * discount_ratio)
        raw_discounts.append(disc)

    sum_raw_discounts = sum(raw_discounts, Decimal("0.00"))
    leftover = total_discount - sum_raw_discounts

    # Put leftover paise on the line item with the highest gross amount
    if items:
        largest_idx = max(range(len(items)), key=lambda i: items[i].amount)
        raw_discounts[largest_idx] += leftover

    # Assign discounts and net amounts
    for i, item in enumerate(items):
        item.discount_amount = raw_discounts[i]
        item.net_amount = item.amount - item.discount_amount

    return total_discount, net_base


TAXABLE_CATEGORY_KEYS = {
    "Room Accommodation": "tax_category_room",
    "Hospital Procedure Tariff": "tax_category_procedure",
    "Surgeon Professional Fee": "tax_category_surgeon",
    "Operation Theatre Charges": "tax_category_ot",
    "Anaesthesia Charges": "tax_category_anaesthesia",
    "Surgical Consumables & Implants": "tax_category_consumables",
    "Investigations & Diagnostics": "tax_category_investigations",
}


def is_category_taxable(category_name: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    """
    Check if a line item category is taxable according to settings.
    Default: False (nothing taxable).
    """
    if not settings:
        return False
    key = TAXABLE_CATEGORY_KEYS.get(category_name)
    if not key:
        return False
    val = str(settings.get(key, "0")).strip().lower()
    return val in ("1", "true", "yes")


def calculate_estimate(
    patient_name: str,
    stay_days: int,
    room_type: Dict[str, Any],
    doctor: Dict[str, Any],
    procedure: Dict[str, Any],
    department: Dict[str, Any],
    discount_pct: Any = 0,
    settings: Optional[Dict[str, Any]] = None,
    remarks: str = "",
) -> EstimateResult:
    """
    Pure calculation function producing an EstimateResult.
    - Surgeon Fee = Procedure Base Cost * Doctor Surgeon Fee % / 100
    - Room Charges = Stay Days * Room Daily Rate (0 if stay_days == 0)
    - Contingency applies to post-discount net base and is not taxed
    - Tax applies to post-discount net of taxable lines (consumables, investigations)
    - Min/Max range band is computed as total_amount ± estimate_range_pct
    """
    if settings is None:
        settings = {}

    stay_days = max(0, int(stay_days))
    daily_rate = to_decimal(room_type.get("daily_rate", 0))
    proc_base = to_decimal(procedure.get("base_cost", 0))
    ot_charge = to_decimal(procedure.get("ot_charge", 0))
    anaes_charge = to_decimal(procedure.get("anaesthesia_charge", 0))
    consumables = to_decimal(procedure.get("consumables_cost", 0))
    investigations = to_decimal(procedure.get("investigations_charge", 0))
    surgeon_pct = to_decimal(doctor.get("surgeon_fee_pct", 0))

    # Surgeon fee calculation
    surgeon_fee = quantize_money(proc_base * surgeon_pct / Decimal("100.0"))

    # Room accommodation calculation
    room_charges = quantize_money(Decimal(stay_days) * daily_rate)

    # Rates & percentages from settings
    tax_pct = to_decimal(settings.get("tax_rate", "0"))
    contingency_pct = to_decimal(settings.get("contingency_rate", "10"))
    range_pct = to_decimal(settings.get("estimate_range_pct", "10"))
    discount_pct_dec = min(Decimal("100.0"), max(Decimal("0.0"), to_decimal(discount_pct)))

    # Assemble line items
    line_items: List[LineItem] = []

    # 1. Room accommodation (if stay_days > 0 or explicitly 0 day care)
    room_desc = f"{room_type.get('name', 'Room')} ({stay_days} day{'s' if stay_days != 1 else ''} @ {format_inr(daily_rate, whole_rupees=True)}/day)"
    if stay_days == 0:
        room_desc = f"{room_type.get('name', 'Room')} - Day Care (0 days stay)"
    line_items.append(
        LineItem(
            category="Room Accommodation",
            description=room_desc,
            quantity=Decimal(stay_days),
            unit_price=daily_rate,
            amount=room_charges,
            is_taxable=is_category_taxable("Room Accommodation", settings),
        )
    )

    # 2. Procedure hospital base charge
    line_items.append(
        LineItem(
            category="Hospital Procedure Tariff",
            description=f"{procedure.get('name', 'Procedure')} (Institutional Base Charge)",
            quantity=Decimal(1),
            unit_price=proc_base,
            amount=proc_base,
            is_taxable=is_category_taxable("Hospital Procedure Tariff", settings),
        )
    )

    # 3. Surgeon Professional Fee
    line_items.append(
        LineItem(
            category="Surgeon Professional Fee",
            description=f"{doctor.get('name', 'Surgeon')} ({surgeon_pct}% of base tariff)",
            quantity=Decimal(1),
            unit_price=surgeon_fee,
            amount=surgeon_fee,
            is_taxable=is_category_taxable("Surgeon Professional Fee", settings),
        )
    )

    # 4. Operation Theatre (OT) charges
    line_items.append(
        LineItem(
            category="Operation Theatre Charges",
            description="OT facility, monitoring & surgical equipment",
            quantity=Decimal(1),
            unit_price=ot_charge,
            amount=ot_charge,
            is_taxable=is_category_taxable("Operation Theatre Charges", settings),
        )
    )

    # 5. Anaesthesia charges
    line_items.append(
        LineItem(
            category="Anaesthesia Charges",
            description="Anaesthetist team fee & gas/medication delivery",
            quantity=Decimal(1),
            unit_price=anaes_charge,
            amount=anaes_charge,
            is_taxable=is_category_taxable("Anaesthesia Charges", settings),
        )
    )

    # 6. Surgical Consumables & Implants
    line_items.append(
        LineItem(
            category="Surgical Consumables & Implants",
            description="Implants, sutures, surgical packs & disposables",
            quantity=Decimal(1),
            unit_price=consumables,
            amount=consumables,
            is_taxable=is_category_taxable("Surgical Consumables & Implants", settings),
        )
    )

    # 7. Diagnostic & Lab Investigations
    line_items.append(
        LineItem(
            category="Investigations & Diagnostics",
            description="Pre-operative & post-operative laboratory & imaging panels",
            quantity=Decimal(1),
            unit_price=investigations,
            amount=investigations,
            is_taxable=is_category_taxable("Investigations & Diagnostics", settings),
        )
    )

    # Subtotal calculation
    subtotal = sum((item.amount for item in line_items), Decimal("0.00"))

    # Discount allocation across lines with leftover paise resolution
    discount_amount, net_base = allocate_discount(line_items, discount_pct_dec)

    # Contingency on post-discount net base (NOT taxed)
    contingency_amount = quantize_money(net_base * (contingency_pct / Decimal("100.0")))

    # Tax on post-discount net of taxable line items
    taxable_base = sum(
        (item.net_amount for item in line_items if item.is_taxable),
        Decimal("0.00"),
    )
    tax_amount = quantize_money(taxable_base * (tax_pct / Decimal("100.0")))

    # Grand total
    total_amount = net_base + contingency_amount + tax_amount

    # Range band calculation (± range_pct)
    range_delta = quantize_money(total_amount * (range_pct / Decimal("100.0")))
    min_estimate = quantize_money(total_amount - range_delta)
    max_estimate = quantize_money(total_amount + range_delta)

    return EstimateResult(
        patient_name=patient_name.strip(),
        stay_days=stay_days,
        room_type_name=room_type.get("name", ""),
        room_daily_rate=daily_rate,
        department_name=department.get("name", ""),
        doctor_name=doctor.get("name", ""),
        doctor_fee_pct=surgeon_pct,
        procedure_name=procedure.get("name", ""),
        line_items=line_items,
        subtotal=subtotal,
        discount_pct=discount_pct_dec,
        discount_amount=discount_amount,
        net_base=net_base,
        taxable_base=taxable_base,
        tax_pct=tax_pct,
        tax_amount=tax_amount,
        contingency_pct=contingency_pct,
        contingency_amount=contingency_amount,
        total_amount=total_amount,
        estimate_range_pct=range_pct,
        min_estimate=min_estimate,
        max_estimate=max_estimate,
        hospital_name=settings.get("hospital_name", "Sample Multispeciality Hospital"),
        hospital_address=settings.get("hospital_address", ""),
        hospital_phone=settings.get("hospital_phone", ""),
        hospital_email=settings.get("hospital_email", ""),
        validity_days=int(settings.get("validity_days", 15)),
        disclaimer_text=settings.get("disclaimer_text", ""),
        remarks=remarks.strip(),
        created_at=datetime.now().isoformat(),
    )


# ==============================================================================
# Validation Helpers
# ==============================================================================

def validate_patient_name(name: str) -> Tuple[bool, str]:
    """
    Validate patient name: non-blank, max 100 characters.
    Allows Unicode letters, combining marks, spaces, periods, apostrophes, hyphens, and slashes.
    """
    if not name or not name.strip():
        return False, "Patient name cannot be blank."
    stripped = name.strip()
    if len(stripped) > 100:
        return False, "Patient name cannot exceed 100 characters."
    allowed_chars = set(" .'-/")
    valid = all(
        unicodedata.category(c)[0] in ("L", "M") or c.isspace() or c in allowed_chars
        for c in stripped
    )
    if not valid:
        return False, "Patient name contains invalid characters."
    has_letter = any(unicodedata.category(c)[0] == "L" for c in stripped)
    if not has_letter:
        return False, "Patient name must contain alphabetic characters."
    return True, ""


def validate_money_field(val: Any, field_name: str, allow_zero: bool = True) -> Tuple[bool, Decimal, str]:
    """Validate monetary amount: must be non-negative Decimal."""
    try:
        dec = to_decimal(val)
        if dec < Decimal("0.00"):
            return False, dec, f"{field_name} cannot be negative."
        if not allow_zero and dec == Decimal("0.00"):
            return False, dec, f"{field_name} must be greater than zero."
        return True, quantize_money(dec), ""
    except Exception:
        return False, Decimal("0.00"), f"Invalid number format for {field_name}."


def validate_percentage_field(val: Any, field_name: str, min_pct: Decimal = Decimal("0"), max_pct: Decimal = Decimal("100")) -> Tuple[bool, Decimal, str]:
    """Validate percentage value: must be within range."""
    try:
        dec = to_decimal(val)
        if dec < min_pct or dec > max_pct:
            return False, dec, f"{field_name} must be between {min_pct}% and {max_pct}%."
        return True, quantize_money(dec), ""
    except Exception:
        return False, Decimal("0.00"), f"Invalid percentage for {field_name}."


def validate_department_availability(doctors: List[Any], procedures: List[Any]) -> Tuple[bool, str]:
    """Verify that a selected department has both active doctors and active procedures."""
    if not doctors:
        return False, "Selected department has no active doctors configured."
    if not procedures:
        return False, "Selected department has no active procedures configured."
    return True, ""


def validate_whole_rupee_field(val: Any, field_name: str, allow_zero: bool = True) -> Tuple[bool, int, str]:
    """Validate that value is a non-negative whole rupee integer amount (no decimals or paise)."""
    try:
        dec = to_decimal(val)
        if dec < Decimal("0"):
            return False, 0, f"{field_name} cannot be negative."
        if not allow_zero and dec == Decimal("0"):
            return False, 0, f"{field_name} must be greater than zero."
        if dec != dec.to_integral_value():
            return False, 0, f"{field_name} must be a whole rupee amount (columns are whole rupees)."
        return True, int(dec), ""
    except Exception:
        return False, 0, f"Invalid number format for {field_name}."


def validate_whole_percentage_field(val: Any, field_name: str, min_pct: int = 0, max_pct: int = 100) -> Tuple[bool, int, str]:
    """Validate that percentage is an integer within [min_pct, max_pct] (no fractional percentages)."""
    try:
        dec = to_decimal(val)
        if dec < Decimal(str(min_pct)) or dec > Decimal(str(max_pct)):
            return False, 0, f"{field_name} must be between {min_pct}% and {max_pct}%."
        if dec != dec.to_integral_value():
            return False, 0, f"{field_name} must be a whole percentage (fractional percentages not allowed; columns are whole rupees)."
        return True, int(dec), ""
    except Exception:
        return False, 0, f"Invalid percentage format for {field_name}."
