"""
app.py - Automated Hospital Estimate Billing Generator
Streamlit application featuring reactive cascading dropdowns, session state preservation,
immutable estimates, and PDF letterhead exports.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

import billing
import database
import pdf_generator
import seed_data


# Streamlit Page Config
st.set_page_config(
    page_title="Hospital Estimate Billing Generator",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_db():
    """Obtain or initialize SQLite database connection."""
    conn = database.get_connection("hospital.db")
    database.init_db(conn)
    seed_data.seed(conn)
    return conn


conn = get_db()


# Initialize Session State
if "active_estimate" not in st.session_state:
    st.session_state["active_estimate"] = None
if "saved_estimate_number" not in st.session_state:
    st.session_state["saved_estimate_number"] = None
if "inputs_dirty" not in st.session_state:
    st.session_state["inputs_dirty"] = False
if "last_proc_id" not in st.session_state:
    st.session_state["last_proc_id"] = None
if "admin_authenticated" not in st.session_state:
    st.session_state["admin_authenticated"] = False


def mark_inputs_changed():
    """Callback triggered whenever any generator input field is modified."""
    st.session_state["inputs_dirty"] = True
    st.session_state["saved_estimate_number"] = None


# Sidebar Navigation
st.sidebar.title("🏥 Hospital Billing")
st.sidebar.caption("Automated Tariff & Estimate Generator")
navigation_option = st.sidebar.radio(
    "Navigation Menu",
    ["🏥 Generate Estimate", "📋 Estimate History", "⚙️ Admin Portal"],
    index=0,
)
st.sidebar.divider()
st.sidebar.info(
    "**Currency:** Indian Rupees (₹)\n\n"
    "**Mode:** Concurrency-Safe SQLite WAL\n\n"
    "**Snapshot:** Immutable JSON Records"
)


# ==============================================================================
# VIEW 1: GENERATE ESTIMATE (Main UI)
# ==============================================================================
if navigation_option == "🏥 Generate Estimate":
    st.header("🏥 Patient Cost Estimation Generator")
    st.caption("Generate an itemized treatment estimate with professional doctor fees, OT, consumables, and hospital tariffs.")

    settings = database.get_settings(conn)
    hospital_name = settings.get("hospital_name", "Sample Multispeciality Hospital")
    contingency_rate = settings.get("contingency_rate", "10")
    tax_rate = settings.get("tax_rate", "0")
    range_pct = settings.get("estimate_range_pct", "10")

    # Master Data Lookups (Active records only)
    all_rooms = database.get_room_types(conn, active_only=True)
    all_departments = database.get_departments(conn, active_only=True)

    # Top Row: Patient Name & Room Category
    col_p1, col_p2 = st.columns([2, 2])
    with col_p1:
        patient_name = st.text_input(
            "Patient Full Name *",
            placeholder="e.g. Rahul Sharma",
            key="input_patient_name",
            on_change=mark_inputs_changed,
        )
    with col_p2:
        room_options = {r["id"]: r for r in all_rooms}
        selected_room_id = st.selectbox(
            "Room Category *",
            options=list(room_options.keys()),
            format_func=lambda rid: f"{room_options[rid]['name']} — ₹{room_options[rid]['daily_rate']:,}/day",
            key="input_room_id",
            on_change=mark_inputs_changed,
        )
        selected_room = room_options.get(selected_room_id)

    # Cascading Dropdowns: Department -> Doctor & Procedure (OUTSIDE st.form)
    st.subheader("Clinical Consultation & Procedure")
    col_c1, col_c2, col_c3 = st.columns([1.5, 1.5, 2])

    with col_c1:
        dept_options = {d["id"]: d for d in all_departments}
        selected_dept_id = st.selectbox(
            "Department *",
            options=list(dept_options.keys()),
            format_func=lambda did: dept_options[did]["name"],
            key="input_dept_id",
            on_change=mark_inputs_changed,
        )
        selected_dept = dept_options.get(selected_dept_id)

    # Filter Doctors & Procedures by Selected Department
    dept_doctors = database.get_doctors(conn, department_id=selected_dept_id, active_only=True)
    dept_procedures = database.get_procedures(conn, department_id=selected_dept_id, active_only=True)

    with col_c2:
        if not dept_doctors:
            st.warning("⚠️ No active doctors in this department.")
            selected_doctor = None
        else:
            doc_options = {doc["id"]: doc for doc in dept_doctors}
            selected_doc_id = st.selectbox(
                "Attending Doctor / Specialist *",
                options=list(doc_options.keys()),
                format_func=lambda did: f"{doc_options[did]['name']} ({doc_options[did]['specialization']})",
                key="input_doc_id",
                on_change=mark_inputs_changed,
            )
            selected_doctor = doc_options.get(selected_doc_id)

    with col_c3:
        if not dept_procedures:
            st.warning("⚠️ No active procedures in this department.")
            selected_procedure = None
        else:
            proc_options = {p["id"]: p for p in dept_procedures}
            selected_proc_id = st.selectbox(
                "Surgical / Medical Procedure *",
                options=list(proc_options.keys()),
                format_func=lambda pid: f"{proc_options[pid]['name']} (Base: ₹{proc_options[pid]['base_cost']:,})",
                key="input_proc_id",
                on_change=mark_inputs_changed,
            )
            selected_procedure = proc_options.get(selected_proc_id)

    # Update Length of Stay default if procedure changed
    if selected_procedure:
        proc_current_id = selected_procedure["id"]
        if st.session_state["last_proc_id"] != proc_current_id:
            st.session_state["input_stay_days"] = selected_procedure["default_stay_days"]
            st.session_state["last_proc_id"] = proc_current_id
            st.session_state["inputs_dirty"] = True

    # Adjustments & Overrides Row
    col_o1, col_o2, col_o3 = st.columns([1.2, 1.2, 2.5])
    with col_o1:
        stay_days = st.number_input(
            "Length of Stay (Days) *",
            min_value=0,
            max_value=365,
            value=st.session_state.get("input_stay_days", 1),
            help="Default stay for this procedure. Enter 0 for Day Care / OPD surgery.",
            key="input_stay_days",
            on_change=mark_inputs_changed,
        )
    with col_o2:
        discount_pct = st.number_input(
            "Special Discount (%)",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=1.0,
            help="Proportionally allocated across all line items.",
            key="input_discount_pct",
            on_change=mark_inputs_changed,
        )
    with col_o3:
        remarks = st.text_input(
            "Remarks / Specific Clinical Notes",
            placeholder="e.g. High-risk cardiac history, robotic assistance",
            key="input_remarks",
            on_change=mark_inputs_changed,
        )

    st.markdown("<br/>", unsafe_allow_html=True)
    col_act1, col_act2 = st.columns([1.5, 4])
    with col_act1:
        generate_clicked = st.button("⚡ Generate Estimate", type="primary", use_container_width=True)

    # Handle Generation Action
    if generate_clicked:
        # Validations
        valid_name, err_name = billing.validate_patient_name(patient_name)
        if not valid_name:
            st.error(f"❌ {err_name}")
        elif not selected_doctor or not selected_procedure:
            st.error("❌ Please select a valid doctor and procedure for this department.")
        else:
            est_result = billing.calculate_estimate(
                patient_name=patient_name,
                stay_days=stay_days,
                room_type=selected_room,
                doctor=selected_doctor,
                procedure=selected_procedure,
                department=selected_dept,
                discount_pct=discount_pct,
                settings=settings,
                remarks=remarks,
            )
            st.session_state["active_estimate"] = est_result
            st.session_state["saved_estimate_number"] = None
            st.session_state["inputs_dirty"] = False

    # Render Estimate if available in session_state
    active_est = st.session_state.get("active_estimate")
    if active_est:
        st.divider()

        # Warning banner if inputs modified after generation
        if st.session_state.get("inputs_dirty"):
            st.warning("⚠️ **Notice:** You have modified input fields since this estimate was generated. Click **'Generate Estimate'** to recalculate before saving or downloading.")

        saved_num = st.session_state.get("saved_estimate_number")
        if saved_num:
            st.success(f"✅ **Estimate Saved to Database:** Record Number **{saved_num}**")

        st.subheader("📋 Itemized Estimate Breakdown")

        # Metric Tiles
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Gross Subtotal", billing.format_inr(active_est.subtotal))
        m2.metric(
            f"Discount ({active_est.discount_pct}%)",
            f"-{billing.format_inr(active_est.discount_amount)}",
        )
        m3.metric(
            f"Contingency ({active_est.contingency_pct}%)",
            billing.format_inr(active_est.contingency_amount),
        )
        m4.metric(
            f"Tax ({active_est.tax_pct}%)",
            billing.format_inr(active_est.tax_amount),
        )
        m5.metric(
            "ESTIMATED TOTAL",
            billing.format_inr(active_est.total_amount),
        )

        # Expected Range Banner
        st.info(
            f"**Expected Cost Range (±{active_est.estimate_range_pct}%):** "
            f"**{billing.format_inr(active_est.min_estimate)}** — **{billing.format_inr(active_est.max_estimate)}** "
            f"*(accounts for potential variations in surgical supplies, length of stay, or medical management)*"
        )

        # Table of Line Items
        table_records = []
        for item in active_est.line_items:
            table_records.append(
                {
                    "Category": item.category,
                    "Description": item.description,
                    "Quantity": f"{item.quantity:.0f}" if item.quantity == int(item.quantity) else str(item.quantity),
                    "Unit Rate": billing.format_inr(item.unit_price),
                    "Gross Amount": billing.format_inr(item.amount),
                    "Discount": f"-{billing.format_inr(item.discount_amount)}" if item.discount_amount > Decimal("0") else "—",
                    "Net Amount": billing.format_inr(item.net_amount),
                    "Taxable": "Yes" if item.is_taxable else "No",
                }
            )

        df = pd.DataFrame(table_records)
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.markdown("<br/>", unsafe_allow_html=True)

        # Action Buttons (Save & Download PDF)
        btn_col1, btn_col2, btn_col3 = st.columns([1.5, 1.8, 3])

        with btn_col1:
            save_disabled = bool(st.session_state.get("inputs_dirty") or st.session_state.get("saved_estimate_number"))
            save_btn_label = "💾 Save Estimate" if not st.session_state.get("saved_estimate_number") else "✅ Saved"
            if st.button(save_btn_label, disabled=save_disabled, type="primary", use_container_width=True):
                # Generate unique atomic estimate number inside BEGIN IMMEDIATE
                est_number = database.get_next_estimate_number(conn)
                active_est.estimate_number = est_number
                snapshot = active_est.to_snapshot_dict()

                # Store in database
                save_data = {
                    "estimate_number": est_number,
                    "patient_name": active_est.patient_name,
                    "room_type_id": selected_room["id"] if selected_room else None,
                    "department_id": selected_dept["id"] if selected_dept else None,
                    "doctor_id": selected_doctor["id"] if selected_doctor else None,
                    "procedure_id": selected_procedure["id"] if selected_procedure else None,
                    "stay_days": active_est.stay_days,
                    "discount_pct": str(active_est.discount_pct),
                    "subtotal": str(active_est.subtotal),
                    "discount_amount": str(active_est.discount_amount),
                    "contingency_amount": str(active_est.contingency_amount),
                    "tax_amount": str(active_est.tax_amount),
                    "total_amount": str(active_est.total_amount),
                    "min_estimate": str(active_est.min_estimate),
                    "max_estimate": str(active_est.max_estimate),
                    "line_items_json": snapshot,
                    "remarks": active_est.remarks,
                }
                database.save_estimate(conn, save_data)
                st.session_state["saved_estimate_number"] = est_number
                st.rerun()

        with btn_col2:
            is_draft = st.session_state.get("saved_estimate_number") is None
            pdf_bytes = pdf_generator.generate_estimate_pdf(active_est, is_draft=is_draft)
            file_label = f"Hospital_Estimate_{st.session_state.get('saved_estimate_number') or 'DRAFT'}.pdf"
            st.download_button(
                label="📄 Download PDF Letterhead",
                data=pdf_bytes,
                file_name=file_label,
                mime="application/pdf",
                disabled=bool(st.session_state.get("inputs_dirty")),
                use_container_width=True,
            )

        with btn_col3:
            if is_draft:
                st.caption("ℹ️ *PDF downloaded now will be marked **DRAFT - NOT SAVED**. Save the estimate to generate a permanent estimate number.*")
            else:
                st.caption(f"ℹ️ *Estimate saved with permanent number **{st.session_state['saved_estimate_number']}**.*")


# ==============================================================================
# VIEW 2: ESTIMATE HISTORY & REPRINT
# ==============================================================================
elif navigation_option == "📋 Estimate History":
    st.header("📋 Estimate Records & Historical Reprint")
    st.caption("Search past estimates, inspect itemized line items, and reprint PDF estimates directly from frozen snapshots.")

    # Search & Filter Controls
    col_s1, col_s2 = st.columns([3, 2])
    with col_s1:
        search_query = st.text_input(
            "🔍 Search by Patient Name or Estimate #",
            placeholder="e.g. Sharma or EST-2026...",
            key="history_search_input",
        )
    with col_s2:
        all_depts = database.get_departments(conn, active_only=False)
        dept_filter_opts = {0: "All Departments"} | {d["id"]: d["name"] for d in all_depts}
        selected_dept_filter = st.selectbox(
            "Filter by Department",
            options=list(dept_filter_opts.keys()),
            format_func=lambda k: dept_filter_opts[k],
            key="history_dept_filter",
        )

    dept_filter_id = selected_dept_filter if selected_dept_filter != 0 else None
    estimates = database.get_estimates(
        conn,
        search_query=search_query,
        department_id=dept_filter_id,
        limit=100,
    )

    if not estimates:
        st.info("No estimates found matching your search criteria.")
    else:
        st.write(f"Showing **{len(estimates)}** estimate records:")

        # Summary Table
        summary_rows = []
        for e in estimates:
            summary_rows.append(
                {
                    "Estimate Number": e["estimate_number"],
                    "Date": e["created_at"][:10] if e.get("created_at") else "N/A",
                    "Patient Name": e["patient_name"],
                    "Department": e.get("department_name") or "—",
                    "Procedure": e.get("procedure_name") or "—",
                    "Doctor": e.get("doctor_name") or "—",
                    "Room Category": e.get("room_type_name") or "—",
                    "Total Amount": billing.format_inr(e["total_amount"]),
                }
            )
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("🔍 Inspect & Reprint Historical Estimate")

        # Selectbox to pick an estimate to view in detail
        est_map = {e["estimate_number"]: e for e in estimates}
        selected_est_num = st.selectbox(
            "Select Estimate Record to View Details / Reprint",
            options=list(est_map.keys()),
            key="history_selected_est",
        )

        if selected_est_num:
            sel_record = est_map[selected_est_num]
            try:
                snapshot = (
                    json.loads(sel_record["line_items_json"])
                    if isinstance(sel_record["line_items_json"], str)
                    else sel_record["line_items_json"]
                )
            except Exception:
                snapshot = {}

            meta = snapshot.get("metadata", {})
            patient_info = snapshot.get("patient", {})
            clinical_info = snapshot.get("clinical", {})
            fin_info = snapshot.get("financials", {})
            stored_items = snapshot.get("line_items", [])

            # Alert box highlighting immutability
            st.info(
                f"🔒 **Immutable Historical Record:** Estimate **{selected_est_num}** generated on **{meta.get('created_at', sel_record['created_at'])}**.<br/>"
                f"*This estimate is rendered from a frozen snapshot. Future changes to master room, procedure, or doctor prices do NOT alter this estimate.*",
                icon="ℹ️",
            )

            # Metadata display
            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(f"**Patient:** {patient_info.get('name', sel_record['patient_name'])}")
            c2.markdown(f"**Department:** {clinical_info.get('department', sel_record.get('department_name'))}")
            c3.markdown(f"**Doctor:** {clinical_info.get('doctor', sel_record.get('doctor_name'))}")
            c4.markdown(f"**Procedure:** {clinical_info.get('procedure', sel_record.get('procedure_name'))}")

            # Metric tiles from frozen snapshot
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Gross Subtotal", billing.format_inr(fin_info.get("subtotal", sel_record["subtotal"])))
            disc_pct_str = fin_info.get("discount_pct", sel_record["discount_pct"])
            disc_amt_str = fin_info.get("discount_amount", sel_record["discount_amount"])
            m2.metric(f"Discount ({disc_pct_str}%)", f"-{billing.format_inr(disc_amt_str)}")
            m3.metric(
                f"Contingency ({fin_info.get('contingency_pct', '10')}%)",
                billing.format_inr(fin_info.get("contingency_amount", sel_record["contingency_amount"])),
            )
            m4.metric(
                f"Tax ({fin_info.get('tax_pct', '0')}%)",
                billing.format_inr(fin_info.get("tax_amount", sel_record["tax_amount"])),
            )
            m5.metric("TOTAL AMOUNT", billing.format_inr(fin_info.get("total_amount", sel_record["total_amount"])))

            # Range band
            min_e = billing.format_inr(fin_info.get("min_estimate", sel_record["min_estimate"]))
            max_e = billing.format_inr(fin_info.get("max_estimate", sel_record["max_estimate"]))
            rng_p = fin_info.get("estimate_range_pct", "10")
            st.caption(f"**Estimated Cost Range (±{rng_p}%):** {min_e} — {max_e}")

            # Line items table
            if stored_items:
                st.write("**Itemized Line Items (Frozen Tariff Snapshot):**")
                table_data = []
                for item in stored_items:
                    table_data.append(
                        {
                            "Category": item.get("category", ""),
                            "Description": item.get("description", ""),
                            "Quantity": item.get("quantity", "1"),
                            "Unit Rate": billing.format_inr(item.get("unit_price", 0)),
                            "Gross Amount": billing.format_inr(item.get("amount", 0)),
                            "Discount": f"-{billing.format_inr(item.get('discount_amount', 0))}"
                            if Decimal(str(item.get("discount_amount", 0))) > Decimal(0)
                            else "—",
                            "Net Amount": billing.format_inr(item.get("net_amount", 0)),
                            "Taxable": "Yes" if item.get("is_taxable") else "No",
                        }
                    )
                st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)

            # Reprint PDF Button
            col_pdf, _ = st.columns([2, 4])
            with col_pdf:
                # Generate PDF strictly from stored snapshot
                reprint_pdf_bytes = pdf_generator.generate_estimate_pdf(snapshot, is_draft=False)
                st.download_button(
                    label=f"📄 Reprint PDF ({selected_est_num})",
                    data=reprint_pdf_bytes,
                    file_name=f"Estimate_{selected_est_num}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    key="btn_reprint_pdf",
                )


# ==============================================================================
# VIEW 3: ADMIN PORTAL (Placeholder for Phase 6)
# ==============================================================================
elif navigation_option == "⚙️ Admin Portal":
    st.header("⚙️ Administration & Tariff Management")
    st.info("Admin Portal will be enabled in Phase 6 with PBKDF2 authentication, soft delete management, CSV import/export, and DB backup.")
