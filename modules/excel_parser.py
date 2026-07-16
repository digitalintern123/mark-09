"""
excel_parser.py — Reads revenue data out of Excel workbooks.

Main entry point:
  parse_excel_auto(file_obj) — scans every sheet in the workbook, finds the
      one that looks like a long-format revenue table (a row containing
      Date + Location/Business/Outlet headers, regardless of what the sheet
      itself is named — "Data", "DATABASE", "Sheet1", anything), and parses
      it. This is what makes the historical importer accept any workbook
      layout rather than requiring one hardcoded sheet name.

Supporting entry points:
  detect_long_format_sheet(file_obj) — the sheet-scanning logic on its own,
      useful if the caller wants to show the user which sheet was picked.

  parse_revenue_dashboard(file_obj, sheet_name=..., header_row_idx=...) —
      parses one specific sheet once you already know which one to use.

  parse_generic_excel(file_obj) — best-effort fuzzy column-name matching for
      a single sheet, used as a fallback for non-bulk uploads where
      pandas's default "first row is the header" assumption is more likely
      to already be correct (e.g. a small ad-hoc spreadsheet).

All return a long-format DataFrame: date, segment, outlet, location, pax,
revenue, aop (aop is NaN unless a matching column exists).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

REQUIRED_OUTPUT_COLS = ["date", "segment", "outlet", "location", "pax", "revenue", "aop"]

# Column-name aliases used by the generic parser's fuzzy matching.
COLUMN_ALIASES = {
    "date": ["date", "report date", "day"],
    "segment": ["segment", "business", "business segment", "category"],
    "outlet": ["outlet", "sub-business", "sub business", "sub_business", "outlet name", "service"],
    "location": ["location", "city", "airport"],
    "pax": ["pax", "passengers", "footfall", "guests"],
    "revenue": ["revenue", "rev", "amount", "total revenue"],
    "aop": ["aop", "budget", "target", "aop target"],
    "traffic": ["traffic", "airport traffic", "total traffic"],
}


LOCATION_NORMALIZATION = {
    "delhi": "Delhi",
    "hyderabad": "Hyderabad",
    "goa": "Goa",
}


def _normalize_location(raw_value: str) -> str:
    key = str(raw_value).strip().lower()
    return LOCATION_NORMALIZATION.get(key, str(raw_value).strip())


class ExcelParseError(Exception):
    """Raised when an Excel file can't be parsed into the revenue schema."""


def _norm_header_label(value: Optional[str]) -> str:
    """
    Lowercase, strip, and drop a trailing period — for tolerant matching
    of wide-pivot column labels like "PAX."/"pax"/"PAX", "Revenue."/
    "revenue", or "Sum of AOP"/"SUM OF AOP"/"sum of aop.". Real-world
    workbooks are inconsistent about casing and whether the trailing
    period is present; matching on the literal exact string (as this
    used to) silently fails to detect the file at all if a source
    workbook happens to use different casing. Returns "" for None/blank,
    which simply won't match anything — same as the old exact-match
    behavior for a blank cell.
    """
    if value is None:
        return ""
    v = str(value).strip().lower()
    if v.endswith("."):
        v = v[:-1]
    return v


_PAXREV_LABELS = {"pax", "revenue"}
_AOP_LABEL = "sum of aop"


