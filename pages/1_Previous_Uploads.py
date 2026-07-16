"""
pages/1_Previous_Uploads.py — Upload history, available dates, and the
"load a historical date as active" workflow.
"""

from __future__ import annotations

import streamlit as st

from modules import database, date_picker
from modules.formatting import format_money, format_pax
from modules.session import bootstrap_session, set_active_date, set_compare_date

st.set_page_config(page_title="Previous Uploads", page_icon="📂", layout="wide")

bootstrap_session()

st.title("📂 Previous Uploads")

# ---------------------------------------------------------------------------
# Upload History Table
# ---------------------------------------------------------------------------

st.header("Upload History")

history_df = database.get_upload_history()
if history_df.empty:
    st.info("No files have been uploaded yet. Go to the main page to upload a report.")
else:
    st.caption(
        "Shows every upload — daily/historical revenue reports, AOP target workbooks, "
        "and traffic files. **Total Value** is Total Revenue for Revenue uploads, the "
        "total AOP target for AOP uploads, or total traffic for Traffic uploads."
    )
    display_df = history_df.copy()
    if "upload_type" not in display_df.columns:
        display_df["upload_type"] = "Revenue"  # pre-migration rows
    display_df["total_revenue"] = display_df["total_revenue"].apply(format_money)
    display_df["total_pax"] = display_df.apply(
        lambda r: format_pax(r["total_pax"]) if r["upload_type"] == "Revenue" else "—", axis=1
    )
    display_df = display_df.rename(
        columns={
            "upload_type": "Type",
            "file_name": "File Name",
            "report_date": "Report Date",
            "row_count": "Rows",
            "total_revenue": "Total Value",
            "total_pax": "Total PAX",
            "uploaded_at": "Uploaded At",
            "status": "Status",
        }
    )[
        ["Type", "File Name", "Report Date", "Rows", "Total Value", "Total PAX", "Uploaded At", "Status"]
    ]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Available Dates in Database
# ---------------------------------------------------------------------------

st.header("Available Dates in Database")

dates_summary = database.get_dates_summary()
if dates_summary.empty:
    st.info("No revenue data stored yet.")
