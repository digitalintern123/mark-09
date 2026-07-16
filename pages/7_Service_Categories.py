"""
pages/7_Service_Categories.py — Per-business tabs for Encalm Group's three
top-level segments (EHPL, Sky Plates, Encalm Eats), with EHPL further
broken out by its Lounges / Atithya / Others business units. Shows trend,
AOP variance, and location breakdown, driven by the same anchor date +
comparison-type selector (Day/Week/Month/Year-wise, Full Period/To-Date)
used across the rest of the app. Penetration % and SPP are shown once
Traffic data becomes available; until then a clear placeholder is shown
instead of empty/misleading charts.
"""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from modules import comparison_widget, database, date_picker, revenue_analysis as ra, table_style
from modules.formatting import format_money, format_pax, format_pct, format_spp
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Service Categories", page_icon="🏢", layout="wide")

bootstrap_session()

st.title("🏢 Service Categories")
st.caption(
    "EHPL (Encalm Hospitality Private Ltd) is the largest segment, covering "
    "Lounges, Atithya, and Other services. Sky Plates and Encalm Eats are "
    "tracked as separate businesses."
)

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No data available yet. Upload a report on the main page first.")
    st.stop()

anchor_date = date_picker.render_date_dropdown(
    available_dates,
    key_prefix="service_cat_anchor",
    label="Anchor Date (defines the 'current' period)",
    default_date=default_active_date(),
)

ranges = comparison_widget.render_comparison_selector(anchor_date, key_prefix="service_cat")
current_short_label, compare_short_label = ra.short_period_label_for_ranges(ranges)

set_active_date(anchor_date)

current_df = database.load_for_date_range(ranges["current_start"], ranges["current_end"])
compare_df = database.load_for_date_range(ranges["compare_start"], ranges["compare_end"])

st.caption(f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**.")

if current_df.empty:
    st.warning("No revenue rows found for the current period. Try a different date/period.")
    st.stop()

CATEGORY_FILTERS = {
    "Total Airport Services": lambda df: df,
    "EHPL — All": lambda df: df[df["segment"] == "EHPL"],
    "EHPL — Lounges": lambda df: df[df["business_unit"] == "Lounges"],
    "EHPL — Atithya": lambda df: df[df["business_unit"] == "Atithya"],
    "EHPL — Others": lambda df: df[df["business_unit"] == "Others"],
    "Sky Plates": lambda df: df[df["segment"] == "Sky Plates"],
    "Encalm Eats": lambda df: df[df["segment"] == "Encalm Eats"],
}

tabs = st.tabs(list(CATEGORY_FILTERS.keys()))

for tab, (label, filter_fn) in zip(tabs, CATEGORY_FILTERS.items()):
    with tab:
      with safe_run(f"Service Category tab {label}", error_type="comparison_error"):
        cat_df = filter_fn(current_df)
        cat_compare_df = filter_fn(compare_df) if compare_df is not None and not compare_df.empty else None

        if cat_df.empty:
            st.info(f"No data for **{label}** in {ranges['current_label']}.")
            continue

        summary = ra.summarize_period(cat_df)
        m1, m2, m3 = st.columns(3)
        m1.metric(f"Revenue ({current_short_label})", format_money(summary["revenue"]))
        m2.metric(f"PAX ({current_short_label})", format_pax(summary["pax"]))

        if cat_compare_df is not None and not cat_compare_df.empty:
            change = ra.day_over_day(cat_df, cat_compare_df)
            m3.metric(
                f"Revenue Change ({compare_short_label})",
                format_money(change["revenue_change"]),
                **table_style.metric_delta_args(change["revenue_pct_change"]),
            )
        else:
            m3.metric("Revenue Change", "—")

        st.markdown("**AOP Variance**")
        aop_joined = database.join_revenue_with_aop(cat_df)
        if aop_joined is not None and not aop_joined.empty and aop_joined["aop_target"].notna().any():
            aop_work = aop_joined.rename(columns={"aop_target": "aop"})
            aop_row = ra.aop_variance(aop_work).iloc[0]
            if aop_row["aop_target"] and aop_row["aop_target"] != 0:
                a1, a2, a3 = st.columns(3)
                a1.metric("Actual", format_money(aop_row["actual_revenue"]))
                a2.metric("AOP Target", format_money(aop_row["aop_target"]))
                a3.metric("Variance", format_money(aop_row["variance"]), delta=format_pct(aop_row["variance_pct"]))
            else:
                st.caption("AOP target not set for this category/period.")
        else:
            st.caption("No AOP data available — upload an AOP workbook on the main page to see this.")

        st.markdown("**Penetration % / SPP**")
        # Use per-outlet terminal traffic (not whole-airport total)
        outlet_traffic = database.join_revenue_with_traffic_by_outlet(cat_df)
        if outlet_traffic is not None and not outlet_traffic.empty and "traffic" in outlet_traffic.columns:
            # Merge per-outlet terminal traffic back to cat_df for PEN/SPP calculation
            traffic_joined = cat_df.drop(columns=["traffic"], errors="ignore").merge(
                outlet_traffic[["outlet", "location", "traffic"]],
                on=["outlet", "location"], how="left"
            )
        else:
            traffic_joined = cat_df
        if ra.has_traffic_data(traffic_joined):
            pen_table = ra.penetration_and_spp_table(traffic_joined).copy()
            if "spp" in pen_table.columns:
                pen_table["spp"] = pen_table["spp"].apply(format_spp)
            st.dataframe(pen_table, use_container_width=True, hide_index=True)
        else:
            st.info(
                "📡 No traffic data loaded for this period yet — Penetration % (PAX ÷ Traffic) "
                "and SPP (Revenue ÷ Traffic) will appear here once airport traffic data is "
                "uploaded for this period."
            )

        st.markdown("**Location-wise Breakdown**")
        loc_breakdown = cat_df.groupby("location", as_index=False).agg(
            revenue=("revenue", "sum"), pax=("pax", "sum")
        ).sort_values("revenue", ascending=True)
        fig = px.bar(
            loc_breakdown, x="revenue", y="location", orientation="h",
            text=loc_breakdown["revenue"].apply(format_money),
            color="location", color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig.update_layout(showlegend=False, xaxis_title="Revenue", yaxis_title="")
        st.plotly_chart(fig, use_container_width=True, key=f"chart_{label}")
