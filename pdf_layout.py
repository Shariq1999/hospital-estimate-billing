"""
pdf_layout.py - Interactive PDF print layout configuration and Streamlit editor.
Supports hospital letterhead branding, custom typography, table headers,
metadata visibility toggles, legal disclaimers, and live base64 PDF preview.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any, Dict, Optional
from xml.sax.saxutils import escape as xml_escape

import streamlit as st

import billing
import database
import pdf_generator


DEFAULT_PDF_LAYOUT: Dict[str, Any] = {
    # 1. Hospital Letterhead & Branding
    "hospital_name": "Sample Multispeciality Hospital",
    "hospital_subtitle": "NABH & JCI Accredited Tertiary Care Center",
    "hospital_address": "Plot 12, Health City Road, Sample Nagar, Mumbai - 400 001",
    "hospital_contact": "Tel: +91 22 0000 0000 | Email: billing@samplehospital.example | GSTIN: 27AAAAA0000A1Z5",
    "header_alignment": "LEFT",  # "LEFT", "CENTER", "RIGHT"
    "primary_color": "#1A365D",  # Deep Navy
    "secondary_color": "#2B6CB0",
    "table_header_bg": "#2B6CB0",
    "table_header_text_color": "#FFFFFF",
    "table_border_color": "#CBD5E0",
    "table_row_even_bg": "#FFFFFF",
    "table_row_odd_bg": "#F7FAFC",
    "accent_color": "#EBF8FF",

    # 2. Page Setup & Typography
    "page_size": "A4",  # "A4", "LETTER"
    "margin_top_mm": 15.0,
    "margin_bottom_mm": 15.0,
    "margin_left_mm": 15.0,
    "margin_right_mm": 15.0,
    "title_font_size": 18,
    "body_font_size": 8.5,
    "table_font_size": 8.5,

    # 3. Metadata & Section Visibility Toggles
    "show_letterhead_placeholder": True,
    "show_patient_uhid_mobile": True,
    "show_attending_doctor": True,
    "show_validity_date": True,
    "show_discount_row": True,
    "show_contingency_row": True,
    "show_tax_row": True,
    "show_range_band_row": True,
    "show_room_upgrade_comparison": True,
    "show_signature_block": True,

    # 4. Custom Table Column Headers
    "col_header_sno": "#",
    "col_header_description": "Particulars / Tariff Category",
    "col_header_category": "Category",
    "col_header_taxable": "Taxable",
    "col_header_amount": "Amount (INR)",
    "table_title_text": "ESTIMATED HOSPITAL TARIFF & BILLING PROPOSAL",

    # 5. Watermarks, Legal/Clinical Disclaimers & Signatories
    "draft_watermark_text": "*** DRAFT - NOT SAVED ***",
    "disclaimer_heading": "IMPORTANT TERMS & CONDITIONS:",
    "disclaimers_text": (
        "1. This estimate is based on the standard treatment protocol and selected room category.\n"
        "2. Admission is subject to bed availability in the requested category.\n"
        "3. High-risk cases, unanticipated ICU stay, blood components, or non-formulary medications will be charged on actuals.\n"
        "4. This estimate remains valid for the stipulated validity window from date of issue."
    ),
    "left_signatory_title": "Prepared By (Financial Counseling)",
    "left_signatory_dept": "Billing Desk",
    "right_signatory_title": "Authorized Hospital Signatory",
    "right_signatory_dept": "Patient Billing & Tariffs",
}


def get_merged_layout(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a complete layout dictionary with missing keys filled from DEFAULT_PDF_LAYOUT."""
    merged = dict(DEFAULT_PDF_LAYOUT)
    if config:
        merged.update(config)
    return merged