else:
    display_dates = dates_summary.copy()
    display_dates["total_revenue"] = display_dates["total_revenue"].apply(format_money)
    display_dates["total_pax"] = display_dates["total_pax"].apply(format_pax)
    display_dates = display_dates.rename(
        columns={
            "date": "Date",
            "total_revenue": "Total Revenue",
            "total_pax": "Total PAX",
            "outlets": "Outlets",
        }
    )
    st.dataframe(display_dates, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Preview Uploaded Data
# ---------------------------------------------------------------------------

st.header("🔍 Preview Uploaded Data")
st.caption("Spot-check what is actually stored in the database for Revenue, AOP, and Traffic.")

available_dates = database.get_available_dates()

preview_tab1, preview_tab2 = st.tabs(["📅 Single Date", "📆 Date Range"])

with preview_tab1:
    if not available_dates:
        st.info("No revenue data yet. Upload a report on the Home page.")
    else:
        single_date = date_picker.render_date_dropdown(
            available_dates, key_prefix="preview_single", label="Date to preview"
        )
        single_df = database.load_for_date(single_date)
        if single_df.empty:
            st.warning(f"No rows found for {single_date}.")
        else:
            s1, s2, s3 = st.columns(3)
            s1.metric("Rows", f"{len(single_df):,}")
            s2.metric("Total Revenue", format_money(single_df["revenue"].sum()))
            s3.metric("Total PAX", format_pax(single_df["pax"].sum()))

            # Enrich display copy with AOP and per-outlet terminal traffic
            # (joins are done on a copy AFTER metrics so original pax/revenue stays intact)
            display_single = single_df.copy()
            try:
                _aop = database.join_revenue_with_aop(single_df)
                if _aop is not None and not _aop.empty and "aop_target" in _aop.columns:
                    display_single = display_single.merge(
                        _aop[["outlet","location","aop_target"]].rename(columns={"aop_target":"aop"}),
                        on=["outlet","location"], how="left"
                    )
            except Exception:
                pass
            try:
                _traf = database.join_revenue_with_traffic_by_outlet(single_df)
                if _traf is not None and not _traf.empty and "traffic" in _traf.columns:
                    display_single = display_single.drop(columns=["traffic"], errors="ignore").merge(
                        _traf[["outlet","location","traffic"]],
                        on=["outlet","location"], how="left"
                    )
            except Exception:
                pass
            location_filter = st.multiselect("Filter by location",
                options=sorted(single_df["location"].unique()), default=[],
                key="preview_single_location_filter")
            if location_filter:
                display_single = display_single[display_single["location"].isin(location_filter)]
            display_single = display_single.drop(
                columns=[c for c in ["id","uploaded_at"] if c in display_single.columns],
                errors="ignore"
            )
            display_single["revenue"] = display_single["revenue"].apply(format_money)
            display_single["pax"]     = display_single["pax"].apply(format_pax)
            if "aop_target" in display_single.columns:
                display_single = display_single.rename(columns={"aop_target": "aop"})
            if "aop" in display_single.columns:
                display_single["aop"] = display_single["aop"].apply(
                    lambda v: format_money(v) if v is not None and str(v) != "None" and v == v else "—"
                )
            if "traffic" in display_single.columns:
                display_single["traffic"] = display_single["traffic"].apply(
                    lambda v: format_pax(v) if v is not None and str(v) != "None" and v == v else "—"
                )
            display_single = display_single.rename(columns={
                "date":"Date","segment":"Segment","business_unit":"Business Unit",
                "outlet":"Outlet","location":"Location","pax":"PAX","revenue":"Revenue",
                "aop":"AOP","traffic":"Traffic","source_file":"Source File"})
            st.dataframe(display_single, use_container_width=True, hide_index=True)

with preview_tab2:
    if not available_dates:
        st.info("No revenue data yet.")
    else:
        range_start = date_picker.render_date_dropdown(
            available_dates, key_prefix="preview_range_start", label="From",
            default_date=min(available_dates))
        end_options = [d for d in available_dates if d >= range_start]
        range_end = date_picker.render_date_dropdown(
            end_options, key_prefix="preview_range_end", label="To",
            default_date=max(end_options))
        range_df = database.load_for_date_range(range_start, range_end)
        if range_df.empty:
            st.warning(f"No rows found between {range_start} and {range_end}.")
        else:
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Days Covered", f"{range_df['date'].nunique():,}")
            r2.metric("Rows", f"{len(range_df):,}")
            r3.metric("Total Revenue", format_money(range_df["revenue"].sum()))
            r4.metric("Total PAX", format_pax(range_df["pax"].sum()))
            view_mode = st.radio("View as", ["Daily totals","Full row detail"],
                                 index=0, horizontal=True, key="preview_range_view_mode")
            if view_mode == "Daily totals":
                dt = range_df.groupby("date",as_index=False).agg(
                    revenue=("revenue","sum"),pax=("pax","sum")).sort_values("date")
                dt["revenue"] = dt["revenue"].apply(format_money)
                dt["pax"] = dt["pax"].apply(format_pax)
                st.dataframe(dt.rename(columns={"date":"Date","revenue":"Revenue","pax":"PAX"}),
                             use_container_width=True, hide_index=True)
            else:
                loc_f = st.multiselect("Filter by location",
                    options=sorted(range_df["location"].unique()), default=[],
                    key="preview_range_location_filter")
                dr = range_df[range_df["location"].isin(loc_f)].copy() if loc_f else range_df.copy()
                # Enrich display copy with AOP and per-outlet terminal traffic
                try:
                    _aop2 = database.join_revenue_with_aop(dr)
                    if _aop2 is not None and not _aop2.empty and "aop_target" in _aop2.columns:
                        dr = dr.merge(
                            _aop2[["outlet","location","aop_target"]].rename(columns={"aop_target":"aop"}),
                            on=["outlet","location"], how="left"
                        )
                except Exception:
                    pass
                try:
                    _traf2 = database.join_revenue_with_traffic_by_outlet(dr)
                    if _traf2 is not None and not _traf2.empty and "traffic" in _traf2.columns:
                        dr = dr.drop(columns=["traffic"], errors="ignore").merge(
                            _traf2[["outlet","location","traffic"]],
                            on=["outlet","location"], how="left"
                        )
                except Exception:
                    pass
                dr = dr.drop(
                    columns=[c for c in ["id","uploaded_at"] if c in dr.columns],
                    errors="ignore"
                )
                dr["revenue"] = dr["revenue"].apply(format_money)
                dr["pax"]     = dr["pax"].apply(format_pax)
                if "aop_target" in dr.columns:
                    dr = dr.rename(columns={"aop_target": "aop"})
                if "aop" in dr.columns:
                    dr["aop"] = dr["aop"].apply(
                        lambda v: format_money(v) if v is not None and str(v) != "None" and v == v else "—"
                    )
                if "traffic" in dr.columns:
                    dr["traffic"] = dr["traffic"].apply(
                        lambda v: format_pax(v) if v is not None and str(v) != "None" and v == v else "—"
                    )
                dr = dr.rename(columns={"date":"Date","segment":"Segment","business_unit":"Business Unit",
                    "outlet":"Outlet","location":"Location","pax":"PAX","revenue":"Revenue",
                    "aop":"AOP","traffic":"Traffic","source_file":"Source File"})
                st.dataframe(dr, use_container_width=True, hide_index=True)



st.divider()

st.header("Load Historical Date")
st.caption(
    "Set any date already in the database as the active analysis date for "
    "the other pages — no re-uploading required."
)

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No dates available yet. Upload a report or import historical data first.")
else:
    selected_date = date_picker.render_date_dropdown(
        available_dates, key_prefix="load_date", label="Date"
    )
    if st.button("📥 Load Selected Date", type="primary"):
        set_active_date(selected_date)
        comparison_dates = database.find_comparison_dates(selected_date)
        nearest_compare = comparison_dates.get("yesterday")
        if nearest_compare:
            set_compare_date(nearest_compare)
        st.success(
            f"**{selected_date}** is now the active analysis date. "
            f"Head to **Executive Summary**, **Revenue Comparison**, or "
            f"**Outlet Performance** to explore it."
        )
        comparisons_found = {k: v for k, v in comparison_dates.items() if v is not None}
        if comparisons_found:
            st.caption(
                "Auto-detected comparison dates: "
                + ", ".join(f"{k.replace('_', ' ').title()} → {v}" for k, v in comparisons_found.items())
            )
