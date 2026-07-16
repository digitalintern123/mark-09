"""
pdf_parser.py
Parses Encalm Group's daily "Detailed Revenue" PDF report into the long-format
revenue_master schema: date, segment, outlet, location, pax, revenue.

The PDF has two pages:
  Page 1 — segment-level summary (For the Day / MTD / YTD blocks)
  Page 2 — outlet-level detail, columns: Outlet | DELHI(PAX,Rev) | HYDERABAD(PAX,Rev) | GOA(PAX,Rev) | Total Revenue

This module only extracts from Page 2 (the detailed table) since that's the
finest grain available, and it derives the report date from Page 1's header.

Known quirks handled here (observed in real exports of this report):
  - Numbers carry stray spaces from PDF text reflow, e.g. "5 ,01,992", "2 72"
  - Indian digit grouping (lakhs/crores): 49,35,256 -> 4935256
  - Missing cells render as '' (empty string), explicit zeros as '-'
  - Section headers (e.g. "Lounges & Spa") are rows with text only in col 0
  - Subtotal rows have a BLANK col 0 but populated numeric columns - skip them
  - The literal "Total" row (grand total) must also be skipped
  - Occasionally two numbers get merged into one cell on the Total row
    (e.g. "8,22,588 3,55,25,522") - we never read the Total row, so this is
    harmless, but extract_locations() tolerates it defensively.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

import pandas as pd

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

DATE_HEADER_RE = re.compile(r"For the Day\s*-+>\s*(\d{2})-(\d{2})-(\d{4})")
LOCATIONS = ["Delhi", "Hyderabad", "Goa"]

# Normalize PDF section-header labels onto the canonical segment names used
# throughout the rest of the system (and in the historical Excel "Business"
# column), so that PDF-sourced and Excel-sourced rows aggregate together
# cleanly without "Lounges & Spa" vs "Lounges" showing up as two segments.
SEGMENT_NORMALIZATION = {
    "lounges & spa": "Lounges",
    "lounges and spa": "Lounges",
    "lounges": "Lounges",
    "atithya": "Atithya",
    "others": "Others",
    "subsidiary": "Subsidiary",
}


def _normalize_segment(raw_label: str) -> str:
    key = raw_label.strip().lower()
    return SEGMENT_NORMALIZATION.get(key, raw_label.strip())


# ---------------------------------------------------------------------------
# Outlet name canonicalization
# ---------------------------------------------------------------------------
# The daily PDF and the historical Revenue_Dashboard.xlsx describe the same
# physical outlets with different naming conventions — the Excel's
# "Sub-Business" names are short and location-agnostic (the Location column
# already carries that), while the PDF's outlet labels are sometimes
# location-specific compound names (e.g. "Domestic Lounge (DEL DLO2/3/4,
# HYD)" covers what the Excel records separately, per-location, as just
# "Lounge DL 02,03,04" for Delhi and "Domestic Lounge" for Hyderabad).
#
# This map is keyed on (pdf_outlet_name, location) -> canonical Excel-style
# outlet name, since the same PDF row can correspond to *different* Excel
# outlet names depending on which location's PAX/Revenue pair it is. Built
# from a row-by-row reconciliation of a real report against the historical
# workbook's Input/Data sheets.
_OUTLET_ALIAS_BY_LOCATION = {
    ('Domestic Lounge - T1 L5', 'Delhi'): 'T1D L4&5 Lounge',
    ('Domestic Bar - T1 L5', 'Delhi'): 'T1D L4&5 Lounge',
    ('Domestic Lounge - T1 L4', 'Delhi'): 'T1D L4&5 Lounge',
    ('Ceremonial(Del)  /  GA (Hyd)  /  CIP(Goa)', 'Delhi'): 'Reserved Lounge',
    ('Reserve Lounges', 'Delhi'): 'Reserved Lounge',
    ('International Lounge (DEL INL5&6; HYD & GOA)', 'Delhi'): 'INL 5&6',
    ('International Bar - INL5&6, Hyd & Goa', 'Delhi'): 'INL 5&6',
    ('Domestic Lounge (DEL DLO2/3/4, HYD)', 'Delhi'): 'Lounge DL 02,03,04',
    ('Domestic Bar - DLO2/3/4, Hyd & Goa', 'Delhi'): 'Lounge DL 02,03,04',
    ('Domestic Lounge - D49', 'Delhi'): 'T2 Domestic',
    ('Domestic Bar - D49', 'Delhi'): 'T2 Domestic',
    ('Domestic Lounge - T2', 'Delhi'): 'T3 D49',
    ('Domestic Bar - T2', 'Delhi'): 'T3 D49',
    ('NAP - Premium Lounge', 'Delhi'): 'Premium Lounge',
    ('SPA - Premium Lounge', 'Delhi'): 'Premium Lounge',
    ('International Lounge - Premium', 'Delhi'): 'Premium Lounge',
    ('Sleeping Pod - Premium Lounge', 'Delhi'): 'Premium Lounge',
    ('International Bar -  Premium Lounge', 'Delhi'): 'Premium Lounge',
    ('DomesticLounge- Centurion Amex T3', 'Delhi'): 'Centurion Lounge',
    ('DomesticLounge- Centurion Amex T1', 'Delhi'): 'T1D new Amex Lounge (level 4)',
    ('Transit Lounge - LA01', 'Delhi'): 'Nap & Shower LA01',
    ('Domestic Lounge - T1 Prive', 'Delhi'): 'T1D new premium lounge 2 (level 5)',
    ('Transit Lounge - LA12', 'Delhi'): 'Nap & Shower LA12',
    ('Porter Services -T2', 'Delhi'): 'Porter',
    ('Porter Services -T3', 'Delhi'): 'Porter',
    ('Porter Services- T1', 'Delhi'): 'Porter',
    ('International Lounge - Air India', 'Delhi'): 'AI International Lounge',
    ('Welcome & Assist', 'Delhi'): 'Meet & Greet',
    ('Business Centre', 'Delhi'): 'Business Center',
    ('Enwrap Services', 'Delhi'): 'Baggage Wrapping',
    ('International Spa- INL07 T3', 'Delhi'): 'Spa - International',
    ('Domestic Spa- DPA10 T3', 'Delhi'): 'SPA Domestic',
    ('Buggy Services', 'Delhi'): 'Buggy Service',
    ('Arrival Lounge - LA22', 'Delhi'): 'Arrival Lounge LA 22',
    ('Domestic Lounge - Air India', 'Delhi'): 'Air India',
    ('Domestic Lounge - Rupay', 'Delhi'): 'Rupay',
    ('Domestic Bar - Rupay', 'Delhi'): 'Rupay',
    ('Domestic Spa- T1', 'Delhi'): 'T1D SPA',
    ('Xenia - INL T3', 'Delhi'): 'First Class - Xenia Lounge',
    ('Encalm Eats', 'Delhi'): 'Encalm Eats',
    ('Round D Clock -Motel', 'Delhi'): 'Round D Clock (RDC)',
    ('Round D Clock (RDC)-Restaurant', 'Delhi'): 'Round D Clock (RDC)',
    ('Encalm Sky Plates', 'Delhi'): 'Encalm Sky Plates',
    ('Welcome & Assist', 'Hyderabad'): 'Meet & Greet',
    ('Airport Lodge', 'Hyderabad'): 'Airport Lodge',
    ('Enwrap Services', 'Hyderabad'): 'Baggage Wrapping',
    ('Reserve Lounges', 'Hyderabad'): 'Reserved Lounge',
    ('International Lounge (DEL INL5&6; HYD & GOA)', 'Hyderabad'): 'International Lounge',
    ('International Bar - INL5&6, Hyd & Goa', 'Hyderabad'): 'International Lounge',
    ('Domestic Lounge (DEL DLO2/3/4, HYD)', 'Hyderabad'): 'Domestic Lounge',
    ('Domestic Bar - DLO2/3/4, Hyd & Goa', 'Hyderabad'): 'Domestic Lounge',
    ('Ceremonial(Del)  /  GA (Hyd)  /  CIP(Goa)', 'Hyderabad'): 'GAT',
    ('International Lounge - Premium', 'Hyderabad'): 'Prive',
    ('Sleeping Pod - Premium Lounge', 'Hyderabad'): 'Prive',
    ('NAP - Premium Lounge', 'Hyderabad'): 'Prive',
    ('SPA - Premium Lounge', 'Hyderabad'): 'Prive',
    ('International Bar -  Premium Lounge', 'Hyderabad'): 'Prive',
    ('Encalm Sky Plates', 'Hyderabad'): 'Encalm Sky Plates',
    ('Ceremonial(Del)  /  GA (Hyd)  /  CIP(Goa)', 'Goa'): 'CIP Lounge',
    ('Welcome & Assist', 'Goa'): 'Meet & Greet',
    ('Domestic Lounge (DEL DLO2/3/4, HYD)', 'Goa'): 'Domestic Lounge',
    ('Domestic Bar - DLO2/3/4, Hyd & Goa', 'Goa'): 'Domestic Lounge',
    ('Porter Services- T1', 'Goa'): 'Porter',
    ('Reserve Lounges', 'Goa'): 'Reserved Lounge',
    ('Enwrap Services', 'Goa'): 'Baggage Wrapping',
    ('International Lounge (DEL INL5&6; HYD & GOA)', 'Goa'): 'International Lounge',
    ('International Bar - INL5&6, Hyd & Goa', 'Goa'): 'International Lounge',
}


def canonicalize_outlet(outlet: str, location: str) -> str:
    """
    Map a PDF-sourced outlet name to the canonical (Excel-style) outlet name
    for the given location, so daily PDF uploads and the bulk historical
    Excel import refer to the same outlet under one name. Falls back to the
    original PDF name unchanged if no mapping is known (e.g. a brand-new
    outlet not yet seen in the historical data) — this keeps new outlets
    visible rather than silently dropping them.

    Whitespace is normalized (collapsed runs of spaces) before lookup, since
    the source Excel's hand-typed reference labels and a given day's PDF
    text extraction don't always agree on spacing around punctuation.
    """
    norm_outlet = re.sub(r"\s+", " ", outlet.strip())
    norm_location = location.strip()
    if (norm_outlet, norm_location) in _OUTLET_ALIAS_LOOKUP:
        return _OUTLET_ALIAS_LOOKUP[(norm_outlet, norm_location)]
    return outlet.strip()


# Pre-normalize the alias map's own keys once at import time, so lookups
# above are comparing like-for-like regardless of how the map was authored.
_OUTLET_ALIAS_LOOKUP = {
    (re.sub(r"\s+", " ", k[0].strip()), k[1].strip()): v
    for k, v in _OUTLET_ALIAS_BY_LOCATION.items()
}

# Outlets that are recorded under "Atithya"/"Others"/"Subsidiary" segments are
# detected purely from the inline section header rows in the PDF, so no
# hardcoded outlet->segment map is required. This keeps the parser resilient
# to new outlets being added to the report in future months.


class PDFParseError(Exception):
    """Raised when the PDF doesn't match the expected Encalm report layout."""


