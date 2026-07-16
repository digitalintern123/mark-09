"""
revenue_analysis.py — Comparison and metrics engine.

Every function here takes already-loaded DataFrames (the caller is
responsible for pulling them from the database via modules.database) and
returns either a scalar metrics dict or a comparison DataFrame. Keeping this
module DB-agnostic makes it easy to unit-test and reuse across pages.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import numpy as np
import pandas as pd

# FIX (Bug 5): import the single canonical month-shift implementation.
# The private _safe_month_shift_local() defined at the bottom of this file
# is removed; callers that imported it directly (comparison_widget.py)
# are updated to use this public name instead.
from .date_utils import safe_month_shift as _safe_month_shift_local

GROWTH_THRESHOLD = 0.05  # +5%
DECLINE_THRESHOLD = -0.05  # -5%

GROUP_COLS = ["segment", "outlet", "location"]


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------

def classify_trend(pct_change: Optional[float], metric_label: str = "Revenue") -> str:
    """
    Map a fractional change (e.g. 0.08 for +8%) to a trend label.

    `metric_label` names whatever is actually being classified (e.g.
    "Revenue" or "PAX") — this function is reused for both, so the label
    must be passed in rather than hardcoded, or a PAX trend would
    incorrectly read "Revenue Increase"/"Revenue Decline".
    """
    if pct_change is None or pd.isna(pct_change):
        return "➡️ Stable"
    if pct_change > GROWTH_THRESHOLD:
        return f"📈 {metric_label} Increase"
    if pct_change < DECLINE_THRESHOLD:
        return f"📉 {metric_label} Decline"
    return "➡️ Stable"


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """
    Fractional change from `previous` to `current`. Returns None if undefined.

    FIX (Bug 3): previously returned float("inf") / float("-inf") when
    previous == 0 and current != 0.  While mathematically correct, inf
    propagated into the UI as the literal string "inf%" (via format_pct,
    which already had an isinf guard, but some call-sites used the raw
    value directly before formatting — e.g. st.metric() delta strings).

    The fix caps the result at +10.0 (+1000%) for a new-positive and
    -1.0 (-100%) for a new-negative when the base is zero, which:
      - Is a meaningful, renderable percentage ("+1,000.00%" reads as
        "new entrant / no prior baseline").
      - Keeps classify_trend() working correctly (anything > 0.05
        is still "Revenue Increase").
      - Preserves None for the 0→0 case (no change, no baseline).
      - Never produces the raw string "inf" in any display path.

    Internal logic that truly needs to distinguish "infinity" from "large
    number" should not rely on this helper; those callers already check
    math.isinf() via _is_usable() before consuming the value.
    """
    if current is None or previous is None or pd.isna(current) or pd.isna(previous):
        return None
    if previous == 0:
        if current == 0:
            return None          # 0 → 0: no change, no meaningful %
        # FIX: use a capped sentinel rather than inf so format_pct() and
        # st.metric() always receive a finite, displayable number.
        return 10.0 if current > 0 else -1.0
    return (current - previous) / previous


def safe_div(numerator, denominator):
    """Division that returns NaN instead of raising/inf on a zero denominator."""
    if denominator is None or denominator == 0 or pd.isna(denominator):
        return np.nan
    if numerator is None or pd.isna(numerator):
        return np.nan
    return numerator / denominator


# ---------------------------------------------------------------------------
# Core comparison: any two periods, at outlet grain
# ---------------------------------------------------------------------------

def compare_periods(
    current_df: pd.DataFrame,
    compare_df: pd.DataFrame,
    group_cols: list[str] = GROUP_COLS,
) -> pd.DataFrame:
    """
    Build an outlet-level comparison table between two already-filtered
    DataFrames (typically one date's worth of rows each, but works for any
    date range — the caller decides what "current" and "compare" mean).

    Returns columns:
      segment, outlet, location,
      current_revenue, compare_revenue, revenue_change, revenue_pct_change, revenue_trend,
      current_pax, compare_pax, pax_change, pax_pct_change, pax_trend
    """
    current_agg = _aggregate(current_df, group_cols, suffix="current")
    compare_agg = _aggregate(compare_df, group_cols, suffix="compare")

    merged = pd.merge(current_agg, compare_agg, on=group_cols, how="outer")

    for col in ["current_revenue", "compare_revenue", "current_pax", "compare_pax"]:
        if col not in merged.columns:
            merged[col] = 0.0
        merged[col] = merged[col].fillna(0.0)

    merged["revenue_change"] = merged["current_revenue"] - merged["compare_revenue"]
    merged["revenue_pct_change"] = merged.apply(
        lambda r: pct_change(r["current_revenue"], r["compare_revenue"]), axis=1
    )
    merged["revenue_trend"] = merged["revenue_pct_change"].apply(classify_trend)

    merged["pax_change"] = merged["current_pax"] - merged["compare_pax"]
    merged["pax_pct_change"] = merged.apply(
        lambda r: pct_change(r["current_pax"], r["compare_pax"]), axis=1
    )
    merged["pax_trend"] = merged["pax_pct_change"].apply(lambda v: classify_trend(v, "PAX"))

    merged = merged.sort_values("current_revenue", ascending=False).reset_index(drop=True)
    return merged


def _aggregate(df: pd.DataFrame, group_cols: list[str], suffix: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=group_cols + [f"revenue_{suffix}", f"pax_{suffix}"])
    agg = (
        df.groupby(group_cols, as_index=False)
        .agg(revenue=("revenue", "sum"), pax=("pax", "sum"))
        .rename(columns={"revenue": f"{suffix}_revenue", "pax": f"{suffix}_pax"})
    )
    return agg


def compare_segments(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> pd.DataFrame:
    """Same as compare_periods but aggregated to segment level only."""
    return compare_periods(current_df, compare_df, group_cols=["segment"])


def compare_locations(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> pd.DataFrame:
    """Same as compare_periods but aggregated to location level only."""
    return compare_periods(current_df, compare_df, group_cols=["location"])


# ---------------------------------------------------------------------------
# Named comparisons (DoD / WoW / MoM / YoY)
# ---------------------------------------------------------------------------

def summarize_period(df: pd.DataFrame) -> dict:
    """Scalar totals for a single period's worth of rows."""
    if df is None or df.empty:
        return {"revenue": 0.0, "pax": 0.0, "rev_per_pax": np.nan, "aop": np.nan}
    revenue = float(pd.to_numeric(df["revenue"], errors="coerce").sum())
    pax = float(pd.to_numeric(df["pax"], errors="coerce").sum())
    aop = (
        float(pd.to_numeric(df["aop"], errors="coerce").sum())
        if "aop" in df.columns and df["aop"].notna().any()
        else np.nan
    )
    return {
        "revenue": revenue,
        "pax": pax,
        "rev_per_pax": safe_div(revenue, pax),
        "aop": aop,
    }