def create_sample_estimate_snapshot() -> Dict[str, Any]:
    """Generate a realistic mock estimate snapshot for the live layout preview."""
    return {
        "metadata": {
            "estimate_number": "EST-20261025-0042",
            "hospital_name": DEFAULT_PDF_LAYOUT["hospital_name"],
            "hospital_address": DEFAULT_PDF_LAYOUT["hospital_address"],
            "hospital_phone": "+91 22 0000 0000",
            "hospital_email": "billing@samplehospital.example",
            "validity_days": 15,
            "created_at": "2026-10-25T10:30:00",
            "disclaimer_text": DEFAULT_PDF_LAYOUT["disclaimers_text"],
        },
        "patient": {
            "name": "Rajesh Kumar",
            "stay_days": 3,
            "remarks": "Elective surgery under general anesthesia",
            "created_at": "2026-10-25T10:30:00",
            "uhid": "UHID-2026-8819",
            "mobile_number": "+91 98765 43210",
        },
        "clinical": {
            "department": "Orthopaedics",
            "doctor": "Dr. Sanjay Deshmukh",
            "doctor_fee_pct": "35",
            "surgeon_fee_type": "fixed",
            "surgeon_fee_amount": "30000",
            "procedure": "Total Knee Replacement (Unilateral)",
            "room_type": "Single Private Room",
            "room_daily_rate": 5500,
        },
        "financials": {
            "subtotal": "185000.00",
            "discount_pct": "5.0",
            "discount_amount": "9250.00",
            "net_base": "175750.00",
            "taxable_base": "0.00",
            "tax_pct": "0.0",
            "tax_amount": "0.00",
            "contingency_pct": "10.0",
            "contingency_amount": "17575.00",
            "total_amount": "193325.00",
            "min_estimate": "174000.00",
            "max_estimate": "212700.00",
            "estimate_range_pct": "10",
        },
        "line_items": [
            {
                "category": "Room Accommodation",
                "description": "Room Charges - Single Private Room (3 Days @ 5,500/day)",
                "quantity": "3",
                "unit_price": "5500.00",
                "amount": "16500.00",
                "is_taxable": False,
                "discount_amount": "825.00",
                "net_amount": "15675.00",
            },
            {
                "category": "Nursing Charges",
                "description": "Per Diem Clinical Nursing Care (3 Days @ 1,000/day)",
                "quantity": "3",
                "unit_price": "1000.00",
                "amount": "3000.00",
                "is_taxable": False,
                "discount_amount": "150.00",
                "net_amount": "2850.00",
            },
            {
                "category": "RMO & Duty Doctor",
                "description": "Resident Medical Officer Duty Charges (3 Days @ 500/day)",
                "quantity": "3",
                "unit_price": "500.00",
                "amount": "1500.00",
                "is_taxable": False,
                "discount_amount": "75.00",
                "net_amount": "1425.00",
            },
            {
                "category": "Hospital Procedure",
                "description": "Total Knee Replacement (Unilateral) Hospital Tariff",
                "quantity": "1",
                "unit_price": "85000.00",
                "amount": "85000.00",
                "is_taxable": False,
                "discount_amount": "4250.00",
                "net_amount": "80750.00",
            },
            {
                "category": "Operating Theatre",
                "description": "OT Suite Tariff (Escalation Factor 1.00x applied)",
                "quantity": "1",
                "unit_price": "28000.00",
                "amount": "28000.00",
                "is_taxable": False,
                "discount_amount": "1400.00",
                "net_amount": "26600.00",
            },
            {
                "category": "Anaesthesia Services",
                "description": "Anaesthesiologist Charges & Monitoring",
                "quantity": "1",
                "unit_price": "12000.00",
                "amount": "12000.00",
                "is_taxable": False,
                "discount_amount": "600.00",
                "net_amount": "11400.00",
            },
            {
                "category": "Consumables & Implants",
                "description": "Surgical Consumables, Implants & Disposables",
                "quantity": "1",
                "unit_price": "35000.00",
                "amount": "35000.00",
                "is_taxable": False,
                "discount_amount": "1750.00",
                "net_amount": "33250.00",
            },
            {
                "category": "Laboratory & Radiology",
                "description": "Pre-operative Investigations & Imaging",
                "quantity": "1",
                "unit_price": "4000.00",
                "amount": "4000.00",
                "is_taxable": False,
                "discount_amount": "200.00",
                "net_amount": "3800.00",
            },
        ],
    }


