"""
analytics_dashboard.py - Comprehensive hospital executive analytics suite.
Tracks patient conversion rates, estimation forecasting accuracy vs actual bills,
departmental efficiency, doctor leaderboards, revision chain deduplication,
and multi-sheet Excel exports.
"""

from __future__ import annotations

import io
import re
import sqlite3
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import pandas as pd
import streamlit as st

import billing
import database


# ==============================================================================
# 1. Revision Deduplication & Data Extraction Helpers
# ==============================================================================

def get_deduplicated_estimates(
    estimates: List[Dict[str, Any]],
    exclude_superseded: bool = True,
) -> List[Dict[str, Any]]:
    """
    Filter estimates to include only the latest leaf revision in each revision chain
    when exclude_superseded is True.
    If EST-20261025-0001 has revisions -R1 and -R2, only -R2 is retained.
    """
    if not exclude_superseded or not estimates:
        return estimates

    root_groups: Dict[str, List[Tuple[int, int, Dict[str, Any]]]] = {}

    for e in estimates:
        est_num = (e.get("estimate_number") or "").strip()
        root = re.sub(r"-R\d+$", "", est_num)

        match = re.search(r"-R(\d+)$", est_num)
        rev_idx = int(match.group(1)) if match else 0
        record_id = int(e.get("id") or 0)

        if root not in root_groups:
            root_groups[root] = []
        root_groups[root].append((rev_idx, record_id, e))

    deduped: List[Dict[str, Any]] = []
    for root, group in root_groups.items():
        # Sort by revision index ascending, then record id ascending
        group.sort(key=lambda item: (item[0], item[1]))
        # Keep the latest leaf revision
        deduped.append(group[-1][2])

    # Preserve original ordering (by id descending)
    deduped.sort(key=lambda x: int(x.get("id") or 0), reverse=True)
    return deduped