def day_over_day(today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> dict:
    """Scalar DoD comparison: revenue/PAX deltas plus trend label."""
    return _named_comparison(today_df, yesterday_df)


def week_over_week(this_week_df: pd.DataFrame, last_week_df: pd.DataFrame) -> dict:
    return _named_comparison(this_week_df, last_week_df)


def month_over_month(current_df: pd.DataFrame, last_month_df: pd.DataFrame) -> dict:
    return _named_comparison(current_df, last_month_df)


def year_over_year(current_df: pd.DataFrame, last_year_df: pd.DataFrame) -> dict:
    return _named_comparison(current_df, last_year_df)


def compare_two_periods_summary(current_df: pd.DataFrame, previous_df: pd.DataFrame) -> dict:
    """
    Public entry point for a scalar (non-outlet-level) comparison between
    any two already-loaded period DataFrames — used by pages that let the
    user pick the comparison granularity (Day/Month/Year) rather than
    hardcoding which named comparison applies.
    """
    return _named_comparison(current_df, previous_df)


def _named_comparison(current_df: pd.DataFrame, previous_df: pd.DataFrame) -> dict:
    current = summarize_period(current_df)
    previous = summarize_period(previous_df)
    rev_pct = pct_change(current["revenue"], previous["revenue"])
    pax_pct = pct_change(current["pax"], previous["pax"])
    return {
        "current_revenue": current["revenue"],
        "previous_revenue": previous["revenue"],
        "revenue_change": current["revenue"] - previous["revenue"],
        "revenue_pct_change": rev_pct,
        "revenue_trend": classify_trend(rev_pct),
        "current_pax": current["pax"],
        "previous_pax": previous["pax"],
        "pax_change": current["pax"] - previous["pax"],
        "pax_pct_change": pax_pct,
        "pax_trend": classify_trend(pax_pct, "PAX"),
        "current_rev_per_pax": current["rev_per_pax"],
        "previous_rev_per_pax": previous["rev_per_pax"],
    }


# ---------------------------------------------------------------------------
# AOP variance
# ---------------------------------------------------------------------------

def aop_variance(df: pd.DataFrame, group_cols: Optional[list[str]] = None) -> pd.DataFrame:
    """
    Actual revenue vs AOP target, with variance amount and variance %.
    If group_cols is None, returns a single-row overall summary; otherwise
    grouped by the given columns (e.g. ["segment"] or ["segment","outlet","location"]).
    """
    if df is None or df.empty:
        cols = (group_cols or []) + ["actual_revenue", "aop_target", "variance", "variance_pct"]
        return pd.DataFrame(columns=cols)

    work = df.copy()
    if work.columns.duplicated().any():
        # Guard against a caller accidentally producing two columns with
        # the same name (e.g. renaming a new column onto a name that
        # already existed without dropping the original first) — with
        # duplicate names, `work["aop"]` returns a DataFrame instead of a
        # Series and pd.to_numeric() raises. Keep the LAST occurrence of
        # each duplicated name, since that's normally the most recently
        # computed/intended value in a rename-without-drop scenario.
        work = work.loc[:, ~work.columns.duplicated(keep="last")]
    work["revenue"] = pd.to_numeric(work["revenue"], errors="coerce").fillna(0.0)
    work["aop"] = pd.to_numeric(work.get("aop"), errors="coerce")

    if group_cols:
        agg = work.groupby(group_cols, as_index=False).agg(
            actual_revenue=("revenue", "sum"),
            # min_count=1 (via a lambda, since .agg()'s tuple form doesn't
            # accept sum's kwargs directly): a group where every row's
            # "aop" is NaN — e.g. Sky Plates/Encalm Eats, which aren't in
            # scope for the outlet_monthly AOP format and so never get a
            # target uploaded — must stay NaN here, matching the
            # ungrouped branch below. Without min_count, pandas' default
            # sum() silently turns an all-NaN group into 0.0, which then
            # reads as "this segment's AOP target is genuinely zero"
            # (wrong) and produces a nonsensical variance_pct of +inf%
            # instead of the correct "no target set" NaN/"—" downstream.
            aop_target=("aop", lambda s: s.sum(min_count=1)),
        )
    else:
        agg = pd.DataFrame(
            {
                "actual_revenue": [work["revenue"].sum()],
                "aop_target": [work["aop"].sum(min_count=1)],
            }
        )

    agg["variance"] = agg["actual_revenue"] - agg["aop_target"]
    agg["variance_pct"] = agg.apply(
        lambda r: pct_change(r["actual_revenue"], r["aop_target"]), axis=1
    )
    return agg


# ---------------------------------------------------------------------------
# Revenue per PAX / volume vs spend driver classification
# ---------------------------------------------------------------------------

def revenue_per_pax_table(df: pd.DataFrame) -> pd.DataFrame:
    """Outlet-level Revenue, PAX, and Revenue/PAX for a single period."""
    if df is None or df.empty:
        return pd.DataFrame(columns=GROUP_COLS + ["revenue", "pax", "rev_per_pax"])
    agg = df.groupby(GROUP_COLS, as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )
    agg["rev_per_pax"] = agg.apply(lambda r: safe_div(r["revenue"], r["pax"]), axis=1)
    return agg.sort_values("revenue", ascending=False).reset_index(drop=True)


def classify_driver(pax_pct: Optional[float], rev_per_pax_pct: Optional[float]) -> str:
    """
    Classify whether an outlet's revenue change is mainly Volume-driven
    (PAX moved more than spend-per-head) or Spend-driven (the reverse),
    or Mixed/Flat when neither moved meaningfully.
    """
    pax_pct = pax_pct if (pax_pct is not None and not pd.isna(pax_pct)) else 0.0
    rpp_pct = rev_per_pax_pct if (rev_per_pax_pct is not None and not pd.isna(rev_per_pax_pct)) else 0.0

    if abs(pax_pct) < GROWTH_THRESHOLD and abs(rpp_pct) < GROWTH_THRESHOLD:
        return "Flat"
    if abs(pax_pct) >= abs(rpp_pct) * 1.2:
        return "Volume-driven"
    if abs(rpp_pct) >= abs(pax_pct) * 1.2:
        return "Spend-driven"
    return "Mixed"


def volume_vs_spend_table(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> pd.DataFrame:
    """
    Outlet-level table classifying each outlet's revenue movement as
    Volume-driven, Spend-driven, Mixed, or Flat.

    Columns: segment, outlet, location, pax_pct_change, revenue_pct_change,
             rev_per_pax_pct_change, driver
    """
    comparison = compare_periods(current_df, compare_df)
    current_rpp = comparison.apply(
        lambda r: safe_div(r["current_revenue"], r["current_pax"]), axis=1
    )
    compare_rpp = comparison.apply(
        lambda r: safe_div(r["compare_revenue"], r["compare_pax"]), axis=1
    )
    comparison["current_rev_per_pax"] = current_rpp
    comparison["compare_rev_per_pax"] = compare_rpp
    comparison["rev_per_pax_pct_change"] = comparison.apply(
        lambda r: pct_change(r["current_rev_per_pax"], r["compare_rev_per_pax"]), axis=1
    )
    comparison["driver"] = comparison.apply(
        lambda r: classify_driver(r["pax_pct_change"], r["rev_per_pax_pct_change"]), axis=1
    )
    return comparison[
        [
            "segment",
            "outlet",
            "location",
            "pax_pct_change",
            "revenue_pct_change",
            "rev_per_pax_pct_change",
            "driver",
        ]
    ]


# ---------------------------------------------------------------------------
# Penetration % and SPP (require traffic data, joined in via
# database.join_revenue_with_traffic before these functions are called)
# ---------------------------------------------------------------------------

def penetration_pct(pax: float, traffic: float) -> float:
    """PAX / Traffic * 100. NaN if traffic is missing or zero."""
    ratio = safe_div(pax, traffic)
    return ratio * 100 if not pd.isna(ratio) else np.nan


def spp(revenue: float, traffic: float) -> float:
    """Revenue / Traffic — sales per airport visitor. NaN if traffic missing/zero."""
    return safe_div(revenue, traffic)


def location_level_summary_with_traffic(df: pd.DataFrame) -> pd.DataFrame:
    """
    Location-level Revenue, PAX, Traffic, Penetration %, and SPP for one
    period's worth of already-traffic-joined revenue rows (i.e. the output
    of database.join_revenue_with_traffic).

    Traffic is intentionally taken once per (date, location) rather than
    summed across outlet rows — every outlet row for a given date+location
    carries the location-level traffic figure (sum of all terminals —
    specific), so summing it across outlets would multiply it by however
    many outlets are active that day. This groups by location only and
    takes the traffic total across distinct (date, location) pairs, which
    is the only operation that's actually correct to sum (each date's
    traffic is added once, across however many days the input spans).
    """
    if df is None or df.empty or "traffic" not in df.columns:
        return pd.DataFrame(columns=["location", "revenue", "pax", "traffic", "penetration_pct", "spp"])

    revenue_by_location = df.groupby("location", as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )

    # One traffic figure per (date, location) — dedupe before summing so a
    # multi-day input correctly adds each day's traffic once, not once per
    # outlet row that happens to share that date+location.
    traffic_by_date_location = df[["date", "location", "traffic"]].drop_duplicates(
        subset=["date", "location"]
    )
    traffic_by_location = traffic_by_date_location.groupby("location", as_index=False)["traffic"].sum(
        min_count=1
    )

    merged = revenue_by_location.merge(traffic_by_location, on="location", how="left")
    merged["penetration_pct"] = merged.apply(
        lambda r: penetration_pct(r["pax"], r["traffic"]), axis=1
    )
    merged["spp"] = merged.apply(lambda r: spp(r["revenue"], r["traffic"]), axis=1)
    return merged


def penetration_and_spp_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    Outlet-level Revenue and PAX alongside the *location's* Penetration %
    and SPP (joined back onto every outlet row for context) — useful for
    a detail table, but the location-level figures themselves come from
    location_level_summary_with_traffic() to avoid the outlet-multiplication
    bug described there. Returns NaN for traffic/penetration/SPP if no
    traffic data has been loaded yet.
    """
    if df is None or df.empty:
        return pd.DataFrame(
            columns=GROUP_COLS + ["pax", "revenue", "traffic", "penetration_pct", "spp"]
        )

    outlet_agg = df.groupby(GROUP_COLS, as_index=False).agg(
        pax=("pax", "sum"), revenue=("revenue", "sum")
    )
    location_summary = location_level_summary_with_traffic(df)[
        ["location", "traffic", "penetration_pct", "spp"]
    ]
    return outlet_agg.merge(location_summary, on="location", how="left")


def has_traffic_data(df: pd.DataFrame) -> bool:
    """True if at least one row has a usable (non-null, non-zero) traffic value."""
    if df is None or df.empty or "traffic" not in df.columns:
        return False
    traffic = pd.to_numeric(df["traffic"], errors="coerce")
    return bool((traffic.fillna(0) > 0).any())


def penetration_spp_variance(
    current_df: pd.DataFrame, compare_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Location-level comparison table: current vs compare period's Traffic,
    Revenue, PAX, Penetration %, and SPP, plus the variance formulas from
    the spec:
        SPP Variance = (Current SPP - Previous SPP) / Previous SPP * 100
        Penetration Variance = (Current Pen% - Previous Pen%) / Previous Pen% * 100
    (each expressed here as a fraction, e.g. 0.10 for +10%, consistent
    with every other *_pct_change column in this module — format_pct()
    converts to the "+10.00%" display form.)
    """
    current_summary = location_level_summary_with_traffic(current_df)
    compare_summary = location_level_summary_with_traffic(compare_df)

    merged = current_summary.merge(
        compare_summary, on="location", how="outer", suffixes=("_current", "_compare")
    )
    for col in merged.columns:
        if col != "location":
            merged[col] = merged[col]

    merged["traffic_pct_change"] = merged.apply(
        lambda r: pct_change(r.get("traffic_current"), r.get("traffic_compare")), axis=1
    )
    merged["revenue_pct_change"] = merged.apply(
        lambda r: pct_change(r.get("revenue_current"), r.get("revenue_compare")), axis=1
    )
    merged["pax_pct_change"] = merged.apply(
        lambda r: pct_change(r.get("pax_current"), r.get("pax_compare")), axis=1
    )
    merged["penetration_pct_change"] = merged.apply(
        lambda r: pct_change(r.get("penetration_pct_current"), r.get("penetration_pct_compare")), axis=1
    )
    merged["spp_pct_change"] = merged.apply(
        lambda r: pct_change(r.get("spp_current"), r.get("spp_compare")), axis=1
    )
    return merged


def explain_revenue_driver(
    traffic_pct: Optional[float],
    penetration_pct_change: Optional[float],
    spp_pct_change: Optional[float],
    revenue_pct: Optional[float],
) -> str:
    """
    Produce a one-line plain-English attribution for why revenue moved the
    way it did, in terms of Traffic, Penetration, and SPP — the three
    factors that multiply together to make up revenue (Revenue = Traffic
    × Penetration% × SPP). Mirrors the spec's examples:
      "Revenue increased despite traffic decline due to higher SPP"
      "Penetration dropped because PAX growth was lower than traffic growth"

    All inputs are fractional changes (e.g. 0.08 for +8%), matching every
    other *_pct_change value in this module. Returns "—" if there isn't
    enough information (e.g. no traffic data loaded) to say anything.
    """
    def _is_usable(x):
        return x is not None and not pd.isna(x) and x not in (float("inf"), float("-inf"))

    if not _is_usable(revenue_pct):
        return "—"

    revenue_up = revenue_pct > 0.05
    revenue_down = revenue_pct < -0.05

    traffic_known = _is_usable(traffic_pct)
    pen_known = _is_usable(penetration_pct_change)
    spp_known = _is_usable(spp_pct_change)

    if not (traffic_known or pen_known or spp_known):
        return "—"

    traffic_up = traffic_known and traffic_pct > 0.05
    traffic_down = traffic_known and traffic_pct < -0.05
    pen_up = pen_known and penetration_pct_change > 0.05
    pen_down = pen_known and penetration_pct_change < -0.05
    spp_up = spp_known and spp_pct_change > 0.05
    spp_down = spp_known and spp_pct_change < -0.05

    # Identify which single factor moved the most, to credit/blame the
    # right driver when more than one moved in the same direction.
    candidates = []
    if pen_known:
        candidates.append(("penetration", abs(penetration_pct_change)))
    if spp_known:
        candidates.append(("SPP", abs(spp_pct_change)))
    if traffic_known:
        candidates.append(("traffic", abs(traffic_pct)))
    dominant = max(candidates, key=lambda c: c[1])[0] if candidates else None

    if revenue_up:
        if traffic_down and (spp_up or pen_up):
            driver = "higher SPP" if (spp_known and (not pen_known or abs(spp_pct_change) >= abs(penetration_pct_change or 0))) else "higher penetration"
            return f"Revenue increased despite traffic decline due to {driver}."
        if traffic_up and spp_down and pen_down:
            return "Revenue increased on higher traffic alone, despite lower penetration and SPP."
        if dominant == "SPP" and spp_up:
            return "Revenue increased mainly due to higher spend per visitor (SPP)."
        if dominant == "penetration" and pen_up:
            return "Revenue increased mainly due to higher penetration (more visitors converted to customers)."
        if dominant == "traffic" and traffic_up:
            return "Revenue increased mainly due to higher airport traffic."
        return "Revenue increased."

    if revenue_down:
        if traffic_up and (spp_down or pen_down):
            driver = "lower SPP" if (spp_known and (not pen_known or abs(spp_pct_change) >= abs(penetration_pct_change or 0))) else "lower penetration"
            return f"Revenue declined despite higher traffic due to {driver}."
        if traffic_down and spp_up and pen_up:
            return "Revenue declined due to lower traffic, despite higher penetration and SPP."
        if dominant == "SPP" and spp_down:
            return "Revenue declined mainly due to lower spend per visitor (SPP)."
        if dominant == "penetration" and pen_down:
            return "Revenue declined mainly due to lower penetration (fewer visitors converted to customers)."
        if dominant == "traffic" and traffic_down:
            return "Revenue declined mainly due to lower airport traffic."
        return "Revenue declined."

    return "Revenue was broadly stable."


def explain_penetration_driver(
    traffic_pct: Optional[float],
    pax_pct: Optional[float],
    penetration_pct_change: Optional[float],
) -> str:
    """
    Produce a one-line plain-English attribution for why Penetration %
    moved, in terms of PAX growth vs Traffic growth (Penetration% = PAX ÷
    Traffic, so Penetration rises when PAX grows faster than Traffic, and
    falls when Traffic grows faster than PAX). Mirrors the spec's example:
      "Penetration dropped because PAX growth was lower than traffic growth"
    """
    def _is_usable(x):
        return x is not None and not pd.isna(x) and x not in (float("inf"), float("-inf"))

    if not _is_usable(penetration_pct_change):
        return "—"
    if not (_is_usable(traffic_pct) and _is_usable(pax_pct)):
        return "—"

    pen_up = penetration_pct_change > 0.05
    pen_down = penetration_pct_change < -0.05

    if pen_up:
        return "Penetration rose because PAX growth outpaced traffic growth."
    if pen_down:
        if pax_pct < 0 and traffic_pct >= 0:
            return "Penetration dropped because PAX declined while traffic held or grew."
        return "Penetration dropped because PAX growth was lower than traffic growth."
    return "Penetration was broadly stable, with PAX and traffic moving together."


# ---------------------------------------------------------------------------
# Top / bottom performers
# ---------------------------------------------------------------------------

def top_bottom_outlets(
    current_df: pd.DataFrame,
    compare_df: Optional[pd.DataFrame] = None,
    n: int = 10,
) -> dict:
    """
    Top-N and bottom-N outlets by current-period revenue. If `compare_df` is
    given, also includes each outlet's revenue % change vs that period.
    """
    if compare_df is not None:
        comparison = compare_periods(current_df, compare_df)
        ranked = comparison.sort_values("current_revenue", ascending=False)
    else:
        ranked = revenue_per_pax_table(current_df).rename(columns={"revenue": "current_revenue"})
        ranked["revenue_pct_change"] = np.nan

    ranked = ranked[ranked["current_revenue"] > 0]
    return {
        "top": ranked.head(n).reset_index(drop=True),
        "bottom": ranked.tail(n).sort_values("current_revenue").reset_index(drop=True),
    }


def top_growing_declining_segments(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> dict:
    """Identify the single fastest-growing and fastest-declining segment by revenue %."""
    seg = compare_segments(current_df, compare_df)
    seg = seg[seg["revenue_pct_change"].notna() & np.isfinite(seg["revenue_pct_change"])]
    if seg.empty:
        return {"top_growing": None, "top_declining": None}
    top_growing = seg.sort_values("revenue_pct_change", ascending=False).iloc[0]
    top_declining = seg.sort_values("revenue_pct_change", ascending=True).iloc[0]
    return {"top_growing": top_growing, "top_declining": top_declining}


# ---------------------------------------------------------------------------
# Date-range helpers (used by pages to select comparison windows)
# ---------------------------------------------------------------------------

def week_range(target_date: dt.date) -> tuple[dt.date, dt.date]:
    """Return (Monday, Sunday) of the ISO week containing `target_date`."""
    monday = target_date - dt.timedelta(days=target_date.weekday())
    sunday = monday + dt.timedelta(days=6)
    return monday, sunday


def previous_week_range(target_date: dt.date) -> tuple[dt.date, dt.date]:
    monday, _ = week_range(target_date)
    prev_monday = monday - dt.timedelta(days=7)
    prev_sunday = prev_monday + dt.timedelta(days=6)
    return prev_monday, prev_sunday


# ---------------------------------------------------------------------------
# Comparison-type resolution: Week-wise / Month-wise / Year-wise
# ---------------------------------------------------------------------------
#
# This is the core of the "intelligent comparison" feature: instead of the
# UI always comparing exactly two single dates, the user picks a
# COMPARISON_TYPE (how granular) and, for Month/Year, a COMPARISON_MODE
# (whether to compare the full period or only the same partial range
# to-date). Resolving that choice into two concrete date ranges happens
# here, once, so every page that offers this dropdown gets identical
# behavior instead of each page reimplementing its own date math.

COMPARISON_TYPES = ["Day-wise", "Week-wise", "Month-wise", "Year-wise"]
COMPARISON_MODES = ["Full Period", "To-Date"]


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    """First and last calendar date of the given (year, month)."""
    first = dt.date(year, month, 1)
    if month == 12:
        next_first = dt.date(year + 1, 1, 1)
    else:
        next_first = dt.date(year, month + 1, 1)
    last = next_first - dt.timedelta(days=1)
    return first, last


def year_bounds(year: int) -> tuple[dt.date, dt.date]:
    """First and last calendar date of the given year."""
    return dt.date(year, 1, 1), dt.date(year, 12, 31)


def resolve_comparison_ranges(
    comparison_type: str,
    anchor_date: dt.date,
    compare_year: Optional[int] = None,
    compare_month: Optional[int] = None,
    compare_week_start: Optional[dt.date] = None,
    compare_date: Optional[dt.date] = None,
    mode: str = "Full Period",
    available_dates: Optional[list] = None,
) -> dict:
    """
    Turn a (comparison_type, anchor_date, compare target, mode) choice into
    two concrete, ready-to-load date ranges.

    comparison_type: "Day-wise" | "Week-wise" | "Month-wise" | "Year-wise"
    anchor_date: the "current" date the user is analyzing from (its day,
        week, month, and year define the "current" period in all modes).
        Daily data is still uploaded and stored exactly as before — this
        only controls how the stored data is grouped for comparison.
    compare_date: which single day to compare against (Day-wise). Defaults
        to the day immediately before anchor_date; if `available_dates` is
        given and that day has no data, falls back to the nearest earlier
        date that does, so a gap in uploads doesn't silently produce an
        empty comparison.
    compare_week_start: the Monday of the week to compare against
        (Week-wise). Defaults to the Monday of the week immediately before
        the anchor date's week if not given.
    compare_year / compare_month: which year (Year-wise) or (year, month)
        pair (Month-wise) to compare against. Ignored for Day-wise/Week-wise.
    mode: "Full Period" (the whole calendar week/month/year on both sides)
        or "To-Date" (same partial range, e.g. Monday through the anchor
        date's weekday/day-of-month/day-of-year, on both sides). Ignored
        for Day-wise, which always compares exactly one day to one day.
    available_dates: if given, used to gracefully handle comparison periods
        that have no data at all (the caller can detect an empty range and
        show a clear message rather than erroring), and to find the
        nearest available prior date for Day-wise when the literal
        previous day has no data.

    Returns a dict:
        {
            "current_label": str, "current_start": date, "current_end": date,
            "compare_label": str, "compare_start": date, "compare_end": date,
        }
    Every comparison_type returns a date *range* (current_start..current_end
    and compare_start..compare_end) — callers always aggregate over a range
    rather than branching on comparison_type themselves.
    """
    if comparison_type == "Day-wise":
        if compare_date is not None:
            cmp_date = compare_date
        else:
            cmp_date = anchor_date - dt.timedelta(days=1)
            if available_dates:
                earlier = sorted(d for d in available_dates if d < anchor_date)
                if earlier and cmp_date not in earlier:
                    cmp_date = earlier[-1]

        return {
            "current_label": f"{anchor_date:%d %b %Y}",
            "current_start": anchor_date,
            "current_end": anchor_date,
            "compare_label": f"{cmp_date:%d %b %Y}",
            "compare_start": cmp_date,
            "compare_end": cmp_date,
        }
    if comparison_type == "Week-wise":
        cur_monday, cur_sunday = week_range(anchor_date)
        if compare_week_start is not None:
            cmp_monday = compare_week_start - dt.timedelta(days=compare_week_start.weekday())
        else:
            cmp_monday = cur_monday - dt.timedelta(days=7)
        cmp_sunday = cmp_monday + dt.timedelta(days=6)

        if mode == "To-Date":
            cur_end = min(anchor_date, cur_sunday)
            day_offset = (cur_end - cur_monday).days
            cmp_end = min(cmp_monday + dt.timedelta(days=day_offset), cmp_sunday)
            return {
                "current_label": f"Week of {cur_monday:%d %b} (Mon–{cur_end:%a %d})",
                "current_start": cur_monday,
                "current_end": cur_end,
                "compare_label": f"Week of {cmp_monday:%d %b} (Mon–{cmp_end:%a %d})",
                "compare_start": cmp_monday,
                "compare_end": cmp_end,
            }

        return {
            "current_label": f"Week of {cur_monday:%d %b %Y} (full week)",
            "current_start": cur_monday,
            "current_end": cur_sunday,
            "compare_label": f"Week of {cmp_monday:%d %b %Y} (full week)",
            "compare_start": cmp_monday,
            "compare_end": cmp_sunday,
        }
    if comparison_type == "Month-wise":
        if compare_year is None or compare_month is None:
            # default to the previous calendar month
            prev = _safe_month_shift_local(anchor_date, -1)
            compare_year, compare_month = prev.year, prev.month

        cur_first, cur_last = month_bounds(anchor_date.year, anchor_date.month)
        cmp_first, cmp_last = month_bounds(compare_year, compare_month)

        if mode == "To-Date":
            cur_end = min(anchor_date, cur_last)
            day_offset = (cur_end - cur_first).days
            cmp_end = min(cmp_first + dt.timedelta(days=day_offset), cmp_last)
            return {
                "current_label": f"{cur_first:%b %Y} (1–{cur_end.day})",
                "current_start": cur_first,
                "current_end": cur_end,
                "compare_label": f"{cmp_first:%b %Y} (1–{cmp_end.day})",
                "compare_start": cmp_first,
                "compare_end": cmp_end,
            }

        return {
            "current_label": f"{cur_first:%b %Y} (full month)",
            "current_start": cur_first,
            "current_end": cur_last,
            "compare_label": f"{cmp_first:%b %Y} (full month)",
            "compare_start": cmp_first,
            "compare_end": cmp_last,
        }

    if comparison_type == "Year-wise":
        if compare_year is None:
            compare_year = anchor_date.year - 1

        cur_first, cur_last = year_bounds(anchor_date.year)
        cmp_first, cmp_last = year_bounds(compare_year)

        if mode == "To-Date":
            cur_end = min(anchor_date, cur_last)
            day_offset = (cur_end - cur_first).days
            cmp_end = min(cmp_first + dt.timedelta(days=day_offset), cmp_last)
            return {
                "current_label": f"{anchor_date.year} (Jan 1–{cur_end:%b %d})",
                "current_start": cur_first,
                "current_end": cur_end,
                "compare_label": f"{compare_year} (Jan 1–{cmp_end:%b %d})",
                "compare_start": cmp_first,
                "compare_end": cmp_end,
            }

        return {
            "current_label": f"{anchor_date.year} (full year)",
            "current_start": cur_first,
            "current_end": cur_last,
            "compare_label": f"{compare_year} (full year)",
            "compare_start": cmp_first,
            "compare_end": cmp_last,
        }

    raise ValueError(f"Unknown comparison_type: {comparison_type!r}")


def short_period_label(start_date: dt.date, end_date: dt.date, comparison_type: str) -> str:
    """
    Build a short, table-header-friendly label identifying a date range by
    its week/month/year name — e.g. "Week of 15 Jun 2026", "Jun 2026",
    "2026" — for embedding directly in a comparison table's column headers
    (e.g. "Current Rev (Jun 2026)" vs "Compare Rev (May 2026)"), so a reader
    looking only at the table, without the page's caption above it, can
    still see exactly which two periods are being compared.

    Falls back to a plain date-range string ("15 Jun 2026 – 21 Jun 2026")
    for any comparison_type this doesn't recognise, so a future comparison
    type doesn't end up with a blank or broken label.
    """
    if comparison_type == "Week-wise":
        monday = start_date - dt.timedelta(days=start_date.weekday())
        return f"Week of {monday:%d %b %Y}"
    if comparison_type == "Month-wise":
        return f"{start_date:%b %Y}"
    if comparison_type == "Year-wise":
        return f"{start_date.year}"
    if start_date == end_date:
        return f"{start_date:%d %b %Y}"
    return f"{start_date:%d %b %Y} – {end_date:%d %b %Y}"


def short_period_label_for_ranges(ranges: dict) -> tuple[str, str]:
    """
    Convenience wrapper: given the dict returned by resolve_comparison_ranges(),
    return (current_short_label, compare_short_label) — the comparison_type
    is read from ranges["comparison_type"] if present (set by
    comparison_widget.render_comparison_selector), otherwise inferred from
    whether the current range spans exactly one calendar week/month/year.
    """
    comparison_type = ranges.get("comparison_type")
    if comparison_type is None:
        span_days = (ranges["current_end"] - ranges["current_start"]).days
        if span_days == 0:
            comparison_type = "Day-wise"
        elif span_days <= 6:
            comparison_type = "Week-wise"
        elif span_days <= 31:
            comparison_type = "Month-wise"
        else:
            comparison_type = "Year-wise"
    current_short = short_period_label(ranges["current_start"], ranges["current_end"], comparison_type)
    compare_short = short_period_label(ranges["compare_start"], ranges["compare_end"], comparison_type)
    return current_short, compare_short


# FIX (Bug 5): _safe_month_shift_local was duplicated here and in database.py.
# The canonical implementation now lives in modules/date_utils.py and is
# imported at the top of this file as `_safe_month_shift_local` so all
# existing callers within this module continue to work without any other
# changes. The local definition is removed to eliminate the duplicate.
