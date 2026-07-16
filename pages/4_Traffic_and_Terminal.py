"""
pages/4_Traffic_and_Terminal.py — Airport traffic (Source 3) analysis:
Penetration % and SPP with Week/Month/Year-wise variance and a
plain-English driver narrative ("revenue increased despite traffic
decline due to higher SPP"), plus a dedicated Terminal-wise section
comparing terminal-level traffic against the outlets mapped to each
terminal.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from modules import comparison_widget, database, date_picker, revenue_analysis as ra, table_style, terminal_mapping
from modules.formatting import format_money, format_pax, format_pct, format_spp
from modules.session import bootstrap_session, default_active_date, set_active_date
from modules.app_logger import safe_run, log_exception, show_friendly_error

st.set_page_config(page_title="Traffic & Terminal Analysis", page_icon="🛂", layout="wide")

bootstrap_session()


def _pct_or_dash(value) -> str:
    return f"{value:.2f}%" if pd.notna(value) else "—"


st.title("🛂 Traffic & Terminal Analysis")
st.caption(
    "Traffic = total airport visitors that day. PAX = customers who used Encalm's "
    "services. Penetration % = PAX ÷ Traffic. SPP = Revenue ÷ Traffic."
)

available_dates = database.get_available_dates()
available_traffic_dates = database.get_available_traffic_dates()

if not available_dates:
    st.info("No revenue data available yet. Upload a report on the main page first.")
    st.stop()

if not available_traffic_dates:
    st.info(
        "📡 No traffic data loaded yet. "
        "Go to the **Home** page and use the **Traffic Data Import** section "
        "to upload a traffic file — once imported, Penetration %, SPP, and "
        "terminal-wise analysis will appear here automatically."
    )
    st.stop()


# ---------------------------------------------------------------------------
# Date + comparison selector
# ---------------------------------------------------------------------------

selected_date = date_picker.render_date_dropdown(
    available_dates, key_prefix="traffic", label="Report Date", default_date=default_active_date()
)
set_active_date(selected_date)

ranges = comparison_widget.render_comparison_selector(selected_date, key_prefix="traffic")

current_revenue_df = database.load_for_date_range(ranges["current_start"], ranges["current_end"])
compare_revenue_df = database.load_for_date_range(ranges["compare_start"], ranges["compare_end"])

current_joined = database.join_revenue_with_traffic(current_revenue_df)
compare_joined = database.join_revenue_with_traffic(compare_revenue_df)

st.caption(f"Comparing **{ranges['current_label']}** against **{ranges['compare_label']}**.")

current_short_label, compare_short_label = ra.short_period_label_for_ranges(ranges)

if current_revenue_df.empty:
    st.warning(f"No revenue rows found for {ranges['current_label']}.")
    st.stop()

if not ra.has_traffic_data(current_joined):
    st.info(
        f"No traffic data loaded for {ranges['current_label']} yet — "
        f"upload traffic covering this period to see Penetration %/SPP here."
    )
    st.stop()

if "traffic_is_estimated" in current_joined.columns and current_joined["traffic_is_estimated"].fillna(False).any():
    st.info(
        "ℹ️ Some traffic figures for this period are estimated by prorating a monthly "
        "total (no daily-level traffic was available for that stretch) — treat "
        "Penetration %/SPP for the affected location as approximate."
    )
if "traffic_missing_days" in current_joined.columns and (current_joined["traffic_missing_days"].fillna(0) > 0).any():
    missing_summary = (
        current_joined.drop_duplicates(subset=["location"])[["location", "traffic_missing_days"]]
    )
    missing_summary = missing_summary[missing_summary["traffic_missing_days"] > 0]
    if not missing_summary.empty:
        details = ", ".join(
            f"{r['location']} ({int(r['traffic_missing_days'])} day(s))" for _, r in missing_summary.iterrows()
        )
        st.warning(
            f"⚠️ No traffic data at all for part of {ranges['current_label']}: {details}. "
            f"Traffic/Penetration %/SPP figures below are understated for these locations."
        )

# ---------------------------------------------------------------------------
# Location-level Penetration % / SPP + variance
# ---------------------------------------------------------------------------

st.subheader("📊 Penetration % and SPP by Location")

with safe_run("PEN SPP Table", error_type="traffic_columns"):
    table_style.render_penetration_spp_table(
    st, ra, database, current_joined, compare_joined, current_revenue_df,
    current_short_label, compare_short_label,
    format_money, format_pax, format_pct,
)

st.divider()

# ---------------------------------------------------------------------------
# Driver narrative
# ---------------------------------------------------------------------------

st.subheader("🔎 What's Driving the Change")

if current_joined is None or current_joined.empty or not ra.has_traffic_data(current_joined):
    st.info("No traffic data loaded for this period yet — driver narrative needs Traffic to explain Penetration %/SPP moves.")
else:
    with safe_run("Driver Narrative"):
        variance = ra.penetration_spp_variance(current_joined, compare_joined)
        for _, row in variance.iterrows():
            location = row["location"]
            revenue_explanation = ra.explain_revenue_driver(
                traffic_pct=row.get("traffic_pct_change"),
                penetration_pct_change=row.get("penetration_pct_change"),
                spp_pct_change=row.get("spp_pct_change"),
                revenue_pct=row.get("revenue_pct_change"),
            )
            penetration_explanation = ra.explain_penetration_driver(
                traffic_pct=row.get("traffic_pct_change"),
                pax_pct=row.get("pax_pct_change"),
                penetration_pct_change=row.get("penetration_pct_change"),
            )
            st.markdown(f"**{location}**")
            # If both are "—", traffic data is missing for the comparison period
            if revenue_explanation == "—" and penetration_explanation == "—":
                rev_pct = row.get("revenue_pct_change")
                if rev_pct is not None and rev_pct == rev_pct:
                    direction = "increased" if rev_pct > 0 else "declined" if rev_pct < 0 else "was stable"
                    st.markdown(f"- Revenue {direction} ({ra.format_pct(rev_pct) if hasattr(ra, 'format_pct') else f'{rev_pct*100:.1f}%'}). Traffic data for the comparison period is not available — upload traffic for that period to see the full driver analysis.")
                else:
                    st.caption(f"Traffic data for the comparison period is not available for {location}. Upload traffic covering that period to see the driver narrative.")
            else:
                if revenue_explanation != "—":
                    st.markdown(f"- {revenue_explanation}")
                if penetration_explanation != "—":
                    st.markdown(f"- {penetration_explanation}")

st.divider()

# ---------------------------------------------------------------------------
# Terminal-wise section (Delhi has T1/T2/T3; Hyderabad/Goa single-terminal)
# ---------------------------------------------------------------------------

st.subheader("🛫 Terminal-wise Breakdown")
st.caption(
    "Outlets are mapped to terminals based on naming convention and known "
    "airport layout — this mapping is provisional until cross-checked "
    "against the traffic file's own terminal labels. Unmapped outlets are "
    "shown separately below rather than silently excluded."
)

with safe_run("Terminal Breakdown", error_type="traffic_columns"):
  current_tagged = terminal_mapping.add_terminal_column(current_revenue_df)
  location_for_terminal = st.selectbox(
    "Location", options=sorted(current_tagged["location"].unique()), key="terminal_location_select"
)

terminal_slice = current_tagged[current_tagged["location"] == location_for_terminal]
terminal_summary = terminal_slice.groupby("terminal", dropna=False, as_index=False).agg(
    revenue=("revenue", "sum"), pax=("pax", "sum")
)
terminal_summary["terminal"] = terminal_summary["terminal"].fillna("Airport-wide services")

traffic_for_location = database.load_traffic_for_date_range(
    ranges["current_start"], ranges["current_end"]
)
traffic_for_location = traffic_for_location[traffic_for_location["location"] == location_for_terminal]
if not traffic_for_location.empty:
    traffic_by_terminal = traffic_for_location.groupby("terminal", as_index=False)["traffic"].sum()
    traffic_by_terminal["terminal"] = traffic_by_terminal["terminal"].replace(
        {"": terminal_mapping.MAIN_TERMINAL}
    )
    terminal_summary = terminal_summary.merge(
        traffic_by_terminal, on="terminal", how="left"
    )
    terminal_summary["penetration_pct"] = terminal_summary.apply(
        lambda r: ra.penetration_pct(r["pax"], r.get("traffic")), axis=1
    )
    terminal_summary["spp"] = terminal_summary.apply(
        lambda r: ra.spp(r["revenue"], r.get("traffic")), axis=1
    )
else:
    st.info(
        f"No terminal-level traffic loaded for {location_for_terminal} yet — "
        f"showing revenue/PAX by terminal only."
    )

term_display = terminal_summary.copy()
term_display["revenue"] = term_display["revenue"].apply(format_money)
term_display["pax"] = term_display["pax"].apply(format_pax)
if "traffic" in term_display.columns:
    term_display["traffic"] = term_display["traffic"].apply(format_pax)
if "penetration_pct" in term_display.columns:
    term_display["penetration_pct"] = term_display["penetration_pct"].apply(_pct_or_dash)
if "spp" in term_display.columns:
    term_display["spp"] = term_display["spp"].apply(format_spp)  # was format_money
term_display = term_display.rename(
    columns={
        "terminal": "Terminal", "revenue": "Revenue", "pax": "PAX",
        "traffic": "Traffic", "penetration_pct": "Penetration %", "spp": "SPP",
    }
)
st.dataframe(term_display, use_container_width=True, hide_index=True)

unmapped = terminal_mapping.get_unmapped_outlets(current_revenue_df)
unmapped_here = unmapped[unmapped["location"] == location_for_terminal]
if not unmapped_here.empty:
    with st.expander(f"⚠️ {len(unmapped_here)} outlet(s) not yet mapped to a terminal"):
        st.dataframe(unmapped_here, use_container_width=True, hide_index=True)

fig = px.bar(
    terminal_summary, x="terminal", y="revenue",
    text=terminal_summary["revenue"].apply(format_money),
    color="terminal", color_discrete_sequence=px.colors.qualitative.Set2,
)
fig.update_layout(showlegend=False, xaxis_title="Terminal", yaxis_title="Revenue")
st.plotly_chart(fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Domestic vs International traffic (shown for any location whose traffic
# file reports this split — e.g. via the "Sum of PAX" Arrival/Departure by
# Domestic/International cross-tab format) — kept separate from the
# outlet-based Terminal-wise section above, since Domestic/International is
# a breakdown of the traffic data itself, not a physical terminal outlets
# get mapped to.
# ---------------------------------------------------------------------------

st.subheader("🌍 Domestic vs International Traffic")
st.caption(
    "Shown for any location whose traffic file reports a Domestic/International "
    "split. A location's overall Traffic total elsewhere on this page already "
    "includes both — this section just breaks that total apart."
)

_DOM_INTL_LABELS = ("Domestic", "International")

# Delhi stores traffic per terminal pool (T1 Dep, T3 Dom Dep etc.) rather than
# as "Domestic"/"International" labels. Map them here so Delhi appears in the
# Dom vs Int section alongside Hyderabad and Goa.
_DELHI_DOM_TERMINALS  = {"T1 Dep", "T1 Arr", "T2 Dep", "T2 Arr", "T3 Dom Dep", "T3 Dom Arr"}
_DELHI_INT_TERMINALS  = {"T3 Int Dep", "T3 Int Arr"}
# Load Dom/Int traffic using the widest possible date window for each
# selected period. Some locations store traffic under the actual dates
# in their source file — e.g. a June upload may have May dates in the
# sheet. We expand the search window by ±31 days to catch these.
import datetime as _dt_page4
_expand = _dt_page4.timedelta(days=31)
_cur_start  = ranges["current_start"]  - _expand
_cur_end    = ranges["current_end"]    + _expand
_cmp_start  = ranges["compare_start"] - _expand
_cmp_end    = ranges["compare_end"]   + _expand
current_traffic_all = database.load_traffic_for_date_range(_cur_start, _cur_end)
compare_traffic_all = database.load_traffic_for_date_range(_cmp_start, _cmp_end)

current_dom_intl = current_traffic_all[current_traffic_all["terminal"].isin(_DOM_INTL_LABELS)].copy()

# Add Delhi as Domestic / International by aggregating its terminal pools
for _traf_df, _dom_intl_df_name in [
    (current_traffic_all, "current"),
    (compare_traffic_all, "compare"),
]:
    _delhi = _traf_df[_traf_df["location"] == "Delhi"]
    if not _delhi.empty:
        _dom_total = _delhi[_delhi["terminal"].isin(_DELHI_DOM_TERMINALS)]["traffic"].sum()
        _int_total = _delhi[_delhi["terminal"].isin(_DELHI_INT_TERMINALS)]["traffic"].sum()
        _new_rows = []
        if _dom_total > 0:
            _new_rows.append({"location": "Delhi", "terminal": "Domestic",
                               "traffic": _dom_total, "granularity": "derived"})
        if _int_total > 0:
            _new_rows.append({"location": "Delhi", "terminal": "International",
                               "traffic": _int_total, "granularity": "derived"})
        if _new_rows:
            import pandas as _pd_tmp
            _new_df = _pd_tmp.DataFrame(_new_rows)
            if _dom_intl_df_name == "current":
                current_dom_intl = _pd_tmp.concat([current_dom_intl, _new_df], ignore_index=True)
if current_dom_intl.empty:
    st.info(
        "No location currently has Domestic/International traffic data loaded for "
        "this period — upload a traffic file with that breakdown to see it here."
    )
else:
    current_summary = current_dom_intl.groupby(["location", "terminal"], as_index=False)["traffic"].sum()
    current_summary = current_summary.rename(
        columns={"location": "Location", "terminal": "Type", "traffic": f"Traffic ({current_short_label})"}
    )

    _cmp_base = (
        compare_traffic_all[compare_traffic_all["terminal"].isin(_DOM_INTL_LABELS)].copy()
        if compare_traffic_all is not None and not compare_traffic_all.empty
        else pd.DataFrame(columns=["location", "terminal", "traffic"])
    )
    # Add Delhi Dom/Int from compare traffic
    if compare_traffic_all is not None and not compare_traffic_all.empty:
        _delhi_cmp = compare_traffic_all[compare_traffic_all["location"] == "Delhi"]
        if not _delhi_cmp.empty:
            _dom_c = _delhi_cmp[_delhi_cmp["terminal"].isin(_DELHI_DOM_TERMINALS)]["traffic"].sum()
            _int_c = _delhi_cmp[_delhi_cmp["terminal"].isin(_DELHI_INT_TERMINALS)]["traffic"].sum()
            _cmp_rows = []
            if _dom_c > 0:
                _cmp_rows.append({"location": "Delhi", "terminal": "Domestic", "traffic": _dom_c})
            if _int_c > 0:
                _cmp_rows.append({"location": "Delhi", "terminal": "International", "traffic": _int_c})
            if _cmp_rows:
                _cmp_base = pd.concat([_cmp_base, pd.DataFrame(_cmp_rows)], ignore_index=True)
    compare_dom_intl = _cmp_base
    if not compare_dom_intl.empty:
        compare_summary = compare_dom_intl.groupby(["location", "terminal"], as_index=False)["traffic"].sum()
        compare_summary = compare_summary.rename(
            columns={"location": "Location", "terminal": "Type", "traffic": f"Traffic ({compare_short_label})"}
        )
        merged = current_summary.merge(compare_summary, on=["Location", "Type"], how="outer")
    else:
        merged = current_summary.copy()
        merged[f"Traffic ({compare_short_label})"] = 0.0

    merged[f"Traffic ({current_short_label})"] = merged[f"Traffic ({current_short_label})"].fillna(0.0)
    merged[f"Traffic ({compare_short_label})"] = merged[f"Traffic ({compare_short_label})"].fillna(0.0)
    merged["Traffic Δ%"] = merged.apply(
        lambda r: ra.pct_change(r[f"Traffic ({current_short_label})"], r[f"Traffic ({compare_short_label})"]),
        axis=1,
    )

    display = merged.copy()
    display[f"Traffic ({current_short_label})"] = display[f"Traffic ({current_short_label})"].apply(format_pax)
    display[f"Traffic ({compare_short_label})"] = display[f"Traffic ({compare_short_label})"].apply(format_pax)
    display["Traffic Δ%"] = display["Traffic Δ%"].apply(lambda v: format_pct(v) if pd.notna(v) else "—")
    ordered_cols = ["Location", "Type", f"Traffic ({current_short_label})", f"Traffic ({compare_short_label})", "Traffic Δ%"]
    display = display[[c for c in ordered_cols if c in display.columns]]
    st.dataframe(table_style.style_pct_columns(display, ["Traffic Δ%"]), use_container_width=True, hide_index=True)

    st.markdown("**Domestic vs International Mix (current period)**")
    mix_cols = st.columns(min(3, current_dom_intl["location"].nunique()) or 1)
    for i, loc in enumerate(sorted(current_dom_intl["location"].unique())):
        loc_rows = current_dom_intl[current_dom_intl["location"] == loc]
        total = loc_rows["traffic"].sum()
        if total <= 0:
            continue
        fig_mix = px.pie(
            loc_rows, values="traffic", names="terminal", title=loc, hole=0.55,
            color="terminal",
            color_discrete_map={"Domestic": "#2E8B57", "International": "#4A6FA5"},
        )
        fig_mix.update_traces(textinfo="percent+label")
        fig_mix.update_layout(showlegend=False, margin=dict(t=40, b=10, l=10, r=10), height=280)
        with mix_cols[i % len(mix_cols)]:
            st.plotly_chart(fig_mix, use_container_width=True, key=f"domintl_mix_{loc}")
