"""
date_utils.py — Shared date helper utilities.

Previously _safe_month_shift existed as two identical private functions:
  - database._safe_month_shift()
  - revenue_analysis._safe_month_shift_local()

Both are now removed and replaced with this single canonical implementation.
comparison_widget.py also accessed revenue_analysis._safe_month_shift_local
directly (a private-symbol import across modules); that usage now points here.
"""

from __future__ import annotations

import datetime as dt


def safe_month_shift(d: dt.date, months: int) -> dt.date:
    """
    Shift a date by a given number of months, clamping the day to the
    last valid day of the target month if needed (e.g. Jan 31 + 1 month
    → Feb 28/29 rather than raising an error).

    Handles both positive (forward) and negative (backward) shifts.

    Args:
        d:      The starting date.
        months: Number of months to shift; negative shifts backward.

    Returns:
        A dt.date in the target month, day clamped to the month's length.

    Examples:
        safe_month_shift(dt.date(2024, 1, 31), 1)  → 2024-02-29 (leap year)
        safe_month_shift(dt.date(2024, 3, 31), -1) → 2024-02-29
        safe_month_shift(dt.date(2024, 1, 15), -12) → 2023-01-15
    """
    # Convert to a flat month index (0-based), shift, then convert back.
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1

    # Determine the last day of the target month.
    if month == 12:
        next_month_first = dt.date(year + 1, 1, 1)
    else:
        next_month_first = dt.date(year, month + 1, 1)
    last_day_of_month = (next_month_first - dt.timedelta(days=1)).day

    # Clamp the day so e.g. Jan 31 → Feb 28 instead of raising ValueError.
    day = min(d.day, last_day_of_month)
    return dt.date(year, month, day)
