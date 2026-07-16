"""
pages/3_Revenue_Comparison.py — Intelligent comparison: choose Week-wise,
Month-wise, or Year-wise comparison, with tabs for detailed outlet-level
comparison, segment summary, location summary, Penetration %/SPP, and
a new Domestic vs International breakdown tab.

Also includes AOP (Annual Operating Plan) target tracking directly in
each table — AOP Target and Variance (AOP vs Actuals) columns for the
current period, alongside the period-over-period Revenue/PAX comparison
— rather than as a separate page, since both were answering closely
related questions about the same underlying data.
"""

from __future__ import annotations

import streamlit as st
import pandas as pd

from modules import comparison_widget, database, date_picker, revenue_analysis as ra, table_style
from modules.formatting import format_money, format_pax, format_pct, format_spp
from modules.session import bootstrap_session, default_active_date, set_active_date, set_compare_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Revenue Comparison", page_icon="🔄", layout="wide")

bootstrap_session()

st.title("🔄 Revenue Comparison")
st.caption(
    "Includes AOP (Annual Operating Plan) target tracking — each table below shows "
    "AOP Target and Variance (AOP vs Actuals) for the current period alongside the "
    "Revenue/PAX comparison, wherever AOP data is available."
)

available_dates = database.get_available_dates()
if len(available_dates) < 1:
    st.info("No data available yet. Upload a report on the main page first.")
    st.stop()

anchor_date = date_picker.render_date_dropdown(
    available_dates,
    key_prefix="cmp_anchor_date",
    label="Anchor Date (defines the 'current' period)",
    default_date=default_active_date(),
)

ranges = comparison_widget.render_comparison_selector(anchor_date, key_prefix="rev_cmp")

current_short_label, compare_short_label = ra.short_period_label_for_ranges(ranges)
delta_suffix = table_style.COMPARISON_TYPE_SHORT.get(ranges["comparison_type"], ranges["comparison_type"])

set_active_date(anchor_date)
set_compare_date(ranges["compare_start"])

current_df = database.load_for_date_range(ranges["current_start"], ranges["current_end"])
compare_df = database.load_for_date_range(ranges["compare_start"], ranges["compare_end"])

st.caption(
    f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**."
)

if current_df.empty or compare_df.empty:
    st.warning(
        "One of the selected periods has no revenue rows in the database. "
        "Try a different date/period, or upload more data."
    )
    st.stop()

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "Detailed Comparison",
        "Segment Summary",
        "Location → Services Summary",
        "Penetration % / SPP by Location",
        "🏠 Domestic vs International",
    ]
)

# ---------------------------------------------------------------------------
# Tab 1 — Detailed outlet-level comparison
# ---------------------------------------------------------------------------

with tab1:
  try:
    comparison = ra.compare_periods(current_df, compare_df)

    available_locations_tab1 = sorted(comparison["location"].dropna().unique())
    selected_locations_tab1 = st.multiselect(
        "📍 Filter by Location",
        options=available_locations_tab1,
        default=available_locations_tab1,
        key="tab1_location_filter",
    )
    if selected_locations_tab1:
        comparison = comparison[comparison["location"].isin(selected_locations_tab1)]
        filtered_current_df_tab1 = current_df[current_df["location"].isin(selected_locations_tab1)]
        filtered_compare_df_tab1 = compare_df[compare_df["location"].isin(selected_locations_tab1)]
    else:
        filtered_current_df_tab1 = current_df
        filtered_compare_df_tab1 = compare_df

    # Hide outlets where both periods have zero/null revenue
    comparison = comparison[
        (comparison["current_revenue"].fillna(0) != 0) |
        (comparison["compare_revenue"].fillna(0) != 0)
    ]

    comparison, traffic_pen_cols = table_style.add_location_traffic_pen_columns(
        comparison, database, ra, filtered_current_df_tab1, filtered_compare_df_tab1,
        current_short_label, compare_short_label, delta_suffix,
    )
    comparison, aop_cols = table_style.add_aop_columns(
        comparison, database, ra, filtered_current_df_tab1, ["segment", "outlet", "location"], current_short_label
    )
    display = comparison.copy()
    display["current_revenue"] = display["current_revenue"].apply(format_money)
    display["compare_revenue"] = display["compare_revenue"].apply(format_money)
    display["revenue_pct_change"] = display["revenue_pct_change"].apply(format_pct)
    display["current_pax"] = display["current_pax"].apply(format_pax)
    display["compare_pax"] = display["compare_pax"].apply(format_pax)
    display["pax_pct_change"] = display["pax_pct_change"].apply(format_pct)
    display = table_style.format_traffic_pen_columns(display, traffic_pen_cols, format_pax, format_money)
    display = table_style.format_aop_columns(display, aop_cols, format_money)
    current_rev_col = f"Rev ({current_short_label})"
    compare_rev_col = f"Rev ({compare_short_label})"
    current_pax_col = f"PAX ({current_short_label})"
    compare_pax_col = f"PAX ({compare_short_label})"
    display = display.rename(
        columns={
            "segment": "Segment",
            "outlet": "Outlet",
            "location": "Location",
            "current_revenue": current_rev_col,
            "compare_revenue": compare_rev_col,
            "revenue_pct_change": "Rev Δ%",
            "revenue_trend": "Rev Trend",
            "current_pax": current_pax_col,
            "compare_pax": compare_pax_col,
            "pax_pct_change": "PAX Δ%",
            "pax_trend": "PAX Trend",
        }
    )[
        [
            "Segment", "Outlet", "Location", current_rev_col, compare_rev_col, "Rev Δ%", "Rev Trend",
            current_pax_col, compare_pax_col, "PAX Δ%", "PAX Trend",
        ] + aop_cols + traffic_pen_cols
    ]
    pct_cols_tab1 = ["Rev Δ%", "PAX Δ%"] + [c for c in aop_cols if "Variance" in c] + [c for c in traffic_pen_cols if "Δ%" in c]
    st.dataframe(
        table_style.style_pct_columns(display, pct_cols_tab1),
        use_container_width=True,
        hide_index=True,
        column_config={"Segment": st.column_config.Column(pinned=True), "Outlet": st.column_config.Column(pinned=True), "Location": st.column_config.Column(pinned=True)},
    )
    if traffic_pen_cols:
        st.caption(
            "ℹ️ Traffic shown is each outlet's own terminal traffic (e.g. T1D Lounge uses T1 Dep, INL 5&6 uses T3 Int Dep). "
            "PEN % = this outlet's PAX ÷ location traffic. "
            "SPP = this outlet's Revenue ÷ location traffic."
        )
    if not aop_cols:
        st.caption("ℹ️ No AOP target data available for this period yet — upload an AOP workbook on the main page to see AOP/Variance here.")

    overall = ra.summarize_period(filtered_current_df_tab1)
    overall_compare = ra.summarize_period(filtered_compare_df_tab1)
    rev_pct = ra.pct_change(overall["revenue"], overall_compare["revenue"])
    pax_pct = ra.pct_change(overall["pax"], overall_compare["pax"])

    s1, s2, s3, s4 = st.columns(4)
    s1.metric(f"Revenue Total ({current_short_label})", format_money(overall["revenue"]))
    s2.metric(f"Revenue Total ({compare_short_label})", format_money(overall_compare["revenue"]))
    s3.metric("Revenue Change %", format_money(overall["revenue"] - overall_compare["revenue"]), **table_style.metric_delta_args(rev_pct))
    s4.metric("PAX Change %", format_pax(overall["pax"] - overall_compare["pax"]), **table_style.metric_delta_args(pax_pct))

  except Exception as _e:
    log_exception(_e, context="Tab 1 Detailed Comparison")
    show_friendly_error("comparison_error")


