"""
pages/8_Business_Performance.py — Grouped Business Performance Report

Mirrors the AOP Delhi / AOP Hyd / AOP Gox sheets from the Business Plan Excel.
Produces a hierarchical summary table with subtotals by terminal group, with:
  - Revenue (Current Period vs Prior Period + YOY %)
  - PAX (Current vs Prior + YOY %)
  - Penetration % (PAX / Traffic, per terminal-correct traffic pool)
  - SPP (Revenue / Traffic — Spend Per Passenger using airport traffic)
  - AOP Revenue target and variance
  - Traffic (Current vs Prior + change %)

Formulas implemented per Business Plan (June 2026):
  Penetration % = PAX / Terminal_Traffic * 100
  SPP           = Revenue / Terminal_Traffic          (NOT Revenue / PAX)
  YOY Revenue % = (Current - Prior) / Prior
  AOP Variance  = (Actual - AOP_Target) / AOP_Target
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from modules import comparison_widget, database, date_picker, revenue_analysis as ra, table_style
from modules.formatting import format_money, format_pax, format_pct, format_spp
from modules.outlet_groups import (
    DELHI_GROUPS, DELHI_SUBTOTALS, HYD_GROUPS, HYD_SUBTOTALS, GOA_GROUPS,
    get_display_name, get_outlet_group,
)
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Business Performance", page_icon="📊", layout="wide")
bootstrap_session()

st.title("📊 Business Performance Report")
st.caption(
    "Grouped summary mirroring the AOP management report structure. "
    "Penetration % = PAX ÷ Terminal Traffic. "
    "SPP = Revenue ÷ Terminal Traffic (not Revenue ÷ PAX)."
)

available_dates = database.get_available_dates()
if not available_dates:
    st.info("No data available. Upload a report on the main page first.")
    st.stop()

anchor_date = date_picker.render_date_dropdown(
    available_dates, key_prefix="bp_anchor", label="Report Date",
    default_date=default_active_date(),
)
set_active_date(anchor_date)

ranges = comparison_widget.render_comparison_selector(anchor_date, key_prefix="bp_cmp")
current_df  = database.load_for_date_range(ranges["current_start"],  ranges["current_end"])
compare_df  = database.load_for_date_range(ranges["compare_start"],  ranges["compare_end"])

if current_df.empty:
    st.warning("No revenue data for the selected period.")
    st.stop()

current_label  = ranges["current_label"]
compare_label  = ranges["compare_label"]

# ---------------------------------------------------------------------------
# Attach terminal-correct traffic to each outlet row
# ---------------------------------------------------------------------------
current_joined  = database.join_revenue_with_traffic(current_df)
compare_joined  = database.join_revenue_with_traffic(compare_df) if not compare_df.empty else None
has_traffic = ra.has_traffic_data(current_joined)


def _get_terminal_traffic(df_joined: pd.DataFrame, terminal: str) -> float:
    """
    Return total traffic for a specific terminal label from a joined df.
    join_revenue_with_traffic returns one row per location — we need to
    look up traffic by (location, terminal) from the raw database instead.
    """
    if df_joined is None or df_joined.empty:
        return 0.0
    # join_revenue_with_traffic aggregates to location level so terminal
    # info is lost. We query the traffic table directly.
    return 0.0  # placeholder — see note below


def _build_group_summary(
    df: pd.DataFrame,
    groups: dict[str, list[str]],
    subtotals: list[tuple[str, list[str]]],
    aop_df: pd.DataFrame | None = None,
    compare_df: pd.DataFrame | None = None,
    traffic_joined: pd.DataFrame | None = None,
    compare_traffic_joined: pd.DataFrame | None = None,
    location_filter: str = "",   # e.g. "Delhi", "Hyderabad", "Goa" — used for display name resolution
) -> pd.DataFrame:
    """
    Build a grouped summary DataFrame with outlet rows and subtotal rows.

    Columns: Group, Outlet (display name), Revenue (current), Revenue (compare),
             Rev YOY%, PAX (current), PAX (compare), PAX YOY%,
             Traffic (current), Traffic (compare), Traffic Δ%,
             PEN % (current), PEN % (compare), PEN Δ%,
             SPP (current), SPP (compare), SPP Δ%,
             AOP Target, AOP Variance %
    """
    rows = []

    # Aggregate current period to outlet level.
    # Map raw outlet names to their display names BEFORE aggregating so
    # that multiple raw names sharing the same display name (e.g. "T1D Lounge"
    # and "T1D new premium lounge 2 (level 5)" both → "Encalm Lounge (T1 D)")
    # are summed into a single row rather than appearing as duplicates.
    _cur = df.copy()
    _cur["outlet"] = _cur.apply(
        lambda r: get_display_name(r["outlet"], r.get("location", "")), axis=1
    )
    cur_agg = _cur.groupby(["outlet", "location"], as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )

    # Same display-name normalisation for the compare period.
    if compare_df is not None and not compare_df.empty:
        _cmp = compare_df.copy()
        _cmp["outlet"] = _cmp.apply(
            lambda r: get_display_name(r["outlet"], r.get("location", "")), axis=1
        )
        cmp_agg = _cmp.groupby(["outlet", "location"], as_index=False).agg(
            revenue=("revenue", "sum"), pax=("pax", "sum")
        )
    else:
        cmp_agg = pd.DataFrame(columns=["outlet", "location", "revenue", "pax"])

    # Per-outlet terminal traffic — each outlet gets its own terminal pool,
    # NOT the whole-airport total. Uses join_revenue_with_traffic_by_outlet
    # which applies terminal_mapping.get_terminal_for_outlet per outlet.
    outlet_traffic_cur = database.join_revenue_with_traffic_by_outlet(df) if df is not None and not df.empty else None
    outlet_traffic_cmp = database.join_revenue_with_traffic_by_outlet(compare_df) if compare_df is not None and not compare_df.empty else None

    # Build lookup: (outlet, location) → traffic
    traffic_map = {}         # outlet+location → current traffic
    compare_traffic_map = {} # outlet+location → compare traffic
    if outlet_traffic_cur is not None and not outlet_traffic_cur.empty:
        for _, r in outlet_traffic_cur[outlet_traffic_cur["traffic"].notna()].iterrows():
            traffic_map[(r["outlet"], r["location"])] = r["traffic"]
    if outlet_traffic_cmp is not None and not outlet_traffic_cmp.empty:
        for _, r in outlet_traffic_cmp[outlet_traffic_cmp["traffic"].notna()].iterrows():
            compare_traffic_map[(r["outlet"], r["location"])] = r["traffic"]

    # AOP lookup: outlet → aop value
    aop_map = {}
    if aop_df is not None and not aop_df.empty and "aop" in aop_df.columns:
        aop_agg = aop_df.groupby("outlet", as_index=False)["aop"].sum()
        aop_map = aop_agg.set_index("outlet")["aop"].to_dict()

    def _outlet_row(outlet_name: str, group_label: str) -> dict | None:
        # cur_agg is now keyed by display name, so convert before lookup.
        # Pass location from the tab context so ambiguous names like
        # "Reserved Lounge" resolve correctly per airport.
        display = get_display_name(outlet_name, location_filter)
        cur = cur_agg[cur_agg["outlet"] == display]
        if cur.empty:
            return None
        cur_rev = cur["revenue"].sum()
        cmp_check = cmp_agg[cmp_agg["outlet"] == display]
        cmp_rev_check = cmp_check["revenue"].sum() if not cmp_check.empty else 0
        if cur_rev == 0 and cmp_rev_check == 0:
            return None  # hide outlets with no revenue in either period
        cur_pax = cur["pax"].sum()
        location = cur["location"].iloc[0]
        # Traffic map still uses raw outlet names — look up by raw name first,
        # then fall back to display name in case the map was built from display names.
        traffic = traffic_map.get((outlet_name, location)) or traffic_map.get((display, location))

        cmp = cmp_agg[cmp_agg["outlet"] == display]
        cmp_rev = cmp["revenue"].sum() if not cmp.empty else None
        cmp_pax = cmp["pax"].sum() if not cmp.empty else None
        cmp_traffic = (compare_traffic_map.get((outlet_name, location))
                       or compare_traffic_map.get((display, location)))

        rev_yoy = ra.pct_change(cur_rev, cmp_rev)
        pax_yoy = ra.pct_change(cur_pax, cmp_pax)
        traffic_chg = ra.pct_change(traffic, cmp_traffic)

        # PEN % = PAX / Traffic (terminal-level traffic)
        pen_cur = ra.safe_div(cur_pax, traffic) * 100 if traffic else None
        pen_cmp = ra.safe_div(cmp_pax, cmp_traffic) * 100 if cmp_traffic else None
        pen_chg = ra.pct_change(pen_cur, pen_cmp)

        # SPP = Revenue / Traffic (per Business Plan formula)
        spp_cur = ra.safe_div(cur_rev, traffic) if traffic else None
        spp_cmp = ra.safe_div(cmp_rev, cmp_traffic) if cmp_traffic else None
        spp_chg = ra.pct_change(spp_cur, spp_cmp)

        aop = aop_map.get(outlet_name)
        aop_var = ra.pct_change(cur_rev, aop) if aop else None

        return {
            "Group": group_label,
            "Outlet": display,
            "_is_subtotal": False,
            "cur_rev": cur_rev, "cmp_rev": cmp_rev, "rev_yoy": rev_yoy,
            "cur_pax": cur_pax, "cmp_pax": cmp_pax, "pax_yoy": pax_yoy,
            "cur_traffic": traffic, "cmp_traffic": cmp_traffic, "traffic_chg": traffic_chg,
            "pen_cur": pen_cur, "pen_cmp": pen_cmp, "pen_chg": pen_chg,
            "spp_cur": spp_cur, "spp_cmp": spp_cmp, "spp_chg": spp_chg,
            "aop": aop, "aop_var": aop_var,
        }

    # Build per-group outlet rows and accumulate for subtotals
    group_totals: dict[str, dict] = {}
    subtotal_keys = {s[0] for s in subtotals}
    matched_outlets: set = set()  # track which outlets were matched by a group

    for group_name, outlets in groups.items():
        if group_name in subtotal_keys:
            continue  # subtotals are added after

        g_rows = []
        # Track display names already added in this group to prevent
        # duplicates when multiple raw outlet names share the same display
        # name (e.g. "T1D Lounge" and "T1D new premium lounge 2 (level 5)"
        # both map to "Encalm Lounge (T1 D)").
        seen_display_names: set = set()
        for outlet in outlets:
            display_name = get_display_name(outlet, location_filter)
            if display_name in seen_display_names:
                matched_outlets.add(outlet)  # still mark as matched
                continue
            r = _outlet_row(outlet, group_name)
            if r:
                seen_display_names.add(display_name)
                rows.append(r)
                g_rows.append(r)
                matched_outlets.add(outlet)

        if g_rows:
            group_totals[group_name] = {
                "cur_rev":     sum(r["cur_rev"] or 0 for r in g_rows),
                "cmp_rev":     sum(r["cmp_rev"] or 0 for r in g_rows if r["cmp_rev"]),
                "cur_pax":     sum(r["cur_pax"] or 0 for r in g_rows),
                "cmp_pax":     sum(r["cmp_pax"] or 0 for r in g_rows if r["cmp_pax"]),
                "aop":         sum(r["aop"] or 0 for r in g_rows if r["aop"]),
                # Traffic: each outlet now has its own terminal traffic.
                # For a group subtotal, use the first non-None value (outlets in the
                # same group typically share the same terminal, so this is correct).
                "cur_traffic": next((r["cur_traffic"] for r in g_rows if r["cur_traffic"]), None),
                "cmp_traffic": next((r["cmp_traffic"] for r in g_rows if r["cmp_traffic"]), None),
            }

    # Add any outlets that exist in the data but weren't matched by any group
    # This handles name variants not yet listed in outlet_groups.py
    all_data_outlets = set(cur_agg["outlet"].unique()) if not cur_agg.empty else set()
    unmatched = all_data_outlets - matched_outlets
    if unmatched:
        g_rows_unmatched = []
        for outlet in sorted(unmatched):
            r = _outlet_row(outlet, "Other")
            if r:
                rows.append(r)
                g_rows_unmatched.append(r)
        if g_rows_unmatched:
            group_totals["Other"] = {
                "cur_rev":     sum(r["cur_rev"] or 0 for r in g_rows_unmatched),
                "cmp_rev":     sum(r["cmp_rev"] or 0 for r in g_rows_unmatched if r["cmp_rev"]),
                "cur_pax":     sum(r["cur_pax"] or 0 for r in g_rows_unmatched),
                "cmp_pax":     sum(r["cmp_pax"] or 0 for r in g_rows_unmatched if r["cmp_pax"]),
                "aop":         sum(r["aop"] or 0 for r in g_rows_unmatched if r["aop"]),
                "cur_traffic": next((r["cur_traffic"] for r in g_rows_unmatched if r["cur_traffic"]), None),
                "cmp_traffic": next((r["cmp_traffic"] for r in g_rows_unmatched if r["cmp_traffic"]), None),
            }

    # Add subtotal rows
    for sub_label, source_groups in subtotals:
        sub_cur_rev     = sum(group_totals.get(g, {}).get("cur_rev", 0) for g in source_groups)
        sub_cmp_rev     = sum(group_totals.get(g, {}).get("cmp_rev", 0) for g in source_groups)
        sub_cur_pax     = sum(group_totals.get(g, {}).get("cur_pax", 0) for g in source_groups)
        sub_cmp_pax     = sum(group_totals.get(g, {}).get("cmp_pax", 0) for g in source_groups)
        sub_aop         = sum(group_totals.get(g, {}).get("aop", 0) for g in source_groups)
        # Traffic for subtotals: use first available group traffic (location-level, same for all)
        sub_cur_traffic = next((group_totals.get(g, {}).get("cur_traffic") for g in source_groups
                                if group_totals.get(g, {}).get("cur_traffic")), None)
        sub_cmp_traffic = next((group_totals.get(g, {}).get("cmp_traffic") for g in source_groups
                                if group_totals.get(g, {}).get("cmp_traffic")), None)
        # PEN % and SPP for the subtotal using summed PAX/Revenue against traffic
        sub_pen_cur = ra.safe_div(sub_cur_pax, sub_cur_traffic) * 100 if sub_cur_traffic else None
        sub_pen_cmp = ra.safe_div(sub_cmp_pax, sub_cmp_traffic) * 100 if sub_cmp_traffic else None
        sub_spp_cur = ra.safe_div(sub_cur_rev, sub_cur_traffic) if sub_cur_traffic else None
        sub_spp_cmp = ra.safe_div(sub_cmp_rev, sub_cmp_traffic) if sub_cmp_traffic else None
        rows.append({
            "Group": sub_label,
            "Outlet": sub_label,
            "_is_subtotal": True,
            "cur_rev":      sub_cur_rev,
            "cmp_rev":      sub_cmp_rev if sub_cmp_rev else None,
            "rev_yoy":      ra.pct_change(sub_cur_rev, sub_cmp_rev),
            "cur_pax":      sub_cur_pax,
            "cmp_pax":      sub_cmp_pax if sub_cmp_pax else None,
            "pax_yoy":      ra.pct_change(sub_cur_pax, sub_cmp_pax),
            "cur_traffic":  sub_cur_traffic,
            "cmp_traffic":  sub_cmp_traffic,
            "traffic_chg":  ra.pct_change(sub_cur_traffic, sub_cmp_traffic),
            "pen_cur":      sub_pen_cur,
            "pen_cmp":      sub_pen_cmp,
            "pen_chg":      ra.pct_change(sub_pen_cur, sub_pen_cmp),
            "spp_cur":      sub_spp_cur,
            "spp_cmp":      sub_spp_cmp,
            "spp_chg":      ra.pct_change(sub_spp_cur, sub_spp_cmp),
            "aop":          sub_aop if sub_aop else None,
            "aop_var":      ra.pct_change(sub_cur_rev, sub_aop) if sub_aop else None,
        })

    return pd.DataFrame(rows)


def _render_group_table(summary_df: pd.DataFrame, cur_label: str, cmp_label: str):
    """Format and render the group summary DataFrame as a styled dataframe."""
    if summary_df.empty:
        st.info("No data for this period.")
        return

    out = summary_df.copy()

    # Format columns
    out[f"Rev ({cur_label})"]     = out["cur_rev"].apply(format_money)
    out[f"Rev ({cmp_label})"]     = out["cmp_rev"].apply(lambda v: format_money(v) if v else "—")
    out["Rev YOY%"]               = out["rev_yoy"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")
    out[f"PAX ({cur_label})"]     = out["cur_pax"].apply(format_pax)
    out[f"PAX ({cmp_label})"]     = out["cmp_pax"].apply(lambda v: format_pax(v) if v else "—")
    out["PAX YOY%"]               = out["pax_yoy"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")

    display_cols = [
        "Group", "Outlet",
        f"Rev ({cur_label})", f"Rev ({cmp_label})", "Rev YOY%",
        f"PAX ({cur_label})", f"PAX ({cmp_label})", "PAX YOY%",
    ]

    if out["cur_traffic"].notna().any():
        out[f"Traffic ({cur_label})"]   = out["cur_traffic"].apply(lambda v: format_pax(v) if v else "—")
        out[f"Traffic ({cmp_label})"]   = out["cmp_traffic"].apply(lambda v: format_pax(v) if v else "—")
        out["Traffic Δ%"]               = out["traffic_chg"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")
        out[f"PEN % ({cur_label})"]     = out["pen_cur"].apply(lambda v: f"{v:.2f}%" if v is not None and v == v and v != 0 else "—")
        out[f"PEN % ({cmp_label})"]     = out["pen_cmp"].apply(lambda v: f"{v:.2f}%" if v is not None and v == v and v != 0 else "—")
        out["PEN Δ%"]                   = out["pen_chg"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")
        out[f"SPP ({cur_label})"]       = out["spp_cur"].apply(lambda v: format_spp(v) if v is not None else "—")
        out[f"SPP ({cmp_label})"]       = out["spp_cmp"].apply(lambda v: format_spp(v) if v is not None else "—")
        out["SPP Δ%"]                   = out["spp_chg"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")
        display_cols += [
            f"Traffic ({cur_label})", f"Traffic ({cmp_label})", "Traffic Δ%",
            f"PEN % ({cur_label})", f"PEN % ({cmp_label})", "PEN Δ%",
            f"SPP ({cur_label})", f"SPP ({cmp_label})", "SPP Δ%",
        ]

    if out["aop"].notna().any():
        out["AOP Target"]  = out["aop"].apply(lambda v: format_money(v) if v else "—")
        out["AOP Var %"]   = out["aop_var"].apply(format_pct)
        display_cols += ["AOP Target", "AOP Var %"]

    pct_cols = [c for c in ["Rev YOY%", "PAX YOY%", "Traffic Δ%", "PEN Δ%", "SPP Δ%", "AOP Var %"] if c in out.columns]

    # Build display DataFrame — keep Group and Outlet as regular columns
    # (not index) so st.dataframe can pin them via column_config.
    display_df = out[display_cols].reset_index(drop=True)

    # Build column_config: pin Group and Outlet to the left with fixed
    # widths so they stay visible when the user scrolls horizontally.
    col_cfg = {
        "Group":  st.column_config.TextColumn("Group",  width="small", pinned=True),
        "Outlet": st.column_config.TextColumn("Outlet", width="medium", pinned=True),
    }

    # Apply pct colour styling — pass the plain df, not a slice, so
    # pandas Styler doesn't raise a KeyError on subset matching.
    styled = table_style.style_pct_columns(display_df, pct_cols)

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config=col_cfg,
        column_order=display_cols,
    )
    if out["cur_traffic"].notna().any():
        st.caption(
            "PEN % = PAX ÷ Terminal Traffic × 100. "
            "SPP = Revenue ÷ Terminal Traffic (per airport visitor). "
            "Traffic shown is the outlet's assigned terminal pool."
        )


# ---------------------------------------------------------------------------
# Location tabs
# ---------------------------------------------------------------------------
aop_df = database.load_aop_for_date_range(ranges["current_start"], ranges["current_end"]) \
    if hasattr(database, "load_aop_for_date_range") else None

tab_delhi, tab_hyd, tab_goa = st.tabs(["🏙️ Delhi", "🏙️ Hyderabad", "🏙️ Goa"])

with tab_delhi:
  with safe_run("Delhi Business Performance", error_type="comparison_error"):
    df_cur  = current_df[current_df["location"] == "Delhi"]
    df_cmp  = compare_df[compare_df["location"] == "Delhi"] if not compare_df.empty else None
    df_aop  = aop_df[aop_df["location"] == "Delhi"] if aop_df is not None and not aop_df.empty else None
    tj_cur  = database.join_revenue_with_traffic(df_cur) if not df_cur.empty else None
    tj_cmp  = database.join_revenue_with_traffic(df_cmp) if df_cmp is not None and not df_cmp.empty else None

    if df_cur.empty:
        st.info("No Delhi data for this period.")
    else:
        summary = _build_group_summary(
            df_cur, DELHI_GROUPS, DELHI_SUBTOTALS,
            aop_df=df_aop, compare_df=df_cmp,
            traffic_joined=tj_cur, compare_traffic_joined=tj_cmp,
            location_filter="Delhi",
        )
        _render_group_table(summary, current_label, compare_label)

with tab_hyd:
  with safe_run("Hyderabad Business Performance", error_type="comparison_error"):
    df_cur  = current_df[current_df["location"] == "Hyderabad"]
    df_cmp  = compare_df[compare_df["location"] == "Hyderabad"] if not compare_df.empty else None
    df_aop  = aop_df[aop_df["location"] == "Hyderabad"] if aop_df is not None and not aop_df.empty else None

    if df_cur.empty:
        st.info("No Hyderabad data for this period.")
    else:
        summary = _build_group_summary(
            df_cur, HYD_GROUPS, HYD_SUBTOTALS,
            aop_df=df_aop, compare_df=df_cmp,
            location_filter="Hyderabad",
        )
        _render_group_table(summary, current_label, compare_label)

with tab_goa:
  with safe_run("Goa Business Performance", error_type="comparison_error"):
    df_cur  = current_df[current_df["location"] == "Goa"]
    df_cmp  = compare_df[compare_df["location"] == "Goa"] if not compare_df.empty else None
    df_aop  = aop_df[aop_df["location"] == "Goa"] if aop_df is not None and not aop_df.empty else None

    if df_cur.empty:
        st.info("No Goa data for this period.")
    else:
        summary = _build_group_summary(
            df_cur, GOA_GROUPS, [],
            aop_df=df_aop, compare_df=df_cmp,
            location_filter="Goa",
        )
        _render_group_table(summary, current_label, compare_label)

# ---------------------------------------------------------------------------
# Grand Total
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🌐 Grand Total — All Locations")

with safe_run("Grand Total", error_type="comparison_error"):
    all_cur = current_df.groupby("location", as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )
    all_cmp = compare_df.groupby("location", as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    ) if not compare_df.empty else pd.DataFrame()

    totals = all_cur.merge(
        all_cmp.rename(columns={"revenue": "cmp_rev", "pax": "cmp_pax"}),
        on="location", how="left",
    )
    # Guard: when compare_df is empty the merge produces no cmp_rev/cmp_pax
    if "cmp_rev" not in totals.columns:
        totals["cmp_rev"] = None
    if "cmp_pax" not in totals.columns:
        totals["cmp_pax"] = None

    totals["Rev YOY%"] = totals.apply(lambda r: ra.pct_change(r["revenue"], r.get("cmp_rev")), axis=1)
    totals["PAX YOY%"] = totals.apply(lambda r: ra.pct_change(r["pax"], r.get("cmp_pax")), axis=1)

    display_totals = totals.copy()
    display_totals[f"Rev ({current_label})"]  = display_totals["revenue"].apply(format_money)
    display_totals[f"Rev ({compare_label})"]  = display_totals["cmp_rev"].apply(lambda v: format_money(v) if pd.notna(v) else "—")
    display_totals["Rev YOY%"]               = display_totals["Rev YOY%"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")
    display_totals[f"PAX ({current_label})"]  = display_totals["pax"].apply(format_pax)
    display_totals[f"PAX ({compare_label})"]  = display_totals["cmp_pax"].apply(lambda v: format_pax(v) if pd.notna(v) else "—")
    display_totals["PAX YOY%"]               = display_totals["PAX YOY%"].apply(lambda v: format_pct(v) if v is not None and v == v else "—")

    grand_cols = [
        "location",
        f"Rev ({current_label})", f"Rev ({compare_label})", "Rev YOY%",
        f"PAX ({current_label})", f"PAX ({compare_label})", "PAX YOY%",
    ]
    display_totals = display_totals.rename(columns={"location": "Location"})
    grand_cols[0] = "Location"

    st.dataframe(
        table_style.style_pct_columns(display_totals[grand_cols], ["Rev YOY%", "PAX YOY%"]),
        use_container_width=True,
        hide_index=True,
    )
