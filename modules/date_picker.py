"""
date_picker.py — A more convenient date-selection widget: separate
Year / Month / Day dropdowns instead of one long flat list of every
available date.

A single flat dropdown gets unwieldy fast once the database spans
multiple years (e.g. a multi-year historical import, or AOP-adjacent
revenue history) — scrolling through hundreds of individual dates to
find the one you want is exactly the "headache" this replaces.

Only years/months/days that actually have data in `available_dates` are
ever offered at each step: picking a year narrows the month dropdown to
months with data in that year, and picking a month narrows the day
dropdown to days with data in that year+month — so changing Year resets
Month, and changing Month resets Day, so you always land on a valid date.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Optional

import streamlit as st


def render_date_dropdown(
    available_dates: list[dt.date],
    key_prefix: str,
    label: str = "Date",
    default_date: Optional[dt.date] = None,
) -> Optional[dt.date]:
    """
    Render three chained dropdowns (Year → Month → Day) and return the
    selected date. Returns None if `available_dates` is empty.

    Chaining means:
    - Month list is filtered to the currently selected Year.
    - Day list is filtered to the currently selected Year + Month.
    - When Year changes, Month resets to the latest available month in
      that year (and Day resets accordingly).
    - When Month changes, Day resets to the latest available day in
      that year+month.

    This prevents the stale-index bug where Streamlit keeps an old month
    or day index in session_state after the parent dropdown changes.
    """
    if not available_dates:
        return None

    available_set = set(available_dates)
    most_recent = max(available_dates)
    default_date = default_date if default_date in available_set else most_recent

    years = sorted({d.year for d in available_dates}, reverse=True)

    year_key = f"{key_prefix}_year"
    month_key = f"{key_prefix}_month"
    day_key = f"{key_prefix}_day"
    # Internal tracking keys (not widget keys)
    prev_year_key = f"__dp_{key_prefix}_prev_year"
    prev_month_key = f"__dp_{key_prefix}_prev_month"

    c1, c2, c3 = st.columns(3)

    # ── Year ──────────────────────────────────────────────────────────────
    with c1:
        # BUG FIX: previously year_index was always computed from default_date,
        # so navigating away and back to any page reset the year to whatever
        # default_active_date() returned (always the most recent date in the DB)
        # even if the user had explicitly picked a different year.
        # Month and day already checked session_state first — year must too.
        current_year_val = st.session_state.get(year_key)
        if current_year_val in years:
            year_index = years.index(current_year_val)
        else:
            year_index = years.index(default_date.year) if default_date.year in years else 0
        selected_year = st.selectbox(
            f"{label} — Year", options=years, index=year_index, key=year_key
        )

    # Detect year change → clamp month to nearest valid month in new year
    # and clamp day accordingly. Preserves selections as much as possible.
    if st.session_state.get(prev_year_key) != selected_year:
        prev_month = st.session_state.get(month_key)
        if prev_month is not None and prev_month not in months_in_year:
            closest_month = min(months_in_year, key=lambda m: abs(m - prev_month))
            st.session_state[month_key] = closest_month
        # day will be clamped in the month-change block below if needed
        st.session_state.pop(f"__dp_{key_prefix}_prev_month", None)  # force month re-check
    st.session_state[prev_year_key] = selected_year

    # ── Month — filtered to selected_year ─────────────────────────────────
    months_in_year = sorted({d.month for d in available_dates if d.year == selected_year})
    with c2:
        if default_date.year == selected_year and default_date.month in months_in_year:
            default_month = default_date.month
        else:
            default_month = months_in_year[-1]

        # Use session_state value if it's still valid for this year
        current_month_val = st.session_state.get(month_key)
        if current_month_val in months_in_year:
            month_index = months_in_year.index(current_month_val)
        else:
            month_index = months_in_year.index(default_month)

        selected_month = st.selectbox(
            f"{label} — Month",
            options=months_in_year,
            index=month_index,
            format_func=lambda m: calendar.month_name[m],
            key=month_key,
        )

    # Detect month change → clamp day to nearest valid day in new month
    # instead of resetting to the default (last day). This preserves the
    # user's day selection (e.g. day=29) when they switch months, as long
    # as that day exists in the new month. If not, use the closest day.
    if st.session_state.get(prev_month_key) != (selected_year, selected_month):
        prev_day = st.session_state.get(day_key)
        if prev_day is not None and prev_day not in days_in_month:
            # Day doesn't exist in new month — pick the closest available day
            closest = min(days_in_month, key=lambda d: abs(d - prev_day))
            st.session_state[day_key] = closest
        # If prev_day is valid in new month, leave it — it will be picked up below
    st.session_state[prev_month_key] = (selected_year, selected_month)

    # ── Day — filtered to selected_year + selected_month ──────────────────
    days_in_month = sorted(
        {d.day for d in available_dates if d.year == selected_year and d.month == selected_month},
        reverse=True,
    )
    with c3:
        if (
            default_date.year == selected_year
            and default_date.month == selected_month
            and default_date.day in days_in_month
        ):
            default_day = default_date.day
        else:
            default_day = days_in_month[0]

        current_day_val = st.session_state.get(day_key)
        if current_day_val in days_in_month:
            day_index = days_in_month.index(current_day_val)
        else:
            day_index = days_in_month.index(default_day)

        selected_day = st.selectbox(
            f"{label} — Day",
            options=days_in_month,
            index=day_index,
            key=day_key,
        )

    return dt.date(selected_year, selected_month, selected_day)