def _comparison_rename_map(current_label: str, compare_label: str) -> dict:
    """Build the column rename map for a comparison table, with period names embedded in the headers."""
    return {
        "segment": "Segment",
        "location": "Location",
        "current_revenue": f"Rev ({current_label})",
        "compare_revenue": f"Rev ({compare_label})",
        "revenue_change": "Rev Δ",
        "revenue_pct_change": "Rev Δ%",
        "revenue_trend": "Rev Trend",
        "current_pax": f"PAX ({current_label})",
        "compare_pax": f"PAX ({compare_label})",
        "pax_change": "PAX Δ",
        "pax_pct_change": "PAX Δ%",
        "pax_trend": "PAX Trend",
    }


def _render_grouped_comparison(grouped_df, aop_cols=None):
    """Format + rename a segment- or location-level comparison table for display."""
    aop_cols = aop_cols or []
    out = grouped_df.copy()
    out["current_revenue"] = out["current_revenue"].apply(format_money)
    out["compare_revenue"] = out["compare_revenue"].apply(format_money)
    out["revenue_change"] = out["revenue_change"].apply(format_money)
    out["revenue_pct_change"] = out["revenue_pct_change"].apply(format_pct)
    out["current_pax"] = out["current_pax"].apply(format_pax)
    out["compare_pax"] = out["compare_pax"].apply(format_pax)
    out["pax_change"] = out["pax_change"].apply(format_pax)
    out["pax_pct_change"] = out["pax_pct_change"].apply(format_pct)
    out = table_style.format_aop_columns(out, aop_cols, format_money)
    rename_map = _comparison_rename_map(current_short_label, compare_short_label)
    ordered_cols = [c for c in rename_map if c in out.columns] + aop_cols
    return out[ordered_cols].rename(columns=rename_map)


# ---------------------------------------------------------------------------
# Tab 2 — Segment summary
# ---------------------------------------------------------------------------

with tab2:
  try:
    seg_comparison = ra.compare_segments(current_df, compare_df)
    # Hide segments with no revenue in either period
    seg_comparison = seg_comparison[
        (seg_comparison["current_revenue"].fillna(0) != 0) |
        (seg_comparison["compare_revenue"].fillna(0) != 0)
    ]
    seg_comparison, seg_aop_cols = table_style.add_aop_columns(
        seg_comparison, database, ra, current_df, ["segment"], current_short_label
    )
    seg_display = _render_grouped_comparison(seg_comparison, seg_aop_cols)
    pct_cols_tab2 = ["Rev Δ%", "PAX Δ%"] + [c for c in seg_aop_cols if "Variance" in c]
    st.dataframe(
        table_style.style_pct_columns(seg_display, pct_cols_tab2),
        use_container_width=True,
        hide_index=True,
        column_config={"Segment": st.column_config.Column(pinned=True)},
    )
    if not seg_aop_cols:
        st.caption("ℹ️ No AOP target data available for this period yet — upload an AOP workbook on the main page to see AOP/Variance here.")

  except Exception as _e:
    log_exception(_e, context="Tab 2 Segment Summary")
    show_friendly_error("comparison_error")

# ---------------------------------------------------------------------------
# Tab 3 — Location → Services summary
# ---------------------------------------------------------------------------

with tab3:
  try:
    available_locations_tab3 = sorted(
        set(current_df["location"].dropna().unique()) | set(compare_df["location"].dropna().unique())
    )
    selected_locations_tab3 = st.multiselect(
        "📍 Filter by Location",
        options=available_locations_tab3,
        default=available_locations_tab3,
        key="tab3_location_filter",
    )
    filtered_current_tab3 = (
        current_df[current_df["location"].isin(selected_locations_tab3)]
        if selected_locations_tab3 else current_df
    )
    filtered_compare_tab3 = (
        compare_df[compare_df["location"].isin(selected_locations_tab3)]
        if selected_locations_tab3 else compare_df
    )

    location_segment_comparison = ra.compare_periods(
        filtered_current_tab3, filtered_compare_tab3, group_cols=["location", "segment"]
    )
    location_segment_comparison, tab3_traffic_pen_cols = table_style.add_location_traffic_pen_columns(
        location_segment_comparison, database, ra, filtered_current_tab3, filtered_compare_tab3,
        current_short_label, compare_short_label, delta_suffix,
    )
    # Hide rows where all revenue is zero in both periods
    location_segment_comparison = location_segment_comparison[
        (location_segment_comparison["current_revenue"].fillna(0) != 0) |
        (location_segment_comparison["compare_revenue"].fillna(0) != 0)
    ]
    location_segment_comparison, tab3_aop_cols = table_style.add_aop_columns(
        location_segment_comparison, database, ra, filtered_current_tab3, ["location", "segment"], current_short_label
    )
    out = location_segment_comparison.copy()
    out["current_revenue"] = out["current_revenue"].apply(format_money)
    out["compare_revenue"] = out["compare_revenue"].apply(format_money)
    out["revenue_change"] = out["revenue_change"].apply(format_money)
    out["revenue_pct_change"] = out["revenue_pct_change"].apply(format_pct)
    out["current_pax"] = out["current_pax"].apply(format_pax)
    out["compare_pax"] = out["compare_pax"].apply(format_pax)
    out["pax_change"] = out["pax_change"].apply(format_pax)
    out["pax_pct_change"] = out["pax_pct_change"].apply(format_pct)
    out = table_style.format_traffic_pen_columns(out, tab3_traffic_pen_cols, format_pax, format_money)
    out = table_style.format_aop_columns(out, tab3_aop_cols, format_money)
    loc3_current_rev_col = f"Rev ({current_short_label})"
    loc3_compare_rev_col = f"Rev ({compare_short_label})"
    loc3_current_pax_col = f"PAX ({current_short_label})"
    loc3_compare_pax_col = f"PAX ({compare_short_label})"
    out = out.rename(
        columns={
            "location": "Location",
            "segment": "Service Category",
            "current_revenue": loc3_current_rev_col,
            "compare_revenue": loc3_compare_rev_col,
            "revenue_change": "Rev Δ",
            "revenue_pct_change": "Rev Δ%",
            "revenue_trend": "Rev Trend",
            "current_pax": loc3_current_pax_col,
            "compare_pax": loc3_compare_pax_col,
            "pax_change": "PAX Δ",
            "pax_pct_change": "PAX Δ%",
            "pax_trend": "PAX Trend",
        }
    )[
        [
            "Location", "Service Category", loc3_current_rev_col, loc3_compare_rev_col, "Rev Δ", "Rev Δ%", "Rev Trend",
            loc3_current_pax_col, loc3_compare_pax_col, "PAX Δ", "PAX Δ%", "PAX Trend",
        ] + tab3_aop_cols + tab3_traffic_pen_cols
    ]
    pct_cols_tab3 = ["Rev Δ%", "PAX Δ%"] + [c for c in tab3_aop_cols if "Variance" in c] + [c for c in tab3_traffic_pen_cols if "Δ%" in c]
    st.dataframe(
        table_style.style_pct_columns(out, pct_cols_tab3),
        use_container_width=True,
        hide_index=True,
        column_config={"Location": st.column_config.Column(pinned=True), "Service Category": st.column_config.Column(pinned=True)},
    )
    if not tab3_aop_cols:
        st.caption("ℹ️ No AOP target data available for this period yet — upload an AOP workbook on the main page to see AOP/Variance here.")

