"""
data_cleaner.py — Normalise raw extracted data into clean typed values.

Responsibilities:
  * Parse numbers: ₹ / Rs / INR prefixes, comma groupings, parenthesised
    negatives, unit multipliers (Lakhs / Crores / K / M)
  * Parse dates: Indian day-first, Excel serials, mixed string formats
  * Melt wide layouts (dates-as-columns, locations-as-columns) to long form
  * Forward-fill sparse label columns (merged-cell style location/segment)
  * Remove subtotal / grand-total / blank rows
  * Strip decorative rows (repeated headers, footer notes)
  * Apply unit multipliers declared in title rows ("Rs. in Lakhs")
  * Produce a long-format DataFrame ready for column_mapper
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Unit multipliers ──────────────────────────────────────────────────────
_UNIT_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"in\s+crores?|rs\.?\s*crores?|₹\s*crores?|\bcr\b", re.I), 1e7),
    (re.compile(r"in\s+lakhs?|in\s+lacs?|rs\.?\s*lakhs?|rs\.?\s*lacs?|₹\s*lakhs?", re.I), 1e5),
    (re.compile(r"in\s*'?000s?|in\s+thousands?|₹\s*'000|rs\.?\s*'000", re.I), 1e3),
    (re.compile(r"in\s+millions?|\bmn\b", re.I), 1e6),
]

# Patterns that mark a row as a subtotal / grand-total to be dropped.
_TOTAL_MARKERS = [
    "total", "grand total", "sub total", "subtotal", "grand-total",
    "overall", "net total", "sum", "all locations", "all outlets",
    "combined", "aggregate",
]

# Currency / formatting characters to strip from numeric cells.
_CURRENCY_RE   = re.compile(r"[₹$£€]|rs\.?|inr", re.I)
_NUM_SUFFIX_RE: list[tuple[re.Pattern, float]] = [
    (re.compile(r"\bcr(ores?)?\b\.?$", re.I), 1e7),
    (re.compile(r"\bl(akhs?|acs?)?\b\.?$", re.I), 1e5),
    (re.compile(r"\bk\b$", re.I), 1e3),
    (re.compile(r"\bmn?\b$", re.I), 1e6),
]


# ── Public API ────────────────────────────────────────────────────────────

def clean(
    headers: list[str],
    data: pd.DataFrame,
    title_context: list[str],
    layout: str,
    orientation: str,
) -> tuple[list[str], pd.DataFrame, float]:
    """
    Full cleaning pipeline.

    Returns (cleaned_headers, cleaned_data, unit_multiplier).
    The caller should then run column_mapper on the result.
    """
    from .schema_detector import (
        LAYOUT_WIDE_DATE, LAYOUT_WIDE_LOCATION, ORIENTATION_TRANSPOSED,
    )

    unit_mult = _detect_unit_multiplier(title_context, headers)

    # 1. Melt wide / transposed layouts → long form
    if orientation == ORIENTATION_TRANSPOSED:
        headers, data = _melt_transposed(headers, data)
    elif layout == LAYOUT_WIDE_DATE:
        headers, data = _melt_wide(headers, data, kind="date")
    elif layout == LAYOUT_WIDE_LOCATION:
        headers, data = _melt_wide(headers, data, kind="location")

    # 2. Forward-fill sparse label columns (merged-cell style)
    data = _forward_fill_labels(data, headers)

    # 3. Drop subtotal / blank / repeated-header rows
    data = _drop_junk_rows(data, headers)

    # 4. Clean cell values (numbers, dates, text)
    data = _clean_cells(data, headers, unit_mult)

    return headers, data.reset_index(drop=True), unit_mult


def parse_number(value: Any) -> Optional[float]:
    """Parse a messy real-world number cell."""
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        return None if np.isnan(f) else f
    s = str(value).strip()
    if not s or s in {"-", "–", "—", "na", "n/a", "nil", "none", ""}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CURRENCY_RE.sub("", s).strip()
    mult = 1.0
    for pat, m in _NUM_SUFFIX_RE:
        if pat.search(s):
            s = pat.sub("", s).strip()
            mult = m
            break
    s = s.replace(",", "").replace(" ", "").replace("%", "")
    try:
        f = float(s) * mult
        return -f if negative else f
    except ValueError:
        return None


def parse_date(value: Any) -> Optional[dt.date]:
    """Parse a single cell as a date."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        if 36526 <= f <= 55153:   # Excel serial date range 2000–2050
            try:
                return (dt.datetime(1899, 12, 30) + dt.timedelta(days=f)).date()
            except Exception:
                return None
        return None
    s = str(value).strip()
    if not s or len(s) < 5 or len(s) > 40:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_date(float(s))
    # ISO-format (YYYY-MM-DD / YYYY/MM/DD) — parse dayfirst=False first
    # so "2026-07-05" → July 5 not May 7.
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
        try:
            ts = pd.to_datetime(s, dayfirst=False, errors="raise")
            if isinstance(ts, pd.Timestamp) and 2000 <= ts.year <= 2050:
                return ts.date()
        except Exception:
            pass
    # Indian day-first formats (DD-MM-YYYY, DD/MM/YYYY, "05 Jul 2026" …)
    for dayfirst in (True, False):
        try:
            ts = pd.to_datetime(s, dayfirst=dayfirst, errors="raise")
            if isinstance(ts, pd.Timestamp) and 2000 <= ts.year <= 2050:
                return ts.date()
        except Exception:
            continue
    return None


