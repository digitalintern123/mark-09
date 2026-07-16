"""
universal_parser.py — Universal document processing with automatic schema detection.

This module is the "last line of defense" parser: when a file doesn't match
any of the app's predefined layouts (the Encalm PDF format, the long-format
historical workbook, the wide pivot export, the AOP layouts, the traffic
formats), this parser tries to understand it anyway — with NO manual mapping
and NO code changes per new format.

How it works, end to end:

  1. EXTRACTION — turn any supported file into one or more raw cell grids:
       * Excel (.xlsx/.xls/.xlsm): every sheet, read with header=None so
         title rows / merged cells / multi-row headers survive intact.
       * CSV / TSV / TXT: delimiter sniffed automatically.
       * PDF: pdfplumber table extraction on every page; tables with the
         same column count across pages are stitched together (a common
         "table continues on next page" pattern).

  2. TABLE DETECTION — inside each grid, find the real header row by
     scoring every candidate row on "header-likeness": mostly-text cells,
     matches against a large synonym bank, low numeric content. Rows above
     the header are kept as *title context* (they often carry the report
     date, the location, or a units note like "Rs. in Lakhs"). Multi-row
     headers (e.g. a metric row above an entity row) are merged.

  3. SCHEMA INFERENCE — every column gets a role from
        {date, location, segment, outlet, business_unit,
         pax, revenue, aop, traffic, ignore}
     scored on BOTH:
       * header evidence — fuzzy match against a synonym bank
         ("Txn Dt" -> date, "Guests" -> pax, "Sales (INR)" -> revenue,
          "Airport" -> location, "Business Line" -> segment, ...)
       * content evidence — what the values actually look like:
         a column whose cells parse as dates is a date column whatever its
         header says; a column whose values are Delhi/Hyd/Goa is location;
         values matching Lounges/Atithya/EHPL/... are segment; numeric
         columns are disambiguated pax-vs-revenue by magnitude and
         integer-ness when headers don't settle it.
     Each role is assigned to at most one column, greedily by score.

  4. LAYOUT TRANSFORMS — wide layouts are melted to long form:
       * date-wide: many columns whose headers parse as dates
       * location-wide: columns whose headers are location names
     including two-row (metric x entity) headers.

  5. RECOVERY — required fields that aren't a column are recovered from
     context where safely possible:
       * date: from title rows or the file name ("Report 05-07-2026.xlsx")
       * location: from title rows, the sheet name, or a sparse section-
         label column that gets forward-filled
       * segment: from outlet-name keywords (…lounge… -> Lounges,
         …sky plate… -> Subsidiary/Sky Plates, …eats… -> Subsidiary, …)
       * outlet: synthesized as "<Location> - <Segment>" as a last resort
     Every recovery is reported, never silent.

  6. NORMALIZATION — ₹ / Rs. / INR prefixes, thousands separators,
     parenthesised negatives, unit multipliers declared in titles
     ("in Lakhs", "₹ '000"), Indian day-first dates, Excel serial dates,
     and location aliases (DEL/IGI/New Delhi -> Delhi, HYD/RGIA ->
     Hyderabad, GOI/GOX/Mopa/Dabolim -> Goa).

  7. VALIDATION + CONFIDENCE — the mapping and the data are both checked.
     If overall confidence is below threshold, the parse FAILS LOUDLY with
     an explanation of what was and wasn't identified — bad data is never
     quietly imported. On success, a human-readable schema report explains
     exactly how every field was obtained so the person can verify it.

The output DataFrame has exactly the app's canonical revenue schema
(date, segment, outlet, location, pax, revenue [, aop, traffic]) and is
handed to the existing pipeline (data_processor._validate_and_clean ->
database.save_dataframe), so everything downstream — canonical
segment/business-unit mapping, dedupe, analytics — works unchanged.
"""

from __future__ import annotations

import csv as _csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd


class UniversalParseError(Exception):
    """Raised when a file can't be confidently understood by the universal parser."""


# --------------------------------------------------------------------------
# Vocabulary: header synonyms and value vocabularies
# --------------------------------------------------------------------------