def _clean_number(raw) -> Optional[float]:
    """
    Convert a raw PDF table cell into a float, or None if blank.
    Handles Indian comma grouping and stray whitespace inserted by PDF reflow.
    '-' or '' or None -> None (no value reported)
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "" or s == "-":
        return None
    # Remove all whitespace (PDF reflow sometimes splits a number, e.g. "5 ,01,992")
    s = re.sub(r"\s+", "", s)
    # Remove commas (Indian lakh/crore grouping is just digit grouping; once
    # commas are stripped the digit sequence is the correct value)
    s = s.replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        # Defensive: a merged/garbled cell (e.g. two numbers stuck together).
        # Take the first valid numeric token if one exists, else give up.
        match = re.search(r"-?\d+(\.\d+)?", s)
        if match:
            return float(match.group())
        return None


def extract_report_date(pdf) -> _dt.date:
    """Pull the 'For the Day -> DD-MM-YYYY' date from page 1's header text."""
    first_page_text = pdf.pages[0].extract_text() or ""
    match = DATE_HEADER_RE.search(first_page_text)
    if not match:
        raise PDFParseError(
            "Could not find 'For the Day -> DD-MM-YYYY' header on page 1. "
            "This file may not be an Encalm daily revenue report."
        )
    dd, mm, yyyy = match.groups()
    return _dt.date(int(yyyy), int(mm), int(dd))


