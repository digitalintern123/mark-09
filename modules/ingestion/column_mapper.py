"""
column_mapper.py — Semantic column-role assignment.

Maps arbitrary column headers (and cell-value patterns) to canonical roles:
  date | location | segment | outlet | business_unit | pax | revenue | aop | traffic | ignore

Two-pass approach:
  Pass 1 — exact and fuzzy synonym matching against a curated vocabulary.
  Pass 2 — content-based evidence: what the column's values look like.

Roles are assigned greedily by descending score; each role goes to at most
one column.  The result is a MappingResult that records every assignment
decision for user review.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Canonical roles ───────────────────────────────────────────────────────
ROLE_DATE     = "date"
ROLE_LOCATION = "location"
ROLE_SEGMENT  = "segment"
ROLE_OUTLET   = "outlet"
ROLE_BU       = "business_unit"
ROLE_PAX      = "pax"
ROLE_REVENUE  = "revenue"
ROLE_AOP      = "aop"
ROLE_TRAFFIC  = "traffic"
ROLE_IGNORE   = "ignore"

ALL_ROLES = [
    ROLE_DATE, ROLE_LOCATION, ROLE_SEGMENT, ROLE_OUTLET, ROLE_BU,
    ROLE_PAX, ROLE_REVENUE, ROLE_AOP, ROLE_TRAFFIC,
]

REQUIRED_ROLES = [ROLE_DATE, ROLE_LOCATION, ROLE_SEGMENT, ROLE_OUTLET, ROLE_REVENUE]

# ── Synonym vocabulary ────────────────────────────────────────────────────
# (pattern, weight) — patterns are matched case-insensitively; exact
# full-string matches score the full weight, substring matches score * 0.7.
SYNONYMS: dict[str, list[tuple[str, float]]] = {
    ROLE_DATE: [
        ("date", 1.0), ("report date", 1.0), ("business date", 1.0),
        ("posting date", 1.0), ("transaction date", 1.0), ("txn date", 1.0),
        ("txn dt", 1.0), ("bill date", 0.9), ("invoice date", 0.9),
        ("voucher date", 0.9), ("day", 0.7), ("dt", 0.6),
        ("period", 0.5), ("as on", 0.5), ("month", 0.4),
    ],
    ROLE_LOCATION: [
        ("location", 1.0), ("city", 1.0), ("airport", 1.0),
        ("station", 0.9), ("branch", 0.85), ("site", 0.8),
        ("region", 0.75), ("geography", 0.75), ("geographical segment", 0.95),
        ("airport code", 0.95), ("place", 0.65), ("venue", 0.7),
        ("property", 0.65), ("hub", 0.6), ("base", 0.55),
    ],
    ROLE_SEGMENT: [
        ("segment", 1.0), ("business segment", 1.0), ("business line", 1.0),
        ("business vertical", 0.95), ("line of business", 1.0), ("lob", 0.9),
        ("division", 0.8), ("category", 0.75), ("vertical", 0.8),
        ("department", 0.65), ("dept", 0.55), ("group", 0.5),
        ("type", 0.4),
    ],
    ROLE_OUTLET: [
        ("outlet", 1.0), ("outlet name", 1.0), ("unit", 0.8),
        ("unit name", 0.9), ("unit id", 0.85), ("sub business", 0.95),
        ("sub-business", 0.95), ("sub_business", 0.95), ("service", 0.75),
        ("service point", 0.85), ("store", 0.8), ("shop", 0.75),
        ("counter", 0.75), ("facility", 0.7), ("lounge", 0.85),
        ("lounge name", 0.95), ("cost center", 0.65), ("profit center", 0.65),
    ],
    ROLE_BU: [
        ("business unit", 1.0), ("bu", 0.75), ("sub segment", 0.95),
        ("sub-segment", 0.95), ("sub_segment", 0.95),
    ],
    ROLE_PAX: [
        ("pax", 1.0), ("passengers", 1.0), ("passenger count", 1.0),
        ("no of pax", 1.0), ("no. of pax", 1.0), ("pax count", 1.0),
        ("guests", 1.0), ("guest count", 1.0), ("footfall", 1.0),
        ("covers", 0.95), ("visitors", 0.9), ("headcount", 0.85),
        ("customers", 0.8), ("count", 0.45), ("nos", 0.45),
        ("qty", 0.5), ("quantity", 0.5), ("users", 0.65),
        ("transactions", 0.55),
        # Airport traffic export column names
        ("dom_total_pax", 1.0), ("total_int_pax", 1.0),
        ("dom_arr_pax", 0.9), ("dom_dep_pax", 0.9),
        ("int_arr_pax", 0.9), ("int_dep_pax", 0.9),
        ("dom pax", 0.9), ("int pax", 0.9),
        ("total pax", 0.95), ("pax schedule", 0.9),
    ],
    ROLE_REVENUE: [
        ("revenue", 1.0), ("total revenue", 1.0), ("net revenue", 1.0),
        ("gross revenue", 1.0), ("rev", 0.9), ("sales", 0.95),
        ("net sales", 1.0), ("gross sales", 1.0), ("total sales", 1.0),
        ("amount", 0.85), ("total amount", 0.9), ("net amount", 0.9),
        ("value", 0.65), ("income", 0.85), ("turnover", 0.95),
        ("collection", 0.85), ("billing", 0.8), ("earned", 0.65),
        ("inr", 0.65), ("rs", 0.55), ("receipts", 0.75),
    ],
    ROLE_AOP: [
        ("aop", 1.0), ("budget", 1.0), ("target", 0.95),
        ("aop target", 1.0), ("plan", 0.75), ("budgeted revenue", 1.0),
        ("target revenue", 1.0), ("annual operating plan", 1.0),
        ("budgeted", 0.85), ("planned", 0.7),
    ],
    ROLE_TRAFFIC: [
        ("traffic", 1.0), ("airport traffic", 1.0), ("total traffic", 1.0),
        ("passenger traffic", 0.95), ("throughput", 0.75),
    ],
}

# Headers that should be ignored regardless of fuzzy matches.
IGNORE_PATTERNS = [
    r"^s\.?\s*no\.?$", r"^sr\.?\s*no\.?$", r"^sl\.?\s*no\.?$",
    r"^serial", r"^row\s*no", r"^#$",
    r"growth", r"variance", r"var\b", r"ytd", r"mtd", r"cumulative",
    r"avg\b", r"average", r"rate\b", r"spp\b", r"penetration",
    r"achievement", r"ach\b", r"\bvs\b", r"previous", r"last year",
    r"^%$", r"percent",
]
_IGNORE_RES = [re.compile(p, re.IGNORECASE) for p in IGNORE_PATTERNS]

# Known location values
_LOCATION_VOCAB: dict[str, str] = {
    "delhi": "Delhi", "new delhi": "Delhi", "del": "Delhi",
    "igi": "Delhi", "igia": "Delhi", "indira gandhi": "Delhi",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad", "rgia": "Hyderabad",
    "rajiv gandhi": "Hyderabad", "shamshabad": "Hyderabad",
    "goa": "Goa", "goi": "Goa", "gox": "Goa", "mopa": "Goa",
    "dabolim": "Goa", "manohar": "Goa",
}

# Known segment values
_SEGMENT_VOCAB: dict[str, str] = {
    "lounge": "Lounges", "lounges": "Lounges",
    "atithya": "Atithya", "meet and greet": "Atithya", "meet & greet": "Atithya",
    "others": "Others", "other": "Others",
    "subsidiary": "Subsidiary",
    "ehpl": "EHPL",
    "sky plates": "Sky Plates", "encalm sky plates": "Sky Plates",
    "encalm eats": "Encalm Eats", "eats": "Encalm Eats",
    "spa": "Others", "f&b": "Others",
}


@dataclass
class ColumnAssignment:
    role: str
    source_col: str        # original header label
    col_index: int
    header_score: float
    content_score: float
    total_score: float
    method: str            # "exact", "fuzzy", "content", "combined"
    confidence: float      # 0..1


@dataclass
class MappingResult:
    assignments: dict[str, ColumnAssignment]   # role → assignment
    unmapped_cols: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def has_required(self) -> bool:
        return all(r in self.assignments for r in REQUIRED_ROLES)

    @property
    def missing_required(self) -> list[str]:
        return [r for r in REQUIRED_ROLES if r not in self.assignments]


def map_columns(headers: list[str], data: pd.DataFrame) -> MappingResult:
    """
    Assign canonical roles to columns.
    `headers`  — merged header labels (one per column).
    `data`     — DataFrame of data rows (same column count as headers).
    """
    n = len(headers)
    profiles = {j: _content_profile(data.iloc[:, j] if j < data.shape[1]
                                    else pd.Series([], dtype=object))
                for j in range(n)}

    # Score matrix: scores[(j, role)] = (total, header_score, content_score, method)
    scores: dict[tuple[int, str], tuple[float, float, float, str]] = {}
    for j, header in enumerate(headers):
        if _is_ignorable(header):
            continue
        p = profiles[j]
        for role in ALL_ROLES:
            hs = _header_score(header, role)
            cs = _content_score(p, role)
            if hs <= 0 and cs <= 0:
                continue
            method = ("combined" if hs > 0 and cs > 0
                      else "fuzzy" if hs > 0 else "content")
            total = hs * 0.6 + cs * 0.55
            scores[(j, role)] = (total, hs, cs, method)

    # Magnitude tie-break for numeric columns with weak/absent headers.
    _apply_magnitude_tiebreak(profiles, scores, headers)

    # Greedy assignment: best score first, each role and column used once.
    assignments: dict[str, ColumnAssignment] = {}
    used_cols: set[int] = set()
    warnings: list[str] = []

    for (j, role), (total, hs, cs, method) in sorted(
        scores.items(), key=lambda kv: -kv[1][0]
    ):
        if role in assignments or j in used_cols:
            continue
        if total < 0.25:
            continue
        label = headers[j] if headers[j] else f"col {j + 1}"
        confidence = min(total / 1.15, 0.99)
        assignments[role] = ColumnAssignment(
            role=role, source_col=label, col_index=j,
            header_score=hs, content_score=cs,
            total_score=total, method=method,
            confidence=confidence,
        )
        used_cols.add(j)

    unmapped = [h for j, h in enumerate(headers) if j not in used_cols and h]
    return MappingResult(assignments=assignments, unmapped_cols=unmapped,
                         warnings=warnings)


# ── Private helpers ───────────────────────────────────────────────────────

def _norm_header(s: Any) -> str:
    s = str(s) if s is not None else ""
    s = s.replace("\n", " ").replace("_", " ").replace("-", " ")
    s = re.sub(r"[^\w&%().' ]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower().rstrip(".").strip()
    return s


def _is_ignorable(header: str) -> bool:
    t = _norm_header(header)
    return any(pat.search(t) for pat in _IGNORE_RES)


def _header_score(header: str, role: str) -> float:
    t = _norm_header(header)
    if not t:
        return 0.0
    best = 0.0
    for pat, w in SYNONYMS.get(role, []):
        pat_n = _norm_header(pat)
        if t == pat_n:
            best = max(best, w)
        elif re.search(rf"(^|\W){re.escape(pat_n)}(\W|$)", t) and len(pat_n) >= 3:
            best = max(best, w * 0.82)
        elif pat_n in t and len(pat_n) >= 4:
            best = max(best, w * 0.65)

    # Fuzzy fallback (RapidFuzz)
    if best < 0.6:
        try:
            from rapidfuzz import fuzz
            for pat, w in SYNONYMS.get(role, []):
                ratio = fuzz.token_sort_ratio(t, _norm_header(pat)) / 100.0
                if ratio >= 0.82:
                    best = max(best, w * ratio * 0.9)
        except ImportError:
            pass

    return best


def _content_profile(series: pd.Series) -> dict[str, float]:
    vals = [v for v in series.tolist() if _notnull(v)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "date": 0.0, "num": 0.0, "loc": 0.0, "seg": 0.0,
                "text": 0.0, "int_like": 0.0, "mean_abs": 0.0, "uniq_frac": 0.0}
    sample = vals if n <= 300 else vals[:: max(1, n // 300)]
    m = len(sample)
    dates  = sum(1 for v in sample if _is_date_like(v))
    nums_list = [_to_num(v) for v in sample]
    nums   = [x for x in nums_list if x is not None]
    locs   = sum(1 for v in sample if _to_location(v) is not None)
    segs   = sum(1 for v in sample if _to_segment(v) is not None)
    texts  = sum(1 for v in sample
                 if _to_num(v) is None and not _is_date_like(v) and str(v).strip())
    int_like = sum(1 for x in nums if abs(x - round(x)) < 1e-9) if nums else 0
    uniq   = len({str(v).strip().lower() for v in sample})
    return {
        "n": n,
        "date":      dates / m,
        "num":       len(nums) / m,
        "loc":       locs / m,
        "seg":       segs / m,
        "text":      texts / m,
        "int_like":  int_like / len(nums) if nums else 0.0,
        "mean_abs":  float(np.mean([abs(x) for x in nums])) if nums else 0.0,
        "uniq_frac": uniq / m,
    }


def _content_score(p: dict, role: str) -> float:
    if p["n"] == 0:
        return 0.0
    if role == ROLE_DATE:
        return p["date"]
    if role == ROLE_LOCATION:
        return p["loc"]
    if role == ROLE_SEGMENT:
        return p["seg"]
    if role == ROLE_OUTLET:
        if p["text"] >= 0.6 and p["loc"] < 0.4 and p["seg"] < 0.4:
            return 0.38 + 0.25 * min(p["uniq_frac"] * 2, 1.0)
        return 0.0
    if role in (ROLE_PAX, ROLE_REVENUE, ROLE_AOP, ROLE_TRAFFIC):
        if p["num"] >= 0.7 and p["date"] < 0.3:
            base = 0.35
            if role == ROLE_PAX and p["int_like"] >= 0.9:
                base += 0.15
            if role == ROLE_REVENUE and p["int_like"] < 0.95:
                base += 0.05
            return base
        return 0.0
    if role == ROLE_BU:
        return 0.28 if p["seg"] >= 0.4 else 0.0
    return 0.0


def _apply_magnitude_tiebreak(
    profiles: dict[int, dict],
    scores: dict[tuple[int, str], tuple],
    headers: list[str],
) -> None:
    """
    Among unlabelled numeric columns, the largest-mean column likely holds
    revenue (large amounts), the smallest integer-ish one likely holds PAX.
    Add a small bonus to break ties.
    """
    numeric_cols = [j for j, p in profiles.items()
                    if p["num"] >= 0.7 and p["date"] < 0.3 and p["n"] > 0]
    if len(numeric_cols) < 2:
        return
    ordered = sorted(numeric_cols, key=lambda j: profiles[j]["mean_abs"])
    small_j, large_j = ordered[0], ordered[-1]
    for j, bonus_role in ((small_j, ROLE_PAX), (large_j, ROLE_REVENUE)):
        key = (j, bonus_role)
        if key in scores:
            total, hs, cs, method = scores[key]
            scores[key] = (total + 0.12, hs, cs, method)


def _notnull(v: Any) -> bool:
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    return str(v).strip() != ""


def _is_date_like(v: Any) -> bool:
    if isinstance(v, (dt.datetime, dt.date)):
        return True
    s = str(v).strip()
    if len(s) < 5 or len(s) > 40:
        return False
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="raise")
        return 2000 <= ts.year <= 2050
    except Exception:
        return False


def _to_num(v: Any) -> Optional[float]:
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return None if np.isnan(f) else f
    s = str(v).strip()
    s = re.sub(r"[₹$£€,\s]", "", s)
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _to_location(v: Any) -> Optional[str]:
    t = str(v).strip().lower()
    if t in _LOCATION_VOCAB:
        return _LOCATION_VOCAB[t]
    for key, canon in _LOCATION_VOCAB.items():
        if len(key) > 3 and key in t:
            return canon
    return None


def _to_segment(v: Any) -> Optional[str]:
    t = str(v).strip().lower()
    if t in _SEGMENT_VOCAB:
        return _SEGMENT_VOCAB[t]
    for key, canon in _SEGMENT_VOCAB.items():
        if len(key) > 4 and key in t:
            return canon
    return None
