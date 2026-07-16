"""
insights.py — Rule-based management summary generator.

Takes the same period DataFrames the analytics engine uses and produces a
human-readable narrative (a list of markdown-formatted bullet/paragraph
strings) covering executive summary, top/bottom performers, volume vs spend
attribution, footfall, segment summary, and AOP status. This intentionally
stays rule-based (no LLM call) so it's fast, deterministic, and free to run
on every page load.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import revenue_analysis as ra
from .formatting import format_money, format_pax, format_pct
from .table_style import DECLINE_COLOR, GROWTH_COLOR, NEUTRAL_COLOR


def _colored_pct(value, signed: bool = True, color_value=None) -> str:
    """
    Render a percentage as a colored inline HTML span (green for growth,
    red for decline, neutral gray for flat/undefined) — matching the same
    color rule used on every comparison table in the app. Callers that
    render this output must use st.markdown(..., unsafe_allow_html=True).

    `color_value`, if given, is used to decide the color instead of `value`
    — needed when `value` has already been through abs() for display (e.g.
    "down 21.00%" with signed=False) but the color still needs to reflect
    the real direction.
    """
    text = format_pct(value, signed=signed)
    direction_value = color_value if color_value is not None else value
    if direction_value is None or pd.isna(direction_value):
        color = NEUTRAL_COLOR
    else:
        try:
            numeric = float(direction_value)
        except (TypeError, ValueError):
            numeric = 0.0
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            color = NEUTRAL_COLOR
        elif numeric > 0:
            color = GROWTH_COLOR
        elif numeric < 0:
            color = DECLINE_COLOR
        else:
            color = NEUTRAL_COLOR
    return f'<span style="color: {color}; font-weight: 700;">{text}</span>'


def _period_label(df: Optional[pd.DataFrame]) -> str:
    """
    Format a period DataFrame's actual date range as a readable label —
    "04 Jul 2026" for a single day, "01–07 Jul 2026" for a range within
    one month, "28 Jun – 04 Jul 2026" for a range spanning months. Used
    everywhere this module used to just say "the prior period" without
    saying what date(s) that actually meant.
    """
    if df is None or df.empty or "date" not in df.columns:
        return "the prior period"
    dates = pd.to_datetime(df["date"])
    start, end = dates.min(), dates.max()
    if start == end:
        return start.strftime("%d %b %Y")
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%d')}–{end.strftime('%d %b %Y')}"
    if start.year == end.year:
        return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def generate_summary(
    current_df: pd.DataFrame,
    yesterday_df: Optional[pd.DataFrame] = None,
    last_month_df: Optional[pd.DataFrame] = None,
    last_year_df: Optional[pd.DataFrame] = None,
    top_n: int = 3,
    exec_compare_df: Optional[pd.DataFrame] = None,
) -> list[str]:
    """
    Build the full management narrative as a list of markdown blocks, each
    one ready to be rendered with st.markdown(). Sections that don't apply
    (e.g. no prior-period data available) are simply omitted rather than
    shown with placeholder text.

    Section order:
      1. Executive Summary       (user-selected compare date)
      2. Selected comparison panel
      3. DoD / MoM / YoY panels (only shown when different from selected)
      4. Top / Bottom outlet performers  (vs selected compare)
      5. Volume vs Spend driver commentary  (vs selected compare)
      6. Footfall  (vs selected compare)
      7. Segment summary  (vs selected compare)
      8. AOP status
    """
    sections: list[str] = []

    # The user-selected compare period drives ALL sections.
    # exec_compare_df is the date the user actually picked in the selector.
    # We fall back to yesterday_df only if no selector compare is available.
    selected_compare_df = (
        exec_compare_df
        if exec_compare_df is not None and not exec_compare_df.empty
        else yesterday_df
    )

    # 1. Executive Summary — uses the user-selected compare date
    sections.append(_executive_summary(current_df, selected_compare_df))

    # 2. Selected-period comparison panel
    if selected_compare_df is not None and not selected_compare_df.empty:
        compare_label = _period_label(selected_compare_df)
        current_label = _period_label(current_df)
        sections.append(
            _period_commentary(
                f"Selected Comparison — {current_label} vs {compare_label}",
                current_df,
                selected_compare_df,
            )
        )

    # 3. Always-on reference panels (DoD / MoM / YoY) — shown only when
    # their dates differ from the user-selected compare (no duplication).
    selected_dates = (
        set(pd.to_datetime(selected_compare_df["date"]).dt.date.tolist())
        if selected_compare_df is not None and not selected_compare_df.empty
        else set()
    )

    def _different_from_selected(df: Optional[pd.DataFrame]) -> bool:
        if df is None or df.empty:
            return False
        these_dates = set(pd.to_datetime(df["date"]).dt.date.tolist())
        return bool(these_dates - selected_dates)

    if _different_from_selected(yesterday_df):
        sections.append(_period_commentary("Day-over-Day (DoD)", current_df, yesterday_df))

    if _different_from_selected(last_month_df):
        sections.append(_period_commentary("Month-over-Month (MoM)", current_df, last_month_df))

    if _different_from_selected(last_year_df):
        sections.append(_period_commentary("Year-over-Year (YoY)", current_df, last_year_df))

    # 4. Top / Bottom performers vs user-selected compare date
    if selected_compare_df is not None and not selected_compare_df.empty:
        top_bottom_text = _top_bottom_section(current_df, selected_compare_df, top_n)
        if top_bottom_text:
            sections.append(top_bottom_text)

        driver_text = _driver_section(current_df, selected_compare_df)
        if driver_text:
            sections.append(driver_text)

        footfall_text = _footfall_section(current_df, selected_compare_df)
        if footfall_text:
            sections.append(footfall_text)

    # 5. Segment summary — vs user-selected compare date
    segment_text = _segment_summary_section(current_df, selected_compare_df)
    if segment_text:
        sections.append(segment_text)

    # 6. AOP
    aop_text = _aop_section(current_df)
    if aop_text:
        sections.append(aop_text)

    return [s for s in sections if s]


def _executive_summary(current_df: pd.DataFrame, yesterday_df: Optional[pd.DataFrame]) -> str:
    current = ra.summarize_period(current_df)
    current_label = _period_label(current_df)
    lines = [
        "### 📋 Executive Summary",
        f"Total revenue for **{current_label}** was **{format_money(current['revenue'])}** "
        f"from **{format_pax(current['pax'])} PAX** "
        f"(Revenue/PAX: {format_money(current['rev_per_pax'])}).",
    ]
    if yesterday_df is not None and not yesterday_df.empty:
        dod = ra.day_over_day(current_df, yesterday_df)
        compare_label = _period_label(yesterday_df)
        direction = "up" if (dod["revenue_pct_change"] or 0) >= 0 else "down"
        lines.append(
            f"That's **{direction} {_colored_pct(abs(dod['revenue_pct_change'] or 0), signed=False, color_value=dod['revenue_pct_change'])}** "
            f"versus **{compare_label}** ({format_money(dod['previous_revenue'])}), "
            f"a change of {format_money(dod['revenue_change'])}. {dod['revenue_trend']}"
        )
    return "\n\n".join(lines)


def _top_bottom_section(current_df: pd.DataFrame, compare_df: pd.DataFrame, n: int) -> Optional[str]:
    result = ra.top_bottom_outlets(current_df, compare_df, n=n)
    top, bottom = result["top"], result["bottom"]
    if top.empty:
        return None

    compare_label = _period_label(compare_df)
    lines = ["### 🏆 Top & Bottom Performers"]
    lines.append(f"**Top {min(n, len(top))} outlets by revenue** (% change vs **{compare_label}**):")
    for _, row in top.head(n).iterrows():
        pct = row.get("revenue_pct_change")
        pct_str = f" ({_colored_pct(pct)})" if pct is not None and pd.notna(pct) else ""
        lines.append(
            f"- **{row['outlet']}** ({row['location']}): "
            f"{format_money(row['current_revenue'])}{pct_str}"
        )

    if not bottom.empty:
        lines.append(f"\n**Bottom {min(n, len(bottom))} outlets by revenue** (% change vs **{compare_label}**):")
        for _, row in bottom.head(n).iterrows():
            pct = row.get("revenue_pct_change")
            pct_str = f" ({_colored_pct(pct)})" if pct is not None and pd.notna(pct) else ""
            lines.append(
                f"- **{row['outlet']}** ({row['location']}): "
                f"{format_money(row['current_revenue'])}{pct_str}"
            )
    return "\n".join(lines)


def _driver_section(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> Optional[str]:
    driver_table = ra.volume_vs_spend_table(current_df, compare_df)
    if driver_table.empty:
        return None
    counts = driver_table["driver"].value_counts()
    total = len(driver_table)
    compare_label = _period_label(compare_df)

    lines = ["### 🔎 Revenue Driver Analysis"]
    lines.append(f"*Comparing {_period_label(current_df)} against {compare_label}.*")
    parts = []
    for label in ["Volume-driven", "Spend-driven", "Mixed", "Flat"]:
        if label in counts.index:
            parts.append(f"**{counts[label]}** {label.lower()}")
    lines.append(f"Across {total} outlet/location combinations: " + ", ".join(parts) + ".")

    volume_driven = driver_table[driver_table["driver"] == "Volume-driven"]
    spend_driven = driver_table[driver_table["driver"] == "Spend-driven"]
    if not volume_driven.empty:
        top_vol = volume_driven.reindex(
            volume_driven["revenue_pct_change"].abs().sort_values(ascending=False).index
        ).head(2)
        examples = ", ".join(f"{r['outlet']} ({r['location']})" for _, r in top_vol.iterrows())
        lines.append(f"Notable volume-driven movers: {examples}.")
    if not spend_driven.empty:
        top_spend = spend_driven.reindex(
            spend_driven["revenue_pct_change"].abs().sort_values(ascending=False).index
        ).head(2)
        examples = ", ".join(f"{r['outlet']} ({r['location']})" for _, r in top_spend.iterrows())
        lines.append(f"Notable spend-driven movers: {examples}.")

    return "\n\n".join(lines)


def _footfall_section(current_df: pd.DataFrame, compare_df: pd.DataFrame) -> Optional[str]:
    dod = ra.day_over_day(current_df, compare_df)
    pax_pct = dod["pax_pct_change"]
    rev_pct = dod["revenue_pct_change"]
    if pax_pct is None or pd.isna(pax_pct):
        return None

    compare_label = _period_label(compare_df)
    lines = ["### 🚶 Footfall Insight"]
    pax_direction = "increased" if pax_pct >= 0 else "decreased"
    lines.append(
        f"PAX {pax_direction} by **{_colored_pct(abs(pax_pct), signed=False, color_value=pax_pct)}** "
        f"vs **{compare_label}** ({format_pax(dod['previous_pax'])} → {format_pax(dod['current_pax'])})."
    )

    if pax_pct is not None and rev_pct is not None and not pd.isna(pax_pct) and not pd.isna(rev_pct):
        if pax_pct < 0 and rev_pct > 0:
            lines.append(
                "Revenue grew **despite a footfall decline** — driven by higher spend per "
                "visitor (Revenue/PAX increased)."
            )
        elif pax_pct > 0 and rev_pct < 0:
            lines.append(
                "Revenue fell **despite higher footfall** — spend per visitor (Revenue/PAX) "
                "declined enough to offset the volume gain."
            )
    return "\n\n".join(lines)


def _segment_summary_section(
    current_df: pd.DataFrame, compare_df: Optional[pd.DataFrame]
) -> Optional[str]:
    if current_df is None or current_df.empty:
        return None
    seg_totals = current_df.groupby("segment", as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    ).sort_values("revenue", ascending=False)

    lines = [f"### 🧩 Segment Summary — {_period_label(current_df)}"]
    for _, row in seg_totals.iterrows():
        lines.append(f"- **{row['segment']}**: {format_money(row['revenue'])} ({format_pax(row['pax'])} PAX)")

    if compare_df is not None and not compare_df.empty:
        growth = ra.top_growing_declining_segments(current_df, compare_df)
        if growth["top_growing"] is not None:
            g = growth["top_growing"]
            d = growth["top_declining"]
            compare_label = _period_label(compare_df)
            lines.append(
                f"\n*vs {compare_label}:* "
                f"🔺 Fastest growing segment: **{g['segment']}** ({_colored_pct(g['revenue_pct_change'])}). "
                f"🔻 Fastest declining: **{d['segment']}** ({_colored_pct(d['revenue_pct_change'])})."
            )
    return "\n".join(lines)


def _aop_section(current_df: pd.DataFrame) -> Optional[str]:
    if current_df is None or current_df.empty or "aop" not in current_df.columns:
        return None
    if not pd.to_numeric(current_df["aop"], errors="coerce").notna().any():
        return None

    overall = ra.aop_variance(current_df)
    if overall.empty or pd.isna(overall.iloc[0]["aop_target"]) or overall.iloc[0]["aop_target"] == 0:
        return None

    row = overall.iloc[0]
    achievement_pct = ra.safe_div(row["actual_revenue"], row["aop_target"]) * 100
    status = "✅ on/above target" if row["variance"] >= 0 else "⚠️ below target"

    lines = [
        f"### 🎯 AOP Achievement — {_period_label(current_df)}",
        f"Actual revenue of {format_money(row['actual_revenue'])} against an AOP target of "
        f"{format_money(row['aop_target'])} — **{achievement_pct:.1f}% achievement**, {status}.",
        f"Variance: {format_money(row['variance'])} ({_colored_pct(row['variance_pct'])}).",
    ]
    return "\n\n".join(lines)


def _period_commentary(label: str, current_df: pd.DataFrame, compare_df: pd.DataFrame) -> str:
    comp = ra._named_comparison(current_df, compare_df)
    direction = "up" if (comp["revenue_pct_change"] or 0) >= 0 else "down"
    current_label = _period_label(current_df)
    compare_label = _period_label(compare_df)
    return (
        f"### 🗓️ {label}\n\n"
        f"Revenue for **{current_label}** is **{direction} {_colored_pct(abs(comp['revenue_pct_change'] or 0), signed=False, color_value=comp['revenue_pct_change'])}** "
        f"vs **{compare_label}** "
        f"({format_money(comp['previous_revenue'])} → {format_money(comp['current_revenue'])}). "
        f"PAX changed by {_colored_pct(comp['pax_pct_change'])}."
    )
