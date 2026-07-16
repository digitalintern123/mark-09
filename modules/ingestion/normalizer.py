"""
normalizer.py — Assemble the canonical output DataFrame.

Takes:
  * MappingResult (role → column assignment)
  * Cleaned data DataFrame
  * Schema context (title lines, sheet name, file name)
  * Unit multiplier

Produces a DataFrame with exactly the standard schema:
  date | location | segment | outlet | business_unit | pax | revenue |
  aop | traffic | source_file | upload_time | confidence_score

Missing required fields are recovered from context wherever safe:
  * date      ← title rows or file name
  * location  ← sheet name, title rows, or sparse column forward-fill
  * segment   ← outlet-name keywords
  * outlet    ← synthesised as "<Location> - <Segment>"

Every recovery is logged and surfaced as a warning, never silent.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Optional

import numpy as np
import pandas as pd

from .column_mapper import (
    MappingResult, REQUIRED_ROLES,
    ROLE_DATE, ROLE_LOCATION, ROLE_SEGMENT, ROLE_OUTLET, ROLE_BU,
    ROLE_PAX, ROLE_REVENUE, ROLE_AOP, ROLE_TRAFFIC,
    _LOCATION_VOCAB, _SEGMENT_VOCAB,
)
from .data_cleaner import parse_number, parse_date

log = logging.getLogger(__name__)

# Outlet-name keywords → segment, for recovery when no segment column exists.
_OUTLET_KW_TO_SEG: list[tuple[str, str]] = [
    ("sky plate", "Subsidiary"), ("skyplate", "Subsidiary"),
    ("eats", "Subsidiary"), ("lounge", "Lounges"),
    ("atithya", "Atithya"), ("meet & greet", "Atithya"),
    ("meet and greet", "Atithya"), ("spa", "Others"),
]

_DATE_IN_TEXT_RE = re.compile(
    r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}"
    r"|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"
    r"|\d{1,2}(st|nd|rd|th)?[\s.\-]+[A-Za-z]{3,9}[\s.\-,]+\d{2,4}"
    r"|[A-Za-z]{3,9}[\s.\-]+\d{1,2}(st|nd|rd|th)?[\s.\-,]+\d{4})"
)


STANDARD_COLS = [
    "date", "location", "segment", "business_unit", "outlet",
    "pax", "revenue", "aop", "traffic",
]


def build_output(
    mapping: MappingResult,
    data: pd.DataFrame,
    title_context: list[str],
    sheet_name: str,
    file_name: str,
    unit_multiplier: float = 1.0,
    upload_time: Optional[dt.datetime] = None,
    confidence: float = 0.0,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Assemble the canonical output DataFrame.

    Returns (df, warnings_list).
    Raises ValueError if required fields cannot be recovered.
    """
    warnings: list[str] = []
    n = len(data)
    out = pd.DataFrame(index=range(n))

    def _col(role: str) -> Optional[pd.Series]:
        a = mapping.assignments.get(role)
        if a is None:
            return None
        j = a.col_index
        if j >= data.shape[1]:
            return None
        return data.iloc[:, j]

    # ── date ─────────────────────────────────────────────────────────────
    if ROLE_DATE in mapping.assignments:
        out["date"] = _col(ROLE_DATE).map(parse_date)
    else:
        recovered = _recover_date(title_context, file_name)
        if recovered is None:
            raise ValueError(
                "Could not identify a Date column or recover one from context. "
                "Please ensure the file has a Date / Report Date / Business Date column."
            )
        out["date"] = recovered
        warnings.append(
            f"No Date column found — applied {recovered.isoformat()} "
            "(from the file title / name) to every row."
        )

    # ── location ─────────────────────────────────────────────────────────
    if ROLE_LOCATION in mapping.assignments:
        loc_series = _col(ROLE_LOCATION)
        if loc_series.map(_is_blank).mean() > 0.2:
            loc_series = loc_series.map(lambda v: None if _is_blank(v) else v).ffill()
            warnings.append(
                "Location column had many blanks (merged-cell style) — "
                "forward-filled from the value above."
            )
        out["location"] = loc_series.map(_norm_location)
    else:
        loc = _recover_location(title_context, sheet_name, file_name)
        if loc is None:
            raise ValueError(
                "Could not identify a Location column or recover one from context "
                "(sheet name / title rows / file name should mention Delhi, Hyderabad, "
                "Goa, or a known airport code)."
            )
        out["location"] = loc
        warnings.append(
            f"No Location column — applied '{loc}' (from sheet/title/file name) "
            "to every row."
        )

    # ── outlet (needed before segment recovery) ───────────────────────────
    if ROLE_OUTLET in mapping.assignments:
        outlet_series = _col(ROLE_OUTLET).map(
            lambda v: None if _is_blank(v) else str(v).strip()
        )
        out["outlet"] = outlet_series
    else:
        out["outlet"] = None

    # ── segment ───────────────────────────────────────────────────────────
    if ROLE_SEGMENT in mapping.assignments:
        seg_series = _col(ROLE_SEGMENT)
        if seg_series.map(_is_blank).mean() > 0.2:
            seg_series = seg_series.map(lambda v: None if _is_blank(v) else v).ffill()
        out["segment"] = seg_series.map(
            lambda v: _norm_segment(v) if not _is_blank(v) else None
        )
    else:
        # Try recovering from outlet keywords
        if out["outlet"].notna().any():
            derived = out["outlet"].map(_seg_from_outlet)
            if derived.notna().mean() >= 0.5:
                out["segment"] = derived.fillna("Others")
                warnings.append(
                    "No Segment column — derived from outlet-name keywords "
                    "(unmatched outlets set to 'Others')."
                )
            else:
                seg = _recover_segment(title_context, sheet_name, file_name)
                if seg:
                    out["segment"] = seg
                    warnings.append(
                        f"No Segment column — applied '{seg}' from context to every row."
                    )
                else:
                    raise ValueError(
                        "Could not identify a Segment / Business column or recover one "
                        "from context."
                    )
        else:
            seg = _recover_segment(title_context, sheet_name, file_name)
            if seg:
                out["segment"] = seg
                warnings.append(
                    f"No Segment column — applied '{seg}' from context to every row."
                )
            else:
                raise ValueError("Could not identify a Segment / Business column.")

    # ── outlet synthesis if still all-null ────────────────────────────────
    if out["outlet"].isna().all():
        out["outlet"] = out["location"].astype(str) + " - " + out["segment"].astype(str)
        warnings.append(
            "No Outlet column — rows labelled '<Location> - <Segment>' "
            "so they aggregate correctly at segment level."
        )

    # ── business_unit ────────────────────────────────────────────────────
    if ROLE_BU in mapping.assignments:
        out["business_unit"] = _col(ROLE_BU).map(
            lambda v: None if _is_blank(v) else str(v).strip()
        )
    else:
        out["business_unit"] = None

    # ── numeric fields ────────────────────────────────────────────────────
    if ROLE_REVENUE not in mapping.assignments:
        raise ValueError(
            "Could not identify a Revenue / Sales / Amount column."
        )
    out["revenue"] = _col(ROLE_REVENUE).map(parse_number)
    if unit_multiplier != 1.0:
        out["revenue"] = out["revenue"] * unit_multiplier

    out["pax"] = (
        _col(ROLE_PAX).map(parse_number)
        if ROLE_PAX in mapping.assignments
        else pd.NA
    )
    if ROLE_PAX not in mapping.assignments:
        warnings.append("No PAX column identified — PAX will be blank.")

    out["aop"] = (
        _col(ROLE_AOP).map(parse_number)
        if ROLE_AOP in mapping.assignments
        else pd.NA
    )
    if ROLE_AOP in mapping.assignments and unit_multiplier != 1.0:
        out["aop"] = out["aop"] * unit_multiplier

    out["traffic"] = (
        _col(ROLE_TRAFFIC).map(parse_number)
        if ROLE_TRAFFIC in mapping.assignments
        else pd.NA
    )

    # ── metadata columns ─────────────────────────────────────────────────
    out["source_file"]       = file_name
    out["upload_time"]       = (upload_time or dt.datetime.now()).isoformat()
    out["confidence_score"]  = round(confidence, 4)

    return out, warnings


