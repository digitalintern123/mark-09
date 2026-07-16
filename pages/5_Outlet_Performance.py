"""
pages/5_Outlet_Performance.py — Top/bottom 10 outlets, a diverging
change-by-outlet chart, and a Revenue per PAX table — all driven by the
same anchor date + comparison-type selector (Day/Week/Month/Year-wise,
Full Period/To-Date) used across the rest of the app, rather than a
fixed "yesterday" comparison.
"""

from __future__ import annotations

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from modules import comparison_widget, database, date_picker, revenue_analysis as ra
from modules.formatting import format_money, format_pax, format_rev_per_pax
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Outlet Performance", page_icon="🏪", layout="wide")

bootstrap_session()

st.title("🏪 Outlet Performance")

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No data available yet. Upload a report on the main page first.")
    st.stop()

anchor_date = date_picker.render_date_dropdown(
    available_dates,
    key_prefix="outlet_perf_anchor",
    label="Anchor Date (defines the 'current' period)",
    default_date=default_active_date(),
)

ranges = comparison_widget.render_comparison_selector(anchor_date, key_prefix="outlet_perf")
current_short_label, compare_short_label = ra.short_period_label_for_ranges(ranges)

set_active_date(anchor_date)

current_df = database.load_for_date_range(ranges["current_start"], ranges["current_end"])
compare_df = database.load_for_date_range(ranges["compare_start"], ranges["compare_end"])

st.caption(f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**.")

if current_df.empty:
    st.warning("No revenue rows found for the current period. Try a different date/period.")
    st.stop()

has_compare = compare_df is not None and not compare_df.empty

# ---------------------------------------------------------------------------
# Top 10 / Bottom 10 Outlets
# ---------------------------------------------------------------------------

st.subheader("Top 10 / Bottom 10 Outlets by Revenue")

with safe_run("Top Bottom Outlets"):
    result = ra.top_bottom_outlets(current_df, compare_df if has_compare else None, n=10)
top, bottom = result["top"], result["bottom"]

# FIX: use "Outlet (Location)" as Y-axis label so same-named outlets in
# different cities (e.g. Encalm Sky Plates Delhi vs Hyderabad) render as
# separate bars instead of overlapping on the same Y-axis tick.
def _outlet_label(df):
    if "location" in df.columns:
        return df["outlet"] + " (" + df["location"] + ")"
    return df["outlet"]

chart_col1, chart_col2 = st.columns(2)
with chart_col1:
    top_sorted = top.sort_values("current_revenue").copy()
    top_sorted["_label"] = _outlet_label(top_sorted)
    fig_top = px.bar(
        top_sorted, x="current_revenue", y="_label", orientation="h",
        text=top_sorted["current_revenue"].apply(format_money),
        title="Top 10",
        color_discrete_sequence=["#2E8B57"],
    )
    fig_top.update_traces(marker_color="#2E8B57")
    fig_top.update_layout(xaxis_title="Revenue", yaxis_title="")
    st.plotly_chart(fig_top, use_container_width=True)

with chart_col2:
    bottom_sorted = bottom.sort_values("current_revenue").copy()
    bottom_sorted["_label"] = _outlet_label(bottom_sorted)
    fig_bottom = px.bar(
        bottom_sorted, x="current_revenue", y="_label", orientation="h",
        text=bottom_sorted["current_revenue"].apply(format_money),
        title="Bottom 10",
    )
    fig_bottom.update_traces(marker_color="#C0392B")
    fig_bottom.update_layout(xaxis_title="Revenue", yaxis_title="")
    st.plotly_chart(fig_bottom, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Change by Outlet (diverging chart)
# ---------------------------------------------------------------------------

st.subheader(f"Change by Outlet — {current_short_label} vs {compare_short_label}")

if has_compare:
    comparison = ra.compare_periods(current_df, compare_df)
    comparison = comparison[comparison["revenue_change"] != 0]
    comparison = comparison.sort_values("revenue_change")

    if comparison.empty:
        st.caption("No outlet-level revenue changes versus the compare period.")
    else:
        colors = ["#C0392B" if v < 0 else "#2E8B57" for v in comparison["revenue_change"]]
        fig_change = go.Figure(
            go.Bar(
                x=comparison["revenue_change"],
                y=comparison["outlet"] + " (" + comparison["location"] + ")",
                orientation="h",
                marker_color=colors,
                text=[format_money(v) for v in comparison["revenue_change"]],
                textposition="outside",
            )
        )
        fig_change.update_layout(
            xaxis_title=f"Revenue Change vs {compare_short_label}",
            yaxis_title="",
            height=max(400, 22 * len(comparison)),
            shapes=[dict(type="line", x0=0, x1=0, y0=-0.5, y1=len(comparison) - 0.5, line=dict(color="gray", width=1))],
        )
        st.plotly_chart(fig_change, use_container_width=True)
else:
    st.info("No data available for the compare period yet.")

st.divider()

# ---------------------------------------------------------------------------
# Revenue per PAX Table
# ---------------------------------------------------------------------------

st.subheader(f"Revenue per PAX — {current_short_label}")

with safe_run("Revenue per PAX"):
    rpp_table = ra.revenue_per_pax_table(current_df)
display = rpp_table.copy()
display["revenue"] = display["revenue"].apply(format_money)
display["pax"] = display["pax"].apply(format_pax)
display["rev_per_pax"] = display["rev_per_pax"].apply(format_rev_per_pax)
display = display.rename(
    columns={
        "segment": "Segment", "outlet": "Outlet", "location": "Location",
        "revenue": "Revenue", "pax": "PAX", "rev_per_pax": "Rev/PAX",
    }
)
st.dataframe(display, use_container_width=True, hide_index=True)
