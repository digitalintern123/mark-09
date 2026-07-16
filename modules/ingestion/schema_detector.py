"""
schema_detector.py — Header and table-structure detection.

Given a raw grid (all-object DataFrame, no assumed header), finds:
  * The real header row (first, fifth, merged — wherever it actually is)
  * Multi-level headers → merges into single flat labels
  * Title/context rows above the header (date, location, unit declarations)
  * Table orientation: normal (rows = records) vs transposed (rows = metrics)
  * Wide/pivot layout: dates or locations across columns
  * Data slice: the sub-frame below the header

Output: a SchemaInfo object consumed by column_mapper and data_cleaner.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

ORIENTATION_NORMAL     = "normal"       # rows = one record each
ORIENTATION_TRANSPOSED = "transposed"   # rows = metrics, cols = dates
LAYOUT_LONG            = "long"         # normal long-format
LAYOUT_WIDE_DATE       = "wide_date"    # dates across columns
LAYOUT_WIDE_LOCATION   = "wide_location"  # locations across columns
LAYOUT_PIVOT           = "pivot"        # general pivot / cross-tab


@dataclass
class SchemaInfo:
    headers: list[str]             # merged, one per column
    data: pd.DataFrame             # rows below the header
    title_context: list[str]       # text lines above the header
    header_row_idx: int            # row index in the original grid
    orientation: str               # ORIENTATION_*
    layout: str                    # LAYOUT_*
    has_multi_level_header: bool = False
    warnings: list[str] = field(default_factory=list)


# ── Public API ────────────────────────────────────────────────────────────

def detect_schema(grid: pd.DataFrame) -> SchemaInfo:
    """
    Analyse a raw grid and return a SchemaInfo describing its structure.
    Never raises; populates warnings on partial failures.
    """
    warnings: list[str] = []

    if grid is None or grid.empty:
        return SchemaInfo(
            headers=[], data=pd.DataFrame(),
            title_context=[], header_row_idx=0,
            orientation=ORIENTATION_NORMAL, layout=LAYOUT_LONG,
            warnings=["Grid is empty"],
        )

    # If the DataFrame already has meaningful column names (not the default
    # RangeIndex 0/1/2...) prepend them as a synthetic row so the scorer
    # can see them. This handles files that pandas reads with header=0,
    # where the real header is in df.columns, not in the grid rows.
    col_names = [str(c) for c in grid.columns]
    _all_default = all(str(i) == c for i, c in enumerate(col_names))
    _col_score = _header_row_score(col_names)
    if not _all_default and _col_score > 0.25:
        # Prepend column names as row -1 so the scorer can pick them up
        import numpy as _np
        _header_row = pd.DataFrame([col_names], columns=grid.columns)
        grid = pd.concat([_header_row, grid], ignore_index=True)

    # 1. Find the header row (may be multi-level)
    header_rows, header_row_idx = _find_header_rows(grid, warnings)

    # 2. Build merged flat labels
    headers, multi = _merge_header_rows(grid, header_rows)

    # 3. Title/context lines above the header
    context_lines = _extract_context(grid, header_rows[0])

    # 4. Data slice
    data = grid.iloc[header_rows[-1] + 1:].reset_index(drop=True)
    # Trim trailing all-blank rows
    keep = ~data.apply(lambda r: all(_is_blank(v) for v in r), axis=1)
    if keep.any():
        data = data.loc[:keep[keep].index.max()].reset_index(drop=True)

    # 5. Detect orientation and layout
    orientation = _detect_orientation(headers, data)
    layout      = _detect_layout(headers, data, context_lines)

    return SchemaInfo(
        headers=headers, data=data,
        title_context=context_lines,
        header_row_idx=header_row_idx,
        orientation=orientation,
        layout=layout,
        has_multi_level_header=multi,
        warnings=warnings,
    )


# ── Header detection ──────────────────────────────────────────────────────

def _find_header_rows(grid: pd.DataFrame, warnings: list[str]) -> tuple[list[int], int]:
    """
    Find 1 or 2 header rows.

    Scans the first 20 rows.  A header row:
      * Contains mostly text cells (not numbers)
      * Matches vocabulary words from any canonical role
      * Is followed by rows with more numeric content

    Returns ([row_indices...], primary_row_idx).
    """
    n_scan = min(len(grid), 20)
    best_idx, best_score = 0, 0.0

    # Check if pandas already read row 0 as column headers —
    # in that case the grid's column names ARE the header and row 0 data
    # is the first data row. Score the column names as a virtual row -1.
    col_names = [str(c) for c in grid.columns]
    col_score = _header_row_score(col_names)
    if col_score > 0.3:
        # Column names look like a real header — bias strongly toward row 0
        # so the detector picks index 0 (the real first data row after header)
        # and uses the column names, not some footer row.
        best_idx, best_score = 0, col_score + 0.5

    for i in range(n_scan):
        score = _header_row_score(list(grid.iloc[i]))
        # Bonus for row 0: if it contains all text and no numbers, prefer it
        if i == 0:
            r0 = list(grid.iloc[0])
            nb = [c for c in r0 if not _is_blank(c)]
            if nb and all(not _is_number_cell(c) and not _is_date_cell(c) for c in nb):
                score += 0.4
        if score > best_score:
            best_idx, best_score = i, score

    # Check for a second header row immediately above (multi-level)
    header_rows = [best_idx]
    if best_idx > 0:
        above = list(grid.iloc[best_idx - 1])
        above_score = _header_row_score(above)
        above_nonnull = sum(1 for v in above if not _is_blank(v))
        # Accept as part of the header if it has vocab hits or ≥3 filled cells
        joined = " ".join(str(v).strip().lower() for v in above if not _is_blank(v))
        has_vocab = any(
            kw in joined
            for kw in ("pax", "revenue", "sales", "domestic", "international",
                       "arrival", "departure", "dep", "arr", "location", "segment",
                       "lounge", "atithya", "terminal", "budget", "aop",
                       "dom", "int", "atm", "date", "outlet", "cargo",
                       "flight", "traffic", "total", "unit")
        )
        if above_score >= 0.3 and (above_nonnull >= 3 or has_vocab):
            header_rows.insert(0, best_idx - 1)

    return header_rows, best_idx


def _header_row_score(cells: list) -> float:
    non_blank = [c for c in cells if not _is_blank(c)]
    if len(non_blank) < 2:
        return 0.0
    text_cells = 0
    vocab_hits = 0.0
    date_like  = 0
    for c in non_blank:
        if _is_number_cell(c):
            continue
        if _is_date_cell(c):
            date_like += 1
            continue
        text_cells += 1
        t = _norm(c)
        best = _best_vocab_hit(t)
        vocab_hits += best
    text_frac = (text_cells + date_like) / max(1, len(non_blank))
    return text_frac * 0.4 + min(vocab_hits / 3.0, 1.0) * 0.6 + (0.1 if date_like >= 3 else 0.0)


def _merge_header_rows(grid: pd.DataFrame, header_rows: list[int]) -> tuple[list[str], bool]:
    """
    Merge 1 or 2 header rows into flat string labels.
    Upper row is forward-filled across blank cells (merged-cell pattern).
    """
    n_cols = grid.shape[1]
    multi = len(header_rows) == 2
    upper_ffill = [""] * n_cols

    if multi:
        last = ""
        for j in range(n_cols):
            v = grid.iat[header_rows[0], j]
            if not _is_blank(v):
                last = str(v).strip()
            upper_ffill[j] = last

    headers: list[str] = []
    for j in range(n_cols):
        low = grid.iat[header_rows[-1], j]
        low_s = "" if _is_blank(low) else str(low).strip()
        up_s = upper_ffill[j] if multi else ""
        if up_s and up_s != low_s:
            label = f"{up_s} {low_s}".strip()
        else:
            label = low_s or up_s
        headers.append(label)

    return headers, multi


def _extract_context(grid: pd.DataFrame, first_header_row: int) -> list[str]:
    """Collect text from rows above the header (title, units, date hints)."""
    lines: list[str] = []
    for i in range(first_header_row):
        cells = [str(c).strip() for c in grid.iloc[i] if not _is_blank(c)]
        if cells:
            lines.append(" ".join(cells))
    return lines


# ── Orientation & layout detection ───────────────────────────────────────

def _detect_orientation(headers: list[str], data: pd.DataFrame) -> str:
    """
    Transposed layout: the column-0 cells of data rows are metrics
    (PAX, Revenue, Domestic, International …) and dates run across row N.
    """
    if data.empty or data.shape[1] < 3:
        return ORIENTATION_NORMAL

    col0_vals = [str(v).strip().lower() for v in data.iloc[:, 0].tolist()
                 if not _is_blank(v)]
    metric_words = {
        "pax", "passengers", "footfall", "revenue", "sales", "amount",
        "domestic", "international", "arrival", "dep.", "arr.", "departure",
        "dep", "arr", "income", "collection",
    }
    metric_hits = sum(1 for v in col0_vals if v in metric_words)

    # Also check if any row in data contains 3+ date objects
    date_row_exists = False
    for i in range(min(10, len(data))):
        date_count = sum(1 for v in data.iloc[i].tolist() if _is_date_cell(v))
        if date_count >= 3:
            date_row_exists = True
            break

    if metric_hits >= 2 and date_row_exists:
        return ORIENTATION_TRANSPOSED
    return ORIENTATION_NORMAL


def _detect_layout(headers: list[str], data: pd.DataFrame,
                   context: list[str]) -> str:
    """Detect whether the table is long-format or a wide/pivot variant."""
    # Count date-like and location-like headers
    date_headers  = sum(1 for h in headers if _is_date_cell(h))
    loc_headers   = sum(1 for h in headers if _is_location_header(h))

    if date_headers >= 3:
        return LAYOUT_WIDE_DATE
    if loc_headers >= 2:
        return LAYOUT_WIDE_LOCATION

    # Check data cells: if many cells in first few rows are dates → wide
    if not data.empty:
        sample_cells = data.head(3).values.flatten()
        date_cell_count = sum(1 for v in sample_cells if _is_date_cell(v))
        if date_cell_count >= 5:
            return LAYOUT_WIDE_DATE

    return LAYOUT_LONG


# ── Helpers ───────────────────────────────────────────────────────────────

_KNOWN_LOCATION_WORDS = {
    "delhi", "hyderabad", "goa", "del", "hyd", "gox", "goi",
    "igi", "igia", "rgia", "mopa", "dabolim",
}

_ALL_VOCAB: set[str] = set()
def _build_vocab():
    global _ALL_VOCAB
    from .column_mapper import SYNONYMS
    for syns in SYNONYMS.values():
        for pat, _ in syns:
            _ALL_VOCAB.update(pat.lower().split())
_build_vocab()


def _best_vocab_hit(t: str) -> float:
    try:
        from .column_mapper import SYNONYMS
        best = 0.0
        for syns in SYNONYMS.values():
            for pat, w in syns:
                pn = pat.lower().strip()
                if t == pn:
                    best = max(best, w)
                elif pn in t and len(pn) >= 3:
                    best = max(best, w * 0.65)
        return best
    except Exception:
        return 0.0


def _norm(s: Any) -> str:
    s = str(s) if s is not None else ""
    s = re.sub(r"[^\w\s]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == ""


def _is_number_cell(v: Any) -> bool:
    if isinstance(v, (int, float, np.integer, np.floating)):
        return True
    s = str(v).strip().replace(",", "").replace("₹", "").replace("$", "")
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_date_cell(v: Any) -> bool:
    import datetime as _dt
    if isinstance(v, (_dt.datetime, _dt.date)):
        return True
    s = str(v).strip()
    if len(s) < 5 or len(s) > 30:
        return False
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="raise")
        return 2000 <= ts.year <= 2050
    except Exception:
        return False


def _is_location_header(h: str) -> bool:
    t = h.strip().lower()
    return t in _KNOWN_LOCATION_WORDS