# ---------------------------------------------------------------------------
# Tab 4 — Penetration % / SPP by Location
# ---------------------------------------------------------------------------

  except Exception as _e:
    log_exception(_e, context="Tab 3 Location Services")
    show_friendly_error("comparison_error")

with tab4:
  try:
    st.caption(
        "Traffic = total airport visitors that day. Penetration % = PAX ÷ Traffic. "
        "SPP (Sales Per Passenger) = Revenue ÷ Terminal Traffic. "
        "outlet-specific, so this comparison is only available at the location level."
    )
    current_traffic_joined = database.join_revenue_with_traffic(current_df)
    compare_traffic_joined = database.join_revenue_with_traffic(compare_df)
    table_style.render_penetration_spp_table(
        st, ra, database, current_traffic_joined, compare_traffic_joined, current_df,
        current_short_label, compare_short_label,
        format_money, format_pax, format_pct,
    )

  except Exception as _e:
    log_exception(_e, context="Tab 4 PEN SPP")
    show_friendly_error("traffic_columns")

# ---------------------------------------------------------------------------
# Tab 5 — Domestic vs International
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Outlet classification — Domestic vs International
#
# _OUTLET_MAP: exact outlet name → category (highest priority, case-insensitive).
# Sourced from the Master Sheet outlet mapping reference. Add new outlet names
# here as the business expands — no logic changes needed.
#
# _DOM_INT_KEYWORDS: substring fallback for outlets not in _OUTLET_MAP.
# Checked in order — first match wins. Used as a safety net for future outlets
# that haven't been added to _OUTLET_MAP yet.
# ---------------------------------------------------------------------------

_OUTLET_MAP: dict[str, str] = {
    # ── Delhi — Terminal 1 (Domestic) ──────────────────────────────────────
    "t1d lounge-1 node l4&5 card":          "Domestic",
    "t1d lounge":                            "Domestic",
    "t1d new premium lounge 2 (level 5)":   "Domestic",
    "t1d new amex lounge (level 4)":         "Domestic",
    "t1d spa":                               "Domestic",
    # ── Delhi — Terminal 2 (Domestic) ──────────────────────────────────────
    "t2 lounge":                             "Domestic",
    "t2 domestic":                           "Domestic",
    "lounge dl 02,03,04":                    "Domestic",
    "lounge dl 02&03":                       "Domestic",
    # ── Delhi — Terminal 3 Domestic ────────────────────────────────────────
    "t3 d49":                                "Domestic",
    "t3 dlo2/03/04":                         "Domestic",
    "lounge - amex centurion":               "Domestic",
    "centurion lounge":                      "Domestic",
    "lounge - rupay":                        "Domestic",
    "rupay":                                 "Domestic",
    "domestic ai lounge del":                "Domestic",
    "air india":                             "Domestic",
    "domestic lounge (delhi)":               "Domestic",
    "spa - domestic":                        "Domestic",
    "dom spa":                               "Domestic",
    # ── Delhi — Terminal 3 International ───────────────────────────────────
    "t3 inl 5&6":                            "International",
    "inl 5&6":                               "International",
    "t3 premium":                            "International",
    "premium lounge":                        "International",
    "xenia":                                 "International",
    "first class - xenia lounge":            "International",
    "ai international lounge":               "International",
    "international lounge":                  "International",
    "spa - international":                   "International",
    "intl spa":                              "International",
    # ── Delhi — T3 Ancillary serving both Dom+Int arrivals ─────────────────
    "arrival lounge la 22":                  "Ancillary",  # Total Arrival T3
    "la22":                                  "Ancillary",
    "la 22":                                 "Ancillary",
    "nap & shower la01":                     "Ancillary",  # Total Arrival T3
    "la01":                                  "Ancillary",
    "nap & shower la12":                     "Ancillary",  # Total Arrival T3
    "la12":                                  "Ancillary",
    # ── Delhi — T3 Domestic ancillary ───────────────────────────────────────
    "reserved lounge (delhi)":               "Domestic",
    "rl delhi":                              "Domestic",
    "cip lounge":                            "Domestic",
    "dom spa":                               "Domestic",
    # ── Delhi — T3 International ancillary ──────────────────────────────────
    "spa - international":                   "International",
    "intl spa":                              "International",
    # ── Delhi — Airport-wide ancillary (unclassified) ───────────────────────
    "meet & greet (delhi)":                  "Ancillary",
    "meet & greet":                          "Ancillary",
    "m&g del":                               "Ancillary",
    "m&g":                                   "Ancillary",
    "porter (delhi)":                        "Ancillary",  # T1+T2+T3 Arrivals
    "porter del":                            "Ancillary",
    "buggy service (delhi)":                 "Ancillary",  # Total T3 DOM+INT
    "buggy del":                             "Ancillary",
    "baggage wrapping (delhi)":              "Ancillary",  # All Dept
    "baggage wrapping del":                  "Ancillary",
    "business center (delhi)":               "Ancillary",
    "business center":                       "Ancillary",
    # ── Delhi — Non-airport services (exclude from Dom/Int totals) ──────────
    "round d clock (rdc)":                   "Ancillary",
    "rdc":                                   "Ancillary",
    "rdc - rooms":                           "Ancillary",
    "rdc - f&b":                             "Ancillary",
    "encalm eats":                           "Ancillary",
    "encalm sky plates (delhi)":             "Ancillary",
    "encalm sky plates (hyderabad)":         "Ancillary",
    "encalm sky plates":                     "Ancillary",
    # ── Goa — Domestic ─────────────────────────────────────────────────────
    "domestic lounge (goa)":                 "Domestic",
    "lounge (goa domestic)":                 "Domestic",
    "reserved lounge (goa)":                 "Domestic",
    # ── Goa — International ────────────────────────────────────────────────
    "international lounge (goa)":            "International",
    "prive (goa)":                           "International",
    "prive":                                 "International",
    # ── Goa — Ancillary ────────────────────────────────────────────────────
    "baggage wrapping (goa)":                "Ancillary",
    "porter (goa)":                          "Ancillary",
    "meet & greet (goa)":                    "Ancillary",
    # ── Hyderabad — Domestic ───────────────────────────────────────────────
    "domestic lounge (hyderabad)":           "Domestic",
    "lounge (hyderabad domestic)":           "Domestic",
    # ── Hyderabad — International ──────────────────────────────────────────
    "international lounge (hyderabad)":      "International",
    "prive (hyderabad)":                     "International",
    "airport lodge (hyderabad)":             "International",
    # ── Hyderabad — Ancillary ──────────────────────────────────────────────
    "baggage wrapping (hyderabad)":          "Ancillary",
    "porter (hyderabad)":                    "Ancillary",
    "meet & greet (hyderabad)":              "Ancillary",
    "gat (hyderabad)":                       "Ancillary",
}

