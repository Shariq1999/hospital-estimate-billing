"""
app.py - Automated Hospital Estimate Billing Generator
Streamlit application featuring reactive cascading dropdowns, session state preservation,
per-rerun connection management, immutable estimates, multi-room comparative billing,
integrated inline breakdown editing, and PDF exports.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional

import pandas as pd
import streamlit as st

import importlib
import billing
import database
import pdf_generator
import seed_data

# Ensure modules stay synchronized in persistent server workers (e.g. Streamlit Cloud)
try:
    if "surgeon_fee_type" not in getattr(billing.calculate_estimate, "__code__", object()).co_varnames:
        billing = importlib.reload(billing)
    if "surgeon_fee_type" not in getattr(database.save_doctor, "__code__", object()).co_varnames:
        database = importlib.reload(database)
    if "comparative_estimates" not in getattr(billing.EstimateResult, "__annotations__", {}):
        billing = importlib.reload(billing)
except Exception:
    pass


def format_history_timestamp(ts: Optional[str]) -> str:
    """Format ISO timestamp as DD-MMM-YYYY HH:MM for human-readable display."""
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d-%b-%Y %H:%M")
    except Exception:
        return str(ts)


# Streamlit Page Configuration (Clean typography, no emoji)
st.set_page_config(
    page_title="Hospital Estimate Billing Generator",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = os.environ.get("HOSPITAL_DB_PATH", "hospital.db")


@st.cache_resource
def ensure_db_initialized(path: str = DB_PATH):
    """Initialize database schema, seed data, and default admin credentials once (cached)."""
    init_conn = database.get_connection(path)
    try:
        database.init_db(init_conn)
        seed_data.seed(init_conn)
        if "HOSPITAL_DB_PATH" not in os.environ:
            database.ensure_default_admin(init_conn)
    finally:
        init_conn.close()


ensure_db_initialized(DB_PATH)


# Open a fresh SQLite connection per rerun
conn = database.get_connection(DB_PATH)

try:
    # Initialize Session State
    if "active_estimate" not in st.session_state:
        st.session_state["active_estimate"] = None
    if "saved_estimate_number" not in st.session_state:
        st.session_state["saved_estimate_number"] = None
    if "inputs_dirty" not in st.session_state:
        st.session_state["inputs_dirty"] = False
    if "last_proc_id" not in st.session_state:
        st.session_state["last_proc_id"] = None
    if "input_stay_days" not in st.session_state:
        st.session_state["input_stay_days"] = 1
    if "admin_authenticated" not in st.session_state:
        st.session_state["admin_authenticated"] = False
    if "inline_editing" not in st.session_state:
        st.session_state["inline_editing"] = False

    def mark_inputs_changed():
        """Callback triggered whenever any generator input field is modified."""
        st.session_state["inputs_dirty"] = True
        st.session_state["saved_estimate_number"] = None

    # Sidebar Navigation (Clean labels, no emojis)
    st.sidebar.title("Hospital Billing")
    st.sidebar.caption("Automated Tariff & Estimate Generator")

    nav_options = [
        "Generate Estimate",
        "Estimate History",
        "Dashboard",
        "Admin Portal",
    ]
    if "nav_option" in st.session_state and st.session_state["nav_option"] in nav_options:
        default_idx = nav_options.index(st.session_state.pop("nav_option"))
    else:
        default_idx = 0

    navigation_option = st.sidebar.radio(
        "Navigation Menu",
        nav_options,
        index=default_idx,
    )

    if st.session_state.get("admin_authenticated"):
        st.sidebar.markdown(f"**Staff User:** `{st.session_state.get('admin_username', 'admin')}`")
        if st.sidebar.button("Log Out", key="sidebar_logout_btn", width="stretch"):
            st.session_state["admin_authenticated"] = False
            st.session_state["admin_username"] = None
            st.session_state.pop("db_backup_bytes", None)
            st.session_state.pop("db_backup_prepared_at", None)
            st.session_state.pop("revision_source", None)
            st.session_state["inline_editing"] = False
            st.rerun()

    st.sidebar.divider()
    st.sidebar.info(
        "**Currency:** Indian Rupees (₹)\n\n"
        "**Mode:** Concurrency-Safe SQLite WAL\n\n"
        "**Snapshot:** Immutable JSON Records"
    )

    # ==========================================================================
    # VIEW 1: GENERATE ESTIMATE (Main UI)
    # ==========================================================================
    if navigation_option == "Generate Estimate":
        st.header("Patient Cost Estimation Generator")
        st.caption("Generate an itemized treatment estimate with professional doctor fees, OT, consumables, and hospital tariffs.")

        # Authentication Gate for Estimate Generation
        if not st.session_state.get("admin_authenticated"):
            admin_configured = database.is_admin_configured(conn)
            if not admin_configured:
                st.info("**Welcome to First-Run Setup:** Please configure the administrator account in the **Admin Portal** menu to begin.")
            else:
                st.subheader("Staff Authentication")
                st.caption("Please log in with your credentials to generate or revise patient cost estimates.")
                with st.form("generator_login_form"):
                    gate_user = st.text_input("Username", key="gate_user")
                    gate_pwd = st.text_input("Password", type="password", key="gate_pwd")
                    gate_submitted = st.form_submit_button("Log In & Continue", type="primary", width="stretch")
                    if gate_submitted:
                        if database.verify_admin_credentials(conn, gate_user.strip(), gate_pwd):
                            st.session_state["admin_authenticated"] = True
                            st.session_state["admin_username"] = gate_user.strip()
                            st.success("Authenticated successfully.")
                            st.rerun()
                        else:
                            import time
                            time.sleep(1)
                            st.error("Invalid username or password.")
        else:
            settings = database.get_settings(conn)
            hospital_name = settings.get("hospital_name", "Sample Multispeciality Hospital")

            # Revision Banner
            rev_source = st.session_state.get("revision_source")
            if rev_source:
                col_rev_info, col_rev_btn = st.columns([4, 1.2])
                with col_rev_info:
                    orig_num = rev_source["estimate_number"]
                    next_rev = database.get_next_revision_number(conn, orig_num)
                    st.warning(
                        f"**Revision Mode Active:** Modifying Estimate **{orig_num}** -> will be saved as revision **{next_rev}** (original remains preserved in history)."
                    )
                with col_rev_btn:
                    if st.button("Cancel Revision", key="cancel_rev_btn", width="stretch"):
                        st.session_state["revision_source"] = None
                        st.session_state["active_estimate"] = None
                        st.session_state["saved_estimate_number"] = None
                        st.session_state["inline_editing"] = False
                        st.rerun()

            # Master Data Lookups (Active records only)
            all_rooms = database.get_room_types(conn, active_only=True)
            all_departments = database.get_departments(conn, active_only=True)

            if not all_rooms:
                st.error("No active room categories configured. Please configure room categories in the Admin Portal.")
            if not all_departments:
                st.error("No active departments configured. Please configure departments in the Admin Portal.")

            can_generate = bool(all_rooms and all_departments)

            if can_generate:
                # Top Row: Patient Name, UHID, Mobile & Room Category
                col_p1, col_p2, col_p3, col_p4 = st.columns([1.8, 1.1, 1.2, 1.6])
                with col_p1:
                    patient_name = st.text_input(
                        "Patient Full Name *",
                        placeholder="e.g. Rahul Sharma",
                        key="input_patient_name",
                        on_change=mark_inputs_changed,
                    )
                with col_p2:
                    uhid = st.text_input(
                        "UHID No.",
                        placeholder="e.g. UH-001234",
                        key="input_uhid",
                        on_change=mark_inputs_changed,
                    )
                with col_p3:
                    mobile_number = st.text_input(
                        "Mobile No.",
                        placeholder="e.g. 9876543210 (India)",
                        key="input_mobile",
                        on_change=mark_inputs_changed,
                    )
                with col_p4:
                    room_options = {r["id"]: r for r in all_rooms}
                    room_keys = list(room_options.keys())
                    selected_room_id = st.selectbox(
                        "Primary Room Category *",
                        options=room_keys,
                        format_func=lambda rid: f"{room_options[rid]['name']} - {billing.format_inr(room_options[rid]['daily_rate'], whole_rupees=True)}/day",
                        key="input_room_id",
                        on_change=mark_inputs_changed,
                    )
                    selected_room = room_options.get(selected_room_id)

                # Room Comparison Controls (Provision for one or more rooms)
                compare_rooms = st.checkbox(
                    "Compare with other room categories",
                    key="chk_compare_rooms",
                    on_change=mark_inputs_changed,
                )
                selected_comp_rooms = []
                if compare_rooms:
                    comp_candidates = [r for r in all_rooms if r["id"] != selected_room_id]
                    selected_comp_ids = st.multiselect(
                        "Select Room Categories for Comparative Setting (one or more):",
                        options=[r["id"] for r in comp_candidates],
                        format_func=lambda rid: f"{room_options[rid]['name']} - {billing.format_inr(room_options[rid]['daily_rate'], whole_rupees=True)}/day",
                        key="input_comp_room_ids",
                        on_change=mark_inputs_changed,
                    )
                    selected_comp_rooms = [room_options[rid] for rid in selected_comp_ids if rid in room_options]

                # Cascading Dropdowns: Department -> Doctor & Procedure
                st.subheader("Clinical Consultation & Procedure")
                col_c1, col_c2, col_c3 = st.columns([1.5, 1.5, 2])

                with col_c1:
                    dept_options = {d["id"]: d for d in all_departments}
                    dept_keys = list(dept_options.keys())
                    selected_dept_id = st.selectbox(
                        "Department *",
                        options=dept_keys,
                        format_func=lambda did: dept_options[did]["name"],
                        key="input_dept_id",
                        on_change=mark_inputs_changed,
                    )
                    selected_dept = dept_options.get(selected_dept_id)

                dept_doctors = database.get_doctors(conn, department_id=selected_dept_id, active_only=True)
                dept_procedures = database.get_procedures(conn, department_id=selected_dept_id, active_only=True)

                with col_c2:
                    if not dept_doctors:
                        st.warning("No active doctors configured in this department.")
                        selected_doctor = None
                    else:
                        doc_options = {doc["id"]: doc for doc in dept_doctors}
                        selected_doc_id = st.selectbox(
                            "Attending Doctor / Specialist *",
                            options=list(doc_options.keys()),
                            format_func=lambda did: f"{doc_options[did]['name']} ({doc_options[did]['specialization']} - Fixed: {billing.format_inr(doc_options[did].get('surgeon_fee_amount') or (doc_options[did]['consultation_fee'] * 25), whole_rupees=True)})",
                            key="input_doc_id",
                            on_change=mark_inputs_changed,
                        )
                        selected_doctor = doc_options.get(selected_doc_id)

                with col_c3:
                    if not dept_procedures:
                        st.warning("No active procedures configured in this department.")
                        selected_procedure = None
                    else:
                        proc_options = {p["id"]: p for p in dept_procedures}
                        selected_proc_id = st.selectbox(
                            "Surgical / Medical Procedure *",
                            options=list(proc_options.keys()),
                            format_func=lambda pid: f"{proc_options[pid]['name']} (Base: {billing.format_inr(proc_options[pid]['base_cost'], whole_rupees=True)})",
                            key="input_proc_id",
                            on_change=mark_inputs_changed,
                        )
                        selected_procedure = proc_options.get(selected_proc_id)

                # Surgeon Fee Mode Switch (Fixed Amount vs Percentage)
                col_sf1, col_sf2 = st.columns([1.5, 1.5])
                with col_sf1:
                    surgeon_fee_mode = st.radio(
                        "Surgeon Fee Calculation Mode",
                        options=["Fixed Amount", "Percentage of Base Tariff"],
                        horizontal=True,
                        key="input_surgeon_fee_mode",
                        on_change=mark_inputs_changed,
                    )
                with col_sf2:
                    if surgeon_fee_mode == "Fixed Amount":
                        default_surg_amt = 0
                        if selected_doctor:
                            default_surg_amt = int(selected_doctor.get("surgeon_fee_amount") or 0)
                            if default_surg_amt == 0:
                                default_surg_amt = int(selected_doctor.get("consultation_fee", 0) * 25)
                        surgeon_fee_val = st.number_input(
                            "Surgeon Professional Fee (INR) *",
                            min_value=0,
                            value=default_surg_amt,
                            step=1000,
                            key="input_surgeon_fee_val",
                            on_change=mark_inputs_changed,
                        )
                    else:
                        default_pct = int(selected_doctor.get("surgeon_fee_pct", 40)) if selected_doctor else 40
                        surgeon_pct_val = st.number_input(
                            "Surgeon Fee Percentage (%) *",
                            min_value=0,
                            max_value=100,
                            value=default_pct,
                            step=5,
                            key="input_surgeon_pct_val",
                            on_change=mark_inputs_changed,
                        )
                        surgeon_fee_val = None

                # Update Length of Stay default if procedure changed
                if selected_procedure:
                    proc_current_id = selected_procedure["id"]
                    if st.session_state["last_proc_id"] != proc_current_id:
                        if not st.session_state.get("revision_source"):
                            st.session_state["input_stay_days"] = selected_procedure["default_stay_days"]
                        st.session_state["last_proc_id"] = proc_current_id
                        st.session_state["inputs_dirty"] = True

                # Adjustments & Remarks Row
                col_o1, col_o2, col_o3 = st.columns([1.2, 1.2, 2.5])
                with col_o1:
                    stay_days = st.number_input(
                        "Length of Stay (Days) *",
                        min_value=0,
                        max_value=365,
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

                # Advanced Price Overrides Expander (Edit Specific Prices)
                with st.expander("Price & Tariff Overrides (Edit Specific Prices)", expanded=bool(st.session_state.get("revision_source"))):
                    st.caption("Override catalog tariffs for this estimate. Leave blank to use standard master rates.")
                    ov1, ov2, ov3, ov4 = st.columns(4)
                    with ov1:
                        ov_room_rate = st.text_input(
                            "Room Daily Rate (₹)",
                            placeholder=str(selected_room["daily_rate"]) if selected_room else "",
                            key="ov_room_rate",
                            on_change=mark_inputs_changed,
                        )
                    with ov2:
                        ov_base_cost = st.text_input(
                            "Procedure Base Cost (₹)",
                            placeholder=str(selected_procedure["base_cost"]) if selected_procedure else "",
                            key="ov_base_cost",
                            on_change=mark_inputs_changed,
                        )
                    with ov3:
                        ov_surg_fee_label = "Surgeon Fee Amount (₹)" if surgeon_fee_mode == "Fixed Amount" else "Surgeon Fee (%)"
                        ov_surg_holder = str(surgeon_fee_val) if surgeon_fee_mode == "Fixed Amount" else str(surgeon_pct_val)
                        ov_surgeon = st.text_input(
                            ov_surg_fee_label,
                            placeholder=ov_surg_holder,
                            key="ov_surgeon",
                            on_change=mark_inputs_changed,
                        )
                    with ov4:
                        ov_ot = st.text_input(
                            "OT Charge (₹)",
                            placeholder=str(selected_procedure["ot_charge"]) if selected_procedure else "",
                            key="ov_ot",
                            on_change=mark_inputs_changed,
                        )

                    ov5, ov6, ov7 = st.columns(3)
                    with ov5:
                        ov_anaes = st.text_input(
                            "Anaesthesia Charge (₹)",
                            placeholder=str(selected_procedure["anaesthesia_charge"]) if selected_procedure else "",
                            key="ov_anaes",
                            on_change=mark_inputs_changed,
                        )
                    with ov6:
                        ov_cons = st.text_input(
                            "Consumables & Implants (₹)",
                            placeholder=str(selected_procedure["consumables_cost"]) if selected_procedure else "",
                            key="ov_cons",
                            on_change=mark_inputs_changed,
                        )
                    with ov7:
                        ov_inv = st.text_input(
                            "Investigations Charge (₹)",
                            placeholder=str(selected_procedure["investigations_charge"]) if selected_procedure else "",
                            key="ov_inv",
                            on_change=mark_inputs_changed,
                        )

                st.markdown("<br/>", unsafe_allow_html=True)
                col_act1, col_act2 = st.columns([1.5, 4])
                with col_act1:
                    generate_clicked = st.button("Generate Estimate", type="primary", width="stretch")

                # Handle Generation Action
                if generate_clicked:
                    valid_name, err_name = billing.validate_patient_name(patient_name)
                    valid_mobile, err_mobile = billing.validate_mobile_number(mobile_number)
                    if not valid_name:
                        st.error(err_name)
                    elif not valid_mobile:
                        st.error(err_mobile)
                    elif not selected_doctor or not selected_procedure:
                        st.error("Please select a valid doctor and procedure for this department.")
                    else:
                        calc_room = dict(selected_room)
                        if ov_room_rate.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_room_rate, "Room Daily Rate")
                            if v:
                                calc_room["daily_rate"] = dec
                            else:
                                st.error(e)
                                calc_room = None

                        calc_proc = dict(selected_procedure)
                        if calc_room and ov_base_cost.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_base_cost, "Base Cost")
                            if v:
                                calc_proc["base_cost"] = dec
                            else:
                                st.error(e)
                                calc_proc = None
                        if calc_proc and ov_ot.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_ot, "OT Charge")
                            if v:
                                calc_proc["ot_charge"] = dec
                            else:
                                st.error(e)
                                calc_proc = None
                        if calc_proc and ov_anaes.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_anaes, "Anaesthesia Charge")
                            if v:
                                calc_proc["anaesthesia_charge"] = dec
                            else:
                                st.error(e)
                                calc_proc = None
                        if calc_proc and ov_cons.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_cons, "Consumables Cost")
                            if v:
                                calc_proc["consumables_cost"] = dec
                            else:
                                st.error(e)
                                calc_proc = None
                        if calc_proc and ov_inv.strip():
                            v, dec, e = billing.validate_whole_rupee_field(ov_inv, "Investigations Charge")
                            if v:
                                calc_proc["investigations_charge"] = dec
                            else:
                                st.error(e)
                                calc_proc = None

                        calc_doc = dict(selected_doctor)
                        calc_surg_amt = surgeon_fee_val
                        if surgeon_fee_mode == "Percentage of Base Tariff":
                            calc_surg_pct = surgeon_pct_val
                            if ov_surgeon.strip():
                                v, dec, e = billing.validate_whole_percentage_field(ov_surgeon, "Surgeon Fee (%)")
                                if v:
                                    calc_doc["surgeon_fee_pct"] = dec
                                else:
                                    st.error(e)
                                    calc_doc = None
                        else:
                            if ov_surgeon.strip():
                                v, dec, e = billing.validate_whole_rupee_field(ov_surgeon, "Surgeon Fee Amount")
                                if v:
                                    calc_surg_amt = dec
                                else:
                                    st.error(e)
                                    calc_doc = None

                        if calc_room and calc_proc and calc_doc:
                            est_result = billing.calculate_estimate(
                                patient_name=patient_name,
                                stay_days=stay_days,
                                room_type=calc_room,
                                doctor=calc_doc,
                                procedure=calc_proc,
                                department=selected_dept,
                                discount_pct=discount_pct,
                                settings=settings,
                                remarks=remarks,
                                surgeon_fee_type="fixed" if surgeon_fee_mode == "Fixed Amount" else "percentage",
                                surgeon_fee_amount=calc_surg_amt if surgeon_fee_mode == "Fixed Amount" else None,
                                comparative_rooms=selected_comp_rooms if compare_rooms else None,
                            )
                            est_result.mobile_number = mobile_number.strip()
                            est_result.uhid = uhid.strip()
                            st.session_state["active_estimate"] = est_result
                            st.session_state["saved_estimate_number"] = None
                            st.session_state["inputs_dirty"] = False
                            st.session_state["inline_editing"] = False

                active_est = st.session_state.get("active_estimate")
                if active_est:
                    # Backward compatibility for estimates in session state prior to code reload
                    if not hasattr(active_est, "comparative_estimates"):
                        active_est.comparative_estimates = []
                    if not hasattr(active_est, "surgeon_fee_type"):
                        active_est.surgeon_fee_type = "fixed"
                    if not hasattr(active_est, "surgeon_fee_amount"):
                        active_est.surgeon_fee_amount = Decimal("0.00")
                    if not hasattr(active_est, "mobile_number"):
                        active_est.mobile_number = ""
                    if not hasattr(active_est, "uhid"):
                        active_est.uhid = ""

                    st.divider()

                    if st.session_state.get("inputs_dirty"):
                        st.warning("**Notice:** Input fields have been modified since this estimate was generated. Click **'Generate Estimate'** to recalculate before saving or downloading.")

                    saved_num = st.session_state.get("saved_estimate_number")
                    if saved_num:
                        st.success(f"**Estimate Saved to Database:** Record Number **{saved_num}**")

                    # Header with integrated Edit button (Task 4)
                    col_brk_title, col_brk_btn = st.columns([3.8, 1.2])
                    with col_brk_title:
                        st.subheader("Itemized Estimate Breakdown")
                    with col_brk_btn:
                        edit_label = "Close Editor" if st.session_state.get("inline_editing") else "Edit Estimate"
                        if st.button(edit_label, key="btn_toggle_inline_edit", width="stretch"):
                            st.session_state["inline_editing"] = not st.session_state.get("inline_editing", False)
                            st.rerun()

                    # Integrated Inline Editor
                    if st.session_state.get("inline_editing"):
                        with st.container(border=True):
                            st.markdown("##### Inline Estimate Editor")
                            st.caption("Directly adjust tariffs, line items, doctor fees, stay days, or clinical remarks. Click 'Apply Changes' to update the estimate instantly.")
                            ed_c1, ed_c2, ed_c3 = st.columns([1.2, 1.2, 2.5])
                            with ed_c1:
                                edit_stay = st.number_input("Length of Stay (Days)", min_value=0, max_value=365, value=int(active_est.stay_days), key="edit_stay_days")
                            with ed_c2:
                                edit_disc = st.number_input("Discount (%)", min_value=0.0, max_value=100.0, value=float(active_est.discount_pct), step=1.0, key="edit_disc_pct")
                            with ed_c3:
                                edit_remarks = st.text_input("Clinical Remarks", value=active_est.remarks, key="edit_remarks_val")

                            ed_t1, ed_t2, ed_t3, ed_t4 = st.columns(4)
                            with ed_t1:
                                edit_room_rate = st.number_input("Room Daily Rate (INR)", min_value=0, value=int(active_est.room_daily_rate), step=500, key="edit_room_rate_val")
                            with ed_t2:
                                proc_item = next((it for it in active_est.line_items if it.category == "Hospital Procedure Tariff"), None)
                                proc_val = int(proc_item.amount) if proc_item else 0
                                edit_proc_rate = st.number_input("Procedure Base (INR)", min_value=0, value=proc_val, step=1000, key="edit_proc_rate_val")
                            with ed_t3:
                                surg_item = next((it for it in active_est.line_items if it.category == "Surgeon Professional Fee"), None)
                                surg_val = int(surg_item.amount) if surg_item else 0
                                edit_surg_fee = st.number_input("Surgeon Fee (INR)", min_value=0, value=surg_val, step=1000, key="edit_surg_fee_val")
                            with ed_t4:
                                ot_item = next((it for it in active_est.line_items if it.category == "Operation Theatre Charges"), None)
                                ot_val = int(ot_item.amount) if ot_item else 0
                                edit_ot_rate = st.number_input("OT Charges (INR)", min_value=0, value=ot_val, step=1000, key="edit_ot_rate_val")

                            ed_t5, ed_t6, ed_t7 = st.columns(3)
                            with ed_t5:
                                anaes_item = next((it for it in active_est.line_items if it.category == "Anaesthesia Charges"), None)
                                anaes_val = int(anaes_item.amount) if anaes_item else 0
                                edit_anaes_rate = st.number_input("Anaesthesia (INR)", min_value=0, value=anaes_val, step=1000, key="edit_anaes_rate_val")
                            with ed_t6:
                                cons_item = next((it for it in active_est.line_items if it.category == "Surgical Consumables & Implants"), None)
                                cons_val = int(cons_item.amount) if cons_item else 0
                                edit_cons_rate = st.number_input("Consumables (INR)", min_value=0, value=cons_val, step=1000, key="edit_cons_rate_val")
                            with ed_t7:
                                inv_item = next((it for it in active_est.line_items if it.category == "Investigations & Diagnostics"), None)
                                inv_val = int(inv_item.amount) if inv_item else 0
                                edit_inv_rate = st.number_input("Investigations (INR)", min_value=0, value=inv_val, step=1000, key="edit_inv_rate_val")

                            btn_apply, btn_close = st.columns([1.5, 1.5])
                            with btn_apply:
                                if st.button("Apply Changes", type="primary", key="btn_apply_inline_edits", width="stretch"):
                                    temp_room = dict(selected_room) if selected_room else {"name": active_est.room_type_name, "daily_rate": edit_room_rate}
                                    temp_room["daily_rate"] = edit_room_rate
                                    temp_proc = dict(selected_procedure) if selected_procedure else {
                                        "name": active_est.procedure_name,
                                        "base_cost": edit_proc_rate,
                                        "ot_charge": edit_ot_rate,
                                        "anaesthesia_charge": edit_anaes_rate,
                                        "consumables_cost": edit_cons_rate,
                                        "investigations_charge": edit_inv_rate,
                                    }
                                    temp_proc["base_cost"] = edit_proc_rate
                                    temp_proc["ot_charge"] = edit_ot_rate
                                    temp_proc["anaesthesia_charge"] = edit_anaes_rate
                                    temp_proc["consumables_cost"] = edit_cons_rate
                                    temp_proc["investigations_charge"] = edit_inv_rate
                                    temp_doc = dict(selected_doctor) if selected_doctor else {"name": active_est.doctor_name, "surgeon_fee_pct": 0, "consultation_fee": edit_surg_fee}

                                    recalculated = billing.calculate_estimate(
                                        patient_name=active_est.patient_name,
                                        stay_days=edit_stay,
                                        room_type=temp_room,
                                        doctor=temp_doc,
                                        procedure=temp_proc,
                                        department=selected_dept if selected_dept else {"name": active_est.department_name},
                                        discount_pct=edit_disc,
                                        settings=settings,
                                        remarks=edit_remarks,
                                        surgeon_fee_type="fixed",
                                        surgeon_fee_amount=edit_surg_fee,
                                        comparative_rooms=selected_comp_rooms if compare_rooms else None,
                                    )
                                    recalculated.mobile_number = getattr(active_est, "mobile_number", "")
                                    recalculated.uhid = getattr(active_est, "uhid", "")
                                    st.session_state["active_estimate"] = recalculated
                                    st.session_state["saved_estimate_number"] = None
                                    st.session_state["inline_editing"] = False
                                    st.rerun()
                            with btn_close:
                                if st.button("Cancel", key="btn_cancel_inline_edits", width="stretch"):
                                    st.session_state["inline_editing"] = False
                                    st.rerun()

                    # Metric Summary Cards
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

                    st.info(
                        f"**Expected Cost Range (+/-{active_est.estimate_range_pct}%):** "
                        f"**{billing.format_inr(active_est.min_estimate)}** to **{billing.format_inr(active_est.max_estimate)}**\n\n"
                        "*(Accounts for potential variations in surgical supplies, length of stay, or medical management)*"
                    )

                    comp_estimates = getattr(active_est, "comparative_estimates", [])
                    if comp_estimates:
                        st.write("##### Primary and Comparative Room Categories Analysis")
                        rows_comp = []
                        for idx, item in enumerate(active_est.line_items):
                            r = {
                                "Category": item.category,
                                "Description": item.description,
                                f"{active_est.room_type_name} (Primary)": billing.format_inr(item.amount),
                            }
                            for c in comp_estimates:
                                c_name = c.get("clinical", {}).get("room_type", "Comparative")
                                c_items = c.get("line_items", [])
                                if idx < len(c_items):
                                    c_amt = billing.format_inr(c_items[idx].get("amount", 0))
                                else:
                                    c_amt = billing.format_inr(item.amount)
                                r[c_name] = c_amt
                            rows_comp.append(r)

                        # Summary rows
                        r_sub = {"Category": "SUBTOTAL", "Description": "Gross Itemized Charges", f"{active_est.room_type_name} (Primary)": billing.format_inr(active_est.subtotal)}
                        for c in comp_estimates:
                            r_sub[c.get("clinical", {}).get("room_type", "Comparative")] = billing.format_inr(c.get("financials", {}).get("subtotal", 0))
                        rows_comp.append(r_sub)

                        if active_est.discount_pct > Decimal("0"):
                            r_disc = {"Category": "DISCOUNT", "Description": f"{active_est.discount_pct}% special discount", f"{active_est.room_type_name} (Primary)": f"-{billing.format_inr(active_est.discount_amount)}"}
                            for c in comp_estimates:
                                r_disc[c.get("clinical", {}).get("room_type", "Comparative")] = f"-{billing.format_inr(c.get('financials', {}).get('discount_amount', 0))}"
                            rows_comp.append(r_disc)

                        r_cont = {"Category": "CONTINGENCY BUFFER", "Description": f"{active_est.contingency_pct}% non-taxed buffer", f"{active_est.room_type_name} (Primary)": billing.format_inr(active_est.contingency_amount)}
                        for c in comp_estimates:
                            r_cont[c.get("clinical", {}).get("room_type", "Comparative")] = billing.format_inr(c.get("financials", {}).get("contingency_amount", 0))
                        rows_comp.append(r_cont)

                        if active_est.tax_pct > Decimal("0"):
                            r_tax = {"Category": "GST / TAX", "Description": f"{active_est.tax_pct}% tax", f"{active_est.room_type_name} (Primary)": billing.format_inr(active_est.tax_amount)}
                            for c in comp_estimates:
                                r_tax[c.get("clinical", {}).get("room_type", "Comparative")] = billing.format_inr(c.get("financials", {}).get("tax_amount", 0))
                            rows_comp.append(r_tax)

                        r_tot = {"Category": "TOTAL ESTIMATE", "Description": "Total Estimated Cost", f"{active_est.room_type_name} (Primary)": billing.format_inr(active_est.total_amount)}
                        for c in comp_estimates:
                            r_tot[c.get("clinical", {}).get("room_type", "Comparative")] = billing.format_inr(c.get("financials", {}).get("total_amount", 0))
                        rows_comp.append(r_tot)

                        st.dataframe(pd.DataFrame(rows_comp), width="stretch", hide_index=True)
                    else:
                        table_records = []
                        for item in active_est.line_items:
                            table_records.append(
                                {
                                    "Category": item.category,
                                    "Description": item.description,
                                    "Quantity": f"{item.quantity:.0f}" if item.quantity == int(item.quantity) else str(item.quantity),
                                    "Unit Rate": billing.format_inr(item.unit_price),
                                    "Gross Amount": billing.format_inr(item.amount),
                                    "Discount": f"-{billing.format_inr(item.discount_amount)}" if item.discount_amount > Decimal("0") else "-",
                                    "Net Amount": billing.format_inr(item.net_amount),
                                    "Taxable": "Yes" if item.is_taxable else "No",
                                }
                            )
                        st.dataframe(pd.DataFrame(table_records), width="stretch", hide_index=True)

                    st.markdown("<br/>", unsafe_allow_html=True)

                    # Action Buttons (Save & Download PDF)
                    btn_col1, btn_col2, btn_col3 = st.columns([1.5, 1.8, 3])

                    with btn_col1:
                        save_disabled = bool(st.session_state.get("inputs_dirty") or st.session_state.get("saved_estimate_number"))
                        save_btn_label = "Save Estimate" if not st.session_state.get("saved_estimate_number") else "Saved"
                        if st.button(save_btn_label, disabled=save_disabled, type="primary", width="stretch"):
                            rev_source = st.session_state.get("revision_source")
                            if rev_source:
                                est_number = database.get_next_revision_number(conn, rev_source["estimate_number"])
                                revision_of = rev_source.get("revision_of") or rev_source["estimate_number"]
                            else:
                                est_number = database.get_next_estimate_number(conn)
                                revision_of = None

                            active_est.estimate_number = est_number
                            active_est.created_at = datetime.now().isoformat()
                            snapshot = active_est.to_snapshot_dict()

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
                                "created_at": active_est.created_at,
                                "mobile_number": getattr(active_est, "mobile_number", ""),
                                "uhid": getattr(active_est, "uhid", ""),
                                "revision_of": revision_of,
                            }
                            database.save_estimate(conn, save_data)
                            st.session_state["saved_estimate_number"] = est_number
                            st.session_state["revision_source"] = None
                            st.rerun()

                    with btn_col2:
                        is_draft = st.session_state.get("saved_estimate_number") is None
                        pdf_bytes = pdf_generator.generate_estimate_pdf(active_est, is_draft=is_draft, layout_config=database.get_pdf_layout(conn))

                        safe_name = re.sub(r'[^\w\-]', '_', active_est.patient_name.strip())[:30] or "Patient"
                        safe_uhid = re.sub(r'[^\w\-]', '_', (getattr(active_est, "uhid", "") or "").strip() or "NoUHID")[:15]
                        est_num_label = st.session_state.get('saved_estimate_number') or 'DRAFT'
                        file_label = f"{safe_name}_{safe_uhid}_{est_num_label}.pdf"

                        st.download_button(
                            label="Download PDF Letterhead",
                            data=pdf_bytes,
                            file_name=file_label,
                            mime="application/pdf",
                            disabled=bool(st.session_state.get("inputs_dirty")),
                            width="stretch",
                        )

                    with btn_col3:
                        if is_draft:
                            st.caption("*PDF downloaded now will be marked **DRAFT - NOT SAVED**. Save the estimate to assign a permanent estimate number.*")
                        else:
                            st.caption(f"*Estimate saved with permanent number **{st.session_state['saved_estimate_number']}**.*")

    # ==========================================================================
    # VIEW 2: ESTIMATE HISTORY & REPRINT
    # ==========================================================================
    elif navigation_option == "Estimate History":
        st.header("Estimate Records & Historical Reprint")
        st.caption("Search past estimates, update conversion outcomes, revise records, and reprint frozen PDF snapshots.")

        # Search & Filter Controls
        col_s1, col_s2 = st.columns([3, 2])
        with col_s1:
            search_query = st.text_input(
                "Search by Patient Name, Estimate #, UHID, or Mobile",
                placeholder="e.g. Sharma, UH-0012, or 98765...",
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

            summary_rows = []
            for e in estimates:
                summary_rows.append(
                    {
                        "Estimate Number": e["estimate_number"],
                        "Revision Of": e.get("revision_of") or "-",
                        "Date": format_history_timestamp(e.get("created_at")),
                        "Patient Name": e["patient_name"],
                        "UHID": e.get("uhid") or "-",
                        "Mobile": e.get("mobile_number") or "-",
                        "Department": e.get("department_name") or "-",
                        "Procedure": e.get("procedure_name") or "-",
                        "Doctor": e.get("doctor_name") or "-",
                        "Room Category": e.get("room_type_name") or "-",
                        "Total Amount": billing.format_inr(e["total_amount"]),
                        "Converted": "Yes" if e.get("converted") == 1 else "No",
                        "Actual Bill": billing.format_inr(e["actual_amount"]) if e.get("actual_amount") else "-",
                    }
                )
            st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

            st.divider()
            st.subheader("Inspect, Revise & Update Historical Estimate")

            est_map = {e["estimate_number"]: e for e in estimates}
            selected_est_num = st.selectbox(
                "Select Estimate Record to Inspect Details / Update Outcome",
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

                est_created_dt = format_history_timestamp(meta.get("created_at") or sel_record.get("created_at"))
                st.info(
                    f"**Immutable Historical Record:** Estimate **{selected_est_num}** generated on **{est_created_dt}**.\n\n"
                    "*Rendered from a frozen snapshot. Future changes to master room, procedure, or doctor prices do NOT alter this estimate.*"
                )

                # Metadata display
                c1, c2, c3, c4 = st.columns(4)
                c1.markdown(f"**Patient:** {patient_info.get('name', sel_record['patient_name'])}")
                c2.markdown(f"**UHID:** {patient_info.get('uhid') or sel_record.get('uhid') or '-'}")
                c3.markdown(f"**Mobile:** {patient_info.get('mobile_number') or sel_record.get('mobile_number') or '-'}")
                c4.markdown(f"**Department:** {clinical_info.get('department', sel_record.get('department_name'))}")

                c5, c6, c7, c8 = st.columns(4)
                c5.markdown(f"**Doctor:** {clinical_info.get('doctor', sel_record.get('doctor_name'))}")
                c6.markdown(f"**Procedure:** {clinical_info.get('procedure', sel_record.get('procedure_name'))}")
                c7.markdown(f"**Room:** {clinical_info.get('room_type', sel_record.get('room_type_name'))}")
                c8.markdown(f"**Revision Of:** {sel_record.get('revision_of') or 'None (Original)'}")

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

                min_e = billing.format_inr(fin_info.get("min_estimate", sel_record["min_estimate"]))
                max_e = billing.format_inr(fin_info.get("max_estimate", sel_record["max_estimate"]))
                rng_p = fin_info.get("estimate_range_pct", "10")
                st.caption(f"**Estimated Cost Range (+/-{rng_p}%):** {min_e} to {max_e}")

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
                                else "-",
                                "Net Amount": billing.format_inr(item.get("net_amount", 0)),
                                "Taxable": "Yes" if item.get("is_taxable") else "No",
                            }
                        )
                    st.dataframe(pd.DataFrame(table_data), width="stretch", hide_index=True)

                # Update Treatment Outcome & Final Bill Amount
                with st.expander("Update Treatment Outcome & Final Bill Amount", expanded=True):
                    col_out1, col_out2, col_out3 = st.columns([1.5, 2, 1.5])
                    with col_out1:
                        is_conv = st.checkbox(
                            "Patient Accepted Treatment (Converted)",
                            value=bool(sel_record.get("converted", 0)),
                            key=f"conv_toggle_{selected_est_num}",
                        )
                    with col_out2:
                        act_bill = st.text_input(
                            "Final Actual Bill Amount (₹)",
                            value=str(sel_record.get("actual_amount") or ""),
                            placeholder="e.g. 150000",
                            key=f"act_bill_{selected_est_num}",
                        )
                    with col_out3:
                        st.markdown("<br/>", unsafe_allow_html=True)
                        if st.button("Save Outcome", key=f"save_outcome_{selected_est_num}", width="stretch"):
                            if act_bill.strip():
                                v_act, d_act, err_act = billing.validate_money_field(act_bill, "Actual Bill Amount")
                                if not v_act:
                                    st.error(err_act)
                                else:
                                    database.update_estimate_outcome(conn, selected_est_num, is_conv, str(d_act))
                                    st.success("Treatment outcome & actual bill updated.")
                                    st.rerun()
                            else:
                                database.update_estimate_outcome(conn, selected_est_num, is_conv, None)
                                st.success("Treatment outcome updated.")
                                st.rerun()

                    if sel_record.get("actual_amount"):
                        acc = billing.calculate_accuracy(sel_record["total_amount"], sel_record["actual_amount"])
                        if acc is not None:
                            st.info(f"**Accuracy Variance:** **{acc:.2f}%** variance (|Actual - Estimated| / Actual * 100%) against estimated {billing.format_inr(sel_record['total_amount'])}.")

                # Action Buttons: Reprint PDF & Edit/Revise
                col_pdf, col_rev = st.columns([2, 2])
                with col_pdf:
                    reprint_pdf_bytes = pdf_generator.generate_estimate_pdf(snapshot, is_draft=False)
                    p_name = snapshot.get("patient", {}).get("name") or sel_record["patient_name"]
                    p_uhid = snapshot.get("patient", {}).get("uhid") or sel_record.get("uhid") or "NoUHID"
                    safe_name = re.sub(r'[^\w\-]', '_', str(p_name).strip())[:30] or "Patient"
                    safe_uhid = re.sub(r'[^\w\-]', '_', str(p_uhid).strip())[:15]
                    reprint_fname = f"{safe_name}_{safe_uhid}_{selected_est_num}.pdf"
                    st.download_button(
                        label=f"Reprint PDF ({selected_est_num})",
                        data=reprint_pdf_bytes,
                        file_name=reprint_fname,
                        mime="application/pdf",
                        width="stretch",
                        key="btn_reprint_pdf",
                    )
                with col_rev:
                    if st.button("Revise / Edit Estimate", width="stretch", key="btn_revise_est", type="secondary"):
                        st.session_state["revision_source"] = sel_record
                        st.session_state["input_patient_name"] = sel_record["patient_name"]
                        st.session_state["input_uhid"] = sel_record.get("uhid") or ""
                        st.session_state["input_mobile"] = sel_record.get("mobile_number") or ""
                        st.session_state["input_room_id"] = sel_record["room_type_id"]
                        st.session_state["input_dept_id"] = sel_record["department_id"]
                        st.session_state["input_doc_id"] = sel_record["doctor_id"]
                        st.session_state["input_proc_id"] = sel_record["procedure_id"]
                        st.session_state["input_stay_days"] = sel_record["stay_days"]
                        st.session_state["input_discount_pct"] = float(sel_record["discount_pct"])
                        st.session_state["input_remarks"] = sel_record.get("remarks") or ""
                        st.session_state["active_estimate"] = None
                        st.session_state["saved_estimate_number"] = None
                        st.session_state["inputs_dirty"] = True
                        st.session_state["inline_editing"] = True
                        st.session_state["nav_option"] = "Generate Estimate"
                        st.rerun()

    # ==========================================================================
    # VIEW 3: DASHBOARD & EXCEL LEDGER
    # ==========================================================================
    elif navigation_option == "Dashboard":
        import analytics_dashboard
        analytics_dashboard.render_enhanced_analytics_dashboard(conn)

    # ==========================================================================
    # VIEW 4: ADMIN PORTAL
    # ==========================================================================
    elif navigation_option == "Admin Portal":
        st.header("Administration & Tariff Management")
        st.caption("Secure master data management, system settings, CSV import/export, and database backups.")

        # Auth Gate: First-Run Setup vs Login
        admin_configured = database.is_admin_configured(conn)

        if not admin_configured:
            # 1. FIRST-RUN SETUP
            st.info("**Welcome to First-Run Setup:** No administrator account exists yet. Create the primary admin account to get started.")
            with st.form("admin_setup_form"):
                setup_user = st.text_input("Administrator Username", value="admin")
                setup_pwd = st.text_input("Password (min 8 characters)", type="password")
                setup_pwd_confirm = st.text_input("Confirm Password", type="password")
                setup_submitted = st.form_submit_button("Initialize Admin Account", type="primary", width="stretch")

                if setup_submitted:
                    if not setup_user.strip():
                        st.error("Username cannot be blank.")
                    elif len(setup_pwd) < 8:
                        st.error("Password must be at least 8 characters long.")
                    elif setup_pwd != setup_pwd_confirm:
                        st.error("Passwords do not match.")
                    else:
                        database.set_admin_password(conn, setup_user.strip(), setup_pwd)
                        st.session_state["admin_authenticated"] = True
                        st.session_state["admin_username"] = setup_user.strip()
                        st.success("Admin account created successfully! Logged in.")
                        st.rerun()

        elif not st.session_state.get("admin_authenticated", False):
            # 2. LOGIN FORM WITH ATTEMPT DELAY
            st.subheader("Administrator Login")
            with st.form("admin_login_form"):
                login_user = st.text_input("Username")
                login_pwd = st.text_input("Password", type="password")
                login_submitted = st.form_submit_button("Log In", type="primary", width="stretch")

                if login_submitted:
                    if database.verify_admin_credentials(conn, login_user.strip(), login_pwd):
                        st.session_state["admin_authenticated"] = True
                        st.session_state["admin_username"] = login_user.strip()
                        st.success("Logged in successfully.")
                        st.rerun()
                    else:
                        import time
                        time.sleep(1)
                        st.error("Invalid username or password.")

        else:
            # 3. AUTHENTICATED ADMIN CONSOLE
            top_c1, top_c2 = st.columns([4, 1])
            with top_c1:
                st.write(f"Logged in as **{st.session_state.get('admin_username', 'admin')}**")
            with top_c2:
                if st.button("Log Out", width="stretch"):
                    st.session_state["admin_authenticated"] = False
                    st.session_state["admin_username"] = None
                    st.session_state.pop("db_backup_bytes", None)
                    st.session_state.pop("db_backup_prepared_at", None)
                    st.session_state["inline_editing"] = False
                    st.rerun()

            if "admin_flash_message" in st.session_state:
                st.success(st.session_state.pop("admin_flash_message"))

            st.divider()

            admin_tabs = st.tabs([
                "Rooms",
                "Departments",
                "Doctors",
                "Procedures",
                "Settings",
                "CSV Import / Export",
                "Change Password",
                "Database Backup",
                "PDF Print Layout",
            ])

            # ------------------------------------------------------------------
            # TAB 1: ROOM TYPES
            # ------------------------------------------------------------------
            with admin_tabs[0]:
                st.subheader("Room Categories Tariff Management")
                rooms = database.get_room_types(conn, active_only=False)

                room_rows = []
                for r in rooms:
                    room_rows.append({
                        "ID": r["id"],
                        "Name": r["name"],
                        "Daily Rate": billing.format_inr(r["daily_rate"]),
                        "Description": r["description"] or "",
                        "Status": "Active" if r["is_active"] else "Inactive",
                    })
                st.dataframe(pd.DataFrame(room_rows), width="stretch", hide_index=True)

                col_r_add, col_r_edit = st.columns(2)
                with col_r_add:
                    st.write("##### Add New Room Category")
                    with st.form("add_room_form"):
                        new_r_name = st.text_input("Room Category Name *")
                        new_r_rate = st.text_input("Daily Rate (₹) *", placeholder="e.g. 5500")
                        new_r_desc = st.text_input("Description", placeholder="e.g. Single room, attendant bed")
                        add_r_sub = st.form_submit_button("Add Room Category", width="stretch")
                        if add_r_sub:
                            if not new_r_name.strip():
                                st.error("Room category name cannot be blank.")
                            else:
                                valid, rate_dec, err = billing.validate_whole_rupee_field(new_r_rate, "Daily Rate", allow_zero=True)
                                if not valid:
                                    st.error(err)
                                else:
                                    try:
                                        database.save_room_type(conn, new_r_name.strip(), int(rate_dec), new_r_desc.strip())
                                        st.session_state["admin_flash_message"] = f"Room category '{new_r_name.strip()}' added successfully."
                                        st.rerun()
                                    except sqlite3.IntegrityError as e:
                                        st.error(f"Cannot add room category: {e}")

                with col_r_edit:
                    st.write("##### Edit / Soft-Delete Room Category")
                    if rooms:
                        r_edit_map = {r["id"]: r for r in rooms}
                        sel_r_id = st.selectbox(
                            "Select Room Category to Modify",
                            options=list(r_edit_map.keys()),
                            format_func=lambda x: f"{r_edit_map[x]['name']} ({'Active' if r_edit_map[x]['is_active'] else 'Inactive'})",
                            key="sel_room_edit",
                        )
                        target_r = r_edit_map[sel_r_id]
                        with st.form("edit_room_form"):
                            edit_r_name = st.text_input("Room Category Name", value=target_r["name"], key=f"edit_r_name_{sel_r_id}")
                            edit_r_rate = st.text_input("Daily Rate (₹)", value=str(target_r["daily_rate"]), key=f"edit_r_rate_{sel_r_id}")
                            edit_r_desc = st.text_input("Description", value=target_r["description"] or "", key=f"edit_r_desc_{sel_r_id}")
                            edit_r_sub = st.form_submit_button("Save Changes", width="stretch")
                            if edit_r_sub:
                                if not edit_r_name.strip():
                                    st.error("Room category name cannot be blank.")
                                else:
                                    valid, rate_dec, err = billing.validate_whole_rupee_field(edit_r_rate, "Daily Rate", allow_zero=True)
                                    if not valid:
                                        st.error(err)
                                    else:
                                        try:
                                            database.save_room_type(conn, edit_r_name.strip(), int(rate_dec), edit_r_desc.strip(), room_id=sel_r_id)
                                            st.session_state["admin_flash_message"] = f"Room category '{edit_r_name.strip()}' updated successfully."
                                            st.rerun()
                                        except sqlite3.IntegrityError as e:
                                            st.error(f"Cannot update room category: {e}")

                        # Soft Delete / Restore Action (NO HARD DELETE)
                        if target_r["is_active"]:
                            if st.button("Deactivate (Soft Delete)", key="btn_deactivate_room", width="stretch"):
                                database.toggle_room_type_status(conn, sel_r_id, 0)
                                st.warning(f"Room category '{target_r['name']}' deactivated.")
                                st.rerun()
                        else:
                            if st.button("Reactivate (Restore)", key="btn_reactivate_room", width="stretch"):
                                database.toggle_room_type_status(conn, sel_r_id, 1)
                                st.success(f"Room category '{target_r['name']}' restored to active.")
                                st.rerun()

            # ------------------------------------------------------------------
            # TAB 2: DEPARTMENTS
            # ------------------------------------------------------------------
            with admin_tabs[1]:
                st.subheader("Department Management")
                depts = database.get_departments(conn, active_only=False)

                dept_rows = [{"ID": d["id"], "Department Name": d["name"], "Status": "Active" if d["is_active"] else "Inactive"} for d in depts]
                st.dataframe(pd.DataFrame(dept_rows), width="stretch", hide_index=True)

                col_d_add, col_d_edit = st.columns(2)
                with col_d_add:
                    st.write("##### Add New Department")
                    with st.form("add_dept_form"):
                        new_d_name = st.text_input("Department Name *")
                        add_d_sub = st.form_submit_button("Add Department", width="stretch")
                        if add_d_sub:
                            if not new_d_name.strip():
                                st.error("Department name cannot be blank.")
                            else:
                                try:
                                    database.save_department(conn, new_d_name.strip())
                                    st.session_state["admin_flash_message"] = f"Department '{new_d_name.strip()}' created."
                                    st.rerun()
                                except sqlite3.IntegrityError as e:
                                    st.error(f"Cannot create department: {e}")

                with col_d_edit:
                    st.write("##### Edit / Soft-Delete Department")
                    if depts:
                        d_edit_map = {d["id"]: d for d in depts}
                        sel_d_id = st.selectbox(
                            "Select Department",
                            options=list(d_edit_map.keys()),
                            format_func=lambda x: f"{d_edit_map[x]['name']} ({'Active' if d_edit_map[x]['is_active'] else 'Inactive'})",
                            key="sel_dept_edit",
                        )
                        target_d = d_edit_map[sel_d_id]
                        with st.form("edit_dept_form"):
                            edit_d_name = st.text_input("Department Name", value=target_d["name"], key=f"edit_d_name_{sel_d_id}")
                            edit_d_sub = st.form_submit_button("Rename Department", width="stretch")
                            if edit_d_sub:
                                if not edit_d_name.strip():
                                    st.error("Department name cannot be blank.")
                                else:
                                    try:
                                        database.save_department(conn, edit_d_name.strip(), dept_id=sel_d_id)
                                        st.session_state["admin_flash_message"] = "Department renamed."
                                        st.rerun()
                                    except sqlite3.IntegrityError as e:
                                        st.error(f"Cannot rename department: {e}")

                        if target_d["is_active"]:
                            if st.button("Deactivate Department", key="btn_deact_dept", width="stretch"):
                                database.toggle_department_status(conn, sel_d_id, 0)
                                st.warning(f"Department '{target_d['name']}' deactivated.")
                                st.rerun()
                        else:
                            if st.button("Reactivate Department", key="btn_react_dept", width="stretch"):
                                database.toggle_department_status(conn, sel_d_id, 1)
                                st.success(f"Department '{target_d['name']}' restored.")
                                st.rerun()

            # ------------------------------------------------------------------
            # TAB 3: DOCTORS
            # ------------------------------------------------------------------
            with admin_tabs[2]:
                st.subheader("Consulting Doctors & Surgeons")
                docs = database.get_doctors(conn, active_only=False)
                active_depts = database.get_departments(conn, active_only=True)
                all_depts_map = {d["id"]: d["name"] for d in database.get_departments(conn, active_only=False)}

                doc_rows = []
                for d in docs:
                    s_type = d.get("surgeon_fee_type", "fixed")
                    s_amt = billing.format_inr(d.get("surgeon_fee_amount", 0))
                    doc_rows.append({
                        "ID": d["id"],
                        "Doctor Name": d["name"],
                        "Department": d["department_name"],
                        "Consultation Fee": billing.format_inr(d["consultation_fee"]),
                        "Surgeon Fee Mode": "Fixed Amount" if s_type == "fixed" else "Percentage",
                        "Surgeon Fee Amount": s_amt,
                        "Surgeon Fee %": f"{d['surgeon_fee_pct']}%",
                        "Specialization": d["specialization"] or "",
                        "Status": "Active" if d["is_active"] else "Inactive",
                    })
                st.dataframe(pd.DataFrame(doc_rows), width="stretch", hide_index=True)

                col_doc_add, col_doc_edit = st.columns(2)
                with col_doc_add:
                    st.write("##### Add New Doctor")
                    if not active_depts:
                        st.warning("Please activate at least one department first.")
                    else:
                        with st.form("add_doctor_form"):
                            new_doc_name = st.text_input("Doctor Full Name *")
                            new_doc_dept_id = st.selectbox(
                                "Department *",
                                options=[d["id"] for d in active_depts],
                                format_func=lambda x: all_depts_map.get(x, ""),
                                key="add_doc_dept",
                            )
                            new_doc_fee = st.text_input("Consultation Fee (INR) *", placeholder="e.g. 1500")
                            new_doc_pct = st.number_input("Surgeon Fee (%) *", min_value=0, max_value=100, value=40, step=5, key="add_doc_surg_pct")
                            new_doc_spec = st.text_input("Specialization", placeholder="e.g. Cardiothoracic Surgeon")
                            new_doc_fee_mode = st.radio("Surgeon Fee Mode", options=["Fixed Amount", "Percentage of Base Tariff"], horizontal=True, key="add_doc_fee_mode")
                            new_doc_surg_amt = st.number_input("Surgeon Fee Amount (INR)", min_value=0, value=35000, step=1000, key="add_doc_surg_amt")
                            add_doc_sub = st.form_submit_button("Add Doctor", width="stretch")
                            if add_doc_sub:
                                if not new_doc_name.strip():
                                    st.error("Doctor name cannot be blank.")
                                else:
                                    v_fee, fee_dec, e_fee = billing.validate_whole_rupee_field(new_doc_fee, "Consultation Fee", allow_zero=True)
                                    v_pct, pct_val, e_pct = billing.validate_whole_percentage_field(new_doc_pct, "Surgeon Fee (%)")
                                    if not (v_fee and v_pct):
                                        errs = [e for e in (e_fee, e_pct) if e]
                                        st.error("; ".join(errs))
                                    else:
                                        try:
                                            s_type = "fixed" if new_doc_fee_mode == "Fixed Amount" else "percentage"
                                            database.save_doctor(
                                                conn,
                                                name=new_doc_name.strip(),
                                                department_id=new_doc_dept_id,
                                                consultation_fee=fee_dec,
                                                surgeon_fee_pct=pct_val,
                                                specialization=new_doc_spec.strip(),
                                                surgeon_fee_type=s_type,
                                                surgeon_fee_amount=int(new_doc_surg_amt),
                                            )
                                            st.session_state["admin_flash_message"] = f"Doctor '{new_doc_name.strip()}' added successfully."
                                            st.rerun()
                                        except sqlite3.IntegrityError as e:
                                            st.error(f"Cannot add doctor: {e}")

                with col_doc_edit:
                    st.write("##### Edit / Soft-Delete Doctor")
                    if docs:
                        doc_map = {d["id"]: d for d in docs}
                        sel_doc_id = st.selectbox(
                            "Select Doctor",
                            options=list(doc_map.keys()),
                            format_func=lambda x: f"{doc_map[x]['name']} ({doc_map[x]['department_name']}) - {'Active' if doc_map[x]['is_active'] else 'Inactive'}",
                            key="sel_doc_edit",
                        )
                        target_doc = doc_map[sel_doc_id]
                        with st.form("edit_doctor_form"):
                            edit_doc_name = st.text_input("Doctor Name", value=target_doc["name"], key=f"edit_doc_name_{sel_doc_id}")
                            edit_doc_dept_id = st.selectbox(
                                "Department",
                                options=list(all_depts_map.keys()),
                                index=list(all_depts_map.keys()).index(target_doc["department_id"]) if target_doc["department_id"] in all_depts_map else 0,
                                format_func=lambda x: all_depts_map.get(x, ""),
                                key=f"edit_doc_dept_{sel_doc_id}",
                            )
                            edit_doc_fee = st.text_input("Consultation Fee (INR)", value=str(target_doc["consultation_fee"]), key=f"edit_doc_fee_{sel_doc_id}")
                            edit_doc_pct = st.number_input(
                                "Surgeon Fee (%)",
                                min_value=0,
                                max_value=100,
                                value=int(target_doc["surgeon_fee_pct"]),
                                step=1,
                                key=f"edit_doc_pct_{sel_doc_id}",
                            )
                            edit_doc_spec = st.text_input("Specialization", value=target_doc["specialization"] or "", key=f"edit_doc_spec_{sel_doc_id}")
                            curr_s_type = target_doc.get("surgeon_fee_type", "fixed")
                            edit_doc_fee_mode = st.radio(
                                "Surgeon Fee Mode",
                                options=["Fixed Amount", "Percentage of Base Tariff"],
                                index=0 if curr_s_type == "fixed" else 1,
                                horizontal=True,
                                key=f"edit_doc_mode_{sel_doc_id}",
                            )
                            edit_doc_surg_amt = st.number_input(
                                "Surgeon Fee Amount (INR)",
                                min_value=0,
                                value=int(target_doc.get("surgeon_fee_amount") or 0),
                                step=1000,
                                key=f"edit_doc_amt_{sel_doc_id}",
                            )
                            edit_doc_sub = st.form_submit_button("Save Doctor Changes", width="stretch")
                            if edit_doc_sub:
                                if not edit_doc_name.strip():
                                    st.error("Doctor name cannot be blank.")
                                else:
                                    v_fee, fee_dec, e_fee = billing.validate_whole_rupee_field(edit_doc_fee, "Consultation Fee", allow_zero=True)
                                    v_pct, pct_val, e_pct = billing.validate_whole_percentage_field(edit_doc_pct, "Surgeon Fee (%)")
                                    if not (v_fee and v_pct):
                                        errs = [e for e in (e_fee, e_pct) if e]
                                        st.error("; ".join(errs))
                                    else:
                                        try:
                                            s_type = "fixed" if edit_doc_fee_mode == "Fixed Amount" else "percentage"
                                            database.save_doctor(
                                                conn,
                                                name=edit_doc_name.strip(),
                                                department_id=edit_doc_dept_id,
                                                consultation_fee=fee_dec,
                                                surgeon_fee_pct=pct_val,
                                                specialization=edit_doc_spec.strip(),
                                                doc_id=sel_doc_id,
                                                surgeon_fee_type=s_type,
                                                surgeon_fee_amount=int(edit_doc_surg_amt),
                                            )
                                            st.session_state["admin_flash_message"] = f"Doctor '{edit_doc_name.strip()}' updated successfully."
                                            st.rerun()
                                        except sqlite3.IntegrityError as e:
                                            st.error(f"Cannot update doctor: {e}")

                        if target_doc["is_active"]:
                            if st.button("Deactivate Doctor", key="btn_deact_doc", width="stretch"):
                                database.toggle_doctor_status(conn, sel_doc_id, 0)
                                st.warning(f"Doctor '{target_doc['name']}' deactivated.")
                                st.rerun()
                        else:
                            if st.button("Reactivate Doctor", key="btn_react_doc", width="stretch"):
                                database.toggle_doctor_status(conn, sel_doc_id, 1)
                                st.success(f"Doctor '{target_doc['name']}' restored.")
                                st.rerun()

            # ------------------------------------------------------------------
            # TAB 4: PROCEDURES
            # ------------------------------------------------------------------
            with admin_tabs[3]:
                st.subheader("Surgical & Medical Procedures")
                procs = database.get_procedures(conn, active_only=False)

                proc_rows = []
                for p in procs:
                    proc_rows.append({
                        "ID": p["id"],
                        "Procedure": p["name"],
                        "Department": p["department_name"],
                        "Base Cost": billing.format_inr(p["base_cost"]),
                        "OT Charge": billing.format_inr(p["ot_charge"]),
                        "Anaesthesia": billing.format_inr(p["anaesthesia_charge"]),
                        "Consumables": billing.format_inr(p["consumables_cost"]),
                        "Investigations": billing.format_inr(p["investigations_charge"]),
                        "Default Stay": f"{p['default_stay_days']} days",
                        "Status": "Active" if p["is_active"] else "Inactive",
                    })
                st.dataframe(pd.DataFrame(proc_rows), width="stretch", hide_index=True)

                col_pr_add, col_pr_edit = st.columns(2)
                with col_pr_add:
                    st.write("##### Add New Procedure")
                    if not active_depts:
                        st.warning("Please activate at least one department first.")
                    else:
                        with st.form("add_proc_form"):
                            new_pr_name = st.text_input("Procedure Name *")
                            new_pr_dept_id = st.selectbox(
                                "Department *",
                                options=[d["id"] for d in active_depts],
                                format_func=lambda x: all_depts_map.get(x, ""),
                                key="add_pr_dept",
                            )
                            new_pr_base = st.text_input("Hospital Base Tariff (₹) *", placeholder="e.g. 90000")
                            new_pr_ot = st.text_input("OT Charge (₹) *", placeholder="e.g. 35000")
                            new_pr_anaes = st.text_input("Anaesthesia Charge (₹) *", placeholder="e.g. 15000")
                            new_pr_cons = st.text_input("Consumables & Implants Cost (₹) *", placeholder="e.g. 160000")
                            new_pr_inv = st.text_input("Investigations & Diagnostics (₹) *", placeholder="e.g. 10000")
                            new_pr_stay = st.number_input("Default Length of Stay (Days) *", min_value=0, max_value=365, value=1)
                            add_pr_sub = st.form_submit_button("Add Procedure", width="stretch")
                            if add_pr_sub:
                                if not new_pr_name.strip():
                                    st.error("Procedure name cannot be blank.")
                                else:
                                    v_base, dec_base, e_base = billing.validate_whole_rupee_field(new_pr_base, "Base Cost", allow_zero=True)
                                    v_ot, dec_ot, e_ot = billing.validate_whole_rupee_field(new_pr_ot, "OT Charge", allow_zero=True)
                                    v_an, dec_an, e_an = billing.validate_whole_rupee_field(new_pr_anaes, "Anaesthesia Charge", allow_zero=True)
                                    v_co, dec_co, e_co = billing.validate_whole_rupee_field(new_pr_cons, "Consumables Cost", allow_zero=True)
                                    v_in, dec_in, e_in = billing.validate_whole_rupee_field(new_pr_inv, "Investigations Charge", allow_zero=True)
                                    if not (v_base and v_ot and v_an and v_co and v_in):
                                        errs = [e for e in (e_base, e_ot, e_an, e_co, e_in) if e]
                                        st.error("; ".join(errs))
                                    else:
                                        try:
                                            database.save_procedure(
                                                conn,
                                                name=new_pr_name.strip(),
                                                department_id=new_pr_dept_id,
                                                base_cost=dec_base,
                                                ot_charge=dec_ot,
                                                anaesthesia_charge=dec_an,
                                                consumables_cost=dec_co,
                                                investigations_charge=dec_in,
                                                default_stay_days=int(new_pr_stay),
                                            )
                                            st.session_state["admin_flash_message"] = f"Procedure '{new_pr_name.strip()}' created successfully."
                                            st.rerun()
                                        except sqlite3.IntegrityError as e:
                                            st.error(f"Cannot create procedure: {e}")

                with col_pr_edit:
                    st.write("##### Edit / Soft-Delete Procedure")
                    if procs:
                        proc_map = {p["id"]: p for p in procs}
                        sel_pr_id = st.selectbox(
                            "Select Procedure",
                            options=list(proc_map.keys()),
                            format_func=lambda x: f"{proc_map[x]['name']} ({proc_map[x]['department_name']}) - {'Active' if proc_map[x]['is_active'] else 'Inactive'}",
                            key="sel_proc_edit",
                        )
                        target_pr = proc_map[sel_pr_id]
                        with st.form("edit_proc_form"):
                            edit_pr_name = st.text_input("Procedure Name", value=target_pr["name"], key=f"edit_pr_name_{sel_pr_id}")
                            edit_pr_dept_id = st.selectbox(
                                "Department",
                                options=list(all_depts_map.keys()),
                                index=list(all_depts_map.keys()).index(target_pr["department_id"]) if target_pr["department_id"] in all_depts_map else 0,
                                format_func=lambda x: all_depts_map.get(x, ""),
                                key=f"edit_pr_dept_{sel_pr_id}",
                            )
                            edit_pr_base = st.text_input("Base Tariff (₹)", value=str(target_pr["base_cost"]), key=f"edit_pr_base_{sel_pr_id}")
                            edit_pr_ot = st.text_input("OT Charge (₹)", value=str(target_pr["ot_charge"]), key=f"edit_pr_ot_{sel_pr_id}")
                            edit_pr_anaes = st.text_input("Anaesthesia Charge (₹)", value=str(target_pr["anaesthesia_charge"]), key=f"edit_pr_anaes_{sel_pr_id}")
                            edit_pr_cons = st.text_input("Consumables Cost (₹)", value=str(target_pr["consumables_cost"]), key=f"edit_pr_cons_{sel_pr_id}")
                            edit_pr_inv = st.text_input("Investigations Charge (₹)", value=str(target_pr["investigations_charge"]), key=f"edit_pr_inv_{sel_pr_id}")
                            edit_pr_stay = st.number_input(
                                "Default Stay (Days)",
                                min_value=0,
                                max_value=365,
                                value=int(target_pr["default_stay_days"]),
                                key=f"edit_pr_stay_{sel_pr_id}",
                            )
                            edit_pr_sub = st.form_submit_button("Save Procedure Changes", width="stretch")
                            if edit_pr_sub:
                                if not edit_pr_name.strip():
                                    st.error("Procedure name cannot be blank.")
                                else:
                                    v_base, dec_base, e_base = billing.validate_whole_rupee_field(edit_pr_base, "Base Cost", allow_zero=True)
                                    v_ot, dec_ot, e_ot = billing.validate_whole_rupee_field(edit_pr_ot, "OT Charge", allow_zero=True)
                                    v_an, dec_an, e_an = billing.validate_whole_rupee_field(edit_pr_anaes, "Anaesthesia Charge", allow_zero=True)
                                    v_co, dec_co, e_co = billing.validate_whole_rupee_field(edit_pr_cons, "Consumables Cost", allow_zero=True)
                                    v_in, dec_in, e_in = billing.validate_whole_rupee_field(edit_pr_inv, "Investigations Charge", allow_zero=True)
                                    if not (v_base and v_ot and v_an and v_co and v_in):
                                        errs = [e for e in (e_base, e_ot, e_an, e_co, e_in) if e]
                                        st.error("; ".join(errs))
                                    else:
                                        try:
                                            database.save_procedure(
                                                conn,
                                                name=edit_pr_name.strip(),
                                                department_id=edit_pr_dept_id,
                                                base_cost=dec_base,
                                                ot_charge=dec_ot,
                                                anaesthesia_charge=dec_an,
                                                consumables_cost=dec_co,
                                                investigations_charge=dec_in,
                                                default_stay_days=int(edit_pr_stay),
                                                proc_id=sel_pr_id,
                                            )
                                            st.session_state["admin_flash_message"] = f"Procedure '{edit_pr_name.strip()}' updated successfully."
                                            st.rerun()
                                        except sqlite3.IntegrityError as e:
                                            st.error(f"Cannot update procedure: {e}")

                        if target_pr["is_active"]:
                            if st.button("Deactivate Procedure", key="btn_deact_proc", width="stretch"):
                                database.toggle_procedure_status(conn, sel_pr_id, 0)
                                st.warning(f"Procedure '{target_pr['name']}' deactivated.")
                                st.rerun()
                        else:
                            if st.button("Reactivate Procedure", key="btn_react_proc", width="stretch"):
                                database.toggle_procedure_status(conn, sel_pr_id, 1)
                                st.success(f"Procedure '{target_pr['name']}' restored.")
                                st.rerun()

            # ------------------------------------------------------------------
            # TAB 5: SETTINGS
            # ------------------------------------------------------------------
            with admin_tabs[4]:
                st.subheader("Hospital & Tariff Configuration Settings")
                if "settings_saved_msg" in st.session_state:
                    st.success(st.session_state.pop("settings_saved_msg"))

                curr_settings = database.get_settings(conn)
                with st.form("settings_form"):
                    h_name = st.text_input("Hospital Name *", value=curr_settings.get("hospital_name", ""))
                    h_addr = st.text_area("Hospital Address", value=curr_settings.get("hospital_address", ""))
                    col_s1, col_s2 = st.columns(2)
                    with col_s1:
                        h_phone = st.text_input("Phone Number", value=curr_settings.get("hospital_phone", ""))
                    with col_s2:
                        h_email = st.text_input("Email", value=curr_settings.get("hospital_email", ""))

                    col_s3, col_s4, col_s5, col_s6 = st.columns(4)
                    with col_s3:
                        tax_r = st.text_input("Default Tax Rate (%)", value=curr_settings.get("tax_rate", "0"))
                    with col_s4:
                        cont_r = st.text_input("Contingency Rate (%)", value=curr_settings.get("contingency_rate", "10"))
                    with col_s5:
                        rng_p = st.text_input("Estimate Range Band (+/-%)", value=curr_settings.get("estimate_range_pct", "10"))
                    with col_s6:
                        val_d = st.text_input("Estimate Validity (Days)", value=curr_settings.get("validity_days", "15"))

                    disc_txt = st.text_area("Disclaimer Text", value=curr_settings.get("disclaimer_text", ""))

                    st.write("##### Category-wise Tax Applicability (GST)")
                    st.caption("Healthcare services by clinical establishments are generally exempt under GST (tax rate 0%). Note: Room rent exceeding ₹5,000 per day outside ICU may attract 5% GST without ITC. Consult finance before enabling tax on any category.")

                    tax_c1, tax_c2 = st.columns(2)
                    with tax_c1:
                        t_room = st.checkbox(
                            "Room Accommodation (Non-ICU rent > ₹5,000/day)",
                            value=curr_settings.get("tax_category_room", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_room",
                        )
                        t_proc = st.checkbox(
                            "Hospital Procedure Tariff",
                            value=curr_settings.get("tax_category_procedure", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_procedure",
                        )
                        t_doc = st.checkbox(
                            "Surgeon Professional Fee",
                            value=curr_settings.get("tax_category_surgeon", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_surgeon",
                        )
                        t_ot = st.checkbox(
                            "Operation Theatre Charges",
                            value=curr_settings.get("tax_category_ot", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_ot",
                        )
                    with tax_c2:
                        t_anaes = st.checkbox(
                            "Anaesthesia Charges",
                            value=curr_settings.get("tax_category_anaesthesia", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_anaesthesia",
                        )
                        t_cons = st.checkbox(
                            "Surgical Consumables & Implants",
                            value=curr_settings.get("tax_category_consumables", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_consumables",
                        )
                        t_inv = st.checkbox(
                            "Investigations & Diagnostics",
                            value=curr_settings.get("tax_category_investigations", "0") in ("1", "true", "yes"),
                            key="set_tax_cat_investigations",
                        )

                    save_settings_sub = st.form_submit_button("Save System Settings", type="primary", width="stretch")
                    if save_settings_sub:
                        if not h_name.strip():
                            st.error("Hospital Name cannot be blank (required).")
                        else:
                            v_tax, d_tax, e_tax = billing.validate_percentage_field(tax_r, "Tax Rate")
                            v_cont, d_cont, e_cont = billing.validate_percentage_field(cont_r, "Contingency Rate")
                            v_rng, d_rng, e_rng = billing.validate_percentage_field(rng_p, "Range Band")
                            try:
                                val_days_int = int(val_d)
                                if val_days_int <= 0:
                                    v_val = False
                                    e_val = "Validity days must be a positive number."
                                else:
                                    v_val = True
                                    e_val = ""
                            except Exception:
                                v_val = False
                                e_val = "Validity days must be a valid integer."

                            if not (v_tax and v_cont and v_rng and v_val):
                                errs = [e for e in (e_tax, e_cont, e_rng, e_val) if e]
                                st.error("; ".join(errs))
                            else:
                                updated_dict = {
                                    "hospital_name": h_name.strip(),
                                    "hospital_address": h_addr.strip(),
                                    "hospital_phone": h_phone.strip(),
                                    "hospital_email": h_email.strip(),
                                    "tax_rate": str(d_tax),
                                    "contingency_rate": str(d_cont),
                                    "estimate_range_pct": str(d_rng),
                                    "validity_days": str(val_days_int),
                                    "disclaimer_text": disc_txt.strip(),
                                    "tax_category_room": "1" if t_room else "0",
                                    "tax_category_procedure": "1" if t_proc else "0",
                                    "tax_category_surgeon": "1" if t_doc else "0",
                                    "tax_category_ot": "1" if t_ot else "0",
                                    "tax_category_anaesthesia": "1" if t_anaes else "0",
                                    "tax_category_consumables": "1" if t_cons else "0",
                                    "tax_category_investigations": "1" if t_inv else "0",
                                }
                                database.update_settings(conn, updated_dict)
                                st.session_state["settings_saved_msg"] = "System settings updated successfully."
                                st.rerun()

            # ------------------------------------------------------------------
            # TAB 6: CSV IMPORT / EXPORT
            # ------------------------------------------------------------------
            with admin_tabs[5]:
                st.subheader("Master Data CSV Import & Export")
                st.caption("Export master tariffs as UTF-8 CSV with BOM, or batch import data with atomic rollback on error.")

                csv_category = st.radio(
                    "Select Master Data Category",
                    ["Departments", "Room Types", "Doctors", "Procedures"],
                    horizontal=True,
                )

                col_exp, col_imp = st.columns(2)

                # Export Column
                with col_exp:
                    st.write(f"##### Export {csv_category}")
                    if csv_category == "Departments":
                        exp_bytes = database.export_departments_csv(conn)
                        exp_fname = "departments.csv"
                    elif csv_category == "Room Types":
                        exp_bytes = database.export_room_types_csv(conn)
                        exp_fname = "room_types.csv"
                    elif csv_category == "Doctors":
                        exp_bytes = database.export_doctors_csv(conn)
                        exp_fname = "doctors.csv"
                    else:
                        exp_bytes = database.export_procedures_csv(conn)
                        exp_fname = "procedures.csv"

                    st.download_button(
                        label=f"Download {exp_fname}",
                        data=exp_bytes,
                        file_name=exp_fname,
                        mime="text/csv",
                        width="stretch",
                    )
                    st.caption("Formula injection safe; departments referenced by name.")

                # Import Column
                with col_imp:
                    st.write(f"##### Import {csv_category}")
                    uploaded_csv = st.file_uploader(
                        f"Upload {csv_category} CSV",
                        type=["csv"],
                        key=f"csv_upload_{csv_category}",
                    )
                    if uploaded_csv is not None:
                        try:
                            csv_raw_bytes = uploaded_csv.getvalue()
                            df_prev = pd.read_csv(io.BytesIO(csv_raw_bytes), encoding="utf-8-sig")
                            st.write("**Preview Data:**")
                            st.dataframe(df_prev.head(10), width="stretch", hide_index=True)

                            if st.button(f"Confirm & Import {len(df_prev)} Rows", key=f"btn_imp_{csv_category}", type="primary", width="stretch"):
                                try:
                                    if csv_category == "Departments":
                                        ins, upd = database.import_departments_csv(conn, csv_raw_bytes)
                                    elif csv_category == "Room Types":
                                        ins, upd = database.import_room_types_csv(conn, csv_raw_bytes)
                                    elif csv_category == "Doctors":
                                        ins, upd = database.import_doctors_csv(conn, csv_raw_bytes)
                                    else:
                                        ins, upd = database.import_procedures_csv(conn, csv_raw_bytes)
                                    st.session_state["admin_flash_message"] = f"Import successful: {ins} added, {upd} updated."
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Import failed (all changes rolled back): {e}")
                        except Exception as e:
                            st.error(f"Cannot parse CSV file: {e}")

            # ------------------------------------------------------------------
            # TAB 7: CHANGE PASSWORD
            # ------------------------------------------------------------------
            with admin_tabs[6]:
                st.subheader("Change Administrator Password")
                with st.form("change_pwd_form"):
                    curr_pwd = st.text_input("Current Password", type="password")
                    new_pwd = st.text_input("New Password (min 8 characters)", type="password")
                    confirm_new_pwd = st.text_input("Confirm New Password", type="password")
                    change_pwd_sub = st.form_submit_button("Update Password", width="stretch")

                    if change_pwd_sub:
                        admin_user = st.session_state.get("admin_username", "admin")
                        if not database.verify_admin_credentials(conn, admin_user, curr_pwd):
                            import time
                            time.sleep(1)
                            st.error("Current password is incorrect.")
                        elif len(new_pwd) < 8:
                            st.error("New password must be at least 8 characters long.")
                        elif new_pwd != confirm_new_pwd:
                            st.error("New passwords do not match.")
                        else:
                            database.set_admin_password(conn, admin_user, new_pwd)
                            st.success("Password updated successfully.")

            # ------------------------------------------------------------------
            # TAB 8: DATABASE BACKUP
            # ------------------------------------------------------------------
            with admin_tabs[7]:
                st.subheader("Database Backup & Disaster Recovery")
                st.write(
                    "Generate a hot, transactional snapshot of the entire SQLite database (`hospital.db`). "
                    "The backup is created using SQLite's native backup API and checkpointed safely even with active readers/writers in WAL mode."
                )

                if st.button("Prepare Backup Snapshot", key="btn_prepare_backup", width="stretch"):
                    st.session_state["db_backup_bytes"] = database.backup_database(conn)
                    st.session_state["db_backup_prepared_at"] = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
                    st.rerun()

                if "db_backup_bytes" in st.session_state:
                    prep_time = st.session_state.get("db_backup_prepared_at", "just now")
                    st.success(f"Backup snapshot ready (prepared at {prep_time}). Click below to download.")
                    st.download_button(
                        label="Download Database Backup (hospital_backup.db)",
                        data=st.session_state["db_backup_bytes"],
                        file_name="hospital_backup.db",
                        mime="application/octet-stream",
                        width="stretch",
                        key="btn_download_backup",
                    )

            # ------------------------------------------------------------------
            # TAB 9: PDF PRINT LAYOUT
            # ------------------------------------------------------------------
            with admin_tabs[8]:
                import pdf_layout
                pdf_layout.render_pdf_layout_editor_tab(conn)

finally:
    # Ensure SQLite connection is closed at the end of every rerun
    conn.close()