# ── Private helpers ───────────────────────────────────────────────────────

def _detect_unit_multiplier(context_lines: list[str], headers: list[str]) -> float:
    text = " | ".join(context_lines + headers).lower()
    for pat, mult in _UNIT_PATTERNS:
        if pat.search(text):
            return mult
    return 1.0


def _melt_wide(
    headers: list[str], data: pd.DataFrame, kind: str
) -> tuple[list[str], pd.DataFrame]:
    """Melt dates-as-columns or locations-as-columns into long form."""
    from .schema_detector import _is_date_cell, _is_location_header

    matcher = _is_date_cell if kind == "date" else _is_location_header
    new_col = "date" if kind == "date" else "location"

    id_cols: list[int] = []
    val_specs: list[tuple[int, Any, str]] = []   # (col_idx, key_value, metric)

    for j, h in enumerate(headers):
        key = h if matcher(h) else None
        if key is None:
            # Two-row merged header: metric word + entity
            t = h.strip().lower()
            metric = None
            from .column_mapper import SYNONYMS
            for role in ("pax", "revenue", "aop", "traffic"):
                for pat, w in SYNONYMS[role]:
                    if w >= 0.88 and re.search(rf"(^|\W){re.escape(pat)}(\W|$)", t):
                        metric = role
                        break
                if metric:
                    break
            if metric:
                stripped = re.sub(
                    "|".join(re.escape(p) for p, w in SYNONYMS[metric] if w >= 0.88),
                    "", t
                ).strip()
                key2 = stripped if matcher(stripped) else None
                if key2:
                    val_specs.append((j, key2, metric))
                    continue
            id_cols.append(j)
        else:
            t = h.strip().lower()
            metric = "revenue"
            from .column_mapper import SYNONYMS
            for role in ("pax", "traffic", "aop"):
                for pat, w in SYNONYMS[role]:
                    if w >= 0.88 and re.search(rf"(^|\W){re.escape(pat)}(\W|$)", t):
                        metric = role
                        break
            val_specs.append((j, key, metric))

    if not val_specs:
        return headers, data

    long_rows: list[dict] = []
    for _, row in data.iterrows():
        base = {headers[j] or f"col{j}": row.iloc[j] for j in id_cols}
        by_key: dict[Any, dict[str, Any]] = {}
        for j, key, metric in val_specs:
            by_key.setdefault(key, {})[metric] = row.iloc[j]
        for key, metrics in by_key.items():
            r = dict(base)
            r[new_col] = key
            for m, v in metrics.items():
                r[m] = v
            long_rows.append(r)

    long_df = pd.DataFrame(long_rows)
    return list(long_df.columns), long_df.reset_index(drop=True)


