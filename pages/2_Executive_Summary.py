"""
pages/2_Executive_Summary.py — KPI cards, segment growth/decline indicators,
revenue/PAX charts by segment and location, and AOP achievement. Supports
Week-wise / Month-wise / Year-wise comparison via the shared selector.
"""

from __future__ import annotations

import plotly.express as px
import streamlit as st
import pandas as pd

from modules import comparison_widget, database, date_picker, revenue_analysis as ra
from modules.formatting import format_money, format_pax, format_pct
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Executive Summary", page_icon="📈", layout="wide")

bootstrap_session()

st.title("📈 Executive Summary")

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No data available yet. Upload a report on the main page first.")
    st.stop()

selected_date = date_picker.render_date_dropdown(
    available_dates, key_prefix="exec_summary", label="Report Date", default_date=default_active_date()
)
set_active_date(selected_date)

ranges = comparison_widget.render_comparison_selector(selected_date, key_prefix="exec_summary")
comparison_type = ranges["comparison_type"]

current_df = database.load_for_date_range(ranges["current_start"], ranges["current_end"])
compare_df = database.load_for_date_range(ranges["compare_start"], ranges["compare_end"])

st.caption(f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**.")

if current_df.empty:
    st.warning(f"No revenue rows found for {ranges['current_label']}.")
    st.stop()

# ---------------------------------------------------------------------------
# KPI Cards
# ---------------------------------------------------------------------------

with safe_run("Executive Summary KPI computation"):
    current_summary = ra.summarize_period(current_df)
    comp = ra.compare_two_periods_summary(current_df, compare_df) if not compare_df.empty else None
    delta_label = {"Day-wise": "DoD", "Week-wise": "WoW", "Month-wise": "MoM", "Year-wise": "YoY"}[comparison_type]
if "current_summary" not in dir():
    current_summary = {"revenue": 0, "pax": 0}
if "comp" not in dir():
    comp = None
if "delta_label" not in dir():
    delta_label = "Change"

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    f"Total Revenue ({ranges['current_label']})",
    format_money(current_summary["revenue"]),
    delta=format_pct(comp["revenue_pct_change"]) if comp else None,
)
k2.metric(
    f"Compare Revenue ({ranges['compare_label']})",
    format_money(comp["previous_revenue"]) if comp else "—",
)
k3.metric(
    f"PAX ({ranges['current_label']})",
    format_pax(current_summary["pax"]),
    delta=format_pct(comp["pax_pct_change"]) if comp else None,
)
k4.metric(
    f"{delta_label} Growth %",
    format_pct(comp["revenue_pct_change"]) if comp else "—",
)

if compare_df.empty:
    st.caption(
        f"ℹ️ No data found for the comparison period ({ranges['compare_label']}) yet — "
        f"{delta_label} comparisons will appear once that period has data loaded."
    )

st.divider()

# ---------------------------------------------------------------------------
# Segment Growth / Decline Indicators
# ---------------------------------------------------------------------------

if not compare_df.empty:
    growth = ra.top_growing_declining_segments(current_df, compare_df)
    if growth["top_growing"] is not None:
        g, d = growth["top_growing"], growth["top_declining"]
        c1, c2 = st.columns(2)
        c1.metric("🔺 Top Growing Segment", g["segment"], delta=format_pct(g["revenue_pct_change"]))
        c2.metric("🔻 Top Declining Segment", d["segment"], delta=format_pct(d["revenue_pct_change"]))
        st.divider()

# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

chart_col1, chart_col2 = st.columns(2)

segment_totals = current_df.groupby("segment", as_index=False).agg(
    revenue=("revenue", "sum"), pax=("pax", "sum")
)