def _find_detail_page(pdf):
    """Locate the page containing the outlet-level 'Outlet / Business' table."""
    for page in pdf.pages:
        text = page.extract_text() or ""
        if "Outlet / Business" in text or "Detailed Revenue" in text:
            tables = page.extract_tables()
            if tables:
                return tables[0]
    return None


def _is_section_header(row: list) -> bool:
    """True if only column 0 has content and all PAX/Revenue cells are empty/None."""
    if not row or not row[0] or not str(row[0]).strip():
        return False
    rest = row[1:8] if len(row) >= 8 else row[1:]
    return all((c is None or str(c).strip() == "") for c in rest)


def _is_subtotal_or_total(row: list) -> bool:
    """
    Subtotal rows: col 0 blank/None but numeric columns populated.
    Total row: col 0 literally 'Total'.
    """
    if not row:
        return True
    label = str(row[0]).strip() if row[0] is not None else ""
    if label.lower() == "total":
        return True
    if label == "":
        # blank label with any populated numeric cell = subtotal row
        rest = row[1:8] if len(row) >= 8 else row[1:]
        if any((c is not None and str(c).strip() not in ("", "-")) for c in rest):
            return True
    return False


def parse_detail_table(raw_table: list, report_date: _dt.date, source_file: str) -> pd.DataFrame:
    """
    Convert the raw pdfplumber table (list of row-lists) for page 2 into a
    long-format dataframe: date, segment, outlet, location, pax, revenue.
    """
    records = []
    current_segment = None

    for row in raw_table:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue  # fully blank row

        label_raw = row[0]
        label = str(label_raw).strip() if label_raw is not None else ""

        # Header row itself ("Outlet / Business", "PAX"/"Revenue" sub-header)
        if label in ("Outlet / Business", "") and label_raw is None:
            pass
        if label in ("PAX", "Revenue"):
            continue

        if _is_section_header(row):
            current_segment = _normalize_segment(label)
            continue

        if _is_subtotal_or_total(row):
            continue

        if not label:
            continue

        # Defensive: need at least 8 columns (outlet + 3 locations x 2 + total)
        padded = list(row) + [None] * max(0, 8 - len(row))

        pdf_outlet = label
        segment = current_segment or "Unclassified"

        loc_cells = {
            "Delhi": (padded[1], padded[2]),
            "Hyderabad": (padded[3], padded[4]),
            "Goa": (padded[5], padded[6]),
        }

        for location, (pax_raw, rev_raw) in loc_cells.items():
            pax = _clean_number(pax_raw)
            revenue = _clean_number(rev_raw)
            if pax is None and revenue is None:
                continue  # outlet not active at this location on this day
            canonical_outlet = canonicalize_outlet(pdf_outlet, location)
            records.append(
                {
                    "date": report_date,
                    "segment": segment,
                    "outlet": canonical_outlet,
                    "location": location,
                    "pax": pax,
                    "revenue": revenue,
                }
            )

    df = pd.DataFrame.from_records(
        records, columns=["date", "segment", "outlet", "location", "pax", "revenue"]
    )
    if df.empty:
        return df

    # Multiple PDF outlet rows can canonicalize to the same outlet name for
    # a given location (e.g. "Domestic Lounge - T1 L5", "Domestic Lounge -
    # T1 L4", and "Domestic Bar - T1 L5" all roll up into "T1D L4&5
    # Lounge" for Delhi) — sum PAX/Revenue across those rather than leaving
    # duplicate (date, segment, outlet, location) keys, which the database's
    # UNIQUE constraint would otherwise reject all but one of.
    df = (
        df.groupby(["date", "segment", "outlet", "location"], as_index=False)
        .agg(pax=("pax", lambda s: s.sum(min_count=1)), revenue=("revenue", lambda s: s.sum(min_count=1)))
    )
    return df


