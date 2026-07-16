"""
pages/6_Business_Insights.py — One-click rule-based management narrative
driven entirely by the user-selected Compare Date, plus a Volume vs Spend
driver table using the same comparison selector.
"""

from __future__ import annotations

import streamlit as st

from modules import comparison_widget, database, date_picker, insights, revenue_analysis as ra, table_style
from modules.formatting import format_money, format_pax, format_pct
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Business Insights", page_icon="🤖", layout="wide")

bootstrap_session()

st.title("🤖 Business Insights")

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No data available yet. Upload a report on the main page first.")
    st.stop()

selected_date = date_picker.render_date_dropdown(
    available_dates, key_prefix="biz_insights", label="Report Date", default_date=default_active_date()
)

st.caption(
    "The **Compare Date** selector below drives every section of the management narrative "
    "(Executive Summary, Top/Bottom Performers, Driver Analysis, Footfall, Segment Summary). "
    "DoD/MoM/YoY reference panels appear as additional context only when they differ from your selection."
)
driver_ranges = comparison_widget.render_comparison_selector(selected_date, key_prefix="insights")
driver_current_df = database.load_for_date_range(driver_ranges["current_start"], driver_ranges["current_end"])
driver_compare_df = database.load_for_date_range(driver_ranges["compare_start"], driver_ranges["compare_end"])

set_active_date(selected_date)

current_df = database.load_for_date(selected_date)
if current_df.empty:
    st.warning(f"No revenue rows found for {selected_date}.")
    st.stop()

st.divider()

# ---------------------------------------------------------------------------
# Management narrative — ALL sections driven by the user-selected compare date
# ---------------------------------------------------------------------------

st.subheader("📝 Management Summary")

comparison_dates = database.find_comparison_dates(selected_date)
yesterday_date = comparison_dates.get("yesterday")
last_month_date = comparison_dates.get("last_month")
last_year_date = comparison_dates.get("last_year")

yesterday_df = database.load_for_date(yesterday_date) if yesterday_date else None
last_month_df = database.load_for_date(last_month_date) if last_month_date else None
last_year_df = database.load_for_date(last_year_date) if last_year_date else None

st.caption(
    "Selected compare: **"
    + str(driver_ranges.get("compare_label", driver_ranges["compare_start"]))
    + "** | Reference dates — "
    + (", ".join(
        f"{label} → {d}"
        for label, d in [
            ("DoD", yesterday_date),
            ("MoM", last_month_date),
            ("YoY", last_year_date),
        ]
        if d is not None
    ) or "none found yet")
)

if st.button("📝 Generate Summary", type="primary"):
    with st.spinner("Generating management summary..."):
        try:
            sections = insights.generate_summary(
            current_df,
            yesterday_df=yesterday_df,
            last_month_df=last_month_df,
            last_year_df=last_year_df,
            exec_compare_df=driver_compare_df,
        )
            st.session_state["_insights_sections"] = sections
        except Exception as _e:
            log_exception(_e, context="Generate Summary")
            show_friendly_error("generic")
            sections = None
    st.session_state["_insights_sections_key"] = (
        selected_date, yesterday_date, last_month_date, last_year_date,
        driver_ranges["compare_start"], driver_ranges["compare_end"],
    )

# Invalidate cached summary whenever the date or comparison dates change
_current_key = (
    selected_date, yesterday_date, last_month_date, last_year_date,
    driver_ranges["compare_start"], driver_ranges["compare_end"],
)
if st.session_state.get("_insights_sections_key") != _current_key:
    st.session_state.pop("_insights_sections", None)
    st.session_state.pop("_insights_sections_key", None)

sections = st.session_state.get("_insights_sections")
if sections:
    for section in sections:
        st.markdown(section, unsafe_allow_html=True)
        st.divider()
else:
    st.info("Click **Generate Summary** to produce the management narrative for this date.")

st.divider()

# ---------------------------------------------------------------------------
# Volume vs Spend Driver Table (driven by the comparison selector above)
# ---------------------------------------------------------------------------

st.subheader("Volume vs Spend Driver Analysis")
st.caption(
    f"Comparing **{driver_ranges['current_label']}** against **{driver_ranges['compare_label']}**."
)

if not driver_compare_df.empty and not driver_current_df.empty:
  with safe_run("Volume vs Spend", error_type="comparison_error"):
    driver_table = ra.volume_vs_spend_table(driver_current_df, driver_compare_df)
    # volume_vs_spend_table already excludes outlets with no pct change (both periods zero)
    # Filter: hide rows where revenue_pct_change is None AND pax_pct_change is None
    # (means both periods had zero revenue and zero PAX — no meaningful data)
    driver_table = driver_table[
        driver_table["revenue_pct_change"].notna() |
        driver_table["pax_pct_change"].notna()
    ]
    driver_delta_suffix = table_style.COMPARISON_TYPE_SHORT.get(
        driver_ranges["comparison_type"], driver_ranges["comparison_type"]
    )
    driver_table, driver_traffic_pen_cols = table_style.add_location_traffic_pen_columns(
        driver_table, database, ra, driver_current_df, driver_compare_df,
        driver_ranges["current_label"], driver_ranges["compare_label"], driver_delta_suffix,
    )
    driver_table, driver_aop_cols = table_style.add_aop_columns(
        driver_table, database, ra, driver_current_df, ["segment", "outlet", "location"], driver_ranges["current_label"]
    )
    display = driver_table.copy()
    display["pax_pct_change"] = display["pax_pct_change"].apply(format_pct)
    display["revenue_pct_change"] = display["revenue_pct_change"].apply(format_pct)
    display["rev_per_pax_pct_change"] = display["rev_per_pax_pct_change"].apply(format_pct)
    display = table_style.format_traffic_pen_columns(display, driver_traffic_pen_cols, format_pax, format_money)
    display = table_style.format_aop_columns(display, driver_aop_cols, format_money)
    display = display.rename(
        columns={
            "segment": "Segment", "outlet": "Outlet", "location": "Location",
            "pax_pct_change": "PAX Δ%", "revenue_pct_change": "Revenue Δ%",
            "rev_per_pax_pct_change": "Rev/Pax Δ%", "driver": "Driver",
        }
    )
    driver_filter = st.multiselect(
        "Filter by driver type",
        options=["Volume-driven", "Spend-driven", "Mixed", "Flat"],
        default=["Volume-driven", "Spend-driven", "Mixed", "Flat"],
    )
    filtered = display[display["Driver"].isin(driver_filter)]
    driver_pct_cols = ["PAX Δ%", "Revenue Δ%", "Rev/Pax Δ%"] + [c for c in driver_aop_cols if "Variance" in c] + [c for c in driver_traffic_pen_cols if "Δ%" in c]
    st.dataframe(
        table_style.style_pct_columns(filtered, driver_pct_cols),
        use_container_width=True,
        hide_index=True,
    )
    if driver_traffic_pen_cols:
        st.caption(
            "ℹ️ Traffic and PEN (Penetration %) use each outlet's own terminal traffic — "
            "each outlet shows its location's overall Traffic/PEN/SPP figures."
        )
else:
    st.info("No data available for the selected comparison period yet.")
