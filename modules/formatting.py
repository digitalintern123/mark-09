"""
formatting.py — Shared number formatting helpers.

Centralizing these means every page and the insights narrative format
numbers identically, matching the spec:
  Revenue:    ₹-prefix, Indian lakh/crore grouping           -> ₹49,35,256
  PAX:        Indian digit grouping, no prefix               -> 1,22,385
  Percentage: 2 decimal places, explicit sign                -> +8.70%
  Missing/None: em dash                                      -> —

format_money() now always uses Indian grouping with ₹ prefix.
format_money_indian() is kept as an alias for backward compatibility.
format_pax() uses Indian grouping without a prefix.
"""

from __future__ import annotations

import math
from typing import Optional, Union

Number = Union[int, float, None]

EM_DASH = "—"


def _is_missing(value: Number) -> bool:
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def format_pax(value: Number) -> str:
    """Whole-number PAX with Indian digit grouping. None/NaN -> em dash.
    e.g. 122385 → 1,22,385"""
    if _is_missing(value):
        return EM_DASH
    return _indian_group(round(float(value)))


def format_money(value: Number) -> str:
    """₹-prefixed revenue with Indian lakh/crore digit grouping, no decimals.
    e.g. 4935256 → ₹49,35,256 · None/NaN/inf → em dash."""
    if _is_missing(value):
        return EM_DASH
    if math.isinf(value):
        return EM_DASH
    return f"₹{_indian_group(round(float(value)))}"


def format_money_indian(value: Number, prefix: str = "₹") -> str:
    """₹-prefixed revenue using Indian lakh/crore digit grouping."""
    if _is_missing(value):
        return EM_DASH
    if math.isinf(value):
        return EM_DASH
    return f"{prefix}{_indian_group(round(float(value)))}"


def format_pct(value: Number, signed: bool = True) -> str:
    """
    Fractional change (e.g. 0.087 for +8.7%) formatted as a percentage with
    2 decimal places and an explicit sign. None/NaN/inf -> em dash.
    """
    if _is_missing(value):
        return EM_DASH
    if math.isinf(value):
        return EM_DASH
    pct = float(value) * 100
    sign = "+" if (signed and pct >= 0) else ""
    return f"{sign}{pct:.2f}%"


def format_rev_per_pax(value: Number) -> str:
    """Revenue-per-PAX shown as ₹ with Indian grouping. None/NaN -> em dash."""
    return format_money(value)


def format_spp(value: Number) -> str:
    """
    Intelligent SPP (Spend Per Passenger = Revenue ÷ Traffic) formatter.

    Rules:
      - None / NaN        → "-"
      - Whole number      → no decimals   e.g. 25   → "25",  0 → "0"
      - Has fraction      → 2 dp          e.g. 25.1 → "25.10", 25.567 → "25.57"

    Never displays "25.00" or "0.00" for whole numbers.
    Keeps SPP as a numeric value in all calculations — format-only.
    """
    if _is_missing(value):
        return "-"
    if math.isinf(float(value)):
        return "-"
    v = round(float(value), 2)
    # Whole number check — use modulo to avoid floating-point edge cases
    if v % 1 == 0:
        return str(int(v))
    return f"{v:.2f}"


def _indian_group(n: int) -> str:
    """
    Format an integer with Indian digit grouping: the last 3 digits form
    one group, then every subsequent group is 2 digits (lakhs, crores, ...).
    e.g. 4935256 -> "49,35,256"; -120000 -> "-1,20,000".
    """
    negative = n < 0
    n = abs(n)
    s = str(n)
    if len(s) <= 3:
        grouped = s
    else:
        last_three = s[-3:]
        remainder = s[:-3]
        groups = []
        while len(remainder) > 2:
            groups.insert(0, remainder[-2:])
            remainder = remainder[:-2]
        if remainder:
            groups.insert(0, remainder)
        grouped = ",".join(groups) + "," + last_three
    return f"-{grouped}" if negative else grouped