# Substring fallback — used only when outlet is not in _OUTLET_MAP
_DOM_INT_KEYWORDS: dict[str, str] = {
    "international": "International",
    "intl":          "International",
    "inl":           "International",
    "prive":         "International",
    "domestic":      "Domestic",
    "t1d":           "Domestic",
    "t1":            "Domestic",
    "t2":            "Domestic",
}


def _classify_outlet(outlet: str) -> str:
    """
    Classify an outlet as Domestic, International, or Ancillary.

    Priority:
      1. Exact match in _OUTLET_MAP (case-insensitive, stripped)
      2. Substring match in _DOM_INT_KEYWORDS (case-insensitive, first match wins)
      3. "Ancillary" if nothing matches

    To add a new outlet: insert it into _OUTLET_MAP with the correct category.
    No logic changes needed.
    """
    key = outlet.strip().lower()
    if key in _OUTLET_MAP:
        return _OUTLET_MAP[key]
    lower = key
    for keyword, category in _DOM_INT_KEYWORDS.items():
        if keyword in lower:
            return category
    return "Ancillary"


def _add_dom_int_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add a 'category' column (Domestic / International / Ancillary) to a revenue df."""
    if df is None or df.empty:
        return df
    out = df.copy()
    out["category"] = out["outlet"].apply(_classify_outlet)
    return out


def _dom_int_comparison(
    current_df: pd.DataFrame,
    compare_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Aggregate current and compare DataFrames by Domestic/International category.
    Returns revenue, PAX, Traffic, Penetration %, SPP, and all period-over-period
    change metrics. Traffic is joined via database.join_revenue_with_traffic and
    summed correctly (once per date+location) to avoid outlet-multiplication.
    """
    cur = _add_dom_int_column(current_df)
    cmp = _add_dom_int_column(compare_df)

    # ── Revenue + PAX aggregation ──────────────────────────────────────────
    def _agg_rev_pax(df, suffix):
        return (
            df.groupby("category", as_index=False)
            .agg(revenue=("revenue", "sum"), pax=("pax", "sum"))
            .rename(columns={"revenue": f"{suffix}_revenue", "pax": f"{suffix}_pax"})
        )

    cur_agg = _agg_rev_pax(cur, "current")
    cmp_agg = _agg_rev_pax(cmp, "compare")
    merged = pd.merge(cur_agg, cmp_agg, on="category", how="outer").fillna(0.0)

    # ── Traffic aggregation ────────────────────────────────────────────────
    def _agg_traffic(df):
        """
        Aggregate traffic to category (Domestic/International) level.

        The core challenge: join_revenue_with_traffic returns ONE row per
        location (airport-wide total). A mixed location like Delhi has both
        Domestic (T1/T2) and International (T3/INL) outlets — its single
        traffic figure cannot be assigned to just one category.

        Solution: treat the location's traffic as the shared denominator.
        Each category's penetration % = that category's PAX ÷ location total
        traffic. We do NOT split or apportion traffic itself — instead we
        compute penetration directly from (category_pax / location_traffic)
        and report the full location traffic alongside each category so the
        user understands the context. This matches how Penetration % works
        everywhere else in the app (outlet PAX ÷ airport traffic).

        Returns [category, traffic, penetration_pct, spp_traffic] or None.
        """
        if df is None or df.empty:
            return None
        joined = database.join_revenue_with_traffic(df)
        if joined is None or joined.empty or not ra.has_traffic_data(joined):
            return None

        # Revenue + PAX per (location, category) from original df (has outlet)
        loc_cat = df.copy()
        loc_cat["category"] = loc_cat["outlet"].apply(_classify_outlet)
        loc_cat_agg = loc_cat.groupby(["location", "category"], as_index=False).agg(
            revenue=("revenue", "sum"), pax=("pax", "sum")
        )

        # Location-level traffic from joined (one row per location)
        traffic_map = (
            joined[joined["traffic"].notna() & (joined["traffic"] > 0)]
            .set_index("location")["traffic"]
            .to_dict()
        )

        # Attach location traffic to each (location, category) row
        loc_cat_agg["location_traffic"] = loc_cat_agg["location"].map(traffic_map)

        # Aggregate to category level:
        #   - revenue and PAX sum normally across locations
        #   - traffic: sum location totals that serve this category
        #     (a location with both Dom+Intl outlets contributes its full
        #      traffic to BOTH categories — it is the shared denominator
        #      for penetration at that airport, same as the rest of the app)
        rows = []
        for cat in loc_cat_agg["category"].unique():
            cat_rows = loc_cat_agg[loc_cat_agg["category"] == cat]
            total_rev = cat_rows["revenue"].sum()
            total_pax = cat_rows["pax"].sum()
            # Sum traffic for all locations that have outlets in this category
            locs_with_traffic = cat_rows[cat_rows["location_traffic"].notna()]
            total_traffic = (
                locs_with_traffic["location_traffic"].sum()
                if not locs_with_traffic.empty else None
            )
            pen_pct = (
                ra.safe_div(total_pax, total_traffic) * 100
                if total_traffic and total_traffic > 0 else None
            )
            spp_t = (
                ra.safe_div(total_rev, total_traffic)
                if total_traffic and total_traffic > 0 else None
            )
            rows.append({
                "category": cat,
                "traffic": total_traffic,
                "penetration_pct": pen_pct,
                "spp_traffic": spp_t,
            })

        return pd.DataFrame(rows)

    cur_traffic = _agg_traffic(cur)
    cmp_traffic = _agg_traffic(cmp)

    if cur_traffic is not None:
        merged = merged.merge(
            cur_traffic.rename(columns={
                "traffic": "current_traffic",
                "penetration_pct": "current_pen_pct",
                "spp_traffic": "current_traffic_spp",
            }),
            on="category", how="left",
        )
    else:
        merged["current_traffic"] = None
        merged["current_pen_pct"] = None
        merged["current_traffic_spp"] = None

    if cmp_traffic is not None:
        merged = merged.merge(
            cmp_traffic.rename(columns={
                "traffic": "compare_traffic",
                "penetration_pct": "compare_pen_pct",
                "spp_traffic": "compare_traffic_spp",
            }),
            on="category", how="left",
        )
    else:
        merged["compare_traffic"] = None
        merged["compare_pen_pct"] = None
        merged["compare_traffic_spp"] = None

    # ── Period-over-period deltas ──────────────────────────────────────────
    merged["revenue_change"] = merged["current_revenue"] - merged["compare_revenue"]
    merged["revenue_pct_change"] = merged.apply(
        lambda r: ra.pct_change(r["current_revenue"], r["compare_revenue"]), axis=1
    )
    merged["pax_change"] = merged["current_pax"] - merged["compare_pax"]
    merged["pax_pct_change"] = merged.apply(
        lambda r: ra.pct_change(r["current_pax"], r["compare_pax"]), axis=1
    )
    merged["traffic_pct_change"] = merged.apply(
        lambda r: ra.pct_change(r["current_traffic"], r["compare_traffic"]), axis=1
    )
    merged["pen_pct_change"] = merged.apply(
        lambda r: ra.pct_change(r["current_pen_pct"], r["compare_pen_pct"]), axis=1
    )
    # SPP = revenue / pax (per-passenger spend)
    merged["current_spp"] = merged.apply(
        lambda r: ra.safe_div(r["current_revenue"], r["current_pax"]), axis=1
    )
    merged["compare_spp"] = merged.apply(
        lambda r: ra.safe_div(r["compare_revenue"], r["compare_pax"]), axis=1
    )
    merged["spp_change"] = merged["current_spp"] - merged["compare_spp"]
    merged["spp_pct_change"] = merged.apply(
        lambda r: ra.pct_change(r["current_spp"], r["compare_spp"]), axis=1
    )

    # ── Sort order ─────────────────────────────────────────────────────────
    order = {"Domestic": 0, "International": 1, "Ancillary": 2}
    merged["_sort"] = merged["category"].map(order).fillna(9)
    return merged.sort_values("_sort").drop(columns=["_sort"]).reset_index(drop=True)


