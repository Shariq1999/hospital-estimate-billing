"""
pdf_generator.py - Professional hospital estimate letterhead generator using ReportLab.
Uses bundled Noto Sans TTF for full Unicode and Indian Rupee (₹) support.
"""

from __future__ import annotations

import io
import os
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from billing import EstimateResult, format_inr


# Register bundled Noto Sans Unicode fonts
FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
REGULAR_FONT_PATH = os.path.join(FONT_DIR, "NotoSans-Regular.ttf")
BOLD_FONT_PATH = os.path.join(FONT_DIR, "NotoSans-Bold.ttf")

FONT_NAME = "NotoSans"
BOLD_FONT_NAME = "NotoSans-Bold"

if not os.path.exists(REGULAR_FONT_PATH) or not os.path.exists(BOLD_FONT_PATH):
    raise FileNotFoundError(
        f"Required Noto Sans TTF fonts not found in {FONT_DIR}. "
        "Please ensure NotoSans-Regular.ttf and NotoSans-Bold.ttf are committed in assets/fonts/."
    )

pdfmetrics.registerFont(TTFont(FONT_NAME, REGULAR_FONT_PATH))
pdfmetrics.registerFont(TTFont(BOLD_FONT_NAME, BOLD_FONT_PATH))


def generate_estimate_pdf(
    estimate_data: EstimateResult | Dict[str, Any],
    is_draft: bool = False,
) -> bytes:
    """
    Generate a professional hospital estimate letterhead PDF as bytes.
    If is_draft is True, stamps 'DRAFT - NOT SAVED' in the header.
    """
    # Normalize input data
    if isinstance(estimate_data, EstimateResult):
        data = estimate_data.to_snapshot_dict()
    else:
        data = estimate_data

    meta = data.get("metadata", {})
    est_num = meta.get("estimate_number") or ""
    validity_days = int(meta.get("validity_days", 15))
    created_at = meta.get("created_at") or data.get("patient", {}).get("created_at")

    if created_at:
        try:
            dt = datetime.fromisoformat(created_at)
            created_str = dt.strftime("%d-%b-%Y")
            valid_until_str = (dt + timedelta(days=validity_days)).strftime("%d-%b-%Y")
        except Exception:
            created_str = str(created_at)[:10]
            valid_until_str = f"Within {validity_days} days"
    else:
        dt = datetime.now()
        created_str = dt.strftime("%d-%b-%Y")
        valid_until_str = (dt + timedelta(days=validity_days)).strftime("%d-%b-%Y")

    buffer = io.BytesIO()
    doc_title = f"Hospital Cost Estimate - {'DRAFT - NOT SAVED' if is_draft or not est_num else est_num}"
    doc_subject = f"Estimate Date: {created_str} | Valid Until: {valid_until_str}"
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=doc_title,
        subject=doc_subject,
    )

    story: List[Any] = []
    styles = getSampleStyleSheet()

    # Custom typography styles
    style_h1 = ParagraphStyle(
        "HospitalName",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#1A365D"),  # Deep Navy
    )

    style_sub = ParagraphStyle(
        "HospitalSub",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#4A5568"),
    )

    style_title = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#2B6CB0"),
        alignment=1,  # Center
    )

    style_draft_badge = ParagraphStyle(
        "DraftBadge",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#C53030"),  # Dark Red
        alignment=1,
    )

    style_letterhead_placeholder = ParagraphStyle(
        "LetterheadPlaceholder",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#4A5568"),
        alignment=1,  # Center
    )

    style_cell = ParagraphStyle(
        "CellNormal",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#2D3748"),
    )

    style_cell_bold = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#1A202C"),
    )

    style_cell_right = ParagraphStyle(
        "CellRight",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=8.5,
        leading=11,
        alignment=2,  # Right
        textColor=colors.HexColor("#2D3748"),
    )

    style_cell_right_bold = ParagraphStyle(
        "CellRightBold",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=8.5,
        leading=11,
        alignment=2,  # Right
        textColor=colors.HexColor("#1A202C"),
    )

    style_disclaimer = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#718096"),
    )

    patient = data.get("patient", {})
    clinical = data.get("clinical", {})
    fin = data.get("financials", {})
    line_items = data.get("line_items", [])

    h_name = xml_escape(str(meta.get("hospital_name") or "Sample Multispeciality Hospital"))
    h_addr = xml_escape(str(meta.get("hospital_address") or ""))
    h_phone = xml_escape(str(meta.get("hospital_phone") or ""))
    h_email = xml_escape(str(meta.get("hospital_email") or ""))
    disclaimer = xml_escape(str(meta.get("disclaimer_text") or ""))

    # 1. Header Section - Letterhead Placeholder
    placeholder_box = Table(
        [[Paragraph("[ HOSPITAL LETTERHEAD / LOGO PLACEHOLDER ]", style_letterhead_placeholder)]],
        colWidths=[180 * mm],
    )
    placeholder_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E0")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(placeholder_box)
    story.append(Spacer(1, 4 * mm))

    header_data = [
        [
            Paragraph(f"<b>{h_name}</b>", style_h1),
            Paragraph(
                f"<b>ESTIMATE DATE:</b> {created_str}<br/><b>VALID UNTIL:</b> {valid_until_str}",
                style_cell_right,
            ),
        ],
        [
            Paragraph(f"{h_addr}<br/>Tel: {h_phone} | Email: {h_email}", style_sub),
            "",
        ],
    ]
    header_table = Table(header_data, colWidths=[120 * mm, 60 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("SPAN", (0, 1), (1, 1)),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2B6CB0"), spaceAfter=8))

    # 2. Document Title / Draft Banner
    if is_draft or not est_num:
        doc_header_text = "ESTIMATED HOSPITAL TARIFF & BILLING PROPOSAL"
        draft_banner = Paragraph("<b>*** DRAFT - NOT SAVED ***</b>", style_draft_badge)
        story.append(draft_banner)
        story.append(Spacer(1, 2 * mm))
    else:
        doc_header_text = f"ESTIMATED HOSPITAL TARIFF & BILLING PROPOSAL — {est_num}"

    story.append(Paragraph(f"<b>{doc_header_text}</b>", style_title))
    story.append(Spacer(1, 4 * mm))

    # 3. Patient & Clinical Metadata Grid
    p_name = xml_escape(str(patient.get("name", "N/A")))
    dept_name = xml_escape(str(clinical.get("department", "N/A")))
    proc_name = xml_escape(str(clinical.get("procedure", "N/A")))
    doc_name = xml_escape(str(clinical.get("doctor", "N/A")))
    room_name = xml_escape(str(clinical.get("room_type", "N/A")))
    remarks_raw = patient.get("remarks")
    remarks_val = xml_escape(str(remarks_raw)) if remarks_raw else ""
    uhid_val = xml_escape(str(patient.get("uhid") or ""))
    mobile_val = xml_escape(str(patient.get("mobile_number") or ""))

    patient_info = [
        [
            Paragraph("<b>Patient Name:</b>", style_cell_bold),
            Paragraph(p_name, style_cell),
            Paragraph("<b>Department:</b>", style_cell_bold),
            Paragraph(dept_name, style_cell),
        ],
        [
            Paragraph("<b>Scheduled Procedure:</b>", style_cell_bold),
            Paragraph(proc_name, style_cell),
            Paragraph("<b>Primary Doctor/Surgeon:</b>", style_cell_bold),
            Paragraph(doc_name, style_cell),
        ],
        [
            Paragraph("<b>Room Accommodation:</b>", style_cell_bold),
            Paragraph(
                f"{room_name} (@ {format_inr(clinical.get('room_daily_rate', 0))}/day)",
                style_cell,
            ),
            Paragraph("<b>Estimated Stay:</b>", style_cell_bold),
            Paragraph(
                f"{patient.get('stay_days', 0)} Day{'s' if int(patient.get('stay_days', 0)) != 1 else ''}"
                f"{' (Day Care)' if int(patient.get('stay_days', 0)) == 0 else ''}",
                style_cell,
            ),
        ],
    ]
    # UHID and Mobile row (shown only when at least one is present)
    if uhid_val or mobile_val:
        patient_info.append(
            [
                Paragraph("<b>UHID:</b>", style_cell_bold),
                Paragraph(uhid_val or "—", style_cell),
                Paragraph("<b>Mobile Number:</b>", style_cell_bold),
                Paragraph(mobile_val or "—", style_cell),
            ]
        )
    if remarks_val:
        patient_info.append(
            [
                Paragraph("<b>Clinical Remarks:</b>", style_cell_bold),
                Paragraph(remarks_val, style_cell),
                "",
                "",
            ]
        )

    patient_table = Table(patient_info, colWidths=[38 * mm, 52 * mm, 40 * mm, 50 * mm])
    patient_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#EDF2F7")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    # Span the remarks cell across columns 1-3 (last row)
    if remarks_val:
        remarks_row_idx = len(patient_info) - 1
        patient_table.setStyle(TableStyle([("SPAN", (1, remarks_row_idx), (3, remarks_row_idx))]))
    story.append(patient_table)
    story.append(Spacer(1, 5 * mm))

    # 4. Itemized Cost Table
    table_rows = [
        [
            Paragraph("<b>#</b>", style_cell_bold),
            Paragraph("<b>Billing Item & Description</b>", style_cell_bold),
            Paragraph("<b>Category</b>", style_cell_bold),
            Paragraph("<b>Taxable</b>", style_cell_bold),
            Paragraph("<b>Amount (INR)</b>", style_cell_right_bold),
        ]
    ]

    for idx, item in enumerate(line_items, start=1):
        is_tax = "Yes" if item.get("is_taxable") else "No"
        amt_str = format_inr(item.get("amount", 0))
        cat_val = xml_escape(str(item.get("category", "")))
        desc_val = xml_escape(str(item.get("description", "")))
        table_rows.append(
            [
                Paragraph(str(idx), style_cell),
                Paragraph(f"<b>{cat_val}</b><br/>{desc_val}", style_cell),
                Paragraph(cat_val, style_cell),
                Paragraph(is_tax, style_cell),
                Paragraph(amt_str, style_cell_right),
            ]
        )

    # Financial Totals Rows - Spanning Columns 0-3 to prevent wrapping
    totals_start_row = len(table_rows)

    subtotal_str = format_inr(fin.get("subtotal", 0))
    table_rows.append([Paragraph("<b>Subtotal:</b>", style_cell_right_bold), "", "", "", Paragraph(f"<b>{subtotal_str}</b>", style_cell_right_bold)])

    disc_pct = Decimal(str(fin.get("discount_pct", "0")))
    if disc_pct > Decimal("0"):
        disc_amt_str = f"-{format_inr(fin.get('discount_amount', 0))}"
        table_rows.append([Paragraph(f"Discount ({disc_pct}%):", style_cell_right), "", "", "", Paragraph(disc_amt_str, style_cell_right)])
        net_str = format_inr(fin.get("net_base", 0))
        table_rows.append([Paragraph("Net Base:", style_cell_right), "", "", "", Paragraph(net_str, style_cell_right)])

    cont_pct = fin.get("contingency_pct", "10")
    cont_amt_str = format_inr(fin.get("contingency_amount", 0))
    table_rows.append([Paragraph(f"Contingency Buffer ({cont_pct}%):", style_cell_right), "", "", "", Paragraph(cont_amt_str, style_cell_right)])

    tax_pct = Decimal(str(fin.get("tax_pct", "0")))
    if tax_pct > Decimal("0"):
        tax_amt_str = format_inr(fin.get("tax_amount", 0))
        table_rows.append([Paragraph(f"GST / Tax ({tax_pct}%):", style_cell_right), "", "", "", Paragraph(tax_amt_str, style_cell_right)])

    grand_total_str = format_inr(fin.get("total_amount", 0))
    table_rows.append([Paragraph("<b>TOTAL ESTIMATE:</b>", style_cell_right_bold), "", "", "", Paragraph(f"<b>{grand_total_str}</b>", style_cell_right_bold)])

    totals_end_row = len(table_rows) - 1

    item_table = Table(table_rows, colWidths=[8 * mm, 80 * mm, 40 * mm, 18 * mm, 34 * mm])
    t_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, len(line_items)), 0.5, colors.HexColor("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#1A365D")),
        # Highlight grand total row
        ("BACKGROUND", (0, totals_end_row), (-1, totals_end_row), colors.HexColor("#EBF8FF")),
        ("BOX", (0, totals_end_row), (-1, totals_end_row), 1, colors.HexColor("#2B6CB0")),
    ]
    # Span columns 0-3 for all totals rows and right align
    for r in range(totals_start_row, totals_end_row + 1):
        t_styles.append(("SPAN", (0, r), (3, r)))
        t_styles.append(("ALIGN", (0, r), (3, r), "RIGHT"))

    item_table.setStyle(TableStyle(t_styles))
    story.append(item_table)
    story.append(Spacer(1, 4 * mm))

    # 5. Estimate Range Band Callout
    min_est = format_inr(fin.get("min_estimate", 0))
    max_est = format_inr(fin.get("max_estimate", 0))
    range_pct = fin.get("estimate_range_pct", "10")

    range_text = (
        f"<b>Expected Cost Range (±{range_pct}%): {min_est} — {max_est}</b><br/>"
        f"<i>(Accounts for minor variations in consumable utilization, diagnostics, or clinical course)</i>"
    )
    range_box = Table(
        [[Paragraph(range_text, style_cell)]],
        colWidths=[180 * mm],
    )
    range_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEFCBF")),  # Soft Yellow
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D69E2E")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(range_box)
    story.append(Spacer(1, 5 * mm))

    # 6. Disclaimer & Legal Notice
    disclaimer_block = [
        Paragraph("<b>IMPORTANT TERMS & CONDITIONS:</b>", style_cell_bold),
        Spacer(1, 1 * mm),
        Paragraph(disclaimer, style_disclaimer),
        Spacer(1, 2 * mm),
        Paragraph(
            f"1. This estimate remains valid for <b>{validity_days} days</b> from {created_str}.<br/>"
            "2. Admission is subject to bed availability in the requested category.<br/>"
            "3. High-risk cases, unanticipated ICU stay, blood components, or non-formulary medications will be charged on actuals.",
            style_disclaimer,
        ),
    ]
    story.append(KeepTogether(disclaimer_block))
    story.append(Spacer(1, 8 * mm))

    # 7. Signature Block
    sig_data = [
        [
            Paragraph("Prepared by: Billing Desk", style_cell),
            Paragraph("Authorized Hospital Signatory<br/>(Patient Billing & Tariffs)", style_cell_right),
        ]
    ]
    sig_table = Table(sig_data, colWidths=[90 * mm, 90 * mm])
    sig_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.HexColor("#718096")),
                ("LINEABOVE", (1, 0), (1, 0), 0.5, colors.HexColor("#718096")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(KeepTogether(sig_table))

    # Build PDF
    doc.build(story)
    return buffer.getvalue()