with chart_col1:
    st.subheader("Revenue by Segment")
    fig_rev = px.pie(
        segment_totals, names="segment", values="revenue", hole=0.45,
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig_rev.update_traces(textinfo="percent+label")
    st.plotly_chart(fig_rev, use_container_width=True)

with chart_col2:
    st.subheader("PAX by Segment")
    fig_pax = px.pie(
        segment_totals, names="segment", values="pax", hole=0.45,
        color_discrete_sequence=px.colors.qualitative.Pastel,
    )
    fig_pax.update_traces(textinfo="percent+label")
    st.plotly_chart(fig_pax, use_container_width=True)

st.subheader("Revenue by Location")
location_totals = (
    current_df.groupby("location", as_index=False)
    .agg(revenue=("revenue", "sum"))
    .sort_values("revenue", ascending=True)
)
fig_loc = px.bar(
    location_totals, x="revenue", y="location", orientation="h",
    text=location_totals["revenue"].apply(format_money),
    color="location", color_discrete_sequence=px.colors.qualitative.Set2,
)
fig_loc.update_layout(showlegend=False, xaxis_title="Revenue", yaxis_title="")
st.plotly_chart(fig_loc, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# AOP Section
# ---------------------------------------------------------------------------

st.subheader("🎯 AOP Target vs Actual")

has_legacy_aop = "aop" in current_df.columns and current_df["aop"].notna().any()

if has_legacy_aop:
    overall_aop = ra.aop_variance(current_df)
else:
    aop_joined = database.join_revenue_with_aop(current_df)
    if aop_joined["aop_target"].notna().any():
        # Drop the stale (all-NaN, since has_legacy_aop is False here)
        # legacy `aop` column before renaming `aop_target` onto that name
        # — otherwise the DataFrame ends up with two columns both named
        # "aop", and a later `df["aop"]` lookup returns a DataFrame
        # instead of a Series, which breaks pd.to_numeric downstream.
        aop_joined = aop_joined.drop(columns=["aop"], errors="ignore")
        aop_joined = aop_joined.rename(columns={"aop_target": "aop"})
        overall_aop = ra.aop_variance(aop_joined)
    else:
        overall_aop = None

# Third tier: the simpler daily-total-per-location AOP source (no outlet
# breakdown at all). Only used when neither the legacy per-row aop column
# nor the per-outlet/monthly aop_target table has anything for this
# period — this source can only ever support a whole-total comparison,
# never a per-outlet one, since it has no outlet dimension to begin with.
aop_is_estimated = False
aop_missing_days = 0
if overall_aop is None or overall_aop.empty or not overall_aop.iloc[0]["aop_target"]:
    daily_targets = database.get_aop_target_for_range(ranges["current_start"], ranges["current_end"])
    total_target = sum(v["aop_target"] for v in daily_targets.values())
    if total_target:
        actual_total = current_summary["revenue"]
        variance = actual_total - total_target
        variance_pct = ra.pct_change(actual_total, total_target)
        overall_aop = pd.DataFrame(
            [{"actual_revenue": actual_total, "aop_target": total_target, "variance": variance, "variance_pct": variance_pct}]
        )
        aop_is_estimated = any(v["is_estimated"] for v in daily_targets.values())
        aop_missing_days = sum(v["missing_days"] for v in daily_targets.values())

if overall_aop is not None and not overall_aop.empty:
    row = overall_aop.iloc[0]
    if row["aop_target"] and row["aop_target"] != 0:
        achievement_pct = ra.safe_div(row["actual_revenue"], row["aop_target"]) * 100
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("AOP Target", format_money(row["aop_target"]))
        a2.metric("Actual Revenue", format_money(row["actual_revenue"]))
        a3.metric("Achievement %", f"{achievement_pct:.1f}%" if achievement_pct == achievement_pct else "—")
        a4.metric("Variance", format_money(row["variance"]), delta=format_pct(row["variance_pct"]))
        if aop_is_estimated:
            st.caption("ℹ️ Part of this target is estimated by prorating a monthly figure (no daily AOP data for that stretch).")
        if aop_missing_days:
            st.caption(f"⚠️ {aop_missing_days} day(s) in this period have no AOP target from any source — the target above is understated.")
    else:
        st.caption("AOP target is not set for this date.")
else:
    st.caption(
        "ℹ️ No AOP data available for this date. Upload an AOP workbook on the main "
        "page to unlock this."
    )