# ── Recovery helpers ──────────────────────────────────────────────────────

def _recover_date(context: list[str], file_name: str) -> Optional[dt.date]:
    for text in context + [file_name]:
        for m in _DATE_IN_TEXT_RE.finditer(str(text)):
            d = parse_date(m.group(0))
            if d is not None:
                return d
    return None


def _recover_location(
    context: list[str], sheet_name: str, file_name: str
) -> Optional[str]:
    for text in [sheet_name] + context + [file_name]:
        loc = _scan_location(text)
        if loc:
            return loc
    return None


def _recover_segment(
    context: list[str], sheet_name: str, file_name: str
) -> Optional[str]:
    for text in context + [sheet_name, file_name]:
        t = str(text).strip().lower()
        for key, canon in _SEGMENT_VOCAB.items():
            if len(key) >= 4 and key in t:
                return canon
    return None


def _scan_location(text: str) -> Optional[str]:
    t = str(text).strip().lower()
    for key, canon in _LOCATION_VOCAB.items():
        if key == t:
            return canon
    for key, canon in _LOCATION_VOCAB.items():
        if len(key) > 2 and re.search(rf"\b{re.escape(key)}\b", t):
            return canon
    return None


def _norm_location(v: Any) -> Optional[str]:
    if _is_blank(v):
        return None
    t = str(v).strip().lower()
    if t in _LOCATION_VOCAB:
        return _LOCATION_VOCAB[t]
    for key, canon in _LOCATION_VOCAB.items():
        if len(key) > 2 and key in t:
            return canon
    return str(v).strip().title()


def _norm_segment(v: Any) -> Optional[str]:
    if _is_blank(v):
        return None
    t = str(v).strip().lower()
    if t in _SEGMENT_VOCAB:
        return _SEGMENT_VOCAB[t]
    for key, canon in _SEGMENT_VOCAB.items():
        if len(key) >= 4 and key in t:
            return canon
    return str(v).strip()


def _seg_from_outlet(outlet: Any) -> Optional[str]:
    if _is_blank(outlet):
        return None
    t = str(outlet).strip().lower()
    for kw, seg in _OUTLET_KW_TO_SEG:
        if kw in t:
            return seg
    return None


def _is_blank(v: Any) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() in ("", "nan", "none", "NaN", "None")