def calculate_estimate_metrics(e: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate financial and forecasting accuracy metrics for an estimate record.
    """
    est_total = Decimal(str(e.get("total_amount") or "0"))
    converted = 1 if e.get("converted") == 1 else 0

    actual_str = e.get("actual_amount")
    actual_dec: Optional[Decimal] = None
    if actual_str:
        try:
            cleaned = str(actual_str).replace(",", "").replace("₹", "").strip()
            if cleaned:
                val = Decimal(cleaned)
                if val > Decimal("0"):
                    actual_dec = val
        except Exception:
            actual_dec = None

    variance_pct: Optional[float] = None
    accuracy_pct: Optional[float] = None
    drift_pct: Optional[float] = None
    within_10_band: Optional[bool] = None

    if actual_dec is not None and actual_dec > Decimal("0"):
        diff = abs(actual_dec - est_total)
        var_dec = (diff / actual_dec) * Decimal("100.0")
        variance_pct = float(var_dec)
        accuracy_pct = max(0.0, 100.0 - variance_pct)

        # Signed drift: positive = actual > est (overrun), negative = actual < est (underrun)
        signed_diff = actual_dec - est_total
        drift_dec = (signed_diff / actual_dec) * Decimal("100.0")
        drift_pct = float(drift_dec)

        within_10_band = variance_pct <= 10.0

    return {
        **e,
        "metrics_est_total": est_total,
        "metrics_converted": converted,
        "metrics_actual_dec": actual_dec,
        "metrics_variance_pct": variance_pct,
        "metrics_accuracy_pct": accuracy_pct,
        "metrics_drift_pct": drift_pct,
        "metrics_within_10_band": within_10_band,
    }


def aggregate_kpi_summary(estimates_with_metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate executive KPI metrics across filtered estimates."""
    total_count = len(estimates_with_metrics)
    if total_count == 0:
        return {
            "total_count": 0,
            "total_pipeline_val": Decimal("0.00"),
            "converted_count": 0,
            "conversion_rate_pct": 0.0,
            "realized_est_val": Decimal("0.00"),
            "actual_billed_total": Decimal("0.00"),
            "audited_count": 0,
            "mean_variance_pct": None,
            "mean_accuracy_pct": None,
            "within_10_count": 0,
            "within_10_rate_pct": 0.0,
            "overrun_count": 0,
            "underrun_count": 0,
        }

    total_pipeline_val = sum(e["metrics_est_total"] for e in estimates_with_metrics)
    converted_cases = [e for e in estimates_with_metrics if e["metrics_converted"] == 1]
    converted_count = len(converted_cases)
    conversion_rate_pct = (converted_count / total_count * 100.0) if total_count > 0 else 0.0

    realized_est_val = sum(e["metrics_est_total"] for e in converted_cases)
    actual_billed_total = sum(
        e["metrics_actual_dec"] for e in converted_cases if e["metrics_actual_dec"] is not None
    )

    audited = [e for e in estimates_with_metrics if e["metrics_variance_pct"] is not None]
    audited_count = len(audited)

    if audited_count > 0:
        mean_variance_pct = sum(e["metrics_variance_pct"] for e in audited) / audited_count
        mean_accuracy_pct = sum(e["metrics_accuracy_pct"] for e in audited) / audited_count
        within_10_count = sum(1 for e in audited if e["metrics_within_10_band"] is True)
        within_10_rate_pct = (within_10_count / audited_count * 100.0)

        overrun_count = sum(1 for e in audited if (e["metrics_drift_pct"] or 0) > 10.0)
        underrun_count = sum(1 for e in audited if (e["metrics_drift_pct"] or 0) < -10.0)
    else:
        mean_variance_pct = None
        mean_accuracy_pct = None
        within_10_count = 0
        within_10_rate_pct = 0.0
        overrun_count = 0
        underrun_count = 0

    return {
        "total_count": total_count,
        "total_pipeline_val": total_pipeline_val,
        "converted_count": converted_count,
        "conversion_rate_pct": conversion_rate_pct,
        "realized_est_val": realized_est_val,
        "actual_billed_total": actual_billed_total,
        "audited_count": audited_count,
        "mean_variance_pct": mean_variance_pct,
        "mean_accuracy_pct": mean_accuracy_pct,
        "within_10_count": within_10_count,
        "within_10_rate_pct": within_10_rate_pct,
        "overrun_count": overrun_count,
        "underrun_count": underrun_count,
    }


def aggregate_doctor_performance(
    estimates_with_metrics: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Aggregate estimate volume, conversion, and accuracy performance per doctor."""
    doctor_groups: Dict[str, List[Dict[str, Any]]] = {}
    doc_departments: Dict[str, str] = {}

    for e in estimates_with_metrics:
        doc_name = (e.get("doctor_name") or "Unassigned Doctor").strip()
        dept_name = (e.get("department_name") or "General").strip()
        doc_departments[doc_name] = dept_name

        if doc_name not in doctor_groups:
            doctor_groups[doc_name] = []
        doctor_groups[doc_name].append(e)

    doc_stats: List[Dict[str, Any]] = []
    for doc_name, group in doctor_groups.items():
        total_given = len(group)
        converted = sum(1 for e in group if e["metrics_converted"] == 1)
        conv_rate = (converted / total_given * 100.0) if total_given > 0 else 0.0

        total_est_val = sum(e["metrics_est_total"] for e in group)
        conv_est_val = sum(e["metrics_est_total"] for e in group if e["metrics_converted"] == 1)
        actual_billed = sum(
            e["metrics_actual_dec"] for e in group if e["metrics_actual_dec"] is not None
        )
        avg_est_val = total_est_val / Decimal(total_given) if total_given > 0 else Decimal(0)

        audited = [e for e in group if e["metrics_variance_pct"] is not None]
        if audited:
            avg_var = sum(e["metrics_variance_pct"] for e in audited) / len(audited)
            mean_acc = sum(e["metrics_accuracy_pct"] for e in audited) / len(audited)
            w10 = sum(1 for e in audited if e["metrics_within_10_band"] is True)
            w10_rate = (w10 / len(audited) * 100.0)
        else:
            avg_var = None
            mean_acc = None
            w10_rate = None

        doc_stats.append({
            "doctor_name": doc_name,
            "department": doc_departments.get(doc_name, "General"),
            "estimates_given": total_given,
            "converted_count": converted,
            "conversion_rate_pct": conv_rate,
            "total_est_val": total_est_val,
            "converted_est_val": conv_est_val,
            "actual_billed": actual_billed,
            "avg_est_val": avg_est_val,
            "avg_variance_pct": avg_var,
            "mean_accuracy_pct": mean_acc,
            "within_10_rate_pct": w10_rate,
            "audited_count": len(audited),
        })

    return doc_stats


def aggregate_department_performance(
    estimates_with_metrics: List[Dict[str, Any]],
    all_doctors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Aggregate volume, conversion, and accuracy performance per department."""
    dept_groups: Dict[str, List[Dict[str, Any]]] = {}

    for e in estimates_with_metrics:
        dept_name = (e.get("department_name") or "Unassigned Department").strip()
        if dept_name not in dept_groups:
            dept_groups[dept_name] = []
        dept_groups[dept_name].append(e)

    # Count active doctors per department if available
    dept_doc_counts: Dict[str, int] = {}
    if all_doctors:
        for doc in all_doctors:
            d_name = doc.get("department_name") or ""
            if d_name:
                dept_doc_counts[d_name] = dept_doc_counts.get(d_name, 0) + 1

    dept_stats: List[Dict[str, Any]] = []
    for dept_name, group in dept_groups.items():
        total_given = len(group)
        converted = sum(1 for e in group if e["metrics_converted"] == 1)
        conv_rate = (converted / total_given * 100.0) if total_given > 0 else 0.0

        total_est_val = sum(e["metrics_est_total"] for e in group)
        conv_est_val = sum(e["metrics_est_total"] for e in group if e["metrics_converted"] == 1)
        actual_billed = sum(
            e["metrics_actual_dec"] for e in group if e["metrics_actual_dec"] is not None
        )
        avg_est_val = total_est_val / Decimal(total_given) if total_given > 0 else Decimal(0)

        audited = [e for e in group if e["metrics_variance_pct"] is not None]
        if audited:
            avg_var = sum(e["metrics_variance_pct"] for e in audited) / len(audited)
            mean_acc = sum(e["metrics_accuracy_pct"] for e in audited) / len(audited)
        else:
            avg_var = None
            mean_acc = None

        doc_count = dept_doc_counts.get(dept_name, len({e.get("doctor_name") for e in group if e.get("doctor_name")}))

        dept_stats.append({
            "department": dept_name,
            "active_doctors": doc_count,
            "estimates_given": total_given,
            "converted_count": converted,
            "conversion_rate_pct": conv_rate,
            "total_est_val": total_est_val,
            "converted_est_val": conv_est_val,
            "actual_billed": actual_billed,
            "avg_est_val": avg_est_val,
            "avg_variance_pct": avg_var,
            "mean_accuracy_pct": mean_acc,
            "audited_count": len(audited),
        })

    return dept_stats


# ==============================================================================
# 2. Multi-Sheet Excel Export Generator (openpyxl)
# ==============================================================================

def create_analytics_excel_workbook(
    estimates: List[Dict[str, Any]],
    doctor_stats: List[Dict[str, Any]],
    dept_stats: List[Dict[str, Any]],
) -> bytes:
    """
    Generate a 3-sheet Excel workbook:
    1. 1_All_Estimates_Ledger
    2. 2_Doctor_Leaderboard
    3. 3_Department_Summary
    """
    wb = openpyxl.Workbook()

    # Styling definitions
    header_fill = PatternFill(start_color="1A365D", end_color="1A365D", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    bold_font = Font(name="Calibri", size=11, bold=True)
    regular_font = Font(name="Calibri", size=10)
    border_thin = Border(
        left=Side(style="thin", color="CBD5E0"),
        right=Side(style="thin", color="CBD5E0"),
        top=Side(style="thin", color="CBD5E0"),
        bottom=Side(style="thin", color="CBD5E0"),
    )

    # -------------------------------------------------------------------------
    # Sheet 1: 1_All_Estimates_Ledger
    # -------------------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "1_All_Estimates_Ledger"

    s1_headers = [
        "Estimate Number",
        "Revision Of",
        "Date",
        "Patient Name",
        "UHID",
        "Mobile Number",
        "Department",
        "Doctor",
        "Procedure",
        "Room Type",
        "Stay Days",
        "Subtotal (₹)",
        "Discount %",
        "Discount Amount (₹)",
        "Contingency (₹)",
        "Tax (₹)",
        "Total Amount (₹)",
        "Min Range (₹)",
        "Max Range (₹)",
        "Converted",
        "Actual Final Bill (₹)",
        "Accuracy Variance (%)",
        "Accuracy Score (%)",
        "Within ±10% Band",
        "Clinical Remarks",
    ]
    ws1.append(s1_headers)

    for e in estimates:
        m = calculate_estimate_metrics(e)
        created_raw = e.get("created_at") or ""
        try:
            date_str = datetime.fromisoformat(created_raw).strftime("%d-%b-%Y")
        except Exception:
            date_str = str(created_raw)[:10]

        var_text = f"{m['metrics_variance_pct']:.2f}%" if m["metrics_variance_pct"] is not None else "-"
        acc_text = f"{m['metrics_accuracy_pct']:.2f}%" if m["metrics_accuracy_pct"] is not None else "-"
        w10_text = "Yes" if m["metrics_within_10_band"] is True else ("No" if m["metrics_within_10_band"] is False else "-")

        ws1.append([
            e.get("estimate_number") or "",
            e.get("revision_of") or "-",
            date_str,
            e.get("patient_name") or "",
            e.get("uhid") or "-",
            e.get("mobile_number") or "-",
            e.get("department_name") or "-",
            e.get("doctor_name") or "-",
            e.get("procedure_name") or "-",
            e.get("room_type_name") or "-",
            e.get("stay_days", 1),
            float(e.get("subtotal") or 0),
            float(e.get("discount_pct") or 0),
            float(e.get("discount_amount") or 0),
            float(e.get("contingency_amount") or 0),
            float(e.get("tax_amount") or 0),
            float(e.get("total_amount") or 0),
            float(e.get("min_estimate") or 0),
            float(e.get("max_estimate") or 0),
            "Yes" if e.get("converted") == 1 else "No",
            float(m["metrics_actual_dec"]) if m["metrics_actual_dec"] is not None else "-",
            var_text,
            acc_text,
            w10_text,
            e.get("remarks") or "",
        ])

    # -------------------------------------------------------------------------
    # Sheet 2: 2_Doctor_Leaderboard
    # -------------------------------------------------------------------------
    ws2 = wb.create_sheet(title="2_Doctor_Leaderboard")
    s2_headers = [
        "Rank",
        "Doctor Name",
        "Department",
        "Estimates Given",
        "Converted Count",
        "Conversion Rate (%)",
        "Total Estimated Value (₹)",
        "Converted Value (₹)",
        "Actual Billed Total (₹)",
        "Avg Estimate Value (₹)",
        "Avg Variance (±%)",
        "Mean Accuracy (%)",
        "Within ±10% Band (%)",
    ]
    ws2.append(s2_headers)

    # Sort doctor stats by converted_count DESC, then conversion_rate_pct DESC
    sorted_docs = sorted(
        doctor_stats,
        key=lambda x: (x["converted_count"], x["conversion_rate_pct"], x["estimates_given"]),
        reverse=True,
    )
    for rank, d in enumerate(sorted_docs, start=1):
        ws2.append([
            rank,
            d["doctor_name"],
            d["department"],
            d["estimates_given"],
            d["converted_count"],
            round(d["conversion_rate_pct"], 1),
            float(d["total_est_val"]),
            float(d["converted_est_val"]),
            float(d["actual_billed"]),
            float(d["avg_est_val"]),
            round(d["avg_variance_pct"], 2) if d["avg_variance_pct"] is not None else "-",
            round(d["mean_accuracy_pct"], 2) if d["mean_accuracy_pct"] is not None else "-",
            f"{d['within_10_rate_pct']:.1f}%" if d["within_10_rate_pct"] is not None else "-",
        ])

    # -------------------------------------------------------------------------
    # Sheet 3: 3_Department_Summary
    # -------------------------------------------------------------------------
    ws3 = wb.create_sheet(title="3_Department_Summary")
    s3_headers = [
        "Department",
        "Active Doctors",
        "Estimates Given",
        "Converted Count",
        "Conversion Rate (%)",
        "Total Estimated Value (₹)",
        "Converted Value (₹)",
        "Actual Billed Total (₹)",
        "Avg Estimate Value (₹)",
        "Avg Variance (±%)",
        "Mean Accuracy (%)",
    ]
    ws3.append(s3_headers)

    sorted_depts = sorted(
        dept_stats,
        key=lambda x: (x["conversion_rate_pct"], x["converted_count"]),
        reverse=True,
    )
    for d in sorted_depts:
        ws3.append([
            d["department"],
            d["active_doctors"],
            d["estimates_given"],
            d["converted_count"],
            round(d["conversion_rate_pct"], 1),
            float(d["total_est_val"]),
            float(d["converted_est_val"]),
            float(d["actual_billed"]),
            float(d["avg_est_val"]),
            round(d["avg_variance_pct"], 2) if d["avg_variance_pct"] is not None else "-",
            round(d["mean_accuracy_pct"], 2) if d["mean_accuracy_pct"] is not None else "-",
        ])

    # Format all sheets with header styles, thin borders, and auto-fitted columns
    for ws in [ws1, ws2, ws3]:
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = regular_font
                cell.border = border_thin

        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 45)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ==============================================================================
# 3. Main Streamlit View: render_enhanced_analytics_dashboard
# ==============================================================================

def render_enhanced_analytics_dashboard(conn: sqlite3.Connection) -> None:
    """Render the comprehensive executive analytics and conversion suite."""
    st.header("Estimates Performance Dashboard & Analytics")
    st.caption(
        "Executive oversight into estimation pipeline volume, patient admission conversions, "
        "doctor performance benchmarks, and financial forecasting accuracy."
    )

    # 1. Fetch raw estimate data
    raw_estimates = database.get_estimates(conn, limit=None)
    all_departments = database.get_departments(conn, active_only=False)
    all_doctors = database.get_doctors(conn, active_only=False)

    if not raw_estimates:
        st.info("No estimates generated yet. Estimates saved in the Generator will appear here.")
        return

    # -------------------------------------------------------------------------
    # FILTER BAR
    # -------------------------------------------------------------------------
    with st.expander("Filter & Aggregation Controls", expanded=True):
        f_c1, f_c2, f_c3 = st.columns([1.5, 1.5, 1.2])

        with f_c1:
            # Parse dates for range picker
            dates = []
            for e in raw_estimates:
                c_at = e.get("created_at")
                if c_at:
                    try:
                        dates.append(datetime.fromisoformat(c_at).date())
                    except Exception:
                        pass
            min_date = min(dates) if dates else date.today()
            max_date = max(dates) if dates else date.today()

            date_range = st.date_input(
                "Date Range",
                value=(min_date, max_date),
                key="filter_analytics_date_range",
            )

        with f_c2:
            dept_names = sorted(list({d["name"] for d in all_departments if d.get("name")}))
            selected_depts = st.multiselect(
                "Filter Departments",
                dept_names,
                default=[],
                key="filter_analytics_departments",
            )

        with f_c3:
            conv_status_choice = st.selectbox(
                "Conversion Status",
                ["All Estimates", "Converted Only", "Unconverted Only"],
                index=0,
                key="filter_analytics_conv_status",
            )

        f_c4, f_c5 = st.columns([2, 1.5])
        with f_c4:
            # Cascading Doctor filter based on selected departments
            if selected_depts:
                eligible_dept_ids = {d["id"] for d in all_departments if d["name"] in selected_depts}
                eligible_docs = [doc["name"] for doc in all_doctors if doc.get("department_id") in eligible_dept_ids]
            else:
                eligible_docs = [doc["name"] for doc in all_doctors]

            eligible_doc_names = sorted(list(set(eligible_docs)))
            selected_docs = st.multiselect(
                "Filter Attending Doctors",
                eligible_doc_names,
                default=[],
                key="filter_analytics_doctors",
            )

        with f_c5:
            exclude_superseded = st.toggle(
                "Exclude Superseded Estimates (Latest Revision Only)",
                value=True,
                help="When enabled, if EST-0001 has -R1 and -R2, only -R2 is aggregated.",
                key="filter_analytics_exclude_superseded",
            )

    # -------------------------------------------------------------------------
    # Apply Filters & Deduplication
    # -------------------------------------------------------------------------
    # 1. Deduplication
    processed_estimates = get_deduplicated_estimates(raw_estimates, exclude_superseded=exclude_superseded)
    superseded_excluded_count = len(raw_estimates) - len(processed_estimates)

    # 2. Date Filtering
    start_d, end_d = None, None
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_d, end_d = date_range[0], date_range[1]
    elif isinstance(date_range, (list, tuple)) and len(date_range) == 1:
        start_d = date_range[0]
        end_d = date_range[0]

    filtered: List[Dict[str, Any]] = []
    for e in processed_estimates:
        c_at = e.get("created_at")
        if c_at and start_d and end_d:
            try:
                rec_date = datetime.fromisoformat(c_at).date()
                if rec_date < start_d or rec_date > end_d:
                    continue
            except Exception:
                pass

        if selected_depts:
            if (e.get("department_name") or "") not in selected_depts:
                continue

        if selected_docs:
            if (e.get("doctor_name") or "") not in selected_docs:
                continue

        if conv_status_choice == "Converted Only" and e.get("converted") != 1:
            continue
        elif conv_status_choice == "Unconverted Only" and e.get("converted") == 1:
            continue

        filtered.append(calculate_estimate_metrics(e))

    if not filtered:
        st.warning("No estimates match the selected filters.")
        return

    # Compute KPI summary
    kpi = aggregate_kpi_summary(filtered)
    doc_stats = aggregate_doctor_performance(filtered)
    dept_stats = aggregate_department_performance(filtered, all_doctors=all_doctors)

    st.divider()

    # -------------------------------------------------------------------------
    # ROW 1: 6 EXECUTIVE KPI CARDS
    # -------------------------------------------------------------------------
    k1, k2, k3, k4, k5, k6 = st.columns(6)

    k1.metric(
        label="Total Estimates Given",
        value=str(kpi["total_count"]),
        delta=f"-{superseded_excluded_count} superseded" if (exclude_superseded and superseded_excluded_count > 0) else None,
        help="Total estimates issued in current filter scope.",
    )
    k2.metric(
        label="Converted Admissions",
        value=str(kpi["converted_count"]),
        delta=f"{kpi['conversion_rate_pct']:.1f}% rate",
        help="Patients who accepted treatment and converted to admission.",
    )
    k3.metric(
        label="Total Pipeline Value",
        value=billing.format_inr(kpi["total_pipeline_val"], whole_rupees=True),
        help="Gross monetary value of all proposed estimates.",
    )
    k4.metric(
        label="Realized Estimate Value",
        value=billing.format_inr(kpi["realized_est_val"], whole_rupees=True),
        delta=f"Actual: {billing.format_inr(kpi['actual_billed_total'], whole_rupees=True)}" if kpi["actual_billed_total"] > Decimal(0) else None,
        help="Estimated value of converted cases, with actual recorded revenue delta.",
    )

    if kpi["mean_accuracy_pct"] is not None:
        k5.metric(
            label="Mean Estimation Accuracy",
            value=f"{kpi['mean_accuracy_pct']:.1f}%",
            delta=f"±{kpi['mean_variance_pct']:.2f}% avg var",
            delta_color="normal",
            help="Forecasting accuracy across audited final bills: 100% - (|actual - est| / actual * 100%).",
        )
    else:
        k5.metric(
            label="Mean Estimation Accuracy",
            value="-",
            help="Record final bill amounts in Estimate History to track forecasting accuracy.",
        )

    if kpi["audited_count"] > 0:
        k6.metric(
            label="Within ±10% Band Rate",
            value=f"{kpi['within_10_rate_pct']:.1f}%",
            delta=f"{kpi['within_10_count']}/{kpi['audited_count']} cases",
            help="Percentage of final bills that fell within the ±10% clinical range buffer.",
        )
    else:
        k6.metric(
            label="Within ±10% Band Rate",
            value="-",
            help="Requires audited final bills.",
        )

    st.divider()

    # -------------------------------------------------------------------------
    # ROW 2: CHAMPION SPOTLIGHT CARDS (4 Highlight Callouts)
    # -------------------------------------------------------------------------
    st.write("##### Clinical & Commercial Performance Champions")
    sp1, sp2, sp3, sp4 = st.columns(4)

    # 1. Volume Leader (Most Estimates)
    volume_leader = max(doc_stats, key=lambda x: (x["estimates_given"], x["total_est_val"]), default=None)
    with sp1:
        if volume_leader:
            st.info(
                f"**Volume Leader**\n\n"
                f"**{volume_leader['doctor_name']}**\n\n"
                f"*{volume_leader['department']}*\n\n"
                f"**Estimates:** {volume_leader['estimates_given']} cases\n\n"
                f"**Value:** {billing.format_inr(volume_leader['total_est_val'], whole_rupees=True)}"
            )
        else:
            st.info("No doctor records found.")

    # 2. Conversion Volume Leader (Most Conversions)
    conv_volume_leader = max(doc_stats, key=lambda x: (x["converted_count"], x["conversion_rate_pct"]), default=None)
    with sp2:
        if conv_volume_leader:
            st.success(
                f"**Conversion Volume Leader**\n\n"
                f"**{conv_volume_leader['doctor_name']}**\n\n"
                f"*{conv_volume_leader['department']}*\n\n"
                f"**Conversions:** {conv_volume_leader['converted_count']} admissions\n\n"
                f"**Conversion Rate:** {conv_volume_leader['conversion_rate_pct']:.1f}%"
            )
        else:
            st.success("No doctor records found.")

    # 3. Conversion Rate Champion (Highest % with >= 1 estimate)
    eligible_conv_docs = [d for d in doc_stats if d["estimates_given"] >= 1]
    conv_rate_champ = max(
        eligible_conv_docs,
        key=lambda x: (x["conversion_rate_pct"], x["converted_count"]),
        default=None,
    )
    with sp3:
        if conv_rate_champ:
            st.warning(
                f"**Highest Conversion Rate**\n\n"
                f"**{conv_rate_champ['doctor_name']}**\n\n"
                f"*{conv_rate_champ['department']}*\n\n"
                f"**Conversion Rate:** {conv_rate_champ['conversion_rate_pct']:.1f}%\n\n"
                f"**Ratio:** {conv_rate_champ['converted_count']} of {conv_rate_champ['estimates_given']} cases"
            )
        else:
            st.warning("No doctor records found.")

    # 4. Top Performing Department
    top_dept = max(dept_stats, key=lambda x: (x["conversion_rate_pct"], x["converted_count"]), default=None)
    with sp4:
        if top_dept:
            dept_acc_str = f"{top_dept['mean_accuracy_pct']:.1f}%" if top_dept["mean_accuracy_pct"] is not None else "-"
            st.info(
                f"**Top Performing Department**\n\n"
                f"**{top_dept['department']}**\n\n"
                f"**Conversions:** {top_dept['converted_count']} / {top_dept['estimates_given']} ({top_dept['conversion_rate_pct']:.1f}%)\n\n"
                f"**Mean Accuracy:** {dept_acc_str}"
            )
        else:
            st.info("No department records found.")

    st.divider()

    # -------------------------------------------------------------------------
    # ROW 3: DOCTOR & DEPARTMENT PERFORMANCE VISUALS
    # -------------------------------------------------------------------------
    st.write("##### Department & Doctor Benchmarking Visuals")
    ch_col1, ch_col2 = st.columns(2)

    with ch_col1:
        st.subheader("Estimates vs. Conversions by Doctor")
        if doc_stats:
            doc_chart_rows = []
            for d in sorted(doc_stats, key=lambda x: x["estimates_given"], reverse=True)[:10]:
                doc_chart_rows.append({
                    "Doctor": d["doctor_name"][:20],
                    "Estimates Given": d["estimates_given"],
                    "Converted": d["converted_count"],
                })
            df_doc_chart = pd.DataFrame(doc_chart_rows).set_index("Doctor")
            st.bar_chart(df_doc_chart)
        else:
            st.info("No doctor data available for chart.")

    with ch_col2:
        st.subheader("Department Volume & Pipeline")
        if dept_stats:
            dept_chart_rows = []
            for d in sorted(dept_stats, key=lambda x: x["estimates_given"], reverse=True):
                dept_chart_rows.append({
                    "Department": d["department"][:20],
                    "Estimates": d["estimates_given"],
                    "Conversions": d["converted_count"],
                })
            df_dept_chart = pd.DataFrame(dept_chart_rows).set_index("Department")
            st.bar_chart(df_dept_chart)
        else:
            st.info("No department data available for chart.")

    # Monthly Trend Chart
    st.subheader("Monthly Estimate & Conversion Trend")
    month_data: Dict[str, Dict[str, int]] = {}
    for e in filtered:
        c_at = e.get("created_at") or ""
        try:
            m_key = datetime.fromisoformat(c_at).strftime("%Y-%m")
        except Exception:
            m_key = "Other"

        if m_key not in month_data:
            month_data[m_key] = {"Estimates Issued": 0, "Converted Admissions": 0}
        month_data[m_key]["Estimates Issued"] += 1
        if e["metrics_converted"] == 1:
            month_data[m_key]["Converted Admissions"] += 1

    sorted_months = sorted([m for m in month_data.keys() if m != "Other"])
    if "Other" in month_data:
        sorted_months.append("Other")

    df_month = pd.DataFrame(
        [{"Month": m, **month_data[m]} for m in sorted_months]
    ).set_index("Month")
    st.line_chart(df_month)

    st.divider()

    # -------------------------------------------------------------------------
    # ROW 4: ESTIMATION ACCURACY & FINAL BILL VARIANCE DEEP-DIVE
    # -------------------------------------------------------------------------
    st.write("##### Estimation Accuracy & Final Bill Variance Deep-Dive")
    audited_cases = [e for e in filtered if e["metrics_variance_pct"] is not None]

    if not audited_cases:
        st.info(
            "No audited final bills recorded yet. Enter actual final bill amounts in the "
            "**Estimate History** tab to unlock the accuracy variance deep-dive."
        )
    else:
        var_col1, var_col2, var_col3 = st.columns(3)
        with var_col1:
            st.metric(
                "Cases Within ±10% Band",
                f"{kpi['within_10_count']} of {kpi['audited_count']}",
                f"{kpi['within_10_rate_pct']:.1f}% compliance",
            )
        with var_col2:
            overruns = [e for e in audited_cases if (e["metrics_drift_pct"] or 0) > 10.0]
            avg_over = (sum(e["metrics_drift_pct"] for e in overruns) / len(overruns)) if overruns else 0.0
            st.metric(
                "Under-Estimated / Bill Overruns",
                f"{len(overruns)} cases",
                f"+{avg_over:.1f}% avg overrun" if overruns else "None",
                delta_color="inverse",
            )
        with var_col3:
            underruns = [e for e in audited_cases if (e["metrics_drift_pct"] or 0) < -10.0]
            avg_under = (sum(e["metrics_drift_pct"] for e in underruns) / len(underruns)) if underruns else 0.0
            st.metric(
                "Over-Estimated / Bill Underruns",
                f"{len(underruns)} cases",
                f"{avg_under:.1f}% avg underrun" if underruns else "None",
                delta_color="normal",
            )

        # Visual comparison of Estimated Cost vs Actual Final Bill across audited cases
        st.subheader("Audited Cases: Estimated Tariff vs. Actual Final Bill (₹)")
        audit_chart_rows = []
        for e in audited_cases[:20]:
            p_label = f"{e.get('patient_name', '')[:12]} ({e.get('estimate_number', '')[-4:]})"
            audit_chart_rows.append({
                "Case": p_label,
                "Estimated Total (₹)": float(e["metrics_est_total"]),
                "Actual Final Bill (₹)": float(e["metrics_actual_dec"] or 0),
            })
        df_audit_chart = pd.DataFrame(audit_chart_rows).set_index("Case")
        st.bar_chart(df_audit_chart)

    st.divider()

    # -------------------------------------------------------------------------
    # ROW 5: INTERACTIVE LEADERBOARDS & MULTI-SHEET EXCEL EXPORT
    # -------------------------------------------------------------------------
    col_led_hdr, col_led_export = st.columns([3, 2])
    with col_led_hdr:
        st.write("##### Hospital Performance Matrices & Excel Export")

    with col_led_export:
        xlsx_bytes = create_analytics_excel_workbook(filtered, doc_stats, dept_stats)
        st.download_button(
            label="Download Complete Analytics Workbook (.xlsx)",
            data=xlsx_bytes,
            file_name=f"Hospital_Analytics_Report_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
            key="btn_download_excel_dashboard",
        )

    tab_doc, tab_dept = st.tabs([
        "Doctor Conversion & Accuracy Leaderboard",
        "Department Conversion & Accuracy Matrix",
    ])

    with tab_doc:
        sort_choice = st.selectbox(
            "Sort Leaderboard By",
            ["Most Converted", "Most Estimates Given", "Highest Conversion Rate %", "Highest Accuracy %"],
            index=0,
            key="sort_doctor_leaderboard",
        )

        if sort_choice == "Most Converted":
            sorted_doc_stats = sorted(doc_stats, key=lambda x: (x["converted_count"], x["conversion_rate_pct"]), reverse=True)
        elif sort_choice == "Most Estimates Given":
            sorted_doc_stats = sorted(doc_stats, key=lambda x: (x["estimates_given"], x["converted_count"]), reverse=True)
        elif sort_choice == "Highest Conversion Rate %":
            sorted_doc_stats = sorted(doc_stats, key=lambda x: (x["conversion_rate_pct"], x["converted_count"]), reverse=True)
        else:
            sorted_doc_stats = sorted(doc_stats, key=lambda x: (x["mean_accuracy_pct"] or 0, x["audited_count"]), reverse=True)

        doc_table_rows = []
        for rank, d in enumerate(sorted_doc_stats, start=1):
            doc_table_rows.append({
                "Rank": rank,
                "Doctor Name": d["doctor_name"],
                "Department": d["department"],
                "Estimates Given": d["estimates_given"],
                "Converted Count": d["converted_count"],
                "Conversion Rate": f"{d['conversion_rate_pct']:.1f}%",
                "Total Estimated (₹)": billing.format_inr(d["total_est_val"], whole_rupees=True),
                "Converted Value (₹)": billing.format_inr(d["converted_est_val"], whole_rupees=True),
                "Actual Billed (₹)": billing.format_inr(d["actual_billed"], whole_rupees=True) if d["actual_billed"] > Decimal(0) else "-",
                "Avg Estimate (₹)": billing.format_inr(d["avg_est_val"], whole_rupees=True),
                "Avg Variance (±%)": f"±{d['avg_variance_pct']:.2f}%" if d["avg_variance_pct"] is not None else "-",
                "Mean Accuracy (%)": f"{d['mean_accuracy_pct']:.1f}%" if d["mean_accuracy_pct"] is not None else "-",
                "Within ±10% Band (%)": f"{d['within_10_rate_pct']:.1f}%" if d["within_10_rate_pct"] is not None else "-",
            })

        st.dataframe(pd.DataFrame(doc_table_rows), width="stretch", hide_index=True)

    with tab_dept:
        sorted_dept_stats = sorted(
            dept_stats,
            key=lambda x: (x["conversion_rate_pct"], x["converted_count"]),
            reverse=True,
        )

        dept_table_rows = []
        for d in sorted_dept_stats:
            dept_table_rows.append({
                "Department": d["department"],
                "Active Doctors": d["active_doctors"],
                "Estimates Given": d["estimates_given"],
                "Converted Count": d["converted_count"],
                "Conversion Rate": f"{d['conversion_rate_pct']:.1f}%",
                "Total Estimated (₹)": billing.format_inr(d["total_est_val"], whole_rupees=True),
                "Converted Value (₹)": billing.format_inr(d["converted_est_val"], whole_rupees=True),
                "Actual Billed (₹)": billing.format_inr(d["actual_billed"], whole_rupees=True) if d["actual_billed"] > Decimal(0) else "-",
                "Avg Estimate (₹)": billing.format_inr(d["avg_est_val"], whole_rupees=True),
                "Avg Variance (±%)": f"±{d['avg_variance_pct']:.2f}%" if d["avg_variance_pct"] is not None else "-",
                "Mean Accuracy (%)": f"{d['mean_accuracy_pct']:.1f}%" if d["mean_accuracy_pct"] is not None else "-",
            })

        st.dataframe(pd.DataFrame(dept_table_rows), width="stretch", hide_index=True)