def detect_all_wide_pivot_sheets(file_obj) -> list[dict]:
    """
    Like detect_wide_pivot_sheet, but returns every matching sheet in the
    workbook instead of just the single largest one. Some workbooks split
    this same wide-pivot layout across multiple sheets — e.g. one sheet
    per month (a "APRIL 2026" sheet, a "may 26" sheet, ...) — and
    sometimes a separate multi-year "master" sheet that itself overlaps
    the per-month ones (e.g. one literally named after a specific month
    but actually containing several years of daily data spanning back
    well before and after that month, seen in a real workbook this was
    built against). Only using the single biggest sheet in that case
    would silently drop the other months' data entirely.

    Returned in ascending row-count order — smallest/most-specific-looking
    sheets first — so that when the caller concatenates and de-duplicates
    by (date, segment, outlet, location), a more specific per-month
    sheet's figure wins over a broader master sheet's figure for the same
    day, rather than the reverse (an arbitrary/coincidental ordering).
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise ExcelParseError(f"Could not open this Excel file: {exc}") from exc

    matches = []
    for sheet_name in xl.sheet_names:
        try:
            preview = pd.read_excel(xl, sheet_name=sheet_name, header=None, nrows=20)
        except Exception:
            continue

        paxrev_row_idx = None
        for row_idx in range(len(preview)):
            row_values = [str(v).strip() for v in preview.iloc[row_idx].tolist() if pd.notna(v)]
            if len(row_values) < 4:
                continue
            paxrev_count = sum(1 for v in row_values if _norm_header_label(v) in _PAXREV_LABELS)
            # "Sum of AOP" cells are also an expected, recognized part of
            # this header row (see parse_wide_pivot_sheet) — counting them
            # toward the confidence ratio too (not just PAX/Revenue) means
            # a sheet with a lot of embedded AOP columns relative to its
            # outlet count doesn't get its ratio unfairly diluted by cells
            # that are legitimately part of this format, not noise.
            recognized_count = paxrev_count + sum(
                1 for v in row_values if _norm_header_label(v) == _AOP_LABEL
            )
            if paxrev_count >= 4 and recognized_count >= len(row_values) * 0.6:
                paxrev_row_idx = row_idx
                break

        if paxrev_row_idx is None or paxrev_row_idx < 3:
            continue

        try:
            full_sheet_row_count = xl.book[sheet_name].max_row or len(preview)
        except Exception:
            full_sheet_row_count = len(preview)

        matches.append(
            {
                "sheet_name": sheet_name,
                "outlet_row_idx": paxrev_row_idx - 1,
                "segment_row_idx": paxrev_row_idx - 2,
                "location_row_idx": paxrev_row_idx - 3,
                "paxrev_row_idx": paxrev_row_idx,
                "data_start_row_idx": paxrev_row_idx + 1,
                "_row_count": full_sheet_row_count,
            }
        )

    matches.sort(key=lambda m: m["_row_count"])
    for m in matches:
        m.pop("_row_count", None)
    return matches


def detect_wide_pivot_sheet(file_obj) -> Optional[dict]:
    """
    Scan every sheet for the wide pivot/cross-tab layout used by some
    exports of the historical workbook: one row per date, with repeated
    PAX./Revenue. column-pairs per outlet, grouped under merged
    Location -> Segment -> Outlet header rows (and interspersed segment-
    and location-level subtotal columns like "Atithya PAX.", "Delhi
    Revenue.", which must be skipped rather than treated as outlets).

    Returns {"sheet_name": str, "header_rows": (loc_row_idx, seg_row_idx,
    outlet_row_idx, paxrev_row_idx), "date_row_idx": int} for the
    best-matching sheet, or None if no sheet matches this shape.

    The four header rows are detected by finding a row whose values are
    almost entirely "PAX."/"Revenue." (the innermost header row) and then
    walking upward, since the three rows above it (outlet names, segment
    names, location names) are each less densely populated due to merged
    cells — exactly mirroring the example layout pasted into this app's
    design discussion.
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise ExcelParseError(f"Could not open this Excel file: {exc}") from exc

    best_match = None
    for sheet_name in xl.sheet_names:
        try:
            preview = pd.read_excel(xl, sheet_name=sheet_name, header=None, nrows=20)
        except Exception:
            continue

        paxrev_row_idx = None
        for row_idx in range(len(preview)):
            row_values = [str(v).strip() for v in preview.iloc[row_idx].tolist() if pd.notna(v)]
            if len(row_values) < 4:
                continue
            paxrev_count = sum(1 for v in row_values if _norm_header_label(v) in _PAXREV_LABELS)
            # "Sum of AOP" cells are also an expected, recognized part of
            # this header row (see parse_wide_pivot_sheet) — counting them
            # toward the confidence ratio too (not just PAX/Revenue) means
            # a sheet with a lot of embedded AOP columns relative to its
            # outlet count doesn't get its ratio unfairly diluted by cells
            # that are legitimately part of this format, not noise.
            recognized_count = paxrev_count + sum(
                1 for v in row_values if _norm_header_label(v) == _AOP_LABEL
            )
            if paxrev_count >= 4 and recognized_count >= len(row_values) * 0.6:
                paxrev_row_idx = row_idx
                break

        if paxrev_row_idx is None or paxrev_row_idx < 3:
            continue

        # Prefer the sheet with more total rows (more likely to be the
        # full daily-grain data rather than a smaller monthly/summary
        # pivot that happens to share the same header shape).
        try:
            full_sheet_row_count = xl.book[sheet_name].max_row or len(preview)
        except Exception:
            full_sheet_row_count = len(preview)

        candidate = {
            "sheet_name": sheet_name,
            "outlet_row_idx": paxrev_row_idx - 1,
            "segment_row_idx": paxrev_row_idx - 2,
            "location_row_idx": paxrev_row_idx - 3,
            "paxrev_row_idx": paxrev_row_idx,
            "data_start_row_idx": paxrev_row_idx + 1,
            "_row_count": full_sheet_row_count,
        }
        if best_match is None or candidate["_row_count"] > best_match["_row_count"]:
            best_match = candidate

    if best_match is not None:
        best_match.pop("_row_count", None)
    return best_match


_KNOWN_LOCATIONS = {"delhi", "hyderabad", "goa"}


def parse_wide_pivot_sheet(file_obj, layout: dict) -> pd.DataFrame:
    """
    Parse a wide pivot/cross-tab sheet (see detect_wide_pivot_sheet) into
    the standard long-format DataFrame: date, segment, outlet, location,
    pax, revenue, and (if present) aop_daily.

    Column classification walks left to right, forward-filling the most
    recently seen Location and Segment header (these only appear in the
    leftmost column of their span, due to merged cells in the source
    workbook) and skipping any column whose header is itself a subtotal
    (e.g. "Atithya PAX.", "Delhi Revenue.", "Total PAX.", "Delhi Sum of
    AOP") — those are rollups of outlet columns to their left, not
    outlets themselves, and summing them in addition to the real outlet
    columns would double-count every total.

    Some workbooks add a third sub-column per outlet alongside PAX./
    Revenue. — "Sum of AOP", a genuine per-day AOP figure for that
    outlet on that date (not a monthly total repeated across days). When
    present, it's returned here as `aop_daily` rather than silently
    dropped; the caller is responsible for deciding what to do with it
    (e.g. aggregating to monthly totals and feeding it into the same
    aop_target table a dedicated AOP workbook upload would populate) —
    this function's job is only to not lose the data during parsing.
    `aop_daily` is all-NaN for sheets that only have the PAX./Revenue.
    pair, same as it always behaved before this column existed.
    """
    sheet_name = layout["sheet_name"]
    raw = pd.read_excel(file_obj, sheet_name=sheet_name, engine="openpyxl", header=None)

    location_row = raw.iloc[layout["location_row_idx"]].tolist()
    segment_row = raw.iloc[layout["segment_row_idx"]].tolist()
    outlet_row = raw.iloc[layout["outlet_row_idx"]].tolist()
    paxrev_row = raw.iloc[layout["paxrev_row_idx"]].tolist()
    n_cols = len(paxrev_row)

    def _is_subtotal_label(value: str) -> bool:
        v = _norm_header_label(value)
        return v.endswith("pax") or v.endswith("revenue") or v.endswith(_AOP_LABEL)

    outlet_columns = []  # list of dicts: location, segment, outlet, pax_col, revenue_col, aop_col
    current_location = None
    current_segment = None
    active_outlet_col = None  # the outlet column-group currently being assembled, if any

    for col_idx in range(1, n_cols):  # column 0 is the Date column
        loc_val = _clean_str(location_row[col_idx])
        seg_val = _clean_str(segment_row[col_idx])
        outlet_val = _clean_str(outlet_row[col_idx])
        paxrev_val = _clean_str(paxrev_row[col_idx])

        if loc_val and loc_val.lower() in _KNOWN_LOCATIONS:
            current_location = _normalize_location(loc_val)
        elif loc_val and _is_subtotal_label(loc_val):
            # Location-level subtotal column (e.g. "Delhi PAX.", "Delhi
            # Sum of AOP") — skip, and stop attaching any further
            # sub-columns to whatever outlet group preceded it.
            active_outlet_col = None
            continue

        if seg_val:
            if _is_subtotal_label(seg_val):
                # Segment-level subtotal column (e.g. "Atithya PAX.") — skip.
                active_outlet_col = None
                continue
            current_segment = seg_val

        if outlet_val:
            # Start of a new outlet's PAX column.
            active_outlet_col = {
                "location": current_location,
                "segment": current_segment,
                "outlet": outlet_val,
                "pax_col": col_idx,
                "revenue_col": None,
                "aop_col": None,
            }
            outlet_columns.append(active_outlet_col)
            continue

        if (
            _norm_header_label(paxrev_val) == "revenue"
            and active_outlet_col is not None
            and active_outlet_col["revenue_col"] is None
        ):
            active_outlet_col["revenue_col"] = col_idx
            continue

        if (
            _norm_header_label(paxrev_val) == _AOP_LABEL
            and active_outlet_col is not None
            and active_outlet_col["revenue_col"] is not None
            and active_outlet_col["aop_col"] is None
        ):
            active_outlet_col["aop_col"] = col_idx
            active_outlet_col = None  # this outlet's column group is complete
            continue

        # Any other column with no header info of its own and no active
        # outlet column-group to pair with is something we don't
        # recognize — rather than silently mis-map it, we simply skip
        # it; it contributes no rows, which is safer than guessing.

    data = raw.iloc[layout["data_start_row_idx"] :].reset_index(drop=True)

    records = []
    for _, row in data.iterrows():
        date_val = row[0]
        if not isinstance(date_val, (dt.datetime, dt.date)):
            parsed_date = pd.to_datetime(date_val, errors="coerce")
            if pd.isna(parsed_date):
                continue  # e.g. the trailing "Grand Total" row
            date_val = parsed_date
        for oc in outlet_columns:
            if oc["revenue_col"] is None or oc["location"] is None or oc["segment"] is None:
                continue
            pax = row[oc["pax_col"]] if oc["pax_col"] < len(row) else None
            revenue = row[oc["revenue_col"]] if oc["revenue_col"] < len(row) else None
            aop_daily = None
            if oc["aop_col"] is not None and oc["aop_col"] < len(row):
                aop_daily = row[oc["aop_col"]]
                if pd.isna(aop_daily):
                    aop_daily = None
            if pd.isna(pax):
                pax = None
            if pd.isna(revenue):
                revenue = None
            if pax is None and revenue is None and aop_daily is None:
                continue
            records.append(
                {
                    "date": pd.Timestamp(date_val).date(),
                    "segment": oc["segment"],
                    "outlet": oc["outlet"],
                    "location": oc["location"],
                    "pax": pax,
                    "revenue": revenue,
                    "aop_daily": aop_daily,
                }
            )

    if not records:
        raise ExcelParseError(
            f"The wide pivot sheet '{sheet_name}' was recognized, but no usable "
            f"data rows could be extracted from it."
        )

    df = pd.DataFrame.from_records(records)
    df["aop"] = pd.NA
    df["traffic"] = pd.NA
    df = df.drop_duplicates(subset=["date", "segment", "outlet", "location"], keep="last")
    df = df.reset_index(drop=True)
    return df


def _clean_str(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None


def detect_long_format_sheet(file_obj) -> Optional[dict]:
    """
    Scan every sheet in the workbook and find the one that looks like a
    long-format revenue table (a row containing recognizable headers like
    Date/Location/Business/Outlet/PAX/Revenue), regardless of what the
    sheet itself is named.

    This is what lets the historical importer accept a workbook with *any*
    sheet name ("Data", "DATABASE", "Sheet1", whatever) rather than failing
    the moment someone's workbook doesn't match one hardcoded name — a wide
    pivot-style sheet (one row per date, PAX/Revenue column-pairs repeated
    per outlet) will score 0 and be skipped automatically, since it has no
    single row containing all of Date + Location/Business + Outlet.

    Returns a dict {"sheet_name": str, "header_row_idx": int, "score": int}
    for the best-matching sheet, or None if no sheet in the workbook looks
    like a long-format table at all.
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise ExcelParseError(f"Could not open this Excel file: {exc}") from exc

    best_match = None

    for sheet_name in xl.sheet_names:
        try:
            preview = pd.read_excel(
                xl, sheet_name=sheet_name, header=None, nrows=15
            )
        except Exception:
            continue

        for row_idx in range(len(preview)):
            row_values = [str(v).strip().lower() for v in preview.iloc[row_idx].tolist()]
            score = _score_header_row(row_values)
            if score == 0:
                continue
            if best_match is None or score > best_match["score"]:
                best_match = {
                    "sheet_name": sheet_name,
                    "header_row_idx": row_idx,
                    "score": score,
                }

    return best_match


def _score_header_row(row_values: list[str]) -> int:
    """
    Score how strongly a single row looks like a long-format header row.
    Requires "date" plus at least one of location/business/outlet to count
    at all (this is what rules out wide pivot sheets, which have a "Date"
    column but never a single row that also contains "Location"/"Business"/
    "Sub-Business" — those appear as separate banner rows above the
    PAX./Revenue. column pairs instead). Extra points for pax/revenue
    columns being present too, so that among several candidate rows the
    most complete one wins.
    """
    has_date = "date" in row_values
    has_location = any("location" in v for v in row_values)
    has_business = any(v in ("business", "segment") for v in row_values)
    has_outlet = any(
        v in ("sub-business", "sub business", "subbusiness", "outlet", "outlet name")
        for v in row_values
    )
    has_pax = any("pax" in v for v in row_values)
    has_revenue = any("revenue" in v for v in row_values)

    if not has_date or not (has_location or has_business or has_outlet):
        return 0

    score = 2  # base: date + at least one of location/business/outlet
    score += int(has_location) + int(has_business) + int(has_outlet)
    score += int(has_pax) + int(has_revenue)
    return score


def parse_excel_auto(file_obj, source_file: str = "uploaded.xlsx") -> pd.DataFrame:
    """
    Universal entry point: figure out which sheet (if any) in this workbook
    holds revenue data, parse it, and return a long-format DataFrame. This
    is what data_processor.py calls for any Excel upload — daily report or
    historical bulk import alike — instead of assuming a fixed sheet name
    or layout.

    Tries, in order:
      1. A long-format sheet anywhere in the workbook (one row per
         date+location+segment+outlet) — any sheet name.
      2. A wide pivot/cross-tab sheet anywhere in the workbook (one row per
         date, PAX./Revenue. column-pairs repeated per outlet under merged
         Location/Segment/Outlet header rows) — any sheet name.

    Raises ExcelParseError with a clear, specific message only if neither
    layout is found in any sheet, rather than silently guessing and
    returning garbage.
    """
    long_match = detect_long_format_sheet(file_obj)
    if long_match is not None:
        return parse_revenue_dashboard(
            file_obj,
            sheet_name=long_match["sheet_name"],
            header_row_idx=long_match["header_row_idx"],
        )

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    pivot_match = detect_wide_pivot_sheet(file_obj)
    if pivot_match is not None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        return parse_wide_pivot_sheet(file_obj, pivot_match)

    try:
        sheet_names = list_sheet_names(file_obj)
    except ExcelParseError:
        sheet_names = []
    raise ExcelParseError(
        "Could not find a usable revenue layout in any sheet of this "
        "workbook — neither a long-format table (a row with 'Date' plus "
        "'Location'/'Business'/'Outlet' headers) nor a wide pivot table "
        "(one row per date with repeated PAX./Revenue. column-pairs per "
        f"outlet). Sheets found: {sheet_names}."
    )


def parse_revenue_dashboard(
    file_obj,
    sheet_name: str = "Data",
    header_row_idx: Optional[int] = None,
) -> pd.DataFrame:
    """
    Parse a long-format revenue sheet (originally written for the specific
    Encalm "Data" sheet, but works for any sheet with the same column
    layout under any sheet name — pass `sheet_name` to point at a
    different one, or use `parse_excel_auto()` to find it automatically).

    Expected columns (case-insensitive, order-independent):
      Date, Location, Business, Sub-Business, PAX, Revenue, AOP, Month

    `header_row_idx`, if given, skips the auto-detection scan in
    `_locate_header_row_and_slice` and uses that row directly — this is
    what `parse_excel_auto()` passes through once it has already found the
    right row, so the work isn't done twice.

    Raises ExcelParseError if the sheet or required columns are missing.
    """
    try:
        raw = pd.read_excel(file_obj, sheet_name=sheet_name, engine="openpyxl", header=None)
    except ValueError as exc:
        raise ExcelParseError(
            f"Could not find a sheet named '{sheet_name}' in this workbook. "
            f"({exc})"
        ) from exc
    except Exception as exc:
        raise ExcelParseError(f"Failed to read the Excel file: {exc}") from exc

    raw = _locate_header_row_and_slice(raw, header_row_idx=header_row_idx)

    raw = _normalize_headers(raw)

    # Use the same alias lists as everywhere else in this module (and as
    # _score_header_row's detection logic, which is what decided this sheet
    # counted as long-format in the first place) — a narrower list here
    # previously meant a sheet using "Segment"/"Outlet" headers (both
    # explicitly accepted by the detector) would pass detection and then
    # fail to parse because this column_map only recognized "Business" /
    # "Sub-Business", rejecting an otherwise perfectly valid upload.
    column_map = {
        "date": _find_column(raw, COLUMN_ALIASES["date"]),
        "location": _find_column(raw, COLUMN_ALIASES["location"]),
        "segment": _find_column(raw, COLUMN_ALIASES["segment"] + ["business"]),
        "outlet": _find_column(raw, COLUMN_ALIASES["outlet"] + ["sub-business", "sub business", "subbusiness"]),
        "pax": _find_column(raw, COLUMN_ALIASES["pax"]),
        "revenue": _find_column(raw, COLUMN_ALIASES["revenue"]),
        "aop": _find_column(raw, COLUMN_ALIASES["aop"]),
    }

    missing = [k for k, v in column_map.items() if v is None and k != "aop"]
    if missing:
        raise ExcelParseError(
            f"The '{sheet_name}' sheet is missing expected column(s): {missing}. "
            f"Found columns: {list(raw.columns)}"
        )

    df = pd.DataFrame()
    df["date"] = pd.to_datetime(raw[column_map["date"]], errors="coerce").dt.date
    df["segment"] = raw[column_map["segment"]].astype(str).str.strip()
    df["outlet"] = raw[column_map["outlet"]].astype(str).str.strip()
    df["location"] = raw[column_map["location"]].astype(str).str.strip().map(_normalize_location)
    df["pax"] = pd.to_numeric(raw[column_map["pax"]], errors="coerce")
    df["revenue"] = pd.to_numeric(raw[column_map["revenue"]], errors="coerce")
    df["aop"] = (
        pd.to_numeric(raw[column_map["aop"]], errors="coerce")
        if column_map["aop"] is not None
        else pd.NA
    )
    df["traffic"] = pd.NA

    # Drop rows with no date or no location/segment/outlet — these are
    # usually blank trailer rows at the bottom of a large sheet.
    df = df.dropna(subset=["date"])
    df = df[(df["segment"] != "") & (df["segment"].str.lower() != "nan")]
    df = df[(df["outlet"] != "") & (df["outlet"].str.lower() != "nan")]
    df = df[(df["location"] != "") & (df["location"].str.lower() != "nan")]
    df = df.drop_duplicates(subset=["date", "segment", "outlet", "location"], keep="last")
    df = df.reset_index(drop=True)

    if df.empty:
        raise ExcelParseError(
            f"No usable rows were found in the '{sheet_name}' sheet after cleaning."
        )

    return df


def parse_generic_excel(file_obj, sheet_name: Optional[str] = None) -> pd.DataFrame:
    """
    Best-effort parse of an arbitrary Excel file using fuzzy column matching.
    Falls back to the first sheet if `sheet_name` isn't given.
    """
    try:
        raw = pd.read_excel(file_obj, sheet_name=sheet_name or 0, engine="openpyxl")
    except Exception as exc:
        raise ExcelParseError(f"Failed to read the Excel file: {exc}") from exc

    raw = _normalize_headers(raw)

    column_map = {}
    for target, aliases in COLUMN_ALIASES.items():
        column_map[target] = _find_column(raw, aliases)

    required = ["date", "segment", "outlet", "location", "revenue"]
    missing = [k for k in required if column_map.get(k) is None]
    if missing:
        raise ExcelParseError(
            f"Could not confidently identify required column(s): {missing}. "
            f"Found columns: {list(raw.columns)}. "
            f"Expected something like Date / Segment / Outlet / Location / Revenue."
        )

    df = pd.DataFrame()
    df["date"] = pd.to_datetime(raw[column_map["date"]], errors="coerce").dt.date
    df["segment"] = raw[column_map["segment"]].astype(str).str.strip()
    df["outlet"] = raw[column_map["outlet"]].astype(str).str.strip()
    df["location"] = raw[column_map["location"]].astype(str).str.strip().map(_normalize_location)
    df["revenue"] = pd.to_numeric(raw[column_map["revenue"]], errors="coerce")
    df["pax"] = (
        pd.to_numeric(raw[column_map["pax"]], errors="coerce")
        if column_map.get("pax") is not None
        else pd.NA
    )
    df["aop"] = (
        pd.to_numeric(raw[column_map["aop"]], errors="coerce")
        if column_map.get("aop") is not None
        else pd.NA
    )
    df["traffic"] = (
        pd.to_numeric(raw[column_map["traffic"]], errors="coerce")
        if column_map.get("traffic") is not None
        else pd.NA
    )

    df = df.dropna(subset=["date"])
    df = df[df["revenue"].notna()]
    df = df.drop_duplicates(subset=["date", "segment", "outlet", "location"], keep="last")
    df = df.reset_index(drop=True)

    if df.empty:
        raise ExcelParseError("No usable rows were found after cleaning this file.")

    return df


def list_sheet_names(file_obj) -> list[str]:
    """Return the sheet names in an uploaded Excel file (for a sheet picker UI)."""
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
        return xl.sheet_names
    except Exception as exc:
        raise ExcelParseError(f"Could not read sheet names: {exc}") from exc


def _locate_header_row_and_slice(
    raw: pd.DataFrame, header_row_idx: Optional[int] = None
) -> pd.DataFrame:
    """
    Long-format sheets (the historical "Data"/"DATABASE"/whatever-it's-named
    sheet) sometimes have a couple of blank or title rows above the real
    header row. Scan the first 10 rows for the one that looks like a header
    (contains recognizable column names), then re-slice the DataFrame so
    that row becomes the column header.

    If `header_row_idx` is given (already known from `detect_long_format_sheet`),
    use it directly instead of re-scanning.
    """
    if header_row_idx is not None:
        new_df = raw.iloc[header_row_idx + 1 :].copy()
        new_df.columns = [str(c).strip() for c in raw.iloc[header_row_idx]]
        return new_df.reset_index(drop=True)

    search_rows = min(10, len(raw))
    found_idx = None
    for i in range(search_rows):
        row_values = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        if "date" in row_values and ("location" in row_values or "business" in row_values):
            found_idx = i
            break

    if found_idx is None:
        # Fall back to treating the first row as the header (original
        # behaviour) — downstream column lookups will raise a clear error
        # if this guess is wrong.
        new_df = raw.copy()
        new_df.columns = [str(c).strip() for c in new_df.iloc[0]]
        return new_df.iloc[1:].reset_index(drop=True)

    new_df = raw.iloc[found_idx + 1 :].copy()
    new_df.columns = [str(c).strip() for c in raw.iloc[found_idx]]
    return new_df.reset_index(drop=True)


def _normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _find_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Case-insensitive, whitespace-tolerant column lookup against aliases."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower_map:
            return lower_map[key]
    # try a loose "contains" match as a last resort
    for candidate in candidates:
        key = candidate.strip().lower()
        for lower_col, original_col in lower_map.items():
            if key in lower_col:
                return original_col
    return None
