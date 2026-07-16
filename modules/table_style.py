"""
table_style.py — Shared green-for-growth / red-for-decline color coding.

Two display contexts need this, handled differently:
  1. st.metric() deltas already auto-color (green up / red down) via
     Streamlit's built-in delta_color="normal" behavior — metric_delta_color()
     just centralizes picking the right delta value/color args so every
     page does it the same way (including the "off"/no-data case).
  2. st.dataframe() table cells need an explicit pandas Styler, since plain
     st.dataframe doesn't color text conditionally on its own. style_pct_columns()
     applies the standard color rule to one or more "...Δ%" -style columns
     of an already-formatted-as-strings display DataFrame.

Color rule (applied consistently everywhere): growth -> green, decline ->
red, flat/undefined ("—") -> neutral gray. This mirrors the same ±5%-ish
"stable" band used by revenue_analysis.classify_trend, but for raw coloring
purposes here we color any positive value green and any negative value red
(zero and "—" stay neutral) since the ask is specifically "growth vs
decline", not "above/below the stability threshold".
"""

from __future__ import annotations

from typing import Iterable, Optional, Union

import pandas as pd

GROWTH_COLOR = "#0B7A57"
DECLINE_COLOR = "#B91C1C"
NEUTRAL_COLOR = "#64748B"

GROWTH_BG = "#E9F8F0"
DECLINE_BG = "#FDECEC"