# Header synonym bank. Keys are canonical roles; values are lists of
# (pattern, weight) where pattern is matched against a normalized header
# label. Exact matches score the full weight; substring matches score
# a bit less (handled in _header_score).
HEADER_SYNONYMS: dict[str, list[tuple[str, float]]] = {
    "date": [
        ("date", 1.0), ("report date", 1.0), ("txn date", 1.0), ("txn dt", 1.0),
        ("transaction date", 1.0), ("business date", 1.0), ("bill date", 1.0),
        ("invoice date", 0.9), ("day", 0.7), ("dt", 0.6), ("period", 0.5),
        ("as on", 0.5), ("month", 0.4),
    ],
    "location": [
        ("location", 1.0), ("city", 1.0), ("airport", 1.0), ("station", 0.9),
        ("branch", 0.8), ("site", 0.8), ("region", 0.7), ("geography", 0.7),
        ("geographical segment", 0.9), ("airport code", 0.9), ("loc", 0.7),
        ("place", 0.6),
    ],
    "segment": [
        ("segment", 1.0), ("business segment", 1.0), ("business line", 1.0),
        ("business", 0.9), ("line of business", 1.0), ("lob", 0.9),
        ("division", 0.8), ("category", 0.7), ("vertical", 0.8),
        ("business vertical", 0.9), ("dept", 0.5), ("department", 0.6),
    ],
    "outlet": [
        ("outlet", 1.0), ("outlet name", 1.0), ("unit", 0.8), ("unit name", 0.9),
        ("unit id", 0.8), ("sub business", 0.9), ("sub-business", 0.9),
        ("service", 0.7), ("service point", 0.8), ("store", 0.8),
        ("shop", 0.7), ("counter", 0.7), ("facility", 0.7), ("property", 0.6),
        ("cost center", 0.6), ("profit center", 0.6), ("lounge name", 0.9),
    ],
    "business_unit": [
        ("business unit", 1.0), ("bu", 0.7), ("sub segment", 0.9),
        ("sub-segment", 0.9),
    ],
    "pax": [
        ("pax", 1.0), ("passengers", 1.0), ("passenger count", 1.0),
        ("guests", 1.0), ("guest count", 1.0), ("footfall", 1.0),
        ("covers", 0.9), ("visitors", 0.9), ("headcount", 0.8),
        ("no of pax", 1.0), ("no. of pax", 1.0), ("pax count", 1.0),
        ("qty", 0.5), ("quantity", 0.5), ("count", 0.4), ("nos", 0.4),
        ("users", 0.6), ("customers", 0.7),
    ],
    "revenue": [
        ("revenue", 1.0), ("total revenue", 1.0), ("net revenue", 1.0),
        ("gross revenue", 1.0), ("rev", 0.9), ("sales", 0.9),
        ("net sales", 0.95), ("gross sales", 0.95), ("total sales", 0.95),
        ("amount", 0.8), ("total amount", 0.85), ("net amount", 0.85),
        ("value", 0.6), ("income", 0.8), ("turnover", 0.9),
        ("collection", 0.8), ("billing", 0.75), ("earned", 0.6),
        ("inr", 0.6), ("rs", 0.5),
    ],
    "aop": [
        ("aop", 1.0), ("budget", 1.0), ("target", 0.95), ("aop target", 1.0),
        ("plan", 0.7), ("budgeted revenue", 1.0), ("target revenue", 1.0),
        ("annual operating plan", 1.0),
    ],
    "traffic": [
        ("traffic", 1.0), ("airport traffic", 1.0), ("total traffic", 1.0),
        ("atm", 0.5), ("throughput", 0.7), ("passenger traffic", 0.95),
    ],
}

# Words that, if present in a header, make it very likely IGNORABLE
IGNORE_HEADER_HINTS = [
    "s no", "s.no", "sno", "sl no", "sr no", "serial", "remarks", "notes",
    "comment", "%", "percent", "growth", "variance", "var ", "achievement",
    "ytd", "mtd", "cumulative", "avg", "average", "rate", "spp",
    "penetration",
]

# Value vocabulary for locations, incl. airport codes and common aliases.
LOCATION_VOCAB: dict[str, str] = {
    "delhi": "Delhi", "new delhi": "Delhi", "del": "Delhi", "igi": "Delhi",
    "igia": "Delhi", "indira gandhi": "Delhi", "delhi airport": "Delhi",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad", "rgia": "Hyderabad",
    "rajiv gandhi": "Hyderabad", "shamshabad": "Hyderabad",
    "hyderabad airport": "Hyderabad",
    "goa": "Goa", "goi": "Goa", "gox": "Goa", "mopa": "Goa",
    "dabolim": "Goa", "manohar": "Goa", "goa airport": "Goa",
    "north goa": "Goa", "south goa": "Goa",
}

# Value vocabulary for segments (legacy and canonical labels).
SEGMENT_VOCAB: dict[str, str] = {
    "lounge": "Lounges", "lounges": "Lounges",
    "atithya": "Atithya", "meet and greet": "Atithya", "meet & greet": "Atithya",
    "others": "Others", "other": "Others",
    "subsidiary": "Subsidiary", "subsidiaries": "Subsidiary",
    "ehpl": "EHPL",
    "sky plates": "Sky Plates", "encalm sky plates": "Sky Plates",
    "skyplates": "Sky Plates",
    "encalm eats": "Encalm Eats", "eats": "Encalm Eats",
    "spa": "Others", "encalm spa": "Others",
    "f&b": "Others", "food and beverage": "Others",
}

# Outlet-name keywords -> segment, used only for RECOVERY when the file
# has no segment column at all. Ordered: first match wins.
OUTLET_KEYWORD_TO_SEGMENT: list[tuple[str, str]] = [
    ("sky plate", "Subsidiary"),
    ("skyplate", "Subsidiary"),
    ("eats", "Subsidiary"),
    ("lounge", "Lounges"),
    ("atithya", "Atithya"),
    ("meet & greet", "Atithya"),
    ("meet and greet", "Atithya"),
    ("spa", "Others"),
]

# Words marking subtotal/summary rows we must never import as data.
TOTAL_ROW_MARKERS = [
    "total", "grand total", "sub total", "subtotal", "overall", "sum",
    "net total", "gross total", "all locations", "all outlets", "combined",
]

# Unit multipliers that titles may declare.
UNIT_MULTIPLIER_PATTERNS: list[tuple[str, float]] = [
    (r"in\s+crores?|rs\.?\s*crores?|₹\s*crores?|\bcr\b", 1e7),
    (r"in\s+lakhs?|in\s+lacs?|rs\.?\s*lakhs?|rs\.?\s*lacs?|₹\s*lakhs?", 1e5),
    (r"in\s*'?000s?|in\s+thousands?|₹\s*'000|rs\.?\s*'000", 1e3),
    (r"in\s+millions?|\bmn\b", 1e6),
]

REQUIRED_ROLES = ["date", "location", "segment", "outlet", "revenue"]
ALL_ROLES = ["date", "location", "segment", "outlet", "business_unit",
             "pax", "revenue", "aop", "traffic"]

# Minimum overall confidence to accept a parse (0..1).
CONFIDENCE_THRESHOLD = 0.55


# --------------------------------------------------------------------------
# Result / report dataclasses
# --------------------------------------------------------------------------

@dataclass
class FieldMapping:
    """How one canonical field was obtained."""
    role: str
    source: str            # e.g. "column 'Txn Dt'", "title row", "file name", "recovered from outlet keywords"
    confidence: float      # 0..1
    method: str            # "header", "content", "header+content", "recovered", "constant"


