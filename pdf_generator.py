"""
pdf_generator.py - Professional hospital estimate letterhead generator using ReportLab.
Uses bundled Noto Sans TTF for full Unicode and Indian Rupee (₹) support.
Supports customizable layout, branding, typography, margins, and visibility toggles via pdf_layout.
"""

from __future__ import annotations

import io
import os
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
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
from pdf_layout import DEFAULT_PDF_LAYOUT, get_merged_layout


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


def _parse_hex_color(val: Any, default: str) -> colors.Color:
    """Safely parse hex color string into ReportLab HexColor."""
    try:
        s = str(val).strip()
        if s.startswith("#") and len(s) in (4, 7, 9):
            return colors.HexColor(s)
    except Exception:
        pass
    return colors.HexColor(default)


def generate_estimate_pdf(
    estimate_data: EstimateResult | Dict[str, Any],
    is_draft: bool = False,
    layout_config: Optional[Dict[str, Any]] = None,
) -> bytes:
    """
    Generate a professional hospital estimate letterhead PDF as bytes.
    If is_draft is True, stamps draft watermark in the header.
    Incorporates customizable layout settings from layout_config, frozen snapshot, or DEFAULT_PDF_LAYOUT.
    """
    # Normalize input data
    if isinstance(estimate_data, EstimateResult):
        data = estimate_data.to_snapshot_dict()
    else:
        data = estimate_data

    meta = data.get("metadata", {})

    # Resolve layout configuration
    if layout_config:
        layout = get_merged_layout(layout_config)
    elif isinstance(data, dict) and data.get("pdf_layout_snapshot"):
        layout = get_merged_layout(data.get("pdf_layout_snapshot"))
    else:
        layout = dict(DEFAULT_PDF_LAYOUT)
        if meta.get("hospital_name") and not layout.get("hospital_name"):
            layout["hospital_name"] = meta.get("hospital_name")
            layout["show_hospital_letterhead"] = True

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

    # Page setup and margins
    page_size_name = layout.get("page_size", "A4").upper()
    pagesize = letter if page_size_name == "LETTER" else A4

    margin_top = float(layout.get("margin_top_mm", 15.0)) * mm
    margin_bottom = float(layout.get("margin_bottom_mm", 15.0)) * mm
    margin_left = float(layout.get("margin_left_mm", 15.0)) * mm
    margin_right = float(layout.get("margin_right_mm", 15.0)) * mm

    content_width = pagesize[0] - margin_left - margin_right

    buffer = io.BytesIO()
    doc_title = f"Hospital Cost Estimate - {'DRAFT - NOT SAVED' if is_draft or not est_num else est_num}"
    doc_subject = f"Estimate Date: {created_str} | Valid Until: {valid_until_str}"
    doc = SimpleDocTemplate(
        buffer,
        pagesize=pagesize,
        leftMargin=margin_left,
        rightMargin=margin_right,
        topMargin=margin_top,
        bottomMargin=margin_bottom,
        title=doc_title,
        subject=doc_subject,
    )

    story: List[Any] = []
    styles = getSampleStyleSheet()

    # Color resolution
    primary_color = _parse_hex_color(layout.get("primary_color"), "#1A365D")
    secondary_color = _parse_hex_color(layout.get("secondary_color"), "#2B6CB0")
    table_header_bg = _parse_hex_color(layout.get("table_header_bg"), "#2B6CB0")
    table_header_text_color = _parse_hex_color(layout.get("table_header_text_color"), "#FFFFFF")
    table_border_color = _parse_hex_color(layout.get("table_border_color"), "#CBD5E0")
    accent_color = _parse_hex_color(layout.get("accent_color"), "#EBF8FF")

    title_fs = int(layout.get("title_font_size", 18))
    body_fs = float(layout.get("body_font_size", 8.5))
    table_fs = float(layout.get("table_font_size", 8.5))

    align_map = {"LEFT": 0, "CENTER": 1, "RIGHT": 2}
    h_align = align_map.get(layout.get("header_alignment", "LEFT").upper(), 0)

    # Custom typography styles
    style_h1 = ParagraphStyle(
        "HospitalName",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=title_fs,
        leading=title_fs + 4,
        textColor=primary_color,
        alignment=h_align,
    )

    style_sub = ParagraphStyle(
        "HospitalSub",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=body_fs,
        leading=body_fs + 3.5,
        textColor=colors.HexColor("#4A5568"),
        alignment=h_align,
    )

    style_title = ParagraphStyle(
        "DocTitle",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=max(11, title_fs - 5),
        leading=max(14, title_fs - 2),
        textColor=secondary_color,
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
        fontSize=table_fs,
        leading=table_fs + 2.5,
        textColor=colors.HexColor("#2D3748"),
    )

    style_cell_bold = ParagraphStyle(
        "CellBold",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=table_fs,
        leading=table_fs + 2.5,
        textColor=colors.HexColor("#1A202C"),
    )

    style_cell_right = ParagraphStyle(
        "CellRight",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=table_fs,
        leading=table_fs + 2.5,
        alignment=2,  # Right
        textColor=colors.HexColor("#2D3748"),
    )

    style_cell_right_bold = ParagraphStyle(
        "CellRightBold",
        parent=styles["Normal"],
        fontName=BOLD_FONT_NAME,
        fontSize=table_fs,
        leading=table_fs + 2.5,
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

    h_name = xml_escape(str(layout.get("hospital_name") or ""))
    h_sub = xml_escape(str(layout.get("hospital_subtitle") or ""))
    h_addr = xml_escape(str(layout.get("hospital_address") or ""))
    h_contact = xml_escape(str(layout.get("hospital_contact") or ""))

    # Pre-printed stationery support:
    # If hospital_name is empty or show_hospital_letterhead is disabled, leave blank space for physical letterhead
    should_print_letterhead = bool(h_name) and bool(layout.get("show_hospital_letterhead", True))
    blank_space_mm = float(layout.get("letterhead_blank_space_mm", 40.0))

    if should_print_letterhead:
        # 1. Header Section - Letterhead Placeholder (if toggled)
        if layout.get("show_letterhead_placeholder", False):
            placeholder_box = Table(
                [[Paragraph("[ HOSPITAL LETTERHEAD / LOGO PLACEHOLDER ]", style_letterhead_placeholder)]],
                colWidths=[content_width],
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

        # Header details table
        header_w_left = content_width * 0.65
        header_w_right = content_width * 0.35

        date_block = f"<b>ESTIMATE DATE:</b> {created_str}"
        if layout.get("show_validity_date", True):
            date_block += f"<br/><b>VALID UNTIL:</b> {valid_until_str}"

        sub_parts = []
        if h_sub:
            sub_parts.append(f"<i>{h_sub}</i>")
        if h_addr:
            sub_parts.append(h_addr)
        if h_contact:
            sub_parts.append(h_contact)
        sub_text = "<br/>".join(sub_parts)

        header_data = [
            [
                Paragraph(f"<b>{h_name}</b>", style_h1),
                Paragraph(date_block, style_cell_right),
            ],
            [
                Paragraph(sub_text, style_sub),
                "",
            ],
        ]
        header_table = Table(header_data, colWidths=[header_w_left, header_w_right])
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
        story.append(HRFlowable(width="100%", thickness=1.5, color=secondary_color, spaceAfter=8))
    else:
        # Pre-printed letterhead mode: Leave top blank space for physical letterhead stationery
        if blank_space_mm > 0:
            story.append(Spacer(1, blank_space_mm * mm))

    # 2. Document Title / Draft Banner
    proposal_title = xml_escape(str(layout.get("table_title_text") or "ESTIMATED HOSPITAL TARIFF & BILLING PROPOSAL"))
    draft_watermark_text = xml_escape(str(layout.get("draft_watermark_text") or "*** DRAFT - NOT SAVED ***"))

    if is_draft or not est_num:
        doc_header_text = proposal_title
        draft_banner = Paragraph(f"<b>{draft_watermark_text}</b>", style_draft_badge)
        story.append(draft_banner)
        story.append(Spacer(1, 2 * mm))
    else:
        doc_header_text = f"{proposal_title} - {est_num}"

    story.append(Paragraph(f"<b>{doc_header_text}</b>", style_title))

    if not should_print_letterhead:
        style_date_line = ParagraphStyle(
            "DateLineSub",
            parent=styles["Normal"],
            fontName=BOLD_FONT_NAME,
            fontSize=body_fs,
            leading=body_fs + 3.5,
            textColor=colors.HexColor("#4A5568"),
            alignment=1,  # Center
        )
        date_line_text = f"ESTIMATE DATE: {created_str}"
        if layout.get("show_validity_date", True):
            date_line_text += f" &nbsp; | &nbsp; VALID UNTIL: {valid_until_str}"
        story.append(Spacer(1, 1.5 * mm))
        story.append(Paragraph(date_line_text, style_date_line))
        story.append(Spacer(1, 4 * mm))
    else:
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

    show_doc = layout.get("show_attending_doctor", True)
    doctor_display = doc_name if show_doc else "-"

    stay_val = patient.get("stay_days", 0)
    stay_str = f"{stay_val} Day{'s' if int(stay_val) != 1 else ''}{' (Day Care)' if int(stay_val) == 0 else ''}"

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
            Paragraph(doctor_display, style_cell),
        ],
        [
            Paragraph("<b>Room Accommodation:</b>", style_cell_bold),
            Paragraph(
                f"{room_name} (@ {format_inr(clinical.get('room_daily_rate', 0))}/day)",
                style_cell,
            ),
            Paragraph("<b>Estimated Stay:</b>", style_cell_bold),
            Paragraph(stay_str, style_cell),
        ],
    ]

    # UHID and Mobile row (if toggled and at least one is present)
    if layout.get("show_patient_uhid_mobile", True) and (uhid_val or mobile_val):
        patient_info.append(
            [
                Paragraph("<b>UHID:</b>", style_cell_bold),
                Paragraph(uhid_val or "-", style_cell),
                Paragraph("<b>Mobile Number:</b>", style_cell_bold),
                Paragraph(mobile_val or "-", style_cell),
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

    w_col1 = content_width * 0.21
    w_col2 = content_width * 0.29
    w_col3 = content_width * 0.22
    w_col4 = content_width - (w_col1 + w_col2 + w_col3)

    patient_table = Table(patient_info, colWidths=[w_col1, w_col2, w_col3, w_col4])
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
    if remarks_val:
        remarks_row_idx = len(patient_info) - 1
        patient_table.setStyle(TableStyle([("SPAN", (1, remarks_row_idx), (3, remarks_row_idx))]))
    story.append(patient_table)
    story.append(Spacer(1, 5 * mm))

    # Custom Column Headers
    lbl_sno = xml_escape(str(layout.get("col_header_sno", "#")))
    lbl_desc = xml_escape(str(layout.get("col_header_description", "Billing Item & Description")))
    lbl_cat = xml_escape(str(layout.get("col_header_category", "Category")))
    lbl_tax = xml_escape(str(layout.get("col_header_taxable", "Taxable")))
    lbl_amt = xml_escape(str(layout.get("col_header_amount", "Amount (INR)")))

    # 4. Itemized Cost Table (Comparative or Single-Room)
    comp_estimates = data.get("comparative_estimates", [])
    render_comparative = comp_estimates and layout.get("show_room_upgrade_comparison", True)

    if render_comparative:
        primary_room = xml_escape(str(clinical.get("room_type", "Primary Room")))
        num_comps = len(comp_estimates)
        col_w = max(26.0 * mm, min(36.0 * mm, (content_width * 0.53) / (num_comps + 1)))
        idx_w = 7 * mm
        cat_w = 26 * mm
        desc_w = content_width - idx_w - cat_w - (col_w * (num_comps + 1))
        col_widths = [idx_w, desc_w, cat_w, col_w] + [col_w] * num_comps

        header_row = [
            Paragraph(f"<b>{lbl_sno}</b>", style_cell_bold),
            Paragraph(f"<b>{lbl_desc}</b>", style_cell_bold),
            Paragraph(f"<b>{lbl_cat}</b>", style_cell_bold),
            Paragraph(f"<b>{primary_room} (INR)</b>", style_cell_right_bold),
        ]
        for comp in comp_estimates:
            c_name = xml_escape(str(comp.get("clinical", {}).get("room_type", "Comparative Room")))
            header_row.append(Paragraph(f"<b>{c_name} (INR)</b>", style_cell_right_bold))
        table_rows = [header_row]

        for idx, item in enumerate(line_items, start=1):
            cat_val = xml_escape(str(item.get("category", "")))
            desc_val = xml_escape(str(item.get("description", "")))
            primary_amt = format_inr(item.get("amount", 0))

            row = [
                Paragraph(str(idx), style_cell),
                Paragraph(f"<b>{cat_val}</b><br/>{desc_val}", style_cell),
                Paragraph(cat_val, style_cell),
                Paragraph(primary_amt, style_cell_right),
            ]
            for comp in comp_estimates:
                comp_items = comp.get("line_items", [])
                if idx - 1 < len(comp_items):
                    c_amt = format_inr(comp_items[idx - 1].get("amount", 0))
                else:
                    c_amt = primary_amt
                row.append(Paragraph(c_amt, style_cell_right))
            table_rows.append(row)

        totals_start_row = len(table_rows)

        # Subtotal
        sub_row = [Paragraph("<b>Subtotal:</b>", style_cell_right_bold), "", ""]
        sub_row.append(Paragraph(f"<b>{format_inr(fin.get('subtotal', 0))}</b>", style_cell_right_bold))
        for comp in comp_estimates:
            c_sub = format_inr(comp.get("financials", {}).get("subtotal", 0))
            sub_row.append(Paragraph(f"<b>{c_sub}</b>", style_cell_right_bold))
        table_rows.append(sub_row)

        # Discount (if toggled)
        disc_pct = Decimal(str(fin.get("discount_pct", "0")))
        if layout.get("show_discount_row", True) and disc_pct > Decimal("0"):
            disc_row = [Paragraph(f"Discount ({disc_pct}%):", style_cell_right), "", ""]
            disc_row.append(Paragraph(f"-{format_inr(fin.get('discount_amount', 0))}", style_cell_right))
            for comp in comp_estimates:
                c_disc = format_inr(comp.get("financials", {}).get("discount_amount", 0))
                disc_row.append(Paragraph(f"-{c_disc}", style_cell_right))
            table_rows.append(disc_row)

            net_row = [Paragraph("Net Base:", style_cell_right), "", ""]
            net_row.append(Paragraph(format_inr(fin.get("net_base", 0)), style_cell_right))
            for comp in comp_estimates:
                c_net = format_inr(comp.get("financials", {}).get("net_base", 0))
                net_row.append(Paragraph(c_net, style_cell_right))
            table_rows.append(net_row)

        # Contingency (if toggled)
        if layout.get("show_contingency_row", True):
            cont_pct = fin.get("contingency_pct", "10")
            cont_row = [Paragraph(f"Contingency Buffer ({cont_pct}%):", style_cell_right), "", ""]
            cont_row.append(Paragraph(format_inr(fin.get("contingency_amount", 0)), style_cell_right))
            for comp in comp_estimates:
                c_cont = format_inr(comp.get("financials", {}).get("contingency_amount", 0))
                cont_row.append(Paragraph(c_cont, style_cell_right))
            table_rows.append(cont_row)

        # Tax (if toggled)
        tax_pct = Decimal(str(fin.get("tax_pct", "0")))
        if layout.get("show_tax_row", True) and tax_pct > Decimal("0"):
            tax_row = [Paragraph(f"GST / Tax ({tax_pct}%):", style_cell_right), "", ""]
            tax_row.append(Paragraph(format_inr(fin.get("tax_amount", 0)), style_cell_right))
            for comp in comp_estimates:
                c_tax = format_inr(comp.get("financials", {}).get("tax_amount", 0))
                tax_row.append(Paragraph(c_tax, style_cell_right))
            table_rows.append(tax_row)

        # Grand Total
        tot_row = [Paragraph("<b>TOTAL ESTIMATE:</b>", style_cell_right_bold), "", ""]
        tot_row.append(Paragraph(f"<b>{format_inr(fin.get('total_amount', 0))}</b>", style_cell_right_bold))
        for comp in comp_estimates:
            c_tot = format_inr(comp.get("financials", {}).get("total_amount", 0))
            tot_row.append(Paragraph(f"<b>{c_tot}</b>", style_cell_right_bold))
        table_rows.append(tot_row)

        totals_end_row = len(table_rows) - 1

        item_table = Table(table_rows, colWidths=col_widths)
        t_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), table_header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), table_header_text_color),
            ("GRID", (0, 0), (-1, len(line_items)), 0.5, table_border_color),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, primary_color),
            ("BACKGROUND", (0, totals_end_row), (-1, totals_end_row), accent_color),
            ("BOX", (0, totals_end_row), (-1, totals_end_row), 1, secondary_color),
        ]
        for r in range(totals_start_row, totals_end_row + 1):
            t_styles.append(("SPAN", (0, r), (2, r)))
            t_styles.append(("ALIGN", (0, r), (2, r), "RIGHT"))

        item_table.setStyle(TableStyle(t_styles))
        story.append(item_table)
        story.append(Spacer(1, 4 * mm))

        # Range Box (if toggled)
        if layout.get("show_range_band_row", True):
            range_pct = fin.get("estimate_range_pct", "10")
            range_lines = [f"<b>Expected Cost Ranges (+/-{range_pct}%):</b>"]
            range_lines.append(f"- <b>{primary_room}:</b> {format_inr(fin.get('min_estimate', 0))} to {format_inr(fin.get('max_estimate', 0))}")
            for comp in comp_estimates:
                c_name = xml_escape(str(comp.get("clinical", {}).get("room_type", "Comparative Room")))
                c_fin = comp.get("financials", {})
                range_lines.append(f"- <b>{c_name}:</b> {format_inr(c_fin.get('min_estimate', 0))} to {format_inr(c_fin.get('max_estimate', 0))}")
            range_lines.append("<i>(Accounts for minor variations in consumable utilization, diagnostics, or clinical course)</i>")
            range_text = "<br/>".join(range_lines)

            range_box = Table([[Paragraph(range_text, style_cell)]], colWidths=[content_width])
            range_box.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEFCBF")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D69E2E")),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.append(range_box)
            story.append(Spacer(1, 5 * mm))

    else:
        # Single Room Table
        table_rows = [
            [
                Paragraph(f"<b>{lbl_sno}</b>", style_cell_bold),
                Paragraph(f"<b>{lbl_desc}</b>", style_cell_bold),
                Paragraph(f"<b>{lbl_cat}</b>", style_cell_bold),
                Paragraph(f"<b>{lbl_tax}</b>", style_cell_bold),
                Paragraph(f"<b>{lbl_amt}</b>", style_cell_right_bold),
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

        totals_start_row = len(table_rows)

        subtotal_str = format_inr(fin.get("subtotal", 0))
        table_rows.append([Paragraph("<b>Subtotal:</b>", style_cell_right_bold), "", "", "", Paragraph(f"<b>{subtotal_str}</b>", style_cell_right_bold)])

        disc_pct = Decimal(str(fin.get("discount_pct", "0")))
        if layout.get("show_discount_row", True) and disc_pct > Decimal("0"):
            disc_amt_str = f"-{format_inr(fin.get('discount_amount', 0))}"
            table_rows.append([Paragraph(f"Discount ({disc_pct}%):", style_cell_right), "", "", "", Paragraph(disc_amt_str, style_cell_right)])
            net_str = format_inr(fin.get("net_base", 0))
            table_rows.append([Paragraph("Net Base:", style_cell_right), "", "", "", Paragraph(net_str, style_cell_right)])

        if layout.get("show_contingency_row", True):
            cont_pct = fin.get("contingency_pct", "10")
            cont_amt_str = format_inr(fin.get("contingency_amount", 0))
            table_rows.append([Paragraph(f"Contingency Buffer ({cont_pct}%):", style_cell_right), "", "", "", Paragraph(cont_amt_str, style_cell_right)])

        tax_pct = Decimal(str(fin.get("tax_pct", "0")))
        if layout.get("show_tax_row", True) and tax_pct > Decimal("0"):
            tax_amt_str = format_inr(fin.get("tax_amount", 0))
            table_rows.append([Paragraph(f"GST / Tax ({tax_pct}%):", style_cell_right), "", "", "", Paragraph(tax_amt_str, style_cell_right)])

        grand_total_str = format_inr(fin.get("total_amount", 0))
        table_rows.append([Paragraph("<b>TOTAL ESTIMATE:</b>", style_cell_right_bold), "", "", "", Paragraph(f"<b>{grand_total_str}</b>", style_cell_right_bold)])

        totals_end_row = len(table_rows) - 1

        w_idx = 8 * mm
        w_cat = 38 * mm
        w_tax = 16 * mm
        w_amt = 32 * mm
        w_desc = content_width - (w_idx + w_cat + w_tax + w_amt)

        item_table = Table(table_rows, colWidths=[w_idx, w_desc, w_cat, w_tax, w_amt])
        t_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), table_header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), table_header_text_color),
            ("GRID", (0, 0), (-1, len(line_items)), 0.5, table_border_color),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, primary_color),
            ("BACKGROUND", (0, totals_end_row), (-1, totals_end_row), accent_color),
            ("BOX", (0, totals_end_row), (-1, totals_end_row), 1, secondary_color),
        ]
        for r in range(totals_start_row, totals_end_row + 1):
            t_styles.append(("SPAN", (0, r), (3, r)))
            t_styles.append(("ALIGN", (0, r), (3, r), "RIGHT"))

        item_table.setStyle(TableStyle(t_styles))
        story.append(item_table)
        story.append(Spacer(1, 4 * mm))

        if layout.get("show_range_band_row", True):
            min_est = format_inr(fin.get("min_estimate", 0))
            max_est = format_inr(fin.get("max_estimate", 0))
            range_pct = fin.get("estimate_range_pct", "10")

            range_text = (
                f"<b>Expected Cost Range (+/-{range_pct}%): {min_est} to {max_est}</b><br/>"
                f"<i>(Accounts for minor variations in consumable utilization, diagnostics, or clinical course)</i>"
            )
            range_box = Table(
                [[Paragraph(range_text, style_cell)]],
                colWidths=[content_width],
            )
            range_box.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEFCBF")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D69E2E")),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.append(range_box)
            story.append(Spacer(1, 5 * mm))

    # 5. Disclaimer & Legal Notice
    disclaimer_heading = xml_escape(str(layout.get("disclaimer_heading") or "IMPORTANT TERMS & CONDITIONS:"))
    disclaimers_raw = str(layout.get("disclaimers_text") or meta.get("disclaimer_text") or "").strip()

    disclaimer_lines = [
        Paragraph(f"<b>{disclaimer_heading}</b>", style_cell_bold),
        Spacer(1, 1 * mm),
    ]

    if disclaimers_raw:
        for line in disclaimers_raw.split("\n"):
            line_str = line.strip()
            if line_str:
                disclaimer_lines.append(Paragraph(xml_escape(line_str), style_disclaimer))
    else:
        disclaimer_lines.append(
            Paragraph(
                f"1. This estimate remains valid for <b>{validity_days} days</b> from {created_str}.<br/>"
                "2. Admission is subject to bed availability in the requested category.<br/>"
                "3. High-risk cases, unanticipated ICU stay, blood components, or non-formulary medications will be charged on actuals.",
                style_disclaimer,
            )
        )

    story.append(KeepTogether(disclaimer_lines))
    story.append(Spacer(1, 8 * mm))

    # 6. Signature Block (if toggled)
    if layout.get("show_signature_block", True):
        left_title = xml_escape(str(layout.get("left_signatory_title") or "Prepared by: Billing Desk"))
        left_dept = xml_escape(str(layout.get("left_signatory_dept") or "Billing Desk"))
        right_title = xml_escape(str(layout.get("right_signatory_title") or "Authorized Hospital Signatory"))
        right_dept = xml_escape(str(layout.get("right_signatory_dept") or "(Patient Billing & Tariffs)"))

        sig_left = f"{left_title}<br/><i>{left_dept}</i>" if left_dept else left_title
        sig_right = f"{right_title}<br/><i>{right_dept}</i>" if right_dept else right_title

        sig_w = content_width / 2.0
        sig_data = [
            [
                Paragraph(sig_left, style_cell),
                Paragraph(sig_right, style_cell_right),
            ]
        ]
        sig_table = Table(sig_data, colWidths=[sig_w, sig_w])
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