def parse_pdf(file_obj, source_file: str = "uploaded.pdf") -> pd.DataFrame:
    """
    Main entry point. file_obj can be a path string or a file-like object
    (e.g. Streamlit's UploadedFile).
    Returns a long-format dataframe ready for database.save_dataframe().
    Raises PDFParseError on malformed input.
    """
    if pdfplumber is None:
        raise PDFParseError("pdfplumber is not installed. Add it to requirements.txt.")

    try:
        with pdfplumber.open(file_obj) as pdf:
            if len(pdf.pages) == 0:
                raise PDFParseError("The PDF has no pages.")
            report_date = extract_report_date(pdf)
            raw_table = _find_detail_page(pdf)
            if raw_table is None:
                raise PDFParseError(
                    "Could not find the detailed outlet table "
                    "(expected a page containing 'Outlet / Business')."
                )
    except PDFParseError:
        raise
    except Exception as exc:  # pragma: no cover
        raise PDFParseError(f"Failed to read PDF: {exc}") from exc

    df = parse_detail_table(raw_table, report_date, source_file)

    if df.empty:
        raise PDFParseError(
            "No revenue rows were extracted from this PDF. "
            "The layout may differ from the expected Encalm format."
        )

    return df


def get_summary_totals(df: pd.DataFrame) -> dict:
    """Quick aggregate for a processing-status message after parsing."""
    if df.empty:
        return {"rows": 0, "total_revenue": 0.0, "total_pax": 0.0, "date": None}
    return {
        "rows": len(df),
        "total_revenue": float(df["revenue"].fillna(0).sum()),
        "total_pax": float(df["pax"].fillna(0).sum()),
        "date": df["date"].iloc[0],
    }