def _parse_pct_string(cell: object) -> Optional[float]:
    """
    Recover a numeric sign from an already-formatted percentage string
    (e.g. "+8.70%", "-3.40%", "—"), since by the time a display DataFrame
    reaches styling it's typically already been run through format_pct().
    Also tolerates raw numeric/NaN values directly, so this can be used on
    either formatted or unformatted columns.
    """
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        if pd.isna(cell):
            return None
        return float(cell)
    text = str(cell).strip()
    if text in ("", "—", "-", "nan", "None"):
        return None
    cleaned = text.replace("%", "").replace("+", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _color_for_value(numeric_value: Optional[float], with_background: bool) -> str:
    if numeric_value is None or numeric_value == 0:
        color = NEUTRAL_COLOR
        bg = None
    elif numeric_value > 0:
        color = GROWTH_COLOR
        bg = GROWTH_BG
    else:
        color = DECLINE_COLOR
        bg = DECLINE_BG

    style = f"color: {color}; font-weight: 600;"
    if with_background and bg:
        style += f" background-color: {bg};"
    return style


def style_pct_columns(
    df: pd.DataFrame,
    columns: Iterable[str],
    with_background: bool = False,
) -> "pd.io.formats.style.Styler":
    """
    Return a pandas Styler for `df` that colors the given column(s) green
    for growth, red for decline, neutral gray for flat/undefined — based on
    each cell\'s own value (works whether the column holds raw floats or
    already-formatted percentage strings like "+8.70%").

    Pass the result straight to st.dataframe(): st.dataframe(style_pct_columns(df, ["Rev Δ%"]))
    Columns not present in `df` are silently skipped (no-op), so callers
    don\'t need to guard every call with an `if column in df.columns` check.

    FIX: pandas 2.x on Python 3.14 raises a KeyError inside Styler.map()
    when the DataFrame is a column-slice whose internal index doesn\'t
    align with the subset list. Reset to a clean RangeIndex copy first.
    """
    # Reset index so pandas Styler subset matching works correctly on
    # column-sliced DataFrames in Python 3.14.
    df = df.reset_index(drop=True).copy()
    present_columns = [c for c in columns if c in df.columns]
    styler = df.style
    if not present_columns:
        return styler

    def _apply(cell):
        return _color_for_value(_parse_pct_string(cell), with_background)

    try:
        styler = styler.map(_apply, subset=present_columns)
    except (KeyError, TypeError):
        # Fallback: apply one column at a time to avoid any subset-indexing
        # quirk in the current pandas / Python version.
        for col in present_columns:
            try:
                styler = styler.map(_apply, subset=[col])
            except Exception:
                pass
    return styler


def render_penetration_spp_table(
    st_module,
    ra_module,
    database_module,
    current_joined: pd.DataFrame,
    compare_joined: pd.DataFrame,
    current_raw_df: pd.DataFrame,
    current_label: str,
    compare_label: str,
    format_money,
    format_pax,
    format_pct,
) -> None:
    """
    Render a location-level Traffic / Revenue / PAX / Penetration % / SPP
    / AOP comparison table — current period vs compare period — using
    revenue_analysis.penetration_spp_variance() and add_aop_columns()
    under the hood. Shared so the columns, formatting, and coloring stay
    identical everywhere this shows up.

    `current_joined`/`compare_joined` must already have traffic joined in
    via database.join_revenue_with_traffic(). `current_raw_df` is the
    current period's plain revenue rows (not traffic-joined), needed
    separately to look up AOP targets via database.join_revenue_with_aop.

    Streamlit/revenue_analysis/database are passed in rather than
    imported here, to avoid a circular import (table_style is a
    low-level display module; the page modules that call this already
    import table_style, not the other way around). The three formatter
    functions are passed in for the same reason — they live in
    modules.formatting, which this module doesn't otherwise depend on.
    """
    if current_joined is None or current_joined.empty or not ra_module.has_traffic_data(current_joined):
        st_module.info(
            f"No traffic data loaded for {current_label} yet — upload traffic "
            f"covering this period to see Penetration %/SPP here."
        )
        return

    variance = ra_module.penetration_spp_variance(current_joined, compare_joined)
    variance, aop_cols = add_aop_columns(variance, database_module, ra_module, current_raw_df, ["location"], current_label)

    def _pct_or_dash(value) -> str:
        return f"{value:.2f}%" if pd.notna(value) else "—"

    from .formatting import format_spp  # intelligent SPP formatter

    display = variance.copy()
    for col in ["revenue_current", "revenue_compare"]:
        if col in display.columns:
            display[col] = display[col].apply(format_money)
    for col in ["pax_current", "pax_compare"]:
        if col in display.columns:
            display[col] = display[col].apply(format_pax)
    for col in ["traffic_current", "traffic_compare"]:
        if col in display.columns:
            display[col] = display[col].apply(format_pax)
    for col in ["penetration_pct_current", "penetration_pct_compare"]:
        if col in display.columns:
            display[col] = display[col].apply(_pct_or_dash)
    for col in ["spp_current", "spp_compare"]:
        if col in display.columns:
            display[col] = display[col].apply(format_spp)  # was format_money
    for col in ["traffic_pct_change", "revenue_pct_change", "pax_pct_change", "penetration_pct_change", "spp_pct_change"]:
        if col in display.columns:
            display[col] = display[col].apply(format_pct)
    display = format_aop_columns(display, aop_cols, format_money)

    rename_map = {
        "location": "Location",
        "revenue_current": f"Rev ({current_label})",
        "revenue_compare": f"Rev ({compare_label})",
        "revenue_pct_change": "Rev Δ%",
        "pax_current": f"PAX ({current_label})",
        "pax_compare": f"PAX ({compare_label})",
        "pax_pct_change": "PAX Δ%",
        "traffic_current": f"Traffic ({current_label})",
        "traffic_compare": f"Traffic ({compare_label})",
        "traffic_pct_change": "Traffic Δ%",
        "penetration_pct_current": f"PEN ({current_label})",
        "penetration_pct_compare": f"PEN ({compare_label})",
        "penetration_pct_change": "PEN Δ%",
        "spp_current": f"SPP ({current_label})",
        "spp_compare": f"SPP ({compare_label})",
        "spp_pct_change": "SPP Δ%",
    }
    ordered_cols = [c for c in rename_map if c in display.columns] + aop_cols
    out = display[ordered_cols].rename(columns=rename_map)
    pct_cols = [c for c in ["Rev Δ%", "PAX Δ%", "Traffic Δ%", "PEN Δ%", "SPP Δ%"] if c in out.columns]
    pct_cols += [c for c in aop_cols if "Variance" in c]
    st_module.dataframe(style_pct_columns(out, pct_cols), use_container_width=True, hide_index=True)

    if "traffic_is_estimated" in current_joined.columns and current_joined["traffic_is_estimated"].fillna(False).any():
        st_module.caption(
            "ℹ️ Some traffic figures for the current period are estimated by prorating a "
            "monthly total — treat Penetration %/SPP as approximate for the affected location(s)."
        )
    if not aop_cols:
        st_module.caption("ℹ️ No AOP target data available for this period yet.")


COMPARISON_TYPE_SHORT = {"Day-wise": "DoD", "Week-wise": "WoW", "Month-wise": "MoM", "Year-wise": "YoY"}


def add_location_traffic_pen_columns(
    comparison_df: pd.DataFrame,
    database_module,
    ra_module,
    current_raw_df: pd.DataFrame,
    compare_raw_df: pd.DataFrame,
    current_label: str,
    compare_label: str,
    delta_suffix: str,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Merge Traffic, Penetration %, and SPP onto any comparison table that
    has a "location" column.

    Traffic, Penetration %, and SPP are all grain-sensitive:

    * **Outlet-level table** (has both "outlet" and "location" columns):
      Each outlet receives the traffic of its OWN terminal, not the
      whole-airport total.  This is sourced from terminal_mapping.py
      which maps every outlet to its terminal pool:
        T1D Lounge       → T1 Dep    (1,375,341 for June 2026)
        INL 5&6          → T3 Int Dep  (843,621)
        LA22/LA01/LA12   → T3 Arr (T3 Dom Arr + T3 Int Arr = 1,421,421)
        Enwrap           → All Dep (3,355,424)
        M&G              → All    (6,557,945)
      This means different outlets at the same airport show DIFFERENT
      traffic values, which is correct per the Business Plan formulas:
        PEN  = outlet_pax     ÷ outlet_terminal_traffic × 100
        SPP  = outlet_revenue ÷ outlet_terminal_traffic

    * **Location/segment-level table** (no "outlet" column):
      Location-wide total traffic is used (correct at this grain since
      the PAX and Revenue are also aggregated to location level).

    Returns (dataframe_with_new_columns, list_of_new_column_names).
    Silently returns the input unchanged if `comparison_df` has no
    "location" column or if traffic data is not available for the period.
    """
    if "location" not in comparison_df.columns:
        return comparison_df, []

    import pandas as pd

    empty_traffic = pd.DataFrame(columns=["location", "traffic", "penetration_pct", "spp"])
    outlet_grain = "outlet" in comparison_df.columns

    # Detect unmapped outlets — report them so users know which outlets
    # have no terminal assignment and therefore show "—" in traffic columns.
    if outlet_grain and "outlet" in comparison_df.columns and "location" in comparison_df.columns:
        try:
            from . import terminal_mapping as _tm
            truly_unmapped = []
            for _, row in comparison_df[["outlet", "location"]].drop_duplicates().iterrows():
                t = _tm.get_terminal_for_outlet(row["outlet"], row["location"])
                # None = intentionally excluded (non-airport services, closed outlets)
                # DEFAULT_TERMINAL_FALLBACK = genuinely missing from the mapping
                if t == _tm.DEFAULT_TERMINAL_FALLBACK:
                    truly_unmapped.append(f"{row['outlet']} ({row['location']})")
            if truly_unmapped:
                import streamlit as _st
                _st.caption(
                    f"⚠️ {len(truly_unmapped)} outlet(s) are not yet in the terminal mapping "
                    f"and will show '—' for Traffic/PEN/SPP: {', '.join(truly_unmapped[:5])}"
                    + (" …" if len(truly_unmapped) > 5 else "")
                    + ". Add them to modules/terminal_mapping.py to enable traffic metrics."
                )
        except Exception:
            pass

    def _location_traffic_raw(raw_df):
        """Return joined df with traffic attached (for outlet-grain calc)."""
        if raw_df is None or raw_df.empty:
            return None
        joined = database_module.join_revenue_with_traffic(raw_df)
        if joined is None or joined.empty or not ra_module.has_traffic_data(joined):
            return None
        return joined

    def _location_traffic_summary(raw_df):
        """Return location-level [location, traffic, penetration_pct, spp]."""
        if raw_df is None or raw_df.empty:
            return empty_traffic
        joined = _location_traffic_raw(raw_df)
        if joined is None:
            return empty_traffic
        return ra_module.location_level_summary_with_traffic(joined)[
            ["location", "traffic", "penetration_pct", "spp"]
        ]

    def _outlet_pen_spp(raw_df, joined_with_traffic):
        """
        Compute per-outlet PEN % and SPP using each outlet's CORRECT terminal
        traffic pool (per terminal_mapping.get_terminal_for_outlet), not the
        whole-location total.

        Per Business Plan:
          PEN % = outlet PAX / terminal_traffic * 100
          SPP   = outlet revenue / terminal_traffic
          where terminal_traffic is the outlet's assigned pool:
            T3 Dom Dep for T3D49/DLO2/Air India/etc.
            T3 Int Dep for INL5&6/Premium/Xenia/etc.
            T3 Dom Arr + T3 Int Arr for LA01/LA12/LA22

        Returns DataFrame with [segment, outlet, location, penetration_pct, spp].
        """
        if raw_df is None or raw_df.empty:
            return pd.DataFrame(columns=["segment", "outlet", "location", "penetration_pct", "spp"])

        # Use the outlet-level traffic join for correct per-terminal figures
        outlet_traffic = database_module.join_revenue_with_traffic_by_outlet(raw_df)
        if outlet_traffic is None or outlet_traffic.empty:
            return pd.DataFrame(columns=["segment", "outlet", "location", "penetration_pct", "spp"])

        # Merge segment back in if present
        agg_cols = ["segment", "outlet", "location"] if "segment" in raw_df.columns else ["outlet", "location"]
        outlet_agg = raw_df.groupby(agg_cols, as_index=False).agg(
            pax=("pax", "sum"), revenue=("revenue", "sum")
        )
        merged = outlet_agg.merge(outlet_traffic[["outlet", "location", "traffic"]],
                                  on=["outlet", "location"], how="left")
        merged["penetration_pct"] = merged.apply(
            lambda r: ra_module.safe_div(r["pax"], r["traffic"]) * 100
            if pd.notna(r.get("traffic")) and r["traffic"] > 0 else None,
            axis=1,
        )
        merged["spp"] = merged.apply(
            lambda r: ra_module.safe_div(r["revenue"], r["traffic"])
            if pd.notna(r.get("traffic")) and r["traffic"] > 0 else None,
            axis=1,
        )
        keep = [c for c in ["segment", "outlet", "location", "penetration_pct", "spp"] if c in merged.columns]
        return merged[keep]

    # --- Fetch traffic data ---
    current_joined  = _location_traffic_raw(current_raw_df)
    compare_joined  = _location_traffic_raw(compare_raw_df)
    current_summary = _location_traffic_summary(current_raw_df)
    compare_summary = _location_traffic_summary(compare_raw_df)

    if current_summary.empty and compare_summary.empty:
        return comparison_df, []

    # --- Column names ---
    traffic_current_col = f"Traffic ({current_label})"
    traffic_compare_col = f"Traffic ({compare_label})"
    pen_current_col     = f"PEN ({current_label})"
    pen_compare_col     = f"PEN ({compare_label})"
    spp_current_col     = f"SPP ({current_label})"
    spp_compare_col     = f"SPP ({compare_label})"
    traffic_delta_col   = f"Traffic Δ% ({delta_suffix})"
    pen_delta_col       = f"PEN Δ% ({delta_suffix})"
    spp_delta_col       = f"SPP Δ% ({delta_suffix})"

    # --- Merge Traffic, PEN and SPP ---
    # At outlet grain: each outlet gets its OWN terminal traffic
    #   (T1D Lounge → T1 Dep, INL 5&6 → T3 Int Dep, LA22 → T3 Arr, etc.)
    # At location/segment grain: location-wide total traffic is used.
    if outlet_grain:
        merge_keys = [c for c in ["segment", "outlet", "location"] if c in comparison_df.columns]

        # get_outlet_traffic_series returns (outlet, location) → terminal traffic
        def _outlet_traffic_pen_spp(raw_df):
            """Per-outlet terminal traffic + PEN % + SPP in one call."""
            if raw_df is None or raw_df.empty:
                return pd.DataFrame(columns=["outlet", "location", "traffic", "penetration_pct", "spp"])
            ot = database_module.join_revenue_with_traffic_by_outlet(raw_df)
            if ot is None or ot.empty:
                return pd.DataFrame(columns=["outlet", "location", "traffic", "penetration_pct", "spp"])
            # join_revenue_with_traffic_by_outlet already has traffic, revenue, pax
            ot["penetration_pct"] = ot.apply(
                lambda r: ra_module.safe_div(r["pax"], r["traffic"]) * 100
                if pd.notna(r.get("traffic")) and r["traffic"] > 0 else None,
                axis=1,
            )
            ot["spp"] = ot.apply(
                lambda r: ra_module.safe_div(r["revenue"], r["traffic"])
                if pd.notna(r.get("traffic")) and r["traffic"] > 0 else None,
                axis=1,
            )
            # Merge segment back in so we can join on full merge_keys
            if "segment" in raw_df.columns and "segment" not in ot.columns:
                seg_map = raw_df[["outlet", "location", "segment"]].drop_duplicates(subset=["outlet", "location"])
                ot = ot.merge(seg_map, on=["outlet", "location"], how="left")
            keep = [c for c in ["segment", "outlet", "location", "traffic", "penetration_pct", "spp"] if c in ot.columns]
            return ot[keep]

        cur_ops  = _outlet_traffic_pen_spp(current_raw_df)
        cmp_ops  = _outlet_traffic_pen_spp(compare_raw_df)

        out = comparison_df.copy()

        # Traffic columns — per outlet terminal traffic
        if not cur_ops.empty:
            out = out.merge(
                cur_ops[merge_keys + ["traffic"]].rename(columns={"traffic": traffic_current_col}),
                on=merge_keys, how="left",
            )
        else:
            out[traffic_current_col] = None

        if not cmp_ops.empty:
            out = out.merge(
                cmp_ops[merge_keys + ["traffic"]].rename(columns={"traffic": traffic_compare_col}),
                on=merge_keys, how="left",
            )
        else:
            out[traffic_compare_col] = None

        # PEN % columns
        if not cur_ops.empty:
            out = out.merge(
                cur_ops[merge_keys + ["penetration_pct"]].rename(columns={"penetration_pct": pen_current_col}),
                on=merge_keys, how="left",
            )
        else:
            out[pen_current_col] = None

        if not cmp_ops.empty:
            out = out.merge(
                cmp_ops[merge_keys + ["penetration_pct"]].rename(columns={"penetration_pct": pen_compare_col}),
                on=merge_keys, how="left",
            )
        else:
            out[pen_compare_col] = None

        # SPP columns
        if not cur_ops.empty:
            out = out.merge(
                cur_ops[merge_keys + ["spp"]].rename(columns={"spp": spp_current_col}),
                on=merge_keys, how="left",
            )
        else:
            out[spp_current_col] = None

        if not cmp_ops.empty:
            out = out.merge(
                cmp_ops[merge_keys + ["spp"]].rename(columns={"spp": spp_compare_col}),
                on=merge_keys, how="left",
            )
        else:
            out[spp_compare_col] = None

    else:
        # Location/segment grain — use location-wide totals (correct at this grain)
        out = comparison_df.merge(
            current_summary[["location", "traffic"]].rename(columns={"traffic": traffic_current_col}),
            on="location", how="left",
        )
        out = out.merge(
            compare_summary[["location", "traffic"]].rename(columns={"traffic": traffic_compare_col}),
            on="location", how="left",
        )
        out = out.merge(
            current_summary[["location", "penetration_pct", "spp"]].rename(
                columns={"penetration_pct": pen_current_col, "spp": spp_current_col}
            ),
            on="location", how="left",
        )
        out = out.merge(
            compare_summary[["location", "penetration_pct", "spp"]].rename(
                columns={"penetration_pct": pen_compare_col, "spp": spp_compare_col}
            ),
            on="location", how="left",
        )

    # --- Delta columns ---
    out[traffic_delta_col] = out.apply(
        lambda r: ra_module.pct_change(r.get(traffic_current_col), r.get(traffic_compare_col)), axis=1
    )
    out[pen_delta_col] = out.apply(
        lambda r: ra_module.pct_change(r.get(pen_current_col), r.get(pen_compare_col)), axis=1
    )
    out[spp_delta_col] = out.apply(
        lambda r: ra_module.pct_change(r.get(spp_current_col), r.get(spp_compare_col)), axis=1
    )

    new_cols = [
        traffic_current_col, traffic_compare_col, traffic_delta_col,
        pen_current_col, pen_compare_col, pen_delta_col,
        spp_current_col, spp_compare_col, spp_delta_col,
    ]
    return out, new_cols


def format_traffic_pen_columns(df: pd.DataFrame, new_cols: list[str], format_pax, format_money) -> pd.DataFrame:
    """
    Format the columns added by add_location_traffic_pen_columns() in
    place: Traffic columns as PAX-style integers, PEN columns and all
    three variance columns as percentages, SPP columns as money. Missing
    values (location had no traffic data) show as "—" rather than
    "nan%"/"0".
    """
    out = df.copy()
    for col in new_cols:
        if col not in out.columns:
            continue
        if col.startswith("Traffic (") :
            out[col] = out[col].apply(lambda v: format_pax(v) if pd.notna(v) else "—")
        elif col.startswith("PEN ("):
            out[col] = out[col].apply(lambda v: f"{v:.2f}%" if pd.notna(v) else "—")
        elif col.startswith("SPP ("):
            from .formatting import format_spp  # intelligent SPP formatter
            out[col] = out[col].apply(lambda v: format_spp(v) if pd.notna(v) else "-")
        elif col.startswith(("Traffic Δ%", "PEN Δ%", "SPP Δ%")):
            from .formatting import format_pct

            out[col] = out[col].apply(lambda v: format_pct(v) if pd.notna(v) else "—")
    return out


def add_aop_columns(
    comparison_df: pd.DataFrame,
    database_module,
    ra_module,
    current_raw_df: pd.DataFrame,
    group_cols: list[str],
    current_label: str,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Merge AOP Target and Variance (AOP vs Actuals) — for the CURRENT
    period only, matching the simplified AOP-vs-Actual format used on
    Revenue Comparison — onto any comparison table, grouped the same way
    group_cols says.

    Unlike Traffic (airport-wide, broadcast across every outlet sharing a
    location — see add_location_traffic_pen_columns), AOP targets are
    genuinely per-outlet and additive, so summing them up to segment or
    location grain is a real total, not a broadcast share; a segment
    with three outlets, two of which have targets, correctly gets the
    sum of just those two, not each outlet showing an identical
    location-wide number.

    Returns (df_with_new_columns, [aop_col, variance_col]) — the variance
    column is None-safe (see revenue_analysis.aop_variance): a
    segment/location with genuinely no AOP data (e.g. Sky Plates/Encalm
    Eats, out of scope for the outlet_monthly AOP format) gets NaN, not
    a misleading 0 or an infinite variance %. Silently returns the input
    unchanged if there's no AOP data at all for the period.
    """
    joined = database_module.join_revenue_with_aop(current_raw_df)
    if joined is None or joined.empty or joined["aop_target"].isna().all():
        return comparison_df, []

    if "segment" in group_cols and "segment" not in joined.columns:
        seg_lookup = current_raw_df[["location", "outlet", "segment"]].drop_duplicates(subset=["location", "outlet"])
        joined = joined.merge(seg_lookup, on=["location", "outlet"], how="left")

    work = joined.rename(columns={"aop_target": "aop"})
    variance = ra_module.aop_variance(work, group_cols=group_cols)

    aop_col = f"AOP ({current_label})"
    variance_col = "Variance (AOP vs Actuals)"
    variance = variance.rename(columns={"aop_target": aop_col, "variance_pct": variance_col})[
        group_cols + [aop_col, variance_col]
    ]

    out = comparison_df.merge(variance, on=group_cols, how="left")
    return out, [aop_col, variance_col]


def format_aop_columns(df: pd.DataFrame, new_cols: list[str], format_money) -> pd.DataFrame:
    """Format the columns added by add_aop_columns(): AOP as money, Variance as a percentage. NaN -> '—'."""
    out = df.copy()
    for col in new_cols:
        if col not in out.columns:
            continue
        if col.startswith("AOP ("):
            out[col] = out[col].apply(lambda v: format_money(v) if pd.notna(v) else "—")
        elif col == "Variance (AOP vs Actuals)":
            from .formatting import format_pct

            out[col] = out[col].apply(lambda v: format_pct(v) if pd.notna(v) else "—")
    return out


def metric_delta_args(pct_value: Optional[float]) -> dict:
    """
    Return the kwargs to pass to st.metric(...) so its built-in delta
    coloring (green up / red down) is applied consistently, including the
    no-data case (shows no delta rather than a misleading "0.00%").

    Usage: st.metric("Revenue", value_str, **metric_delta_args(pct))
    """
    if pct_value is None or (isinstance(pct_value, float) and pd.isna(pct_value)):
        return {"delta": None}
    try:
        if pct_value in (float("inf"), float("-inf")):
            return {"delta": None}
    except TypeError:
        return {"delta": None}
    from .formatting import format_pct

    return {"delta": format_pct(pct_value), "delta_color": "normal"}