@dataclass
class UniversalParseResult:
    df: pd.DataFrame
    confidence: float
    mappings: list[FieldMapping]
    warnings: list[str] = field(default_factory=list)
    source_description: str = ""   # e.g. "sheet 'Sheet1'", "PDF pages 1-3"
    rows_dropped: int = 0
    unit_multiplier: float = 1.0

    def report_lines(self) -> list[str]:
        """Human-readable schema report for the upload-status UI."""
        lines = [
            f"Auto-detected schema ({self.source_description}) — overall "
            f"confidence {self.confidence:.0%}:"
        ]
        for m in self.mappings:
            lines.append(f"  • {m.role} ← {m.source} ({m.method}, {m.confidence:.0%})")
        if self.unit_multiplier != 1.0:
            lines.append(f"  • values scaled ×{self.unit_multiplier:,.0f} (declared in the file's title)")
        if self.rows_dropped:
            lines.append(f"  • {self.rows_dropped} subtotal/blank/invalid row(s) excluded")
        return lines


# --------------------------------------------------------------------------
# Small text/number/date helpers
# --------------------------------------------------------------------------

def _norm(s: Any) -> str:
    """Normalize a label for matching: lowercase, collapse whitespace, strip punctuation edges."""
    s = "" if s is None else str(s)
    s = s.replace("\n", " ").replace("_", " ").replace("-", " ")
    s = re.sub(r"[^\w&%().' ]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.rstrip(".").strip()


_CURRENCY_RE = re.compile(r"(₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUM_SUFFIXES = [
    (re.compile(r"\bcr(ores?)?\b\.?$", re.IGNORECASE), 1e7),
    (re.compile(r"\bl(akhs?|acs?)?\b\.?$", re.IGNORECASE), 1e5),
    (re.compile(r"\bk\b$", re.IGNORECASE), 1e3),
    (re.compile(r"\bmn?\b$", re.IGNORECASE), 1e6),
]


def _to_number(value: Any) -> Optional[float]:
    """
    Parse messy real-world numbers: '₹ 1,23,456.78', 'Rs. 5000', '(2,500)',
    '1.2 L', '3 Cr', '  -  ', ''. Returns None if not a number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        return None if np.isnan(f) else f
    s = str(value).strip()
    if not s or s in {"-", "–", "—", "na", "n/a", "nil", "none"}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CURRENCY_RE.sub("", s)
    mult = 1.0
    for pat, m in _NUM_SUFFIXES:
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


def _to_date(value: Any) -> Optional[dt.date]:
    """Parse a single value as a date: datetimes, Excel serials, many string formats (day-first friendly)."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        # Plausible Excel serial date range: 2000-01-01 .. 2050-ish
        if 36526 <= f <= 55153:
            try:
                return (dt.datetime(1899, 12, 30) + dt.timedelta(days=f)).date()
            except Exception:
                return None
        return None
    s = str(value).strip()
    if not s or len(s) < 5 or len(s) > 40:
        return None
    # Refuse pure numbers as strings unless Excel-serial-plausible
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return _to_date(float(s))
    for dayfirst in (True, False):
        try:
            ts = pd.to_datetime(s, dayfirst=dayfirst, errors="raise")
            if isinstance(ts, pd.Timestamp) and 2000 <= ts.year <= 2050:
                return ts.date()
        except Exception:
            continue
    return None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and np.isnan(value):
        return True
    return str(value).strip() == ""


def _looks_like_total_row(cells: list[Any]) -> bool:
    for c in cells[:4]:  # markers live in the leading label cells
        t = _norm(c)
        if t and any(t == m or t.startswith(m + " ") or t.endswith(" " + m) for m in TOTAL_ROW_MARKERS):
            return True
    return False


def _match_location(value: Any) -> Optional[str]:
    t = _norm(value)
    if not t:
        return None
    if t in LOCATION_VOCAB:
        return LOCATION_VOCAB[t]
    # tolerant: "delhi t3", "hyderabad rgia" etc.
    for key, canon in LOCATION_VOCAB.items():
        if len(key) > 3 and key in t:
            return canon
    return None


def _match_segment(value: Any) -> Optional[str]:
    t = _norm(value)
    if not t:
        return None
    if t in SEGMENT_VOCAB:
        return SEGMENT_VOCAB[t]
    for key, canon in SEGMENT_VOCAB.items():
        if len(key) > 4 and key in t:
            return canon
    return None


# --------------------------------------------------------------------------
# 1. EXTRACTION — file -> list of (grid, source_description, sheet_name)
# --------------------------------------------------------------------------

def _extract_grids(file_obj, file_name: str) -> list[tuple[pd.DataFrame, str, str]]:
    """
    Return raw cell grids (header=None DataFrames of objects) from the file.
    Each entry: (grid, source_description, context_name) where context_name
    is the sheet name (Excel) or "" — used for location/date recovery.
    """
    lower = file_name.lower()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        try:
            xl = pd.ExcelFile(file_obj, engine="openpyxl")
        except Exception as exc:
            raise UniversalParseError(f"Could not open the Excel file: {exc}") from exc
        grids = []
        for sheet in xl.sheet_names:
            try:
                grid = xl.parse(sheet, header=None, dtype=object)
            except Exception:
                continue
            if grid.dropna(how="all").empty:
                continue
            grids.append((grid, f"sheet '{sheet}'", sheet))
        if not grids:
            raise UniversalParseError("The workbook has no non-empty sheets.")
        return grids

    if lower.endswith((".csv", ".tsv", ".txt")):
        raw_bytes = file_obj.read()
        if isinstance(raw_bytes, str):
            text = raw_bytes
        else:
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    text = raw_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise UniversalParseError("Could not decode the text file (unknown encoding).")
        sample = text[:8192]
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
            sep = dialect.delimiter
        except Exception:
            sep = "\t" if lower.endswith(".tsv") else ","
        try:
            grid = pd.read_csv(io.StringIO(text), header=None, dtype=object,
                               sep=sep, engine="python", skip_blank_lines=False)
        except Exception as exc:
            raise UniversalParseError(f"Could not read the delimited text file: {exc}") from exc
        if grid.dropna(how="all").empty:
            raise UniversalParseError("The file is empty.")
        return [(grid, "delimited text", "")]

    if lower.endswith(".pdf"):
        return _extract_pdf_grids(file_obj)

    raise UniversalParseError(
        f"Unsupported file type for '{file_name}'. Supported: Excel, CSV/TSV/TXT, PDF."
    )


def _extract_pdf_grids(file_obj) -> list[tuple[pd.DataFrame, str, str]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise UniversalParseError("pdfplumber is not installed; cannot read PDFs.") from exc

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    tables_by_width: dict[int, list[list[list[Any]]]] = {}
    pages_seen: dict[int, list[int]] = {}
    try:
        with pdfplumber.open(file_obj) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                if not tables:
                    # Many real-world reports draw no ruling lines at all —
                    # retry with pdfplumber's text-alignment strategy, which
                    # infers columns from the positions of the words.
                    try:
                        tables = page.extract_tables(
                            {"vertical_strategy": "text", "horizontal_strategy": "text"}
                        ) or []
                    except Exception:
                        tables = []
                for table in tables:
                    if not table:
                        continue
                    width = max(len(r) for r in table)
                    if width < 2:
                        continue
                    norm_rows = [list(r) + [None] * (width - len(r)) for r in table]
                    tables_by_width.setdefault(width, []).append(norm_rows)
                    pages_seen.setdefault(width, []).append(page_no)
    except Exception as exc:
        raise UniversalParseError(f"Could not read tables from the PDF: {exc}") from exc

    if not tables_by_width:
        raise UniversalParseError(
            "No tables could be detected in this PDF. If the PDF is a scanned "
            "image, it would need OCR before it can be imported."
        )

    grids = []
    for width, tables in tables_by_width.items():
        rows: list[list[Any]] = []
        for t in tables:
            rows.extend(t)
        grid = pd.DataFrame(rows, dtype=object)
        pages = sorted(set(pages_seen[width]))
        desc = f"PDF table(s) on page(s) {', '.join(map(str, pages))}"
        grids.append((grid, desc, ""))
    # Widest table first — usually the real data table.
    grids.sort(key=lambda g: (-g[0].shape[1], -g[0].shape[0]))
    return grids


# --------------------------------------------------------------------------
# 2. TABLE DETECTION — find header row(s) + data slice + title context
# --------------------------------------------------------------------------

def _header_row_score(cells: list[Any]) -> float:
    """Score how much a row looks like a header row."""
    non_blank = [c for c in cells if not _is_blank(c)]
    if len(non_blank) < 2:
        return 0.0
    text_cells = 0
    vocab_hits = 0.0
    date_like = 0
    for c in non_blank:
        if _to_number(c) is not None:
            continue
        if _to_date(c) is not None:
            date_like += 1
            continue
        text_cells += 1
        t = _norm(c)
        best = 0.0
        for syns in HEADER_SYNONYMS.values():
            for pat, w in syns:
                if t == pat:
                    best = max(best, w)
                elif pat in t and len(pat) >= 3:
                    best = max(best, w * 0.7)
        vocab_hits += best
    # date-like header cells indicate a date-wide layout header — still a header!
    text_frac = (text_cells + date_like) / len(non_blank)
    return text_frac * 0.4 + min(vocab_hits / 3.0, 1.0) * 0.6 + (0.1 if date_like >= 3 else 0.0)


def _find_table(grid: pd.DataFrame) -> tuple[int, list[str], pd.DataFrame, list[str]]:
    """
    Locate the header row in a raw grid.
    Returns (header_row_idx, merged_header_labels, data_frame, title_lines).
    Raises UniversalParseError if nothing header-like is found.
    """
    grid = grid.reset_index(drop=True)
    n_scan = min(len(grid), 40)
    best_idx, best_score = None, 0.0
    for i in range(n_scan):
        score = _header_row_score(list(grid.iloc[i]))
        if score > best_score:
            best_idx, best_score = i, score
    if best_idx is None or best_score < 0.35:
        raise UniversalParseError(
            "Could not find a header row — no row in the file looks like "
            "column headers (Date / Location / Outlet / Revenue / ...)."
        )

    header_rows = [best_idx]
    # Multi-row header: check the row ABOVE — if it adds vocab or spans
    # (metric row over entity row), merge it in.
    if best_idx > 0:
        above = list(grid.iloc[best_idx - 1])
        above_score = _header_row_score(above)
        above_nonblank = sum(0 if _is_blank(c) else 1 for c in above)
        if above_score >= 0.3 and above_nonblank >= 1:
            # Only treat as part of the header if it's not a plain title line
            # (title lines have 1-2 filled cells; header lines have several
            # OR contain synonym words like pax/revenue)
            joined = " ".join(_norm(c) for c in above if not _is_blank(c))
            has_metric_word = any(
                pat in joined for pat, _w in
                HEADER_SYNONYMS["pax"] + HEADER_SYNONYMS["revenue"] + HEADER_SYNONYMS["aop"]
            )
            if above_nonblank >= 3 or has_metric_word:
                header_rows.insert(0, best_idx - 1)

    # Merge (forward-filling the upper row across merged-cell gaps).
    n_cols = grid.shape[1]
    merged: list[str] = []
    upper_ffill: list[str] = [""] * n_cols
    if len(header_rows) == 2:
        last = ""
        for j in range(n_cols):
            v = grid.iat[header_rows[0], j]
            if not _is_blank(v):
                last = str(v).strip()
            upper_ffill[j] = last
    for j in range(n_cols):
        low = grid.iat[header_rows[-1], j]
        low_s = "" if _is_blank(low) else str(low).strip()
        up_s = upper_ffill[j] if len(header_rows) == 2 else ""
        label = f"{up_s} {low_s}".strip() if up_s and up_s != low_s else (low_s or up_s)
        merged.append(label)

    data = grid.iloc[header_rows[-1] + 1:].reset_index(drop=True)
    # Trim trailing all-blank region
    keep_mask = ~data.apply(lambda r: all(_is_blank(v) for v in r), axis=1)
    if keep_mask.any():
        last_keep = keep_mask[keep_mask].index.max()
        data = data.iloc[: last_keep + 1]

    title_lines = []
    for i in range(header_rows[0]):
        cells = [str(c).strip() for c in grid.iloc[i] if not _is_blank(c)]
        if cells:
            title_lines.append(" ".join(cells))
    return header_rows[-1], merged, data, title_lines


# --------------------------------------------------------------------------
# 3. SCHEMA INFERENCE — assign roles to columns
# --------------------------------------------------------------------------

def _header_score(label: str, role: str) -> float:
    t = _norm(label)
    if not t:
        return 0.0
    for hint in IGNORE_HEADER_HINTS:
        if hint in t and role not in ("pax",):  # "count" etc. handled by synonyms
            # percent/growth/serial headers are poison for every role
            if hint in ("%", "percent", "growth", "variance", "serial",
                        "s no", "s.no", "sno", "sl no", "sr no"):
                return -0.5
    best = 0.0
    for pat, w in HEADER_SYNONYMS.get(role, []):
        if t == pat:
            best = max(best, w)
        elif re.search(rf"(^|\W){re.escape(pat)}(\W|$)", t) and len(pat) >= 3:
            best = max(best, w * 0.8)
        elif pat in t and len(pat) >= 4:
            best = max(best, w * 0.6)
    return best


def _content_profile(series: pd.Series) -> dict[str, float]:
    """Fractions describing what a column's values look like."""
    vals = [v for v in series.tolist() if not _is_blank(v)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "date": 0, "num": 0, "loc": 0, "seg": 0, "text": 0,
                "int_like": 0, "mean_abs": 0.0, "uniq": 0}
    sample = vals if n <= 200 else vals[:: max(1, n // 200)]
    m = len(sample)
    dates = sum(1 for v in sample if _to_date(v) is not None)
    nums_list = [(_to_number(v)) for v in sample]
    nums = [x for x in nums_list if x is not None]
    locs = sum(1 for v in sample if _match_location(v) is not None)
    segs = sum(1 for v in sample if _match_segment(v) is not None)
    texts = sum(
        1 for v in sample
        if _to_number(v) is None and _to_date(v) is None and str(v).strip()
    )
    int_like = sum(1 for x in nums if abs(x - round(x)) < 1e-9)
    uniq = len({_norm(v) for v in sample})
    return {
        "n": n,
        "date": dates / m,
        "num": len(nums) / m,
        "loc": locs / m,
        "seg": segs / m,
        "text": texts / m,
        "int_like": (int_like / len(nums)) if nums else 0.0,
        "mean_abs": float(np.mean([abs(x) for x in nums])) if nums else 0.0,
        "uniq": uniq / m,
    }


def _infer_roles(
    headers: list[str], data: pd.DataFrame
) -> tuple[dict[str, int], dict[str, FieldMapping], dict[int, dict[str, float]]]:
    """
    Score every (column, role) pair and greedily assign each role to its
    best column. Returns (role -> column index, role -> FieldMapping,
    col -> profile).
    """
    n_cols = data.shape[1]
    profiles = {j: _content_profile(data.iloc[:, j]) for j in range(n_cols)}

    scores: dict[tuple[int, str], tuple[float, str]] = {}
    numeric_cols = [j for j in range(n_cols)
                    if profiles[j]["num"] >= 0.7 and profiles[j]["date"] < 0.5
                    and profiles[j]["n"] > 0]
    mean_by_col = {j: profiles[j]["mean_abs"] for j in numeric_cols}

    for j in range(n_cols):
        p = profiles[j]
        if p["n"] == 0:
            continue
        h = headers[j] if j < len(headers) else ""
        for role in ALL_ROLES:
            hs = _header_score(h, role)
            cs = 0.0
            if role == "date":
                cs = p["date"]
            elif role == "location":
                cs = p["loc"]
            elif role == "segment":
                cs = p["seg"]
            elif role == "outlet":
                # text column, not dominated by location/segment vocab
                if p["text"] >= 0.6 and p["loc"] < 0.5 and p["seg"] < 0.5:
                    cs = 0.4 + 0.3 * min(p["uniq"] * 2, 1.0)
            elif role in ("pax", "revenue", "aop", "traffic"):
                if p["num"] >= 0.7 and p["date"] < 0.3:
                    cs = 0.35
                    if role == "pax" and p["int_like"] >= 0.9:
                        cs += 0.15
                    if role == "revenue" and p["int_like"] < 0.9:
                        cs += 0.05
            elif role == "business_unit":
                if p["seg"] >= 0.5:
                    cs = 0.3
            if hs <= 0 and cs <= 0:
                continue
            method = ("header+content" if hs > 0 and cs > 0
                      else "header" if hs > 0 else "content")
            total = min(hs, 1.0) * 0.6 + min(cs, 1.0) * 0.55
            scores[(j, role)] = (total, method)

    # Magnitude tie-break for unlabeled numeric columns: among numeric
    # columns with weak headers, the largest-mean one leans revenue, the
    # smallest integer-ish one leans pax.
    if len(numeric_cols) >= 2:
        ordered = sorted(numeric_cols, key=lambda j: mean_by_col[j])
        small, large = ordered[0], ordered[-1]
        for j, bonus_role in ((small, "pax"), (large, "revenue")):
            key = (j, bonus_role)
            if key in scores:
                s, m = scores[key]
                scores[key] = (s + 0.12, m)

    assignments: dict[str, int] = {}
    mappings: dict[str, FieldMapping] = {}
    used_cols: set[int] = set()
    # Greedy: highest score first
    for (j, role), (s, method) in sorted(scores.items(), key=lambda kv: -kv[1][0]):
        if role in assignments or j in used_cols:
            continue
        if s < 0.30:
            continue
        assignments[role] = j
        used_cols.add(j)
        label = headers[j] if j < len(headers) and headers[j] else f"column {j + 1}"
        mappings[role] = FieldMapping(
            role=role, source=f"column '{label}'",
            confidence=min(s, 0.99), method=method,
        )
    return assignments, mappings, profiles


# --------------------------------------------------------------------------
# 4. LAYOUT TRANSFORMS — wide -> long
# --------------------------------------------------------------------------

def _detect_wide_layout(headers: list[str], data: pd.DataFrame) -> Optional[str]:
    """Return 'date_wide', 'location_wide', or None."""
    date_cols = sum(1 for h in headers if _to_date(h) is not None)
    loc_cols = sum(1 for h in headers if _match_location(h) is not None)
    if date_cols >= 3:
        return "date_wide"
    if loc_cols >= 2:
        return "location_wide"
    return None


def _melt_wide(
    headers: list[str], data: pd.DataFrame, kind: str,
    warnings: list[str],
) -> tuple[list[str], pd.DataFrame]:
    """
    Melt a wide layout into long form. The melted value columns become a
    'revenue' column by default (most one-metric wide exports are revenue);
    if the merged two-row header carries PAX/Revenue metric words, both are
    produced.
    """
    if kind == "date_wide":
        keymatch = _to_date
        new_col = "date"
    else:
        keymatch = _match_location
        new_col = "location"

    id_idx, val_specs = [], []  # val_specs: (col_idx, key_value, metric)
    for j, h in enumerate(headers):
        key = keymatch(h)
        if key is None:
            # Two-row merged headers look like "PAX 01-Apr-25" / "Revenue Delhi"
            t = _norm(h)
            metric = None
            for role in ("pax", "revenue", "aop", "traffic"):
                for pat, w in HEADER_SYNONYMS[role]:
                    if w >= 0.9 and re.search(rf"(^|\W){re.escape(pat)}(\W|$)", t):
                        metric = role
                        break
                if metric:
                    break
            if metric:
                # strip the metric word and retry the key
                stripped = re.sub(r"|".join(
                    re.escape(p) for p, w in HEADER_SYNONYMS[metric] if w >= 0.9
                ), "", t).strip()
                key2 = keymatch(stripped)
                if key2 is not None:
                    val_specs.append((j, key2, metric))
                    continue
            id_idx.append(j)
        else:
            t = _norm(h)
            metric = "revenue"
            for role in ("pax", "traffic", "aop"):
                for pat, w in HEADER_SYNONYMS[role]:
                    if w >= 0.9 and re.search(rf"(^|\W){re.escape(pat)}(\W|$)", t):
                        metric = role
                        break
            val_specs.append((j, key, metric))

    if not val_specs:
        return headers, data

    metrics_present = {m for _j, _k, m in val_specs}
    if metrics_present == {"revenue"}:
        warnings.append(
            f"Wide layout detected ({'dates' if kind == 'date_wide' else 'locations'} "
            f"as columns) — melted to long form; value columns interpreted as Revenue."
        )
    else:
        warnings.append(
            f"Wide layout detected ({'dates' if kind == 'date_wide' else 'locations'} "
            f"as columns) — melted to long form with metrics: "
            f"{', '.join(sorted(metrics_present))}."
        )

    long_rows = []
    for _idx, row in data.iterrows():
        base = {headers[j] if j < len(headers) and headers[j] else f"col{j}": row.iloc[j]
                for j in id_idx}
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


# --------------------------------------------------------------------------
# 5. RECOVERY — fill required roles from context
# --------------------------------------------------------------------------

_DATE_IN_TEXT_RE = re.compile(
    r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}"           # 05/07/2026, 5-7-26
    r"|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"             # 2026-07-05
    r"|\d{1,2}(st|nd|rd|th)?[\s.\-]+[A-Za-z]{3,9}[\s.\-,]+\d{2,4}"  # 06-Jul-2026, 6 July 2026
    r"|[A-Za-z]{3,9}[\s.\-]+\d{1,2}(st|nd|rd|th)?[\s.\-,]+\d{4})"   # July 6, 2026
)


def _recover_date_from_context(title_lines: list[str], file_name: str) -> Optional[dt.date]:
    for text in title_lines + [file_name]:
        for m in _DATE_IN_TEXT_RE.finditer(str(text)):
            d = _to_date(m.group(0))
            if d is not None:
                return d
    return None


def _recover_location_from_context(
    title_lines: list[str], sheet_name: str, file_name: str
) -> Optional[str]:
    for text in [sheet_name] + title_lines + [file_name]:
        loc = _match_location(text)
        if loc is not None:
            return loc
    return None


def _forward_fill_sparse_label_column(
    data: pd.DataFrame, matcher, min_hits: int = 1
) -> Optional[tuple[int, pd.Series]]:
    """
    Detect a "section label" column: mostly blank, but its non-blank values
    match `matcher` (e.g. locations). Returns (col_idx, forward-filled series).
    """
    for j in range(min(3, data.shape[1])):  # section labels live on the left
        col = data.iloc[:, j]
        non_blank = [v for v in col if not _is_blank(v)]
        if not non_blank or len(non_blank) > len(col) * 0.6:
            continue
        hits = sum(1 for v in non_blank if matcher(v) is not None)
        if hits >= min_hits and hits >= len(non_blank) * 0.8:
            filled = col.map(lambda v: None if _is_blank(v) else v).ffill()
            return j, filled
    return None


def _segment_from_outlet(outlet: Any) -> Optional[str]:
    t = _norm(outlet)
    if not t:
        return None
    for kw, seg in OUTLET_KEYWORD_TO_SEGMENT:
        if kw in t:
            return seg
    return None


# --------------------------------------------------------------------------
# 6/7. Assemble + validate one candidate table
# --------------------------------------------------------------------------

def _detect_unit_multiplier(title_lines: list[str], headers: list[str]) -> float:
    text = " | ".join(title_lines + headers).lower()
    for pat, mult in UNIT_MULTIPLIER_PATTERNS:
        if re.search(pat, text):
            return mult
    return 1.0


def _build_candidate(
    grid: pd.DataFrame, source_desc: str, sheet_name: str, file_name: str
) -> UniversalParseResult:
    """Try to parse ONE grid into the canonical schema. Raises UniversalParseError on failure."""
    warnings: list[str] = []
    _hidx, headers, data, title_lines = _find_table(grid)

    # Guard: an AOP/budget workbook must never be silently imported as
    # revenue ACTUALS. If the surrounding context clearly says this is a
    # budget/target file, bail out so the dedicated AOP pipeline (or the
    # AOP uploader) handles it instead.
    context = _norm(" ".join(title_lines + [sheet_name, file_name] + [h for h in headers]))
    aop_context = bool(re.search(r"\baop\b|annual operating plan|\bbudget\b", context))
    aop_layout = ("geographical segment" in context and ("unit id" in context or "business segment" in context))
    has_actuals_header = any(
        _header_score(h, "revenue") >= 0.8 or _header_score(h, "pax") >= 0.8
        for h in headers
    )
    if aop_layout or (aop_context and not has_actuals_header):
        raise UniversalParseError(
            "This file looks like an AOP / budget-target workbook, not actual "
            "revenue — please upload it through the AOP section so targets "
            "aren't imported as actuals."
        )

    wide = _detect_wide_layout(headers, data)
    if wide:
        headers, data = _melt_wide(headers, data, wide, warnings)

    # Drop subtotal rows before profiling so they don't pollute inference.
    before = len(data)
    mask_totals = data.apply(lambda r: _looks_like_total_row(list(r)), axis=1)
    data = data[~mask_totals].reset_index(drop=True)
    dropped_totals = before - len(data)

    assignments, mappings, profiles = _infer_roles(headers, data)

    # ---- RECOVERY of missing required roles -------------------------------
    n = len(data)
    out = pd.DataFrame(index=range(n))

    def col(role: str) -> Optional[pd.Series]:
        j = assignments.get(role)
        return data.iloc[:, j] if j is not None else None

    # date
    if "date" in assignments:
        out["date"] = col("date").map(_to_date)
    else:
        d = _recover_date_from_context(title_lines, file_name)
        if d is None:
            raise UniversalParseError(
                "Could not identify a Date anywhere — no column contains dates and "
                "no date was found in the file's title rows or file name."
            )
        out["date"] = d
        mappings["date"] = FieldMapping(
            "date",
            "a single date found in the file's title/name",
            0.6, "recovered",
        )
        warnings.append(f"No Date column found — applied {d.isoformat()} (from the file's title/name) to every row.")

    # location
    if "location" in assignments:
        loc_series = col("location")
        n_blank = int(loc_series.map(_is_blank).sum())
        if 0 < n_blank < len(loc_series):
            # Merged cells / section-label style: the location is written once
            # and left blank for the rows beneath it — forward-fill.
            loc_series = loc_series.map(lambda v: None if _is_blank(v) else v).ffill()
            if n_blank > len(loc_series) * 0.2:
                warnings.append(
                    f"The Location column had {n_blank} blank cell(s) (merged-cell/"
                    f"section style) — filled down from the value above."
                )
        out["location"] = loc_series.map(lambda v: _match_location(v) or (str(v).strip() if not _is_blank(v) else None))
    else:
        section = _forward_fill_sparse_label_column(data, _match_location)
        if section is not None:
            j, filled = section
            out["location"] = filled.map(lambda v: _match_location(v) or (str(v).strip() if v is not None else None))
            mappings["location"] = FieldMapping(
                "location", f"section labels in column {j + 1} (forward-filled)", 0.7, "recovered")
            warnings.append("Location was given as section headings, not a per-row column — forward-filled down the rows.")
        else:
            loc = _recover_location_from_context(title_lines, sheet_name, file_name)
            if loc is None:
                raise UniversalParseError(
                    "Could not identify a Location — no column, section label, sheet "
                    "name, or title mentions Delhi / Hyderabad / Goa (or a known alias)."
                )
            out["location"] = loc
            mappings["location"] = FieldMapping(
                "location", f"'{loc}' found in the sheet name/title/file name", 0.6, "recovered")
            warnings.append(f"No Location column found — applied '{loc}' (from the sheet/title/file name) to every row.")

    # outlet (needed before segment recovery)
    if "outlet" in assignments:
        out["outlet"] = col("outlet").map(lambda v: None if _is_blank(v) else str(v).strip())
    else:
        out["outlet"] = None  # may be synthesized below

    # segment
    if "segment" in assignments:
        seg_series = col("segment")
        if 0 < int(seg_series.map(_is_blank).sum()) < len(seg_series):
            seg_series = seg_series.map(lambda v: None if _is_blank(v) else v).ffill()
        out["segment"] = seg_series.map(lambda v: _match_segment(v) or (str(v).strip() if not _is_blank(v) else None))
    else:
        derived = out["outlet"].map(_segment_from_outlet) if out["outlet"].notna().any() else pd.Series([None] * n)
        if derived.notna().mean() >= 0.5:
            out["segment"] = derived.fillna("Others")
            mappings["segment"] = FieldMapping(
                "segment", "derived from outlet-name keywords (lounge/atithya/eats/...)", 0.6, "recovered")
            warnings.append("No Segment column found — derived segments from outlet names (unmatched outlets set to 'Others').")
        else:
            seg_ctx = None
            for text in title_lines + [sheet_name, file_name]:
                seg_ctx = _match_segment(text)
                if seg_ctx:
                    break
            if seg_ctx:
                out["segment"] = seg_ctx
                mappings["segment"] = FieldMapping(
                    "segment", f"'{seg_ctx}' found in the title/sheet/file name", 0.55, "recovered")
                warnings.append(f"No Segment column found — applied '{seg_ctx}' from the file's context to every row.")
            else:
                raise UniversalParseError(
                    "Could not identify a Segment — no column matches business-line "
                    "names (Lounges/Atithya/EHPL/Sky Plates/...), outlet names don't "
                    "hint at one, and none appears in the title/sheet/file name."
                )

    # outlet synthesis if still missing
    if out["outlet"].isna().all():
        out["outlet"] = out["location"].astype(str) + " - " + out["segment"].astype(str)
        mappings["outlet"] = FieldMapping(
            "outlet", "synthesized as '<Location> - <Segment>' (no outlet column found)", 0.5, "recovered")
        warnings.append("No Outlet column found — rows were labeled '<Location> - <Segment>' so they aggregate correctly at segment level.")

    # numbers
    unit_mult = _detect_unit_multiplier(title_lines, headers)
    if "revenue" not in assignments:
        raise UniversalParseError(
            "Could not identify a Revenue column — no numeric column has a "
            "revenue-like header (Revenue/Sales/Amount/...) or revenue-like values."
        )
    out["revenue"] = col("revenue").map(_to_number)
    if unit_mult != 1.0:
        out["revenue"] = out["revenue"] * unit_mult

    for opt in ("pax", "aop", "traffic"):
        if opt in assignments:
            out[opt] = col(opt).map(_to_number)
            if opt == "aop" and unit_mult != 1.0:
                out[opt] = out[opt] * unit_mult
        else:
            out[opt] = pd.NA
    if "pax" not in assignments:
        warnings.append("No PAX column was identified — PAX will be blank for these rows.")

    # ---- Row-level validation ---------------------------------------------
    before = len(out)
    out = out[out["date"].notna()]
    out = out[out["location"].notna() & (out["location"].astype(str).str.strip() != "")]
    out = out[out["revenue"].notna()]
    dropped_invalid = before - len(out)

    today = dt.date.today()
    plaus = out["date"].map(lambda d: dt.date(2015, 1, 1) <= d <= today + dt.timedelta(days=366))
    if (~plaus).any():
        warnings.append(f"{int((~plaus).sum())} row(s) had implausible dates (before 2015 or far in the future) and were excluded.")
        out = out[plaus]

    neg = out["revenue"] < 0
    if neg.any():
        warnings.append(f"{int(neg.sum())} row(s) have negative revenue — kept (could be refunds/adjustments), please verify.")

    out = out.reset_index(drop=True)
    if out.empty:
        raise UniversalParseError("After validation, no usable data rows remained.")

    # ---- Confidence ---------------------------------------------------------
    req_conf = [mappings[r].confidence for r in REQUIRED_ROLES if r in mappings]
    if len(req_conf) < len(REQUIRED_ROLES):
        raise UniversalParseError("Not all required fields could be identified.")
    confidence = float(min(req_conf)) * 0.7 + float(np.mean(req_conf)) * 0.3
    if confidence < CONFIDENCE_THRESHOLD:
        detail = "; ".join(f"{m.role}: {m.confidence:.0%} via {m.source}" for m in mappings.values())
        raise UniversalParseError(
            f"Schema was detected but with too little confidence ({confidence:.0%}) "
            f"to import safely. Detected mapping — {detail}. If this mapping looks "
            f"right, consider renaming the file's headers slightly (e.g. 'Date', "
            f"'Location', 'Outlet', 'Revenue') and re-uploading."
        )

    ordered = [mappings[r] for r in ALL_ROLES if r in mappings]
    return UniversalParseResult(
        df=out[["date", "segment", "outlet", "location", "pax", "revenue", "aop", "traffic"]],
        confidence=confidence,
        mappings=ordered,
        warnings=warnings,
        source_description=source_desc,
        rows_dropped=dropped_totals + dropped_invalid,
        unit_multiplier=unit_mult,
    )


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def parse_universal(file_obj, file_name: str) -> UniversalParseResult:
    """
    Parse ANY supported document (Excel / CSV / TSV / TXT / PDF) in an
    arbitrary layout into the canonical revenue schema, with automatic
    schema detection, field standardization, and validation.

    Returns a UniversalParseResult (df + confidence + field-mapping report).
    Raises UniversalParseError with a clear, human-readable reason if the
    file can't be understood confidently.
    """
    grids = _extract_grids(file_obj, file_name)

    candidates: list[UniversalParseResult] = []
    failures: list[str] = []
    for grid, desc, sheet in grids:
        try:
            candidates.append(_build_candidate(grid, desc, sheet, file_name))
        except UniversalParseError as exc:
            failures.append(f"{desc}: {exc}")
        except Exception as exc:  # defensive: one bad sheet must not sink the file
            failures.append(f"{desc}: unexpected error — {exc}")

    if not candidates:
        raise UniversalParseError(
            "Automatic schema detection could not understand this file. "
            + " | ".join(failures[:4])
        )

    # Prefer the highest-confidence candidate; break ties by row count.
    candidates.sort(key=lambda c: (-c.confidence, -len(c.df)))
    best = candidates[0]
    if len(candidates) > 1:
        best.warnings.append(
            f"{len(candidates)} sheet(s)/table(s) in this file looked like revenue "
            f"data — imported the best match ({best.source_description}). "
            f"Upload others individually if they're separate datasets."
        )
    return best