def cross_validate_against_summary(df: pd.DataFrame, file_obj) -> Optional[dict]:
    """
    Best-effort sanity check: re-read page 1's "For the Day" summary table
    and compare its segment x location totals against what was parsed from
    the page-2 detail table. Returns a dict of discrepancies (empty dict if
    everything reconciles, or None if the summary table couldn't be read).

    This never raises — it's a diagnostic aid for the upload UI, not a hard
    gate, since minor PDF-to-PDF layout drift shouldn't block an upload.
    """
    if pdfplumber is None or df.empty:
        return None
    try:
        with pdfplumber.open(file_obj) as pdf:
            text = pdf.pages[0].extract_text() or ""
            if "For the Day" not in text:
                return None
            tables = pdf.pages[0].extract_tables()
            if not tables:
                return None
    except Exception:
        return None

    # Page 1 is a single table with three stacked blocks (For the Day / MTD
    # / YTD), each starting with a "For the Day -> ..." / "MTD -> ..." /
    # "YTD -> ..." label row. We only want the rows belonging to the first
    # ("For the Day") block, up to its own "Total" row.
    table = tables[0]
    block_rows = []
    in_target_block = False
    for row in table:
        label = str(row[0]).strip().lower() if row and row[0] else ""
        if label.startswith("for the day"):
            in_target_block = True
            continue
        if label.startswith("mtd") or label.startswith("ytd"):
            if in_target_block:
                break
            continue
        if not in_target_block:
            continue
        block_rows.append(row)
        if label == "total":
            break

    # The "For the Day" block reports revenue in INR Lakhs (e.g. "278.76"
    # for ~Rs 27.88 lakh), while the page-2 detail table (what `df` was
    # parsed from) reports revenue in absolute INR. Scale accordingly.
    LAKH = 100_000.0

    summary_totals = {}
    for row in block_rows:
        if not row or not row[0]:
            continue
        label = str(row[0]).strip()
        seg = _normalize_segment(label)
        if seg not in ("Lounges", "Atithya", "Others", "Subsidiary"):
            continue
        padded = list(row) + [None] * max(0, 8 - len(row))
        for loc, (pax_idx, rev_idx) in zip(LOCATIONS, [(1, 2), (3, 4), (5, 6)]):
            pax = _clean_number(padded[pax_idx])
            rev_lakh = _clean_number(padded[rev_idx])
            rev = (rev_lakh * LAKH) if rev_lakh is not None else None
            summary_totals[(seg, loc)] = (pax or 0.0, rev or 0.0)

    if not summary_totals:
        return None

    parsed_totals = (
        df.groupby(["segment", "location"])[["pax", "revenue"]]
        .sum()
        .to_dict(orient="index")
    )

    discrepancies = {}
    for key, (summary_pax, summary_rev) in summary_totals.items():
        parsed = parsed_totals.get(key, {"pax": 0.0, "revenue": 0.0})
        pax_diff = abs((parsed["pax"] or 0.0) - summary_pax)
        # Revenue figures in this per-location block are rounded to the
        # nearest whole Lakh (e.g. "219" means ~Rs 21.9 lakh), unlike the
        # rightmost "Total Revenue" column which carries 2 decimals. Allow
        # up to half a lakh of rounding slack before flagging a mismatch.
        rev_diff = abs((parsed["revenue"] or 0.0) - summary_rev)
        rev_tolerance = max(LAKH / 2, 0.01 * summary_rev)
        if pax_diff > 1 or rev_diff > rev_tolerance:
            discrepancies[key] = {
                "summary_pax": summary_pax,
                "parsed_pax": parsed["pax"],
                "summary_revenue": summary_rev,
                "parsed_revenue": parsed["revenue"],
            }
    return discrepancies