def _generate_dom_int_insight(summary_df: pd.DataFrame, current_label: str, compare_label: str) -> str:
    """
    Rule-based text insight for the Domestic vs International tab.
    Identifies direction, primary driver (PAX vs SPP), and contribution share.
    """
    if summary_df.empty:
        return "No data available to generate insights."

    total_current = summary_df["current_revenue"].sum()
    lines = []

    for _, row in summary_df.iterrows():
        cat = row["category"]
        if cat == "Ancillary":
            continue
        rev_pct = row["revenue_pct_change"]
        pax_pct = row["pax_pct_change"]
        spp_pct = row["spp_pct_change"]
        direction = "increased" if (rev_pct or 0) >= 0 else "decreased"
        rev_pct_str = f"{abs(rev_pct or 0):.1f}%"

        # Primary driver: whichever of PAX vs SPP moved more
        if abs(pax_pct or 0) >= abs(spp_pct or 0):
            driver = "higher passenger volume" if (pax_pct or 0) >= 0 else "lower passenger volume"
        else:
            driver = "improved spend per passenger (SPP)" if (spp_pct or 0) >= 0 else "lower spend per passenger (SPP)"

        contrib_pct = (row["current_revenue"] / total_current * 100) if total_current > 0 else 0
        lines.append(
            f"**{cat}** revenue {direction} by **{rev_pct_str}** vs {compare_label}, "
            f"driven by {driver}. "
            f"It contributed **{contrib_pct:.1f}%** of total revenue in {current_label}."
        )

    # Overall winner
    dom_row = summary_df[summary_df["category"] == "Domestic"]
    int_row = summary_df[summary_df["category"] == "International"]
    if not dom_row.empty and not int_row.empty:
        dom_rev = dom_row.iloc[0]["current_revenue"]
        int_rev = int_row.iloc[0]["current_revenue"]
        stronger = "International" if int_rev > dom_rev else "Domestic"
        lines.append(f"Overall, **{stronger}** is the stronger-performing category in {current_label}.")

    return "\n\n".join(lines)