def _melt_transposed(
    headers: list[str], data: pd.DataFrame
) -> tuple[list[str], pd.DataFrame]:
    """
    Transposed: each row is a metric, each column is a date.
    Find the date-header row among data rows, then pivot.
    """
    from .schema_detector import _is_date_cell

    date_header_row = -1
    date_col_map: dict[int, Any] = {}

    for i in range(min(10, len(data))):
        cands = {j: data.iat[i, j] for j in range(data.shape[1])
                 if _is_date_cell(data.iat[i, j])}
        if len(cands) >= 3:
            date_col_map = cands
            date_header_row = i
            break

    if date_header_row == -1:
        return headers, data

    from .column_mapper import SYNONYMS
    _pax_words  = {p for p, w in SYNONYMS["pax"]     if w >= 0.85}
    _rev_words  = {p for p, w in SYNONYMS["revenue"] if w >= 0.85}
    _dom_words  = {"domestic", "dom", "dom."}
    _int_words  = {"international", "intl", "intl."}

    # Group numeric rows after the date header into type groups
    groups: list[tuple[str, dict[int, float]]] = []
    current_group_label = "revenue"
    current_totals: dict[int, float] = {}
    in_group = False

    for i in range(date_header_row + 1, len(data)):
        row = data.iloc[i].tolist()
        label_cell = str(row[0]).strip().lower() if not _is_blank(row[0]) else ""
        numeric_vals = {j: float(row[j]) for j in date_col_map
                        if j < len(row) and _isnumeric(row[j])}
        if numeric_vals:
            if in_group:
                for j, v in numeric_vals.items():
                    current_totals[j] = current_totals.get(j, 0.0) + v
            else:
                in_group = True
                current_totals = dict(numeric_vals)
        else:
            if in_group:
                groups.append((current_group_label, dict(current_totals)))
                in_group = False
                current_totals = {}
            if label_cell:
                if any(w in label_cell for w in _dom_words):
                    current_group_label = "Domestic"
                elif any(w in label_cell for w in _int_words):
                    current_group_label = "International"
                else:
                    current_group_label = label_cell.title()

    if in_group:
        groups.append((current_group_label, current_totals))

    if not groups:
        return headers, data

    rows_out: list[dict] = []
    for label, totals in groups:
        for col_idx, total in totals.items():
            rows_out.append({
                "date": date_col_map[col_idx],
                "terminal": label,
                "revenue": total,
            })

    long_df = pd.DataFrame(rows_out)
    return list(long_df.columns), long_df.reset_index(drop=True)


def _forward_fill_labels(data: pd.DataFrame, headers: list[str]) -> pd.DataFrame:
    """
    Forward-fill the leftmost text columns that look like section labels
    (sparse: written once at the top of a group, blank below).
    """
    for j in range(min(3, data.shape[1])):
        col = data.iloc[:, j]
        non_blank = [v for v in col if not _is_blank(v)]
        if not non_blank:
            continue
        blank_frac = 1 - len(non_blank) / len(col)
        all_text = all(not _isnumeric(v) for v in non_blank)
        # Forward-fill if >20% blank AND all values are text
        if blank_frac > 0.20 and all_text:
            filled = col.map(lambda v: None if _is_blank(v) else v).ffill()
            data = data.copy()
            data.iloc[:, j] = filled
    return data


def _drop_junk_rows(data: pd.DataFrame, headers: list[str]) -> pd.DataFrame:
    """Remove subtotal, blank, and repeated-header rows."""
    keep_mask = pd.Series([True] * len(data), index=data.index)

    header_lower = {str(h).strip().lower() for h in headers if h}

    for idx, row in data.iterrows():
        vals = [str(v).strip() for v in row.tolist()]
        # All-blank row
        if all(v == "" or v.lower() in ("nan", "none", "") for v in vals):
            keep_mask[idx] = False
            continue
        # Subtotal marker in first few cells
        leading = vals[:4]
        if any(
            any(m == lv or lv.startswith(m + " ") or lv.endswith(" " + m)
                for m in _TOTAL_MARKERS)
            for lv in [v.lower() for v in leading if v]
        ):
            keep_mask[idx] = False
            continue
        # Repeated header row
        row_lower = {v.lower() for v in vals if v}
        if len(row_lower & header_lower) >= max(2, len(header_lower) * 0.5):
            keep_mask[idx] = False

    return data[keep_mask]


def _clean_cells(
    data: pd.DataFrame, headers: list[str], unit_mult: float
) -> pd.DataFrame:
    """Lightly clean cell values — heavy type coercion is left to the validator."""
    data = data.copy()
    for col in data.columns:
        data[col] = data[col].map(lambda v: _clean_cell(v))
    return data


def _clean_cell(v: Any) -> Any:
    if _is_blank(v):
        return None
    if isinstance(v, (dt.datetime, dt.date, int, float, np.integer, np.floating)):
        return v
    s = str(v).strip()
    # Remove non-breaking spaces and zero-width chars
    s = s.replace("\xa0", " ").replace("\u200b", "").strip()
    # Collapse internal whitespace
    s = re.sub(r"\s{2,}", " ", s)
    return s if s else None


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() in ("", "nan", "none", "NaN", "None")


def _isnumeric(v: Any) -> bool:
    if isinstance(v, (int, float, np.integer, np.floating)):
        return not np.isnan(float(v))
    s = str(v).strip().replace(",", "").replace("₹", "").replace("$", "")
    try:
        float(s)
        return True
    except ValueError:
        return False