def render_pdf_layout_editor_tab(conn: sqlite3.Connection) -> None:
    """Render the interactive PDF layout editor tab in Streamlit."""
    st.subheader("PDF Print Layout & Letterhead Customizer")
    st.caption(
        "Configure hospital branding, typography, margins, table column titles, "
        "and legal signatories. Changes are saved to database and automatically apply "
        "to newly generated estimates."
    )

    current_config = database.get_pdf_layout(conn)

    col_editor, col_preview = st.columns([1.15, 1.0])

    with col_editor:
        with st.form("pdf_layout_editor_form"):
            st.write("##### 1. Hospital Letterhead & Branding")
            h_name = st.text_input(
                "Hospital / Clinic Name",
                value=current_config.get("hospital_name", DEFAULT_PDF_LAYOUT["hospital_name"]),
                key="cfg_hospital_name",
            )
            h_subtitle = st.text_input(
                "Hospital Subtitle / Accreditation Line",
                value=current_config.get("hospital_subtitle", DEFAULT_PDF_LAYOUT["hospital_subtitle"]),
                key="cfg_hospital_subtitle",
            )
            h_address = st.text_input(
                "Address Line",
                value=current_config.get("hospital_address", DEFAULT_PDF_LAYOUT["hospital_address"]),
                key="cfg_hospital_address",
            )
            h_contact = st.text_input(
                "Contact Line (Phone, Email, GSTIN)",
                value=current_config.get("hospital_contact", DEFAULT_PDF_LAYOUT["hospital_contact"]),
                key="cfg_hospital_contact",
            )

            c_align, c_pri_col, c_hdr_bg = st.columns(3)
            with c_align:
                align_opts = ["LEFT", "CENTER", "RIGHT"]
                cur_align = current_config.get("header_alignment", "LEFT").upper()
                align_idx = align_opts.index(cur_align) if cur_align in align_opts else 0
                h_align = st.selectbox("Header Alignment", align_opts, index=align_idx, key="cfg_header_align")
            with c_pri_col:
                primary_color = st.color_picker(
                    "Primary Theme Color",
                    value=current_config.get("primary_color", DEFAULT_PDF_LAYOUT["primary_color"]),
                    key="cfg_primary_color",
                )
            with c_hdr_bg:
                table_header_bg = st.color_picker(
                    "Table Header Background",
                    value=current_config.get("table_header_bg", DEFAULT_PDF_LAYOUT["table_header_bg"]),
                    key="cfg_table_header_bg",
                )

            st.divider()

            st.write("##### 2. Page Setup & Typography")
            c_pg, c_tfs, c_bfs = st.columns(3)
            with c_pg:
                pg_opts = ["A4", "LETTER"]
                cur_pg = current_config.get("page_size", "A4").upper()
                pg_idx = pg_opts.index(cur_pg) if cur_pg in pg_opts else 0
                page_size = st.selectbox("Page Size", pg_opts, index=pg_idx, key="cfg_page_size")
            with c_tfs:
                title_font_size = st.number_input(
                    "Title Font Size (pt)",
                    min_value=12,
                    max_value=26,
                    value=int(current_config.get("title_font_size", DEFAULT_PDF_LAYOUT["title_font_size"])),
                    key="cfg_title_font_size",
                )
            with c_bfs:
                body_font_size = st.number_input(
                    "Body / Table Font Size (pt)",
                    min_value=7.0,
                    max_value=12.0,
                    step=0.5,
                    value=float(current_config.get("body_font_size", DEFAULT_PDF_LAYOUT["body_font_size"])),
                    key="cfg_body_font_size",
                )

            st.write("###### Page Margins (mm)")
            m_top, m_bot, m_left, m_right = st.columns(4)
            with m_top:
                margin_top_mm = st.number_input(
                    "Top (mm)",
                    min_value=8.0,
                    max_value=35.0,
                    step=1.0,
                    value=float(current_config.get("margin_top_mm", 15.0)),
                    key="cfg_margin_top",
                )
            with m_bot:
                margin_bottom_mm = st.number_input(
                    "Bottom (mm)",
                    min_value=8.0,
                    max_value=35.0,
                    step=1.0,
                    value=float(current_config.get("margin_bottom_mm", 15.0)),
                    key="cfg_margin_bot",
                )
            with m_left:
                margin_left_mm = st.number_input(
                    "Left (mm)",
                    min_value=8.0,
                    max_value=35.0,
                    step=1.0,
                    value=float(current_config.get("margin_left_mm", 15.0)),
                    key="cfg_margin_left",
                )
            with m_right:
                margin_right_mm = st.number_input(
                    "Right (mm)",
                    min_value=8.0,
                    max_value=35.0,
                    step=1.0,
                    value=float(current_config.get("margin_right_mm", 15.0)),
                    key="cfg_margin_right",
                )

            st.divider()

            st.write("##### 3. Metadata & Section Visibility Toggles")
            c_tog1, c_tog2 = st.columns(2)
            with c_tog1:
                show_letterhead_placeholder = st.checkbox(
                    "Show Letterhead / Logo Placeholder",
                    value=bool(current_config.get("show_letterhead_placeholder", True)),
                    key="cfg_show_letterhead_placeholder",
                )
                show_patient_uhid_mobile = st.checkbox(
                    "Show Patient UHID & Mobile Row",
                    value=bool(current_config.get("show_patient_uhid_mobile", True)),
                    key="cfg_show_patient_uhid_mobile",
                )
                show_attending_doctor = st.checkbox(
                    "Show Primary Doctor / Surgeon",
                    value=bool(current_config.get("show_attending_doctor", True)),
                    key="cfg_show_attending_doctor",
                )
                show_validity_date = st.checkbox(
                    "Show Estimate Validity Date",
                    value=bool(current_config.get("show_validity_date", True)),
                    key="cfg_show_validity_date",
                )
            with c_tog2:
                show_discount_row = st.checkbox(
                    "Show Discount Breakdown Line",
                    value=bool(current_config.get("show_discount_row", True)),
                    key="cfg_show_discount_row",
                )
                show_contingency_row = st.checkbox(
                    "Show Contingency Buffer Line",
                    value=bool(current_config.get("show_contingency_row", True)),
                    key="cfg_show_contingency_row",
                )
                show_range_band_row = st.checkbox(
                    "Show Range Band Box (+/- 10%)",
                    value=bool(current_config.get("show_range_band_row", True)),
                    key="cfg_show_range_band_row",
                )
                show_signature_block = st.checkbox(
                    "Show Signatories Block",
                    value=bool(current_config.get("show_signature_block", True)),
                    key="cfg_show_signature_block",
                )

            show_room_upgrade_comparison = st.checkbox(
                "Enable Room Tier Comparative Analysis Table in PDF",
                value=bool(current_config.get("show_room_upgrade_comparison", True)),
                key="cfg_show_room_upgrade_comparison",
            )

            st.divider()

            st.write("##### 4. Custom Table Column Headers")
            c_h1, c_h2, c_h3 = st.columns(3)
            with c_h1:
                col_header_sno = st.text_input(
                    "Column 1 (# / S.No)",
                    value=current_config.get("col_header_sno", "#"),
                    key="cfg_col_header_sno",
                )
            with c_h2:
                col_header_description = st.text_input(
                    "Column 2 (Particulars / Item)",
                    value=current_config.get("col_header_description", "Particulars / Tariff Category"),
                    key="cfg_col_header_desc",
                )
            with c_h3:
                col_header_category = st.text_input(
                    "Column 3 (Category)",
                    value=current_config.get("col_header_category", "Category"),
                    key="cfg_col_header_cat",
                )

            c_h4, c_h5 = st.columns(2)
            with c_h4:
                col_header_taxable = st.text_input(
                    "Column 4 (Taxable)",
                    value=current_config.get("col_header_taxable", "Taxable"),
                    key="cfg_col_header_tax",
                )
            with c_h5:
                col_header_amount = st.text_input(
                    "Column 5 (Amount)",
                    value=current_config.get("col_header_amount", "Amount (INR)"),
                    key="cfg_col_header_amt",
                )

            table_title_text = st.text_input(
                "Document Proposal Title",
                value=current_config.get("table_title_text", DEFAULT_PDF_LAYOUT["table_title_text"]),
                key="cfg_table_title_text",
            )

            st.divider()

            st.write("##### 5. Watermarks, Disclaimers & Signatories")
            draft_watermark_text = st.text_input(
                "Draft Watermark Text",
                value=current_config.get("draft_watermark_text", DEFAULT_PDF_LAYOUT["draft_watermark_text"]),
                key="cfg_draft_watermark",
            )
            disclaimer_heading = st.text_input(
                "Disclaimers Section Title",
                value=current_config.get("disclaimer_heading", DEFAULT_PDF_LAYOUT["disclaimer_heading"]),
                key="cfg_disclaimer_heading",
            )
            disclaimers_text = st.text_area(
                "Disclaimers & Legal Notice (One paragraph/item per line)",
                value=current_config.get("disclaimers_text", DEFAULT_PDF_LAYOUT["disclaimers_text"]),
                height=120,
                key="cfg_disclaimers_text",
            )

            c_sig1, c_sig2 = st.columns(2)
            with c_sig1:
                left_sig_title = st.text_input(
                    "Left Signatory Title",
                    value=current_config.get("left_signatory_title", DEFAULT_PDF_LAYOUT["left_signatory_title"]),
                    key="cfg_left_sig_title",
                )
                left_sig_dept = st.text_input(
                    "Left Signatory Subtitle",
                    value=current_config.get("left_signatory_dept", DEFAULT_PDF_LAYOUT["left_signatory_dept"]),
                    key="cfg_left_sig_dept",
                )
            with c_sig2:
                right_sig_title = st.text_input(
                    "Right Signatory Title",
                    value=current_config.get("right_signatory_title", DEFAULT_PDF_LAYOUT["right_signatory_title"]),
                    key="cfg_right_sig_title",
                )
                right_sig_dept = st.text_input(
                    "Right Signatory Subtitle",
                    value=current_config.get("right_signatory_dept", DEFAULT_PDF_LAYOUT["right_signatory_dept"]),
                    key="cfg_right_sig_dept",
                )

            submitted = st.form_submit_button(
                "Save PDF Layout Template",
                type="primary",
                width="stretch",
            )

            if submitted:
                new_layout = {
                    "hospital_name": h_name.strip() or DEFAULT_PDF_LAYOUT["hospital_name"],
                    "hospital_subtitle": h_subtitle.strip(),
                    "hospital_address": h_address.strip(),
                    "hospital_contact": h_contact.strip(),
                    "header_alignment": h_align,
                    "primary_color": primary_color,
                    "secondary_color": table_header_bg,
                    "table_header_bg": table_header_bg,
                    "table_header_text_color": "#FFFFFF",
                    "table_border_color": "#CBD5E0",
                    "table_row_even_bg": "#FFFFFF",
                    "table_row_odd_bg": "#F7FAFC",
                    "accent_color": "#EBF8FF",
                    "page_size": page_size,
                    "margin_top_mm": float(margin_top_mm),
                    "margin_bottom_mm": float(margin_bottom_mm),
                    "margin_left_mm": float(margin_left_mm),
                    "margin_right_mm": float(margin_right_mm),
                    "title_font_size": int(title_font_size),
                    "body_font_size": float(body_font_size),
                    "table_font_size": float(body_font_size),
                    "show_letterhead_placeholder": show_letterhead_placeholder,
                    "show_patient_uhid_mobile": show_patient_uhid_mobile,
                    "show_attending_doctor": show_attending_doctor,
                    "show_validity_date": show_validity_date,
                    "show_discount_row": show_discount_row,
                    "show_contingency_row": show_contingency_row,
                    "show_tax_row": True,
                    "show_range_band_row": show_range_band_row,
                    "show_room_upgrade_comparison": show_room_upgrade_comparison,
                    "show_signature_block": show_signature_block,
                    "col_header_sno": col_header_sno.strip() or "#",
                    "col_header_description": col_header_description.strip() or "Particulars",
                    "col_header_category": col_header_category.strip() or "Category",
                    "col_header_taxable": col_header_taxable.strip() or "Taxable",
                    "col_header_amount": col_header_amount.strip() or "Amount (INR)",
                    "table_title_text": table_title_text.strip() or DEFAULT_PDF_LAYOUT["table_title_text"],
                    "draft_watermark_text": draft_watermark_text.strip() or DEFAULT_PDF_LAYOUT["draft_watermark_text"],
                    "disclaimer_heading": disclaimer_heading.strip() or DEFAULT_PDF_LAYOUT["disclaimer_heading"],
                    "disclaimers_text": disclaimers_text.strip() or DEFAULT_PDF_LAYOUT["disclaimers_text"],
                    "left_signatory_title": left_sig_title.strip() or DEFAULT_PDF_LAYOUT["left_signatory_title"],
                    "left_signatory_dept": left_sig_dept.strip() or DEFAULT_PDF_LAYOUT["left_signatory_dept"],
                    "right_signatory_title": right_sig_title.strip() or DEFAULT_PDF_LAYOUT["right_signatory_title"],
                    "right_signatory_dept": right_sig_dept.strip() or DEFAULT_PDF_LAYOUT["right_signatory_dept"],
                }
                database.save_pdf_layout(conn, new_layout)
                st.success("PDF Layout template successfully updated.")
                st.rerun()

        if st.button("Reset to Factory Defaults", key="btn_reset_pdf_layout", width="stretch"):
            database.reset_pdf_layout(conn)
            st.success("PDF Layout template reset to factory defaults.")
            st.rerun()

    with col_preview:
        st.write("##### Live Sample PDF Preview")
        st.caption("Live rendering reflects the current saved layout parameters.")

        # Determine snapshot to render: active estimate or sample
        active_est = st.session_state.get("active_estimate")
        if active_est:
            sample_snapshot = active_est.to_snapshot_dict()
        else:
            sample_snapshot = create_sample_estimate_snapshot()

        try:
            sample_pdf_bytes = pdf_generator.generate_estimate_pdf(
                sample_snapshot,
                is_draft=False,
                layout_config=current_config,
            )

            st.download_button(
                label="Download Sample PDF",
                data=sample_pdf_bytes,
                file_name="Sample_Printed_Estimate.pdf",
                mime="application/pdf",
                width="stretch",
                key="btn_download_sample_pdf",
            )

            b64_pdf = base64.b64encode(sample_pdf_bytes).decode("utf-8")
            iframe_html = (
                f'<iframe src="data:application/pdf;base64,{b64_pdf}#toolbar=0" '
                f'width="100%" height="680px" '
                f'style="border: 1px solid #CBD5E0; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">'
                f'</iframe>'
            )
            st.markdown(iframe_html, unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Error rendering PDF preview: {e}")