with tab5:
  try:
    st.subheader("🏠 Domestic vs International Breakdown")
    st.caption(
        f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**. "
        "Outlets are classified by name keywords — configure `_DOM_INT_KEYWORDS` in this file to adjust."
    )

    # ── Location filter ─────────────────────────────────────────────────────
    available_locations_tab5 = sorted(
        set(current_df["location"].dropna().unique()) | set(compare_df["location"].dropna().unique())
    )
    selected_locations_tab5 = st.multiselect(
        "📍 Filter by Location",
        options=available_locations_tab5,
        default=available_locations_tab5,
        key="tab5_location_filter",
    )
    filtered_current_tab5 = (
        current_df[current_df["location"].isin(selected_locations_tab5)]
        if selected_locations_tab5 else current_df
    )
    filtered_compare_tab5 = (
        compare_df[compare_df["location"].isin(selected_locations_tab5)]
        if selected_locations_tab5 else compare_df
    )

    summary_df = _dom_int_comparison(filtered_current_tab5, filtered_compare_tab5)
    classified_current = _add_dom_int_column(filtered_current_tab5)
    classified_compare = _add_dom_int_column(filtered_compare_tab5)

    # ── Ancillary warning ────────────────────────────────────────────────
    unclassified = summary_df[summary_df["category"] == "Ancillary"]
    if not unclassified.empty:
        unclassified_outlets = sorted(
            classified_current[classified_current["category"] == "Ancillary"]["outlet"].unique()
        )
        st.warning(
            f"⚠️ {len(unclassified_outlets)} outlet(s) could not be classified as Domestic or International "
            f"and appear under 'Ancillary': {', '.join(unclassified_outlets[:10])}"
            + (" …" if len(unclassified_outlets) > 10 else "")
        )

    # ── KPI cards ──────────────────────────────────────────────────────────
    dom = summary_df[summary_df["category"] == "Domestic"]
    intl = summary_df[summary_df["category"] == "International"]

    def _get(row_df, col, default=0.0):
        if row_df.empty or col not in row_df.columns:
            return default
        val = row_df.iloc[0][col]
        return val if pd.notna(val) else default

    dom_cur_rev      = _get(dom,  "current_revenue")
    dom_rev_pct      = _get(dom,  "revenue_pct_change", None)
    dom_pax_pct      = _get(dom,  "pax_pct_change", None)
    dom_cur_pax      = _get(dom,  "current_pax")
    dom_cur_spp      = _get(dom,  "current_spp")
    dom_cur_traffic  = _get(dom,  "current_traffic", None)
    dom_traffic_pct  = _get(dom,  "traffic_pct_change", None)
    dom_cur_pen      = _get(dom,  "current_pen_pct", None)
    dom_pen_pct      = _get(dom,  "pen_pct_change", None)

    int_cur_rev      = _get(intl, "current_revenue")
    int_rev_pct      = _get(intl, "revenue_pct_change", None)
    int_pax_pct      = _get(intl, "pax_pct_change", None)
    int_cur_pax      = _get(intl, "current_pax")
    int_cur_spp      = _get(intl, "current_spp")
    int_cur_traffic  = _get(intl, "current_traffic", None)
    int_traffic_pct  = _get(intl, "traffic_pct_change", None)
    int_cur_pen      = _get(intl, "current_pen_pct", None)
    int_pen_pct      = _get(intl, "pen_pct_change", None)

    has_traffic = (dom_cur_traffic is not None) or (int_cur_traffic is not None)

    st.markdown("#### 📊 KPI Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        f"🏠 Domestic Revenue ({current_short_label})",
        format_money(dom_cur_rev),
        **table_style.metric_delta_args(dom_rev_pct),
    )
    c2.metric(
        f"✈️ International Revenue ({current_short_label})",
        format_money(int_cur_rev),
        **table_style.metric_delta_args(int_rev_pct),
    )
    c3.metric(
        "🏠 Domestic PAX",
        format_pax(dom_cur_pax),
        **table_style.metric_delta_args(dom_pax_pct),
    )
    c4.metric(
        "✈️ International PAX",
        format_pax(int_cur_pax),
        **table_style.metric_delta_args(int_pax_pct),
    )

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("🏠 Domestic SPP (Rev/PAX)", format_spp(dom_cur_spp))
    c6.metric("✈️ International SPP (Rev/PAX)", format_spp(int_cur_spp))
    total_rev = dom_cur_rev + int_cur_rev
    dom_share = (dom_cur_rev / total_rev * 100) if total_rev > 0 else 0.0
    int_share = (int_cur_rev / total_rev * 100) if total_rev > 0 else 0.0
    c7.metric("🏠 Domestic Revenue Share", f"{dom_share:.1f}%")
    c8.metric("✈️ International Revenue Share", f"{int_share:.1f}%")

    if has_traffic:
        c9, c10, c11, c12 = st.columns(4)
        c9.metric(
            "🏠 Domestic Traffic",
            format_pax(dom_cur_traffic) if dom_cur_traffic is not None else "N/A",
            **table_style.metric_delta_args(dom_traffic_pct),
        )
        c10.metric(
            "✈️ International Traffic",
            format_pax(int_cur_traffic) if int_cur_traffic is not None else "N/A",
            **table_style.metric_delta_args(int_traffic_pct),
        )
        c11.metric(
            "🏠 Domestic Penetration %",
            f"{dom_cur_pen:.2f}%" if dom_cur_pen is not None else "N/A",
            **table_style.metric_delta_args(dom_pen_pct),
        )
        c12.metric(
            "✈️ International Penetration %",
            f"{int_cur_pen:.2f}%" if int_cur_pen is not None else "N/A",
            **table_style.metric_delta_args(int_pen_pct),
        )

    st.divider()

    # ── Summary table ───────────────────────────────────────────────────────
    st.markdown("#### 📋 Comparison Table")
    display_summary = summary_df.copy()
    display_summary = display_summary.rename(columns={"category": "Category"})
    display_summary[f"Rev ({current_short_label})"] = display_summary["current_revenue"].apply(format_money)
    display_summary[f"Rev ({compare_short_label})"] = display_summary["compare_revenue"].apply(format_money)
    display_summary["Rev Δ"] = display_summary["revenue_change"].apply(format_money)
    display_summary["Rev Δ%"] = display_summary["revenue_pct_change"].apply(format_pct)
    display_summary[f"PAX ({current_short_label})"] = display_summary["current_pax"].apply(format_pax)
    display_summary[f"PAX ({compare_short_label})"] = display_summary["compare_pax"].apply(format_pax)
    display_summary["PAX Δ%"] = display_summary["pax_pct_change"].apply(format_pct)
    display_summary[f"SPP ({current_short_label})"] = display_summary["current_spp"].apply(format_spp)
    display_summary[f"SPP ({compare_short_label})"] = display_summary["compare_spp"].apply(format_spp)
    display_summary["SPP Δ%"] = display_summary["spp_pct_change"].apply(format_pct)

    table_cols = [
        "Category",
        f"Rev ({current_short_label})", f"Rev ({compare_short_label})", "Rev Δ", "Rev Δ%",
        f"PAX ({current_short_label})", f"PAX ({compare_short_label})", "PAX Δ%",
        f"SPP ({current_short_label})", f"SPP ({compare_short_label})", "SPP Δ%",
    ]
    pct_style_cols = ["Rev Δ%", "PAX Δ%", "SPP Δ%"]

    # Append traffic columns only when traffic data is available
    if has_traffic:
        display_summary[f"Traffic ({current_short_label})"] = display_summary["current_traffic"].apply(
            lambda v: format_pax(v) if pd.notna(v) else "—"
        )
        display_summary[f"Traffic ({compare_short_label})"] = display_summary["compare_traffic"].apply(
            lambda v: format_pax(v) if pd.notna(v) else "—"
        )
        display_summary["Traffic Δ%"] = display_summary["traffic_pct_change"].apply(format_pct)
        display_summary[f"PEN % ({current_short_label})"] = display_summary["current_pen_pct"].apply(
            lambda v: f"{v:.2f}%" if pd.notna(v) else "—"
        )
        display_summary[f"PEN % ({compare_short_label})"] = display_summary["compare_pen_pct"].apply(
            lambda v: f"{v:.2f}%" if pd.notna(v) else "—"
        )
        display_summary["PEN Δ%"] = display_summary["pen_pct_change"].apply(format_pct)
        table_cols += [
            f"Traffic ({current_short_label})", f"Traffic ({compare_short_label})", "Traffic Δ%",
            f"PEN % ({current_short_label})", f"PEN % ({compare_short_label})", "PEN Δ%",
        ]
        pct_style_cols += ["Traffic Δ%", "PEN Δ%"]

    st.dataframe(
        table_style.style_pct_columns(display_summary[table_cols], pct_style_cols),
        use_container_width=True,
        hide_index=True,
        column_config={"Category": st.column_config.Column(pinned=True)},
    )
    if has_traffic:
        st.caption(
            "ℹ️ Traffic shown is each category's combined terminal traffic. "
            "Penetration % = category PAX ÷ sum of traffic across locations in that category. "
            "SPP (Rev/PAX) = Revenue ÷ PAX."
        )

    st.divider()

    # ── Charts ──────────────────────────────────────────────────────────────
    try:
        import plotly.graph_objects as go
        import plotly.express as px

        chart_df = summary_df[summary_df["category"].isin(["Domestic", "International"])].copy()

        col_l, col_r = st.columns(2)

        # Grouped bar — Revenue current vs compare
        with col_l:
            st.markdown("#### 📊 Revenue: Current vs Prior Period")
            fig_bar = go.Figure()
            fig_bar.add_trace(go.Bar(
                name=current_short_label,
                x=chart_df["category"],
                y=chart_df["current_revenue"],
                marker_color=["#1f77b4", "#ff7f0e"],
                text=[format_money(v) for v in chart_df["current_revenue"]],
                textposition="outside",
            ))
            fig_bar.add_trace(go.Bar(
                name=compare_short_label,
                x=chart_df["category"],
                y=chart_df["compare_revenue"],
                marker_color=["#aec7e8", "#ffbb78"],
                text=[format_money(v) for v in chart_df["compare_revenue"]],
                textposition="outside",
            ))
            fig_bar.update_layout(
                barmode="group",
                height=380,
                margin=dict(t=20, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                yaxis_title="Revenue (₹)",
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        # Donut — contribution share
        with col_r:
            st.markdown("#### 🍩 Revenue Contribution")
            fig_pie = go.Figure(go.Pie(
                labels=chart_df["category"].tolist(),
                values=chart_df["current_revenue"].tolist(),
                hole=0.45,
                marker_colors=["#1f77b4", "#ff7f0e"],
                textinfo="label+percent",
                hovertemplate="%{label}: %{value:,.0f} (%{percent})<extra></extra>",
            ))
            fig_pie.update_layout(
                height=380,
                margin=dict(t=20, b=20),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.1),
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        # PAX grouped bar
        st.markdown("#### 👥 PAX: Current vs Prior Period")
        fig_pax = go.Figure()
        fig_pax.add_trace(go.Bar(
            name=current_short_label,
            x=chart_df["category"],
            y=chart_df["current_pax"],
            marker_color=["#2ca02c", "#d62728"],
            text=[format_pax(v) for v in chart_df["current_pax"]],
            textposition="outside",
        ))
        fig_pax.add_trace(go.Bar(
            name=compare_short_label,
            x=chart_df["category"],
            y=chart_df["compare_pax"],
            marker_color=["#98df8a", "#ff9896"],
            text=[format_pax(v) for v in chart_df["compare_pax"]],
            textposition="outside",
        ))
        fig_pax.update_layout(
            barmode="group",
            height=360,
            margin=dict(t=20, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            yaxis_title="PAX",
        )
        st.plotly_chart(fig_pax, use_container_width=True)

        # SPP comparison bar
        st.markdown("#### 💰 SPP (Revenue per PAX): Current vs Prior Period")
        fig_spp = go.Figure()
        fig_spp.add_trace(go.Bar(
            name=current_short_label,
            x=chart_df["category"],
            y=chart_df["current_spp"],
            marker_color=["#9467bd", "#8c564b"],
            text=[format_spp(v) for v in chart_df["current_spp"]],
            textposition="outside",
        ))
        fig_spp.add_trace(go.Bar(
            name=compare_short_label,
            x=chart_df["category"],
            y=chart_df["compare_spp"],
            marker_color=["#c5b0d5", "#c49c94"],
            text=[format_spp(v) for v in chart_df["compare_spp"]],
            textposition="outside",
        ))
        fig_spp.update_layout(
            barmode="group",
            height=360,
            margin=dict(t=20, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            yaxis_title="SPP (₹)",
        )
        st.plotly_chart(fig_spp, use_container_width=True)

    except ImportError:
        st.info("Install plotly (`pip install plotly`) to enable charts in this tab.")

    st.divider()

    # ── Outlet-level breakdown inside each category ──────────────────────
    st.markdown("#### 🔍 Outlet-Level Breakdown")
    outlet_comparison = ra.compare_periods(filtered_current_tab5, filtered_compare_tab5)
    outlet_comparison["category"] = outlet_comparison["outlet"].apply(_classify_outlet)

    for cat in ["Domestic", "International", "Ancillary"]:
        cat_rows = outlet_comparison[outlet_comparison["category"] == cat]
        if cat_rows.empty:
            continue
        with st.expander(f"{'🏠' if cat == 'Domestic' else '✈️' if cat == 'International' else '🔧'} {cat} Outlets ({len(cat_rows)})", expanded=(cat != "Ancillary")):
            out = cat_rows.copy()
            out["current_revenue"] = out["current_revenue"].apply(format_money)
            out["compare_revenue"] = out["compare_revenue"].apply(format_money)
            out["revenue_change"] = out["revenue_change"].apply(format_money)
            out["revenue_pct_change"] = out["revenue_pct_change"].apply(format_pct)
            out["current_pax"] = out["current_pax"].apply(format_pax)
            out["compare_pax"] = out["compare_pax"].apply(format_pax)
            out["pax_change"] = out["pax_change"].apply(format_pax)
            out["pax_pct_change"] = out["pax_pct_change"].apply(format_pct)
            out = out.rename(columns={
                "segment": "Segment", "outlet": "Outlet", "location": "Location",
                "current_revenue": f"Rev ({current_short_label})",
                "compare_revenue": f"Rev ({compare_short_label})",
                "revenue_change": "Rev Δ", "revenue_pct_change": "Rev Δ%",
                "current_pax": f"PAX ({current_short_label})",
                "compare_pax": f"PAX ({compare_short_label})",
                "pax_change": "PAX Δ", "pax_pct_change": "PAX Δ%",
            })
            display_cols = [
                "Segment", "Outlet", "Location",
                f"Rev ({current_short_label})", f"Rev ({compare_short_label})", "Rev Δ", "Rev Δ%",
                f"PAX ({current_short_label})", f"PAX ({compare_short_label})", "PAX Δ", "PAX Δ%",
            ]
            st.dataframe(
                table_style.style_pct_columns(out[display_cols], ["Rev Δ%", "PAX Δ%"]),
                use_container_width=True,
                hide_index=True,
                column_config={"Segment": st.column_config.Column(pinned=True), "Outlet": st.column_config.Column(pinned=True), "Location": st.column_config.Column(pinned=True)},
            )

    st.divider()

    # ── AI Insights ─────────────────────────────────────────────────────────
    st.markdown("#### 🤖 Business Insights")
    insight_text = _generate_dom_int_insight(summary_df, ranges["current_label"], ranges["compare_label"])
    st.markdown(insight_text)


  except Exception as _e:
    log_exception(_e, context="Tab 5 Dom vs Int")
    show_friendly_error("comparison_error")

# ---------------------------------------------------------------------------
# Multi-Period Snapshot (WoW + MoM + YoY side by side)
# ---------------------------------------------------------------------------

st.divider()

with safe_run("Multi-Period Snapshot", error_type="comparison_error"):
    st.subheader("🗓️ Multi-Period Snapshot")
    st.caption(
    f"Week-wise (full week), Month-wise (full month), and Year-wise (full year) "
    f"comparisons, all anchored on **{anchor_date}** — shown together regardless "
    f"of which comparison type is selected above."
)

wow_ranges = ra.resolve_comparison_ranges("Week-wise", anchor_date, mode="Full Period")
mom_ranges = ra.resolve_comparison_ranges("Month-wise", anchor_date, mode="Full Period")
yoy_ranges = ra.resolve_comparison_ranges("Year-wise", anchor_date, mode="Full Period")

this_week_label, last_week_label = ra.short_period_label(wow_ranges["current_start"], wow_ranges["current_end"], "Week-wise"), ra.short_period_label(wow_ranges["compare_start"], wow_ranges["compare_end"], "Week-wise")
this_month_label, last_month_label = ra.short_period_label(mom_ranges["current_start"], mom_ranges["current_end"], "Month-wise"), ra.short_period_label(mom_ranges["compare_start"], mom_ranges["compare_end"], "Month-wise")
this_year_label, last_year_label = ra.short_period_label(yoy_ranges["current_start"], yoy_ranges["current_end"], "Year-wise"), ra.short_period_label(yoy_ranges["compare_start"], yoy_ranges["compare_end"], "Year-wise")

this_week_df = database.load_for_date_range(wow_ranges["current_start"], wow_ranges["current_end"])
last_week_df = database.load_for_date_range(wow_ranges["compare_start"], wow_ranges["compare_end"])
this_month_df = database.load_for_date_range(mom_ranges["current_start"], mom_ranges["current_end"])
last_month_df = database.load_for_date_range(mom_ranges["compare_start"], mom_ranges["compare_end"])
this_year_df = database.load_for_date_range(yoy_ranges["current_start"], yoy_ranges["current_end"])
last_year_df = database.load_for_date_range(yoy_ranges["compare_start"], yoy_ranges["compare_end"])

any_snapshot_data = any(
    not df.empty for df in [last_week_df, last_month_df, last_year_df]
)

if not any_snapshot_data:
    st.info(
        "Not enough history yet for a multi-period snapshot — upload more "
        "reports (other weeks, months, or years) to unlock this."
    )
else:
    base = this_week_df[["segment", "outlet", "location"]].drop_duplicates()
    base = base.merge(ra._aggregate(this_week_df, ra.GROUP_COLS, "thisweek"), on=ra.GROUP_COLS, how="left")
    if not last_week_df.empty:
        base = base.merge(ra._aggregate(last_week_df, ra.GROUP_COLS, "lastweek"), on=ra.GROUP_COLS, how="outer")
    if not this_month_df.empty:
        base = base.merge(ra._aggregate(this_month_df, ra.GROUP_COLS, "tm"), on=ra.GROUP_COLS, how="outer")
    if not last_month_df.empty:
        base = base.merge(ra._aggregate(last_month_df, ra.GROUP_COLS, "lm"), on=ra.GROUP_COLS, how="outer")
    if not this_year_df.empty:
        base = base.merge(ra._aggregate(this_year_df, ra.GROUP_COLS, "ty"), on=ra.GROUP_COLS, how="outer")
    if not last_year_df.empty:
        base = base.merge(ra._aggregate(last_year_df, ra.GROUP_COLS, "ly"), on=ra.GROUP_COLS, how="outer")

    for col in base.columns:
        if col not in ra.GROUP_COLS:
            base[col] = base[col].fillna(0.0)

    if "lastweek_revenue" in base.columns:
        base["wow_pct"] = base.apply(lambda r: ra.pct_change(r["thisweek_revenue"], r["lastweek_revenue"]), axis=1)
        base["wow_pax_pct"] = base.apply(lambda r: ra.pct_change(r["thisweek_pax"], r["lastweek_pax"]), axis=1)
    if "tm_revenue" in base.columns and "lm_revenue" in base.columns:
        base["mom_pct"] = base.apply(lambda r: ra.pct_change(r["tm_revenue"], r["lm_revenue"]), axis=1)
        base["mom_pax_pct"] = base.apply(lambda r: ra.pct_change(r["tm_pax"], r["lm_pax"]), axis=1)
    if "ty_revenue" in base.columns and "ly_revenue" in base.columns:
        base["yoy_pct"] = base.apply(lambda r: ra.pct_change(r["ty_revenue"], r["ly_revenue"]), axis=1)
        base["yoy_pax_pct"] = base.apply(lambda r: ra.pct_change(r["ty_pax"], r["ly_pax"]), axis=1)

    base, snapshot_aop_cols = table_style.add_aop_columns(
        base, database, ra, this_month_df, ra.GROUP_COLS, this_month_label
    )

    rename_map = {
        "segment": "Segment", "outlet": "Outlet", "location": "Location",
        "thisweek_revenue": f"Rev ({this_week_label})", "lastweek_revenue": f"Rev ({last_week_label})", "wow_pct": "WoW%",
        "tm_revenue": f"Rev ({this_month_label})", "lm_revenue": f"Rev ({last_month_label})", "mom_pct": "MoM%",
        "ty_revenue": f"Rev ({this_year_label})", "ly_revenue": f"Rev ({last_year_label})", "yoy_pct": "YoY%",
        "thisweek_pax": f"PAX ({this_week_label})", "lastweek_pax": f"PAX ({last_week_label})", "wow_pax_pct": "WoW PAX%",
        "tm_pax": f"PAX ({this_month_label})", "lm_pax": f"PAX ({last_month_label})", "mom_pax_pct": "MoM PAX%",
        "ty_pax": f"PAX ({this_year_label})", "ly_pax": f"PAX ({last_year_label})", "yoy_pax_pct": "YoY PAX%",
    }
    money_cols = [c for c in ["thisweek_revenue", "lastweek_revenue", "tm_revenue", "lm_revenue", "ty_revenue", "ly_revenue"] if c in base.columns]
    pax_cols = [c for c in ["thisweek_pax", "lastweek_pax", "tm_pax", "lm_pax", "ty_pax", "ly_pax"] if c in base.columns]
    pct_cols = [c for c in ["wow_pct", "mom_pct", "yoy_pct", "wow_pax_pct", "mom_pax_pct", "yoy_pax_pct"] if c in base.columns]

    display_base = base.copy()
    for c in money_cols:
        display_base[c] = display_base[c].apply(format_money)
    for c in pax_cols:
        display_base[c] = display_base[c].apply(format_pax)
    for c in pct_cols:
        display_base[c] = display_base[c].apply(format_pct)
    display_base = table_style.format_aop_columns(display_base, snapshot_aop_cols, format_money)

    ordered_cols = [c for c in rename_map if c in display_base.columns] + snapshot_aop_cols
    display_base = display_base[ordered_cols].rename(columns=rename_map)
    # Hide rows where revenue is zero across all comparison periods
    _rev_cols_snap = [c for c in display_base.columns
                      if "Rev" in c and "%" not in c and "Δ" not in c and "Trend" not in c]
    if _rev_cols_snap:
        def _to_num(s):
            try:
                return float(str(s).replace("₹","").replace(",","").strip())
            except (ValueError, TypeError):
                return 0.0
        _rev_numeric = display_base[_rev_cols_snap].apply(lambda col: col.map(_to_num))
        display_base = display_base[(_rev_numeric != 0).any(axis=1)]
    pct_display_cols = [c for c in ["WoW%", "MoM%", "YoY%", "WoW PAX%", "MoM PAX%", "YoY PAX%"] if c in display_base.columns]
    pct_display_cols += [c for c in snapshot_aop_cols if "Variance" in c]
    st.dataframe(
        table_style.style_pct_columns(display_base, pct_display_cols),
        use_container_width=True,
        hide_index=True,
        column_config={"Segment": st.column_config.Column(pinned=True), "Outlet": st.column_config.Column(pinned=True), "Location": st.column_config.Column(pinned=True)},
    )
    if not snapshot_aop_cols:
        st.caption("ℹ️ No AOP target data available for This Month yet — upload an AOP workbook on the main page to see AOP/Variance here.")

    st.caption(
        f"This Week: {wow_ranges['current_label']} · Last Week: {wow_ranges['compare_label']} · "
        f"This Month: {mom_ranges['current_label']} · Last Month: {mom_ranges['compare_label']} · "
        f"This Year: {yoy_ranges['current_label']} · Last Year: {yoy_ranges['compare_label']}"
    )
