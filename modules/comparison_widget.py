"""
comparison_widget.py — Shared "intelligent comparison" selector UI.

Renders the Week-wise / Month-wise / Year-wise dropdown (plus the Full
Period / To-Date radio and week/year/month pickers where relevant) and
returns the resolved date ranges, so every page that wants this selector
gets identical behavior and look from one place rather than copy-pasting
the same ~50 lines of widget code three times.

Note: this only controls comparison *granularity* — daily data is still
uploaded and stored one day at a time exactly as before. Week-wise simply
aggregates the already-stored daily rows into calendar weeks for comparison.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from . import database
from . import date_picker
from . import revenue_analysis as ra
# FIX (Bug 5): previously this module accessed the private function
# ra._safe_month_shift_local(), which is a bad pattern (private symbol of
# another module). Now that the function lives in date_utils, import it
# directly from there with its public name.
from .date_utils import safe_month_shift as _safe_month_shift_local

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


def render_comparison_selector(anchor_date: dt.date, key_prefix: str) -> dict:
    """
    Render the comparison-type controls and return the resolved ranges dict
    from revenue_analysis.resolve_comparison_ranges(), with an extra
    "comparison_type" key included for callers that want to branch on it
    (e.g. to label a metric "WoW" vs "MoM" vs "YoY").

    `key_prefix` must be unique per page (Streamlit widget keys can't
    collide across pages that might render this at the same time within
    one session) — e.g. "exec_summary", "rev_comparison", "insights".
    """
    comparison_type = st.selectbox(
        "🔍 Comparison Type",
        options=ra.COMPARISON_TYPES,
        index=0,
        key=f"{key_prefix}_comparison_type",
        help=(
            "Day-wise, Week-wise, Month-wise, and Year-wise each aggregate "
            "the underlying daily data into that period for comparison — "
            "your daily uploads are unaffected either way."
        ),
    )

    mode = "Full Period"
    compare_year = None
    compare_month = None
    compare_week_start = None
    compare_date = None
    available_dates = database.get_available_dates()

    if comparison_type == "Day-wise":
        # All available dates are offered — no restriction based on anchor_date.
        # Both selectors are fully independent so any two dates in the database
        # can be compared (e.g. 30-Jun vs 01-Jun, or May vs June).
        all_dates_sorted = sorted(available_dates, reverse=True)
        earlier_dates = sorted((d for d in available_dates if d < anchor_date), reverse=True)
        default_compare_date = earlier_dates[0] if earlier_dates else (
            all_dates_sorted[1] if len(all_dates_sorted) > 1 else all_dates_sorted[0]
        )
        date_options = all_dates_sorted or [default_compare_date]
        compare_date = date_picker.render_date_dropdown(
            date_options,
            key_prefix=f"{key_prefix}_day_date",
            label="Compare Date",
            default_date=default_compare_date,
        )
        st.caption("Any date in the database can be selected — both date selectors are fully independent.")

    elif comparison_type == "Week-wise":
        mode_col, week_col = st.columns(2)
        with mode_col:
            mode = st.radio(
                "Compare basis",
                options=ra.COMPARISON_MODES,
                index=0,
                key=f"{key_prefix}_week_mode",
                help=(
                    "Full Period compares the two whole calendar weeks "
                    "(Mon–Sun). To-Date compares only the same partial "
                    "range (e.g. Mon through the anchor date's weekday) "
                    "on both sides."
                ),
            )
        available_week_starts = database.get_available_week_starts()
        cur_monday, _ = ra.week_range(anchor_date)
        default_compare_monday = cur_monday - dt.timedelta(days=7)
        # All weeks available — including the current week (e.g. compare this week vs last week vs any other)
        week_options = available_week_starts or [default_compare_monday]
        week_options = sorted(week_options, reverse=True)
        with week_col:
            compare_week_start = st.selectbox(
                "Compare Week",
                options=week_options,
                index=(
                    week_options.index(default_compare_monday)
                    if default_compare_monday in week_options
                    else 0
                ),
                format_func=lambda w: f"Week of {w:%d %b %Y}",
                key=f"{key_prefix}_week_week",
            )

    elif comparison_type == "Month-wise":
        mode_col, year_col, month_col = st.columns(3)
        with mode_col:
            mode = st.radio(
                "Compare basis",
                options=ra.COMPARISON_MODES,
                index=0,
                key=f"{key_prefix}_month_mode",
            )
        available_year_months = database.get_available_year_months()
        available_years_for_month = sorted({y for y, m in available_year_months}, reverse=True)
        # FIX (Bug 5): use the directly-imported function rather than
        # accessing a private symbol on a different module.
        default_compare_month_date = _safe_month_shift_local(anchor_date, -1)
        with year_col:
            compare_year = st.selectbox(
                "Compare Year",
                options=available_years_for_month or [anchor_date.year],
                index=(
                    available_years_for_month.index(default_compare_month_date.year)
                    if default_compare_month_date.year in available_years_for_month
                    else 0
                ),
                key=f"{key_prefix}_month_year",
            )
        with month_col:
            compare_month = st.selectbox(
                "Compare Month",
                options=list(range(1, 13)),
                index=default_compare_month_date.month - 1,
                format_func=lambda m: MONTH_NAMES[m - 1],
                key=f"{key_prefix}_month_month",
            )

    elif comparison_type == "Year-wise":
        mode_col, year_col = st.columns(2)
        with mode_col:
            mode = st.radio(
                "Compare basis",
                options=ra.COMPARISON_MODES,
                index=0,
                key=f"{key_prefix}_year_mode",
            )
        available_years = database.get_available_years()
        # All years available — including the current year
        other_years = available_years or [anchor_date.year - 1]
        with year_col:
            compare_year = st.selectbox(
                "Compare Year",
                options=sorted(other_years, reverse=True),
                index=0,
                key=f"{key_prefix}_year_year",
            )

    ranges = ra.resolve_comparison_ranges(
        comparison_type,
        anchor_date,
        compare_year=compare_year,
        compare_month=compare_month,
        compare_week_start=compare_week_start,
        compare_date=compare_date,
        mode=mode,
        available_dates=available_dates,
    )
    ranges["comparison_type"] = comparison_type
    ranges["mode"] = mode
    return ranges
