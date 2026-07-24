"""
traffic_parser.py — Reads airport traffic data (Source 3).

Two distinct file formats are supported, auto-detected:

1. SIMPLE FORMAT — a flat table: Date, Location, Traffic, and optionally
   Terminal. One row per day (or per day+terminal). Handled by
   parse_traffic_auto() / detect_traffic_sheet().

2. CROSS-TAB FORMAT — one sheet per location (e.g. "DEL", "HYD", "GOX"),
   with a 2-row header: row 1 has terminal/type names (e.g. "T3
   International", "T3 Domestic", "Domestic", "International") spanning
   merged Dep./Arr. column pairs named in row 2, and one data row per
   period. Two granularities of this format exist:
     - MONTHLY: one row per month, with a month label like "Apr'24" in
       column 0, plus "Fy24-25"-style fiscal-year rollup rows mixed in
       (skipped) and trailing summary clutter below the real data (also
       skipped).
     - DAILY: the same column structure, but one row per day-of-month
       (1-31) instead of one row per month, plus a "Total" row at the
       bottom (skipped). Each sheet/table in this format covers a single
       month, so the calling code needs to know which month it's looking
       at — see parse_cross_tab_daily()'s `year` and `month` parameters.
   Handled by parse_cross_tab_auto(), which tries both granularities.

Both formats produce the same DataFrame shape: date, location, terminal,
traffic, granularity ("daily" or "monthly"), period_end (the last day
covered by a monthly row's figure; NaT for daily rows).
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from typing import Optional

import pandas as pd

REQUIRED_OUTPUT_COLS = ["date", "location", "terminal", "traffic"]

COLUMN_ALIASES = {
    "date": ["date", "traffic date", "day"],
    "location": ["location", "city", "airport", "station"],
    "terminal": ["terminal", "term", "terminal name", "terminal no", "terminal number"],
    "traffic": [
        "traffic", "footfall", "airport traffic", "total traffic",
        "passenger traffic", "pax traffic", "visitors", "total visitors",
    ],
}

_KNOWN_LOCATIONS = {"delhi", "hyderabad", "goa"}
_LOCATION_NORMALIZATION = {
    "delhi": "Delhi", "del": "Delhi",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad",
    "goa": "Goa", "gox": "Goa", "goi": "Goa",
}

# Sheet-name -> location for the cross-tab format, where each sheet is
# named after the airport's IATA-ish code rather than the location itself.
_SHEET_NAME_TO_LOCATION = {
    "del": "Delhi",
    "hyd": "Hyderabad",
    "gox": "Goa",
    "goa": "Goa",
}

# Segment-name -> canonical terminal, for the Delhi sheet's column groups.
# Hyderabad/Goa sheets use "International"/"Domestic" segment names that
# don't correspond to a physical terminal at all (those airports are
# single-terminal) — those get collapsed to terminal="" (whole-airport)
# rather than invented terminal labels.
# ---------------------------------------------------------------------------
# Delhi terminal labels — canonical names stored in airport_traffic table.
#
# Per the Business Plan (June 2026), T3 has FOUR distinct traffic pools:
#   T3 Dom Dep  — T3 domestic DEPARTURES
#                 Outlet denominator for: T3 D49, T3 DLO2/03/04, Air India,
#                 Lounge Rupay, Centurion, Dom Spa
#   T3 Int Dep  — T3 international DEPARTURES
#                 Outlet denominator for: INL 5&6, Premium, Xenia, AI Intl,
#                 INTL Spa
#   T3 Dom Arr  — T3 domestic ARRIVALS   ─┐ summed together for LA01/LA12/LA22
#   T3 Int Arr  — T3 international ARRIVALS ┘ (Total Arrival T3 = Dom Arr + Int Arr)
#
# The groupby-sum in _parse_cross_tab_monthly/_daily keeps these four labels
# distinct — they are never collapsed together.  T3 Arr (the combined arrivals
# pool used by LA outlets) is derived at query time in terminal_mapping.py by
# summing T3 Dom Arr + T3 Int Arr, NOT stored as a separate row here, so no
# double-counting occurs.
# ---------------------------------------------------------------------------
_DELHI_SEGMENT_TO_TERMINAL: dict[str, str] = {
    # — T3 International —
    "t3 international":             "T3 Int Dep",   # Dep. column
    "t3 international dep.":        "T3 Int Dep",
    "t3 international dep":         "T3 Int Dep",
    "t3 international departure":   "T3 Int Dep",
    "t3 international arr.":        "T3 Int Arr",
    "t3 international arr":         "T3 Int Arr",
    "t3 international arrival":     "T3 Int Arr",
    # — T3 Domestic —
    "t3 domestic":                  "T3 Dom Dep",   # Dep. column
    "t3 domestic dep.":             "T3 Dom Dep",
    "t3 domestic dep":              "T3 Dom Dep",
    "t3 domestic departure":        "T3 Dom Dep",
    "t3 domestic arr.":             "T3 Dom Arr",
    "t3 domestic arr":              "T3 Dom Arr",
    "t3 domestic arrival":          "T3 Dom Arr",
    # — T2 — (T2 Lounge uses T2 Dep only per Business Plan)
    "t2 domestic":                  "T2 Dep",
    "t2 domestic dep.":             "T2 Dep",
    "t2 domestic dep":              "T2 Dep",
    "t2 domestic departure":        "T2 Dep",
    "t2 domestic arr.":             "T2 Arr",
    "t2 domestic arr":              "T2 Arr",
    "terminal 2":                   "T2 Dep",
    # — T1 — (T1D Lounge uses T1 Dep only per Business Plan)
    "terminal 1":                   "T1 Dep",
    "terminal 1 dep.":              "T1 Dep",
    "terminal 1 dep":               "T1 Dep",
    "terminal 1 departure":         "T1 Dep",
    "terminal 1 arr.":              "T1 Arr",
    "terminal 1 arr":               "T1 Arr",
    # — Generic T3 fallback (monthly files without Dep/Arr split) —
    "terminal 3":                   "T3",
}

_MONTH_ABBR_TO_NUM = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_FY_ROLLUP_RE = re.compile(r"^fy\s*\d{2}", re.IGNORECASE)
_MONTH_LABEL_RE = re.compile(r"^([A-Za-z]{3,})['’]?\s*(\d{2,4})$")


class TrafficParseError(Exception):
    """Raised when a traffic file can't be parsed into the expected schema."""


# ---------------------------------------------------------------------------
# Simple flat-table format (Date, Location, Traffic, [Terminal])
# ---------------------------------------------------------------------------

def detect_traffic_sheet(file_obj) -> Optional[dict]:
    """
    Scan every sheet in the workbook for one that looks like a SIMPLE
    flat traffic table: a header row containing Date + Location + Traffic
    (Terminal is optional). Returns {"sheet_name": str, "header_row_idx":
    int} for the best match, or None if no sheet matches.
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise TrafficParseError(f"Could not open this Excel file: {exc}") from exc

    best_match = None
    for sheet_name in xl.sheet_names:
        try:
            preview = pd.read_excel(xl, sheet_name=sheet_name, header=None, nrows=10)
        except Exception:
            continue

        for row_idx in range(len(preview)):
            row_values = [str(v).strip().lower() for v in preview.iloc[row_idx].tolist() if pd.notna(v)]
            has_date = any(v in COLUMN_ALIASES["date"] for v in row_values)
            has_location = any(v in COLUMN_ALIASES["location"] for v in row_values)
            has_traffic = any(v in COLUMN_ALIASES["traffic"] for v in row_values)
            if has_date and has_location and has_traffic:
                score = 3 + sum(1 for v in row_values if v in COLUMN_ALIASES["terminal"])
                if best_match is None or score > best_match["score"]:
                    best_match = {"sheet_name": sheet_name, "header_row_idx": row_idx, "score": score}
                break

    if best_match is not None:
        best_match.pop("score", None)
    return best_match


def parse_traffic_simple(file_obj, match: dict) -> pd.DataFrame:
    """Parse one sheet already identified by detect_traffic_sheet() as the simple flat format."""
    raw = pd.read_excel(file_obj, sheet_name=match["sheet_name"], engine="openpyxl", header=None)
    header_row_idx = match["header_row_idx"]
    df = raw.iloc[header_row_idx + 1 :].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row_idx]]
    df = df.reset_index(drop=True)

    column_map = {target: _find_column(df, aliases) for target, aliases in COLUMN_ALIASES.items()}

    missing_required = [k for k in ("date", "location", "traffic") if column_map.get(k) is None]
    if missing_required:
        raise TrafficParseError(
            f"The traffic sheet '{match['sheet_name']}' is missing required column(s): "
            f"{missing_required}. Found columns: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df[column_map["date"]], errors="coerce").dt.date
    out["location"] = df[column_map["location"]].astype(str).str.strip().map(_normalize_location)
    out["traffic"] = pd.to_numeric(df[column_map["traffic"]], errors="coerce")
    if column_map.get("terminal") is not None:
        out["terminal"] = df[column_map["terminal"]].apply(_clean_terminal_value)
    else:
        out["terminal"] = ""
    out["granularity"] = "daily"
    out["period_end"] = pd.NaT

    out = out.dropna(subset=["date", "traffic"])
    out = out[out["location"] != ""]
    out = out.drop_duplicates(subset=["date", "location", "terminal"], keep="last")
    out = out.reset_index(drop=True)

    if out.empty:
        raise TrafficParseError(
            f"The traffic sheet '{match['sheet_name']}' was recognized, but no usable "
            f"rows were found after cleaning (check Date/Traffic values aren't blank)."
        )

    return out


def parse_traffic_auto(file_obj, source_file: str = "traffic.xlsx") -> pd.DataFrame:
    """
    Universal entry point: tries the cross-tab format first (per-location
    sheets with Terminal x Dep./Arr. columns — both monthly and daily
    grain), then the Domestic/International x PAX/Flights cross-tab
    format, then falls back to the simple flat-table format. Raises
    TrafficParseError with a clear, specific message if no format matches
    any sheet in the workbook.
    """
    # Collect results from all parsers — a single workbook may contain sheets
    # for multiple airports in different formats (e.g. DEL as cross-tab,
    # HYD as side-by-side Domestic/International, GOA as dom/intl PAX grid).
    all_parts = []

    try:
        cross_tab_result = parse_cross_tab_auto(file_obj, source_file)
        if cross_tab_result is not None and not cross_tab_result.empty:
            all_parts.append(cross_tab_result)
    except TrafficParseError:
        pass

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        dom_intl_result = _parse_all_dom_intl_sheets(file_obj)
        if dom_intl_result is not None and not dom_intl_result.empty:
            all_parts.append(dom_intl_result)
    except TrafficParseError:
        pass

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        hyd_result = _parse_hyd_side_by_side(file_obj)
        if hyd_result is not None and not hyd_result.empty:
            all_parts.append(hyd_result)
    except Exception:
        pass

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        goa_result = _parse_goa_side_by_side(file_obj)
        if goa_result is not None and not goa_result.empty:
            all_parts.append(goa_result)
    except Exception:
        pass

    # Try flat-table export format (DOM_ARR_PAX / INT_ARR_PAX columns)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        flat_result = _parse_flat_export_format(file_obj)
        if flat_result is not None and not flat_result.empty:
            all_parts.append(flat_result)
    except Exception:
        pass

    if all_parts:
        combined = pd.concat(all_parts, ignore_index=True)
        # Drop rows with clearly invalid dates (year < 2000) — these are
        # artifacts from the day/month-swap correction failing on ambiguous
        # cells (e.g. year=1 from pd.to_datetime on a mangled date string).
        combined = combined[combined["date"].apply(
            lambda d: d.year >= 2000 if hasattr(d, "year") else True
        )]
        combined = combined.drop_duplicates(
            subset=["date", "location", "terminal", "granularity"], keep="last"
        ).reset_index(drop=True)
        return combined

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    match = detect_traffic_sheet(file_obj)
    if match is None:
        try:
            sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
        except Exception:
            sheet_names = []
        raise TrafficParseError(
            "Could not recognize this workbook as any supported traffic format: "
            "a simple flat table (Date, Location, Traffic), a per-location "
            "cross-tab (one sheet per airport, with Terminal x Dep./Arr. "
            "columns), or a Domestic/International x PAX/Flights cross-tab "
            f"(one sheet per airport). Sheets found: {sheet_names}."
        )

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return parse_traffic_simple(file_obj, match)


# ---------------------------------------------------------------------------
# Cross-tab format: per-location sheet, Terminal x Dep./Arr. columns,
# one row per month OR one row per day-of-month
# ---------------------------------------------------------------------------

def parse_cross_tab_auto(file_obj, source_file: str = "traffic.xlsx") -> Optional[pd.DataFrame]:
    """
    Try to parse every sheet in the workbook as the cross-tab format,
    auto-detecting each sheet's location (from its name) and granularity
    (monthly vs daily, from whether column 0's data rows look like month
    labels like "Apr'24" or small integers like day-of-month 1-31).

    Returns a combined DataFrame across all matching sheets, or None if no
    sheet in the workbook matches this format at all.
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise TrafficParseError(f"Could not open this Excel file: {exc}") from exc

    all_rows = []
    for sheet_name in xl.sheet_names:
        try:
            raw = pd.read_excel(xl, sheet_name=sheet_name, header=None)
        except Exception:
            continue

        layout = _detect_cross_tab_layout(raw)
        if layout is None:
            continue

        location = _resolve_location_for_sheet(sheet_name, raw, layout)
        if location is None:
            continue

        granularity, anchor_year_month = _detect_cross_tab_granularity(raw, layout, sheet_name=sheet_name)
        if granularity == "monthly":
            parsed = _parse_cross_tab_monthly(raw, layout, location)
        elif granularity == "daily":
            parsed = _parse_cross_tab_daily(raw, layout, location, anchor_year_month)
        else:
            continue

        if parsed is not None and not parsed.empty:
            all_rows.append(parsed)

    if not all_rows:
        return None

    combined = pd.concat(all_rows, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=["date", "location", "terminal", "granularity"], keep="last"
    )
    return combined.reset_index(drop=True)


def _find_reference_year_month(raw: pd.DataFrame, sheet_name: str = "") -> Optional[tuple]:
    """
    Pull a (year, month) reference from the sheet's title cell or sheet
    name — used by the Domestic/International PAX parser to detect and
    correct day/month-swapped dates.

    Checks, in order:
      1. All cells in row 0 as actual date/datetime objects — picks the
         LATEST year/month found (not just A1). Goa sheets have two side-
         by-side sections; A1 holds the prior-year anchor (e.g. 2025-07-01)
         while the current-year anchor (e.g. 2026-07-01) is in col 10.
         Taking the latest avoids misidentifying 2025 as the ref year.
      2. Cell A1 as a month-label string via _parse_month_label.
      3. The sheet name itself via _parse_month_label.
    """
    if raw.shape[0] > 0 and raw.shape[1] > 0:
        # Scan entire first row for date cells; keep the latest year/month.
        best_ym: Optional[tuple] = None
        for ci in range(raw.shape[1]):
            cell = raw.iat[0, ci]
            if isinstance(cell, (dt.datetime, dt.date)):
                ym = (cell.year, cell.month)
                if best_ym is None or ym > best_ym:
                    best_ym = ym
        if best_ym is not None:
            return best_ym
        # Fall back to month-label string in A1
        title_cell = raw.iat[0, 0]
        if pd.notna(title_cell):
            parsed = _parse_month_label(str(title_cell))
            if parsed is not None:
                return parsed

    if sheet_name:
        # Try the whole name then trailing tokens (same logic as _infer_month_from_context).
        parsed = _parse_month_label(sheet_name.strip())
        if parsed is not None:
            return parsed
        parts = re.split(r"[\s\-_]+", sheet_name.strip())
        for start in range(len(parts) - 1, 0, -1):
            parsed = _parse_month_label(" ".join(parts[start:]))
            if parsed is not None:
                return parsed

    return None


def _correct_possible_day_month_swap(date_val, reference_year_month: Optional[tuple]):
    """
    Returns a corrected date if `date_val`'s (year, month) doesn't match
    `reference_year_month` but swapping day and month would fix it;
    otherwise returns `date_val` unchanged. No-ops if there's no
    reference month to check against, or if the day/month swap isn't
    even a valid date (e.g. day > 12, so it can't be misread as a month).
    """
    if reference_year_month is None or not isinstance(date_val, (dt.datetime, dt.date)):
        return date_val
    if (date_val.year, date_val.month) == reference_year_month:
        return date_val
    if date_val.day > 12:
        return date_val  # can't be a swapped month value (max month is 12)
    try:
        swapped = dt.date(date_val.year, date_val.day, date_val.month)
    except ValueError:
        return date_val
    if (swapped.year, swapped.month) == reference_year_month:
        return swapped
    return date_val


# Type banner labels — checked as substring so "hyd domestic" and
# "hyd international" match the same way as bare "domestic"/"international".
_DOM_INTL_TYPE_LABELS = {"domestic", "international"}
_DOM_INTL_TYPE_SUBSTRINGS = ("domestic", "international")  # for substring matching
_DOM_INTL_ARRDEP_LABELS = {"arrival", "arr", "arr.", "departure", "dep", "dep."}


def detect_domestic_intl_pax_sheet(file_obj) -> Optional[dict]:
    """
    Scan every sheet for the Domestic/International x PAX/Flights x
    Arrival/Departure cross-tab format: a 3-row header —
      Row A: "Domestic" / "International" banners
      Row B: "PAX" / "Flights" banners under each
      Row C: "Date" in column 0, "Arrival"/"Departure" under each PAX/
             Flights group
    Only the PAX columns are used for `pax_columns` — Flights (aircraft
    movements) isn't a metric this app tracks as "traffic" (visitor
    footfall). Location is resolved the same way as the existing
    cross-tab format: from the sheet name (e.g. a sheet named "Goa" or
    "GOX"), falling back to cell A1 — this format has no location
    marker of its own anywhere else in the sheet, so (as with the
    existing cross-tab format) the sheet genuinely needs to be named
    after the airport for the file to be recognized at all.

    Returns {"sheet_name", "arrdep_row_idx", "data_start_row_idx",
    "location", "pax_columns": [(type_label, col_idx), ...],
    "reference_year_month": (year, month) or None} or None if no sheet
    matches.
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise TrafficParseError(f"Could not open this Excel file: {exc}") from exc

    for sheet_name in xl.sheet_names:
        try:
            raw = pd.read_excel(xl, sheet_name=sheet_name, header=None, nrows=10)
        except Exception:
            continue

        for row_idx in range(1, min(8, len(raw)) - 1):
            first_cell = raw.iat[row_idx, 0]
            if pd.isna(first_cell) or str(first_cell).strip().lower() != "date":
                continue

            # "Date" shares its row with the PAX/Flights metric banner (a
            # vertical merge in the source spans this cell down into the
            # Arrival/Departure row below it, so in the flattened
            # header=None read, "Date" and "PAX"/"Flights" land on the
            # same row, with Arrival/Departure one row further down —
            # not the same row "Date" is on, despite visually looking
            # like a single combined header block).
            metric_row = raw.iloc[row_idx].tolist()
            type_row = raw.iloc[row_idx - 1].tolist()
            arrdep_row_idx = row_idx + 1
            if arrdep_row_idx >= len(raw):
                continue
            arrdep_row_values = [
                str(v).strip().lower() for v in raw.iloc[arrdep_row_idx].tolist() if pd.notna(v)
            ]
            arrdep_count = sum(1 for v in arrdep_row_values if v in _DOM_INTL_ARRDEP_LABELS)
            if arrdep_count < 2:
                continue

            metric_values = {str(v).strip().lower() for v in metric_row if pd.notna(v)}
            type_values = {str(v).strip().lower() for v in type_row if pd.notna(v)}
            # Use substring matching so "hyd domestic"/"hyd international" also match
            type_matched = any(
                any(sub in tv for sub in _DOM_INTL_TYPE_SUBSTRINGS)
                for tv in type_values
            )
            if "pax" not in metric_values and "pax(schedule)" not in " ".join(metric_values):
                # Also check for "PAX(Schedule)" style headers
                pax_found = any("pax" in mv for mv in metric_values)
                if not pax_found:
                    continue
            if not type_matched:
                continue

            n_cols = raw.shape[1]
            arrdep_row = raw.iloc[arrdep_row_idx].tolist()

            def _classify_type(tv: str) -> str | None:
                tv = tv.strip().lower()
                if "domestic" in tv:
                    return "Domestic"
                if "international" in tv:
                    return "International"
                return None

            current_type, current_metric = None, None
            for seed_col in (0,):
                t_val = type_row[seed_col] if seed_col < len(type_row) else None
                if pd.notna(t_val):
                    ct = _classify_type(str(t_val))
                    if ct:
                        current_type = ct

            pax_columns = []
            for col_idx in range(1, n_cols):
                t_val = type_row[col_idx] if col_idx < len(type_row) else None
                m_val = metric_row[col_idx] if col_idx < len(metric_row) else None
                a_val = arrdep_row[col_idx] if col_idx < len(arrdep_row) else None

                if pd.notna(t_val):
                    ct = _classify_type(str(t_val))
                    if ct:
                        current_type = ct
                if pd.notna(m_val):
                    mv = str(m_val).strip().lower()
                    if mv in ("pax", "flights"):
                        current_metric = mv
                if (
                    pd.notna(a_val)
                    and current_type is not None
                    and current_metric == "pax"
                    and str(a_val).strip().lower() in _DOM_INTL_ARRDEP_LABELS
                ):
                    pax_columns.append((current_type, col_idx))

            if not pax_columns:
                continue

            location = _resolve_location_for_sheet(sheet_name, raw, {})
            if location is None:
                continue

            return {
                "sheet_name": sheet_name,
                "arrdep_row_idx": arrdep_row_idx,
                "data_start_row_idx": arrdep_row_idx + 1,
                "location": location,
                "pax_columns": pax_columns,
                "reference_year_month": _find_reference_year_month(raw, sheet_name=sheet_name),
            }
    return None


def _parse_goa_side_by_side(file_obj) -> Optional[pd.DataFrame]:
    """
    Parse Goa traffic files that have TWO side-by-side month sections:
      LEFT (C0-C9):   Prior year data  (e.g. June 2025)
      RIGHT (C10-C19): Current year data (e.g. June 2026)

    Each section has the same layout:
      R1: 'Domestic' | 'International'
      R2: Date | PAX | Flights | PAX | Flights
      R3: Arr | Dep | Arr | Dep | Arr | Dep | Arr | Dep
      R4+: day_num | Dom_Arr | Dom_Dep | ... | Int_Arr | Int_Dep | ...
      last row: 'Full Month' with totals

    We parse the section with the LATER reference year/month.
    Returns long-format df with location='Goa'.
    """
    import datetime as _dt, re
    try:
        xl = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception:
        return None

    goa_sheets = [s for s in xl.sheet_names
                  if any(k in s.lower() for k in ('goa', 'gox'))]
    if not goa_sheets:
        return None

    all_rows = []
    for sheet_name in goa_sheets:
        raw = xl.parse(sheet_name, header=None)
        if raw.shape[0] < 5 or raw.shape[1] < 5:
            continue

        # Detect all sections: look for 'Domestic' banner in first 3 rows
        # Each section starts at a column offset where 'Domestic' appears
        section_starts = []
        for ri in range(min(3, len(raw))):
            for ci in range(raw.shape[1]):
                v = raw.iloc[ri, ci]
                if pd.notna(v) and isinstance(v, str) and 'domestic' in v.lower():
                    section_starts.append((ri, ci))

        if not section_starts:
            continue

        # Parse each section and keep the one with the latest year/month
        best_df = None
        best_ym = (0, 0)

        for banner_ri, start_ci in section_starts:
            # Section layout: rows banner_ri, banner_ri+1, banner_ri+2 = headers
            if banner_ri + 3 >= len(raw):
                continue
            type_row  = raw.iloc[banner_ri].tolist()
            metric_row = raw.iloc[banner_ri + 1].tolist()
            arrdep_row = raw.iloc[banner_ri + 2].tolist()
            data_start = banner_ri + 3

            # Find PAX Arr/Dep column indices within this section
            current_type = None
            pax_arr_cols_s = {}
            pax_dep_cols_s = {}
            date_col_s = start_ci

            for ci in range(start_ci, min(start_ci + 12, len(metric_row))):
                t_val = type_row[ci] if ci < len(type_row) else None
                if pd.notna(t_val) and isinstance(t_val, str):
                    tv = t_val.strip().lower()
                    if 'domestic' in tv:
                        current_type = 'Domestic'
                    elif 'international' in tv:
                        current_type = 'International'

                m_val = str(metric_row[ci]).strip().lower() if pd.notna(metric_row[ci]) else ''
                a_val = str(arrdep_row[ci]).strip().lower() if ci < len(arrdep_row) and pd.notna(arrdep_row[ci]) else ''

                if 'date' in m_val or 'date' in a_val:
                    date_col_s = ci

                if 'pax' in m_val and current_type:
                    # scan ahead for Arr/Dep sub-headers
                    # Strip \xa0 (non-breaking space) before comparing
                    for offset in range(0, 2):  # only ci and ci+1
                        sc = ci + offset
                        if sc >= len(arrdep_row): break
                        av = str(arrdep_row[sc]).replace('\xa0', '').strip().lower() \
                            if pd.notna(arrdep_row[sc]) else ''
                        if av in ('arrival', 'arr', 'arr.'):
                            pax_arr_cols_s[current_type] = sc
                        elif av in ('departure', 'dep', 'dep.'):
                            pax_dep_cols_s[current_type] = sc

            if not pax_arr_cols_s and not pax_dep_cols_s:
                continue

            # Infer reference year/month — check R0 of this section first
            # (the Goa file has a datetime(2026,6,1) at R0 of each section).
            ref_ym = None
            for check_ri in range(min(3, len(raw))):
                if start_ci >= raw.shape[1]:
                    break
                cell = raw.iloc[check_ri, start_ci]
                if pd.isna(cell):
                    continue
                import datetime as _dtmod
                if isinstance(cell, (_dtmod.datetime, _dtmod.date)):
                    ref_ym = (cell.year, cell.month)
                    break
                parsed = _parse_month_label(str(cell))
                if parsed:
                    ref_ym = parsed
                    break
            # Fallback: sheet name
            if ref_ym is None:
                ref_ym = _find_reference_year_month(raw, sheet_name=sheet_name)

            # Parse data rows — handle three date formats found in Goa sheets:
            #  1. Day number integer (original format): 1, 2, … 31
            #  2. datetime object with day/month swapped by Excel: 2026-01-07
            #     means July 1 not January 7 — corrected using ref_ym.
            #  3. String date in MM/DD/YYYY or DD-MM-YYYY format: '07-13-2026'
            records = []
            for ri in range(data_start, len(raw)):
                day_label = raw.iloc[ri, date_col_s] if date_col_s < raw.shape[1] else None

                if pd.isna(day_label):
                    continue
                if isinstance(day_label, str) and day_label.strip().lower() in (
                    'full month', 'total', 'avg. daily', 'mid month',
                    '2024-25', '2025-26', '2025-2026',
                ):
                    continue

                row_date = None

                # Format 1: day-of-month integer
                if _looks_like_day_number(day_label):
                    if ref_ym is None:
                        continue
                    year, month = ref_ym
                    day_num = int(float(day_label))
                    last_day = calendar.monthrange(year, month)[1]
                    if day_num > last_day:
                        continue
                    row_date = _dt.date(year, month, day_num)

                # Format 2: datetime/date object — Excel may have swapped day/month
                elif isinstance(day_label, (_dt.datetime, _dt.date)):
                    d = day_label.date() if isinstance(day_label, _dt.datetime) else day_label
                    if ref_ym is not None:
                        ref_year, ref_month = ref_ym
                        # If the parsed month != ref_month but parsed day == ref_month,
                        # Excel did a DD/MM → MM/DD swap: swap back.
                        if d.month != ref_month and d.day == ref_month:
                            try:
                                d = _dt.date(d.year, d.day, d.month)
                            except ValueError:
                                pass
                    row_date = d

                # Format 3: string like '07-13-2026' or '07/13/2026'
                elif isinstance(day_label, str):
                    s = day_label.strip()
                    if s.lower() in ('full month', 'total', 'avg. daily', 'mid month',
                                     '2024-25', '2025-26'):
                        continue
                    try:
                        parsed_dt = pd.to_datetime(s, dayfirst=False, errors='raise')
                        row_date = parsed_dt.date()
                        # Apply same day/month swap correction
                        if ref_ym is not None:
                            ref_year, ref_month = ref_ym
                            if row_date.month != ref_month and row_date.day == ref_month:
                                try:
                                    row_date = _dt.date(row_date.year, row_date.day, row_date.month)
                                except ValueError:
                                    pass
                    except Exception:
                        continue

                if row_date is None:
                    continue

                for type_label in ('Domestic', 'International'):
                    total = 0.0
                    found = False
                    for ci in [pax_arr_cols_s.get(type_label), pax_dep_cols_s.get(type_label)]:
                        if ci is None: continue
                        val = raw.iloc[ri, ci] if ci < raw.shape[1] else None
                        num = _parse_traffic_number(val)
                        if num is not None:
                            total += num
                            found = True
                    if found:
                        records.append({
                            'date': row_date, 'period_end': pd.NaT,
                            'granularity': 'daily', 'location': 'Goa',
                            'terminal': type_label, 'traffic': total,
                        })

            if records and ref_ym > best_ym:
                df = pd.DataFrame.from_records(records)
                df = df.groupby(['date','period_end','granularity','location','terminal'],
                                as_index=False, dropna=False)['traffic'].sum()
                best_df = df
                best_ym = ref_ym

        if best_df is not None:
            all_rows.append(best_df)

    if not all_rows:
        return None
    return pd.concat(all_rows, ignore_index=True).drop_duplicates(
        subset=['date','location','terminal','granularity'], keep='last'
    ).reset_index(drop=True)



def _parse_flat_export_format(file_obj) -> Optional[pd.DataFrame]:
    """
    Parse flat-table traffic exports with columns like:
        DATE, DOM_ARR_PAX, DOM_DEP_PAX, DOM_TOTAL_PAX,
        INT_ARR_PAX, INT_DEP_PAX, TOTAL_INT_PAX, ...

    This format is produced by airport systems (e.g. Hyderabad RGIA export).
    PAX = DOM_ARR_PAX + DOM_DEP_PAX  → terminal='Domestic'
    PAX = INT_ARR_PAX + INT_DEP_PAX  → terminal='International'
    Location is inferred from sheet name or defaults to 'Hyderabad'.
    """
    import datetime as _dt2
    try:
        xl_check = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception:
        return None

    # Must have exactly one sheet named 'Export' or similar flat structure
    # Detect by checking header row for DOM_ARR_PAX / INT_ARR_PAX columns
    all_dfs = []
    for sheet_name in xl_check.sheet_names:
        raw = xl_check.parse(sheet_name, header=None, nrows=2)
        if raw.empty:
            continue
        header_row = [str(v).strip().upper() for v in raw.iloc[0].tolist() if v is not None]
        has_dom = any('DOM_ARR_PAX' in h or 'DOM_DEP_PAX' in h for h in header_row)
        has_int = any('INT_ARR_PAX' in h or 'INT_DEP_PAX' in h for h in header_row)
        if not (has_dom and has_int):
            continue

        # Found a matching sheet — read fully
        df = xl_check.parse(sheet_name, header=0)
        df.columns = [str(c).strip().upper() for c in df.columns]

        # Infer location from sheet name
        sn = sheet_name.lower()
        if 'hyd' in sn or 'hyderabad' in sn or 'rgia' in sn:
            location = 'Hyderabad'
        elif 'goa' in sn or 'manohar' in sn:
            location = 'Goa'
        elif 'del' in sn or 'igi' in sn or 'delhi' in sn:
            location = 'Delhi'
        else:
            location = 'Hyderabad'  # default for this format

        records = []
        for _, row in df.iterrows():
            date_val = row.get('DATE')
            if date_val is None or not isinstance(date_val, (_dt2.datetime, _dt2.date)):
                continue
            row_date = date_val.date() if isinstance(date_val, _dt2.datetime) else date_val

            dom  = float(row.get('DOM_ARR_PAX', 0) or 0) + float(row.get('DOM_DEP_PAX', 0) or 0)
            intl = float(row.get('INT_ARR_PAX', 0) or 0) + float(row.get('INT_DEP_PAX', 0) or 0)

            if dom > 0:
                records.append({
                    'date': row_date, 'period_end': pd.NaT,
                    'granularity': 'daily', 'location': location,
                    'terminal': 'Domestic', 'traffic': dom,
                })
            if intl > 0:
                records.append({
                    'date': row_date, 'period_end': pd.NaT,
                    'granularity': 'daily', 'location': location,
                    'terminal': 'International', 'traffic': intl,
                })

        if records:
            all_dfs.append(pd.DataFrame.from_records(records))

    if not all_dfs:
        return None
    return pd.concat(all_dfs, ignore_index=True)

def _parse_hyd_side_by_side(file_obj) -> Optional[pd.DataFrame]:
    """
    Parse Hyderabad traffic files with the split side-by-side layout:
      LEFT:  HYD Domestic  — DATE col, ATMs cols, PAX Arr/Dep/Total cols
      RIGHT: HYD International — DATE col, ATMs cols, PAX Arr/Dep/Total cols

    Structure (HYD Traffic June-26):
      R1:  'HYD Domestic'  (cols 0-7) | 'HYD International' (cols 8-14)
      R2:  DATE | ATMs | PAX (Domestic schedule)  | DATE | ATMs | PAX (Intl)
      R3:  ARRIVAL | DEPARTURE | TOTAL | ...
      R4+: date | arr_atm | dep_atm | tot_atm | arr_pax | dep_pax | tot_pax | ...
      R40: 'Total' row with monthly sums

    Returns long-format df with location=Hyderabad, terminals=
      'Domestic' (Dom Arr+Dep) and 'International' (Int Arr+Dep),
    or None if the sheet doesn't match this layout.
    """
    import datetime as _dt
    try:
        xl = pd.ExcelFile(file_obj, engine='openpyxl')
    except Exception:
        return None

    hyd_sheets = [s for s in xl.sheet_names if 'hyd' in s.lower()]
    if not hyd_sheets:
        return None

    all_rows = []
    for sheet_name in hyd_sheets:
        raw = xl.parse(sheet_name, header=None)
        if raw.shape[0] < 5 or raw.shape[1] < 4:
            continue  # need at least date + dom arr/dep + int arr/dep

        # Detect column layout — works for both formats:
        #   Old: "HYD Domestic" / "HYD International" on same banner row
        #   New: "DOMESTIC PAX (Schedule)" / "International PAX (Schedule)" on separate rows
        # Strategy: scan first 4 rows, find arrdep row (has Arrival/Departure),
        # then look upward from each column to find its type (Domestic/International).
        import datetime as _dt_hyd
        pax_arr_cols = {}
        pax_dep_cols = {}
        date_col_s   = 0
        arrdep_ri    = None
        data_start   = 3  # fallback

        header_rows_h = {}
        for _ri in range(min(4, len(raw))):
            header_rows_h[_ri] = raw.iloc[_ri].tolist()

        # Find arrdep row — has both 'arrival' and 'departure'
        for _ri, _vals in header_rows_h.items():
            _strs = [str(v).replace('\xa0','').strip().lower() for v in _vals if pd.notna(v)]
            if any(s in ('arrival','arr') for s in _strs) and any(s in ('departure','dep') for s in _strs):
                arrdep_ri = _ri
                data_start = _ri + 1
                break

        if arrdep_ri is None:
            continue

        arrdep_row = header_rows_h[arrdep_ri]
        n_cols_h   = raw.shape[1]

        # For each column, walk upward from arrdep_ri to find type label
        def _classify_type_hyd(v: str) -> str | None:
            s = v.strip().lower()
            if 'domestic' in s: return 'Domestic'
            if 'international' in s: return 'International'
            return None

        col_type_h = {}
        for _ri in range(0, arrdep_ri):
            for _ci in range(n_cols_h):
                v = header_rows_h[_ri][_ci] if _ci < len(header_rows_h[_ri]) else None
                if pd.notna(v) and isinstance(v, str):
                    ct = _classify_type_hyd(v)
                    if ct:
                        # This column and forward columns inherit this type
                        # (forward-fill within the row)
                        for _fc in range(_ci, n_cols_h):
                            if _fc not in col_type_h:
                                col_type_h[_fc] = ct
                            next_v = header_rows_h[_ri][_fc] if _fc < len(header_rows_h[_ri]) else None
                            if _fc > _ci and pd.notna(next_v) and isinstance(next_v, str) and _classify_type_hyd(next_v):
                                break  # stop at next type label

        # Find date col (column 0 usually has datetime values in data rows)
        for _ci in range(n_cols_h):
            sample = raw.iloc[data_start, _ci] if data_start < len(raw) and _ci < raw.shape[1] else None
            if sample is not None and isinstance(sample, (_dt_hyd.datetime, _dt_hyd.date)):
                date_col_s = _ci
                break

        # Map arr/dep columns per type using arrdep_row
        for _ci in range(n_cols_h):
            _t = col_type_h.get(_ci)
            if not _t:
                continue
            _a = str(arrdep_row[_ci]).replace('\xa0','').strip().lower()                 if _ci < len(arrdep_row) and pd.notna(arrdep_row[_ci]) else ''
            if _a in ('arrival', 'arr', 'arr.'):
                pax_arr_cols[_t] = _ci
            elif _a in ('departure', 'dep', 'dep.'):
                pax_dep_cols[_t] = _ci

        if not pax_arr_cols and not pax_dep_cols:
            continue

        records = []
        for ri in range(data_start, len(raw)):
            date_label = raw.iloc[ri, date_col_s]

            # Stop at summary rows
            if pd.notna(date_label) and isinstance(date_label, str):
                if date_label.strip().lower() in ('total', 'full month', 'avg. daily', 'average'):
                    break
                continue

            if pd.isna(date_label):
                continue

            if isinstance(date_label, (_dt.datetime, _dt.date)):
                row_date = date_label.date() if isinstance(date_label, _dt.datetime) else date_label
            else:
                continue

            # Use the date exactly as stored in the sheet — no correction.
            # Sheet names can be irrelevant; the cell dates are the source of truth.

            for type_label in ('Domestic', 'International'):
                arr_ci = pax_arr_cols.get(type_label)
                dep_ci = pax_dep_cols.get(type_label)
                total = 0.0
                found = False
                for ci in [arr_ci, dep_ci]:
                    if ci is None:
                        continue
                    val = raw.iloc[ri, ci] if ci < raw.shape[1] else None
                    num = _parse_traffic_number(val)
                    if num is not None:
                        total += num
                        found = True
                if found:
                    records.append({
                        'date': row_date, 'period_end': pd.NaT,
                        'granularity': 'daily', 'location': 'Hyderabad',
                        'terminal': type_label, 'traffic': total,
                    })

        if records:
            df = pd.DataFrame.from_records(records)
            df = df.groupby(['date','period_end','granularity','location','terminal'],
                            as_index=False, dropna=False)['traffic'].sum()
            all_rows.append(df)

    if not all_rows:
        return None
    return pd.concat(all_rows, ignore_index=True).drop_duplicates(
        subset=['date','location','terminal','granularity'], keep='last'
    ).reset_index(drop=True)


def _parse_all_dom_intl_sheets(file_obj) -> Optional[pd.DataFrame]:
    """
    Scan every sheet in the workbook for the Domestic/International PAX
    format (in either the normal column-oriented or transposed row-oriented
    layout), parse each matching sheet, and return the combined result.

    This mirrors parse_cross_tab_auto's all-sheets approach: the old path
    of calling detect_domestic_intl_pax_sheet (which returns only the FIRST
    matching sheet) caused all sheets after the first to be silently
    dropped. This function fixes that by iterating every sheet independently.

    Layout support:
      - Column-oriented (date in col-0, one row per day) → normal parser
      - Transposed (dates across row-5 as column headers, one row per
        metric) → _parse_transposed_dom_intl_sheet fallback
    """
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise TrafficParseError(f"Could not open this Excel file: {exc}") from exc

    all_frames: list[pd.DataFrame] = []

    for sheet_name in xl.sheet_names:
        try:
            raw_preview = xl.parse(sheet_name, header=None, nrows=10)
        except Exception:
            continue

        # Re-use the existing per-sheet detector (it only reads the first 10
        # rows so it's cheap, and it handles location resolution for us).
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        # Build a single-sheet BytesIO proxy isn't necessary — detect works on
        # the original file_obj since it scans all sheets and returns first
        # match; but we need it scoped to *this* sheet.  Re-detect by scanning
        # just this sheet via a minimal re-implementation of the header check.
        layout = _detect_dom_intl_layout_for_sheet(raw_preview, sheet_name, xl)
        if layout is None:
            continue

        try:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            raw_full = xl.parse(sheet_name, header=None)
        except Exception:
            continue

        # Try column-oriented parse first.
        frame = _parse_dom_intl_sheet_raw(raw_full, layout)
        if frame is None or frame.empty:
            # Transposed: dates across columns.
            frame = _parse_transposed_dom_intl_sheet(raw_full, layout)
        if frame is not None and not frame.empty:
            all_frames.append(frame)

    if not all_frames:
        return None

    combined = pd.concat(all_frames, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=["date", "location", "terminal", "granularity"], keep="last"
    )
    return combined.reset_index(drop=True)


def _detect_dom_intl_layout_for_sheet(
    raw_preview: pd.DataFrame, sheet_name: str, xl
) -> Optional[dict]:
    """
    Per-sheet version of detect_domestic_intl_pax_sheet's inner loop.
    Returns the layout dict for this sheet, or None if it doesn't match.
    """
    _type_labels = {"domestic", "international"}
    _arrdep_labels = {"arrival", "arr", "arr.", "departure", "dep", "dep."}

    for row_idx in range(1, min(8, len(raw_preview)) - 1):
        first_cell = raw_preview.iat[row_idx, 0]
        if pd.isna(first_cell) or str(first_cell).strip().lower() != "date":
            continue

        metric_row = raw_preview.iloc[row_idx].tolist()
        type_row = raw_preview.iloc[row_idx - 1].tolist()
        arrdep_row_idx = row_idx + 1
        if arrdep_row_idx >= len(raw_preview):
            continue
        arrdep_vals = [
            str(v).strip().lower()
            for v in raw_preview.iloc[arrdep_row_idx].tolist()
            if pd.notna(v)
        ]
        if sum(1 for v in arrdep_vals if v in _arrdep_labels) < 2:
            continue

        metric_values = {str(v).strip().lower() for v in metric_row if pd.notna(v)}
        type_values = {str(v).strip().lower() for v in type_row if pd.notna(v)}
        type_matched = any(
            any(sub in tv for sub in ("domestic", "international"))
            for tv in type_values
        )
        pax_found = any("pax" in mv for mv in metric_values)
        if not pax_found or not type_matched:
            continue

        n_cols = raw_preview.shape[1]
        arrdep_row = raw_preview.iloc[arrdep_row_idx].tolist()
        current_type, current_metric = None, None

        def _cls(tv: str) -> str | None:
            tv = tv.strip().lower()
            if "domestic" in tv: return "Domestic"
            if "international" in tv: return "International"
            return None

        # Seed from col 0 (type banner may start there due to merged cells).
        if pd.notna(type_row[0]):
            ct = _cls(str(type_row[0]))
            if ct:
                current_type = ct

        pax_columns = []
        for col_idx in range(1, n_cols):
            t_val = type_row[col_idx] if col_idx < len(type_row) else None
            m_val = metric_row[col_idx] if col_idx < len(metric_row) else None
            a_val = arrdep_row[col_idx] if col_idx < len(arrdep_row) else None
            if pd.notna(t_val):
                ct = _cls(str(t_val))
                if ct:
                    current_type = ct
            if pd.notna(m_val):
                mv = str(m_val).strip().lower()
                if mv in ("pax", "flights"):
                    current_metric = mv
            if (pd.notna(a_val) and current_type is not None
                    and current_metric == "pax"
                    and str(a_val).strip().lower() in _arrdep_labels):
                pax_columns.append((current_type, col_idx))

        if not pax_columns:
            continue

        location = _resolve_location_for_sheet(sheet_name, raw_preview, {})
        if location is None:
            continue

        return {
            "sheet_name": sheet_name,
            "arrdep_row_idx": arrdep_row_idx,
            "data_start_row_idx": arrdep_row_idx + 1,
            "location": location,
            "pax_columns": pax_columns,
            "reference_year_month": _find_reference_year_month(raw_preview, sheet_name=sheet_name),
        }
    return None


def _parse_dom_intl_sheet_raw(raw: pd.DataFrame, layout: dict) -> Optional[pd.DataFrame]:
    """
    Column-oriented DOM/INTL parse: col-0 = date, one data row per day.
    Extracted from parse_domestic_intl_pax_sheet so both orientations
    can be tried without re-reading the file.
    """
    reference_year_month = layout["reference_year_month"]
    records = []

    for row_idx in range(layout["data_start_row_idx"], raw.shape[0]):
        date_val = raw.iat[row_idx, 0]
        resolved: Optional[dt.date] = None

        if isinstance(date_val, (dt.datetime, dt.date)):
            d = date_val.date() if isinstance(date_val, dt.datetime) else date_val
            resolved = _correct_possible_day_month_swap(d, reference_year_month)

        elif pd.notna(date_val) and _looks_like_day_number(date_val):
            if reference_year_month is not None:
                year, month = reference_year_month
                day_num = int(float(date_val))
                last_day = calendar.monthrange(year, month)[1]
                if 1 <= day_num <= last_day:
                    resolved = dt.date(year, month, day_num)

        elif pd.notna(date_val):
            parsed_ts = pd.to_datetime(date_val, errors="coerce")
            if pd.notna(parsed_ts):
                d = parsed_ts.date()
                resolved = _correct_possible_day_month_swap(d, reference_year_month)

        if resolved is None:
            continue

        totals = {"Domestic": 0.0, "International": 0.0}
        seen = {"Domestic": False, "International": False}
        for type_label, col_idx in layout["pax_columns"]:
            if col_idx >= raw.shape[1]:
                continue
            num = _parse_traffic_number(raw.iat[row_idx, col_idx])
            if num is not None:
                totals[type_label] += num
                seen[type_label] = True

        for type_label in ("Domestic", "International"):
            if not seen[type_label]:
                continue
            records.append({
                "date": resolved,
                "period_end": pd.NaT,
                "granularity": "daily",
                "location": layout["location"],
                "terminal": type_label,
                "traffic": totals[type_label],
            })

    if not records:
        return None

    df = pd.DataFrame.from_records(records)
    df = df.drop_duplicates(subset=["date", "location", "terminal", "granularity"], keep="last")
    return df.reset_index(drop=True)


def parse_domestic_intl_pax_sheet(file_obj, layout: dict) -> pd.DataFrame:
    """
    Parse the Domestic/International x PAX/Flights x Arrival/Departure
    format (see detect_domestic_intl_pax_sheet) into the standard
    date/location/terminal/traffic/granularity/period_end shape — one row
    per (date, "Domestic") and one row per (date, "International"),
    using the existing `terminal` field to carry the Domestic/
    International split rather than a physical terminal name (this
    airport has no terminal breakdown of its own; Domestic vs
    International is the breakdown that exists here instead). Each row's
    traffic is that type's Arrival + Departure PAX for that day summed
    together. Downstream, a location's total traffic for a date is still
    the sum across whatever terminal values exist for that (date,
    location) — the same aggregation every other traffic format already
    relies on — so Domestic + International correctly combine into one
    location-wide total for Penetration %/SPP without any special-casing.
    """
    sheet_name = layout["sheet_name"]
    raw = pd.read_excel(file_obj, sheet_name=sheet_name, engine="openpyxl", header=None)

    reference_year_month = layout["reference_year_month"]
    records = []
    corrected_count = 0

    for row_idx in range(layout["data_start_row_idx"], raw.shape[0]):
        date_val = raw.iat[row_idx, 0]
        resolved: Optional[dt.date] = None

        if isinstance(date_val, (dt.datetime, dt.date)):
            # Actual date object — use directly, then correct day/month swap if needed.
            d = date_val.date() if isinstance(date_val, dt.datetime) else date_val
            corrected = _correct_possible_day_month_swap(d, reference_year_month)
            if corrected != d:
                corrected_count += 1
            resolved = corrected

        elif pd.notna(date_val) and _looks_like_day_number(date_val):
            # Day-of-month integer — convert with reference month.
            if reference_year_month is not None:
                year, month = reference_year_month
                day_num = int(float(date_val))
                last_day = calendar.monthrange(year, month)[1]
                if 1 <= day_num <= last_day:
                    resolved = dt.date(year, month, day_num)

        elif pd.notna(date_val):
            # String or other — try pd.to_datetime, then correct swap.
            parsed_ts = pd.to_datetime(date_val, errors="coerce")
            if pd.notna(parsed_ts):
                d = parsed_ts.date()
                corrected = _correct_possible_day_month_swap(d, reference_year_month)
                if corrected != d:
                    corrected_count += 1
                resolved = corrected

        if resolved is None:
            continue
        date_val = resolved

        totals = {"Domestic": 0.0, "International": 0.0}
        seen = {"Domestic": False, "International": False}
        for type_label, col_idx in layout["pax_columns"]:
            if col_idx >= raw.shape[1]:
                continue
            value = raw.iat[row_idx, col_idx]
            numeric = _parse_traffic_number(value)
            if numeric is not None:
                totals[type_label] += numeric
                seen[type_label] = True

        for type_label in ("Domestic", "International"):
            if not seen[type_label]:
                continue
            records.append(
                {
                    "date": date_val,
                    "period_end": pd.NaT,
                    "granularity": "daily",
                    "location": layout["location"],
                    "terminal": type_label,
                    "traffic": totals[type_label],
                }
            )

    if not records:
        # The normal path expects col-0 = date, but this sheet is TRANSPOSED:
        # dates run across ROW 5 as column headers, and each subsequent row
        # is a metric (Dom Arr, Dom Dep, Intl Arr, Intl Dep). Try that layout.
        transposed = _parse_transposed_dom_intl_sheet(raw, layout)
        if transposed is not None and not transposed.empty:
            return transposed
        # Build a diagnostic snapshot so the exact layout is visible in the error.
        header_rows = raw.iloc[: layout["data_start_row_idx"]].to_string(max_cols=10)
        sample_data = raw.iloc[layout["data_start_row_idx"] : layout["data_start_row_idx"] + 5].to_string(max_cols=10)
        raise TrafficParseError(
            f"Sheet '{sheet_name}' was recognized as the Domestic/International PAX "
            "format, but no usable daily rows could be extracted from it. "
            f"Detected layout: data_start_row={layout['data_start_row_idx']}, "
            f"pax_columns={layout['pax_columns']}, "
            f"reference_year_month={layout['reference_year_month']}. "
            f"Header rows:\n{header_rows}\n"
            f"First data rows:\n{sample_data}"
        )

    df = pd.DataFrame.from_records(records)
    df = df.drop_duplicates(subset=["date", "location", "terminal", "granularity"], keep="last")
    df = df.reset_index(drop=True)
    return df


def _parse_transposed_dom_intl_sheet(raw: pd.DataFrame, dom_layout: dict) -> Optional[pd.DataFrame]:
    """
    Parse the transposed DOM/INTL layout where dates run across columns
    instead of down rows.

    Real-world layout (confirmed from diagnostic output):
      Row 0:  [<date>, NaN, ...]          ← title / period cell
      Row 1:  [Domestic, NaN, ..., International, NaN, ...]  ← type banners
      Row 2:  [Date, PAX, NaN, Flights, NaN, PAX, NaN, ...]  ← metric labels
      Row 3:  [NaN, Arrival, Dep., Arrival, Dep., ...]        ← Arr/Dep labels
      Row 4:  [NaN, NaN, ...]                                 ← blank spacer
      Row 5:  [NaN, NaN, <date1>, <date2>, ..., "Total (MTD)"] ← DATE HEADER ROW
      Row 6:  [NaN, NaN, <dom_arr_1>, <dom_arr_2>, ...]      ← Dom PAX Arrival per date
      Row 7:  [NaN, NaN, <dom_dep_1>, <dom_dep_2>, ...]      ← Dom PAX Departure per date
      Row 8:  [NaN, ...]                                      ← blank spacer
      Row 9:  [NaN, NaN, <int_arr_1>, <int_arr_2>, ...]      ← Intl PAX Arrival per date
      Row 10: [NaN, NaN, <int_dep_1>, <int_dep_2>, ...]      ← Intl PAX Departure per date

    Algorithm:
      1. Find the date-header row: first row after data_start with ≥3 date objects.
      2. Collect {col_idx → date} from that row (ignoring "Total" string columns).
      3. Scan subsequent rows for numeric values aligned with date columns;
         group consecutive numeric rows separated by blank/spacer rows.
      4. Assign each group a type label (Domestic, International, …) in the
         order they appear in dom_layout["pax_columns"].
      5. For each group, sum all rows (Arrival + Departure) per date column
         to produce one (date, type) traffic total.

    Returns a long-format DataFrame in the standard traffic schema, or None
    if the transposed layout cannot be detected (< 3 date columns found).
    """
    data_start = dom_layout["data_start_row_idx"]

    # 1. Find the date-header row.
    date_col_map: dict[int, dt.date] = {}
    date_header_row = -1
    for row_idx in range(data_start, min(data_start + 10, len(raw))):
        candidates: dict[int, dt.date] = {}
        for col_idx, val in enumerate(raw.iloc[row_idx].tolist()):
            if isinstance(val, (dt.datetime, dt.date)):
                candidates[col_idx] = val.date() if isinstance(val, dt.datetime) else val
        if len(candidates) >= 3:
            date_col_map = candidates
            date_header_row = row_idx
            break

    if date_header_row == -1 or not date_col_map:
        return None

    # 2. Scan data rows after the date-header row; group by consecutive
    #    non-blank runs (each group = one traffic type).
    numeric_row_groups: list[list[dict[int, float]]] = []
    current_group: list[dict[int, float]] = []

    for row_idx in range(date_header_row + 1, len(raw)):
        row = raw.iloc[row_idx].tolist()
        numeric_in_date_cols: dict[int, float] = {}
        for col_idx in date_col_map:
            if col_idx < len(row):
                val = row[col_idx]
                if isinstance(val, (int, float)) and pd.notna(val):
                    numeric_in_date_cols[col_idx] = float(val)
        if numeric_in_date_cols:
            current_group.append(numeric_in_date_cols)
        else:
            if current_group:
                numeric_row_groups.append(current_group)
                current_group = []

    if current_group:
        numeric_row_groups.append(current_group)

    if not numeric_row_groups:
        return None

    # 3. Determine type labels in legend order from pax_columns.
    seen_types: list[str] = []
    for type_label, _ in dom_layout["pax_columns"]:
        if type_label not in seen_types:
            seen_types.append(type_label)

    # 4. Build records: sum each group's rows per date column.
    records: list[dict] = []
    for group_idx, group_rows in enumerate(numeric_row_groups):
        type_label = seen_types[group_idx] if group_idx < len(seen_types) else f"Group{group_idx}"
        totals: dict[int, float] = {}
        for col_values in group_rows:
            for col_idx, val in col_values.items():
                totals[col_idx] = totals.get(col_idx, 0.0) + val
        for col_idx, total in totals.items():
            records.append({
                "date": date_col_map[col_idx],
                "period_end": pd.NaT,
                "granularity": "daily",
                "location": dom_layout["location"],
                "terminal": type_label,
                "traffic": total,
            })

    if not records:
        return None

    df = pd.DataFrame.from_records(records)
    df = df.drop_duplicates(subset=["date", "location", "terminal", "granularity"], keep="last")
    return df.reset_index(drop=True)


def _detect_cross_tab_layout(raw: pd.DataFrame) -> Optional[dict]:
    """
    Find the 2-row header (segment names + Dep./Arr.) in the first ~5 rows
    of a sheet, and build the column groups: forward-filled segment name
    paired with however many Dep./Arr. (or similarly-named) sub-columns
    follow it, skipping "Sub Totals"/"G.Total" rollup columns.

    Returns {"segment_row_idx": int, "subheader_row_idx": int,
    "data_start_row_idx": int, "column_groups": [(segment, [col_idxs])]}
    or None if no such header is found.
    """
    search_rows = min(5, len(raw))
    for row_idx in range(search_rows):
        row_values = [str(v).strip() for v in raw.iloc[row_idx].tolist() if pd.notna(v)]
        if not row_values:
            continue
        # A segment-header row has at least one non-rollup label followed
        # by a Dep./Arr. pair on the next row — look for the next row
        # being mostly "Dep."/"Arr."-like tokens.
        if row_idx + 1 >= len(raw):
            continue
        next_row = raw.iloc[row_idx + 1].tolist()
        depart_arrive_count = sum(
            1 for v in next_row if pd.notna(v) and str(v).strip().lower() in ("dep.", "dep", "arr.", "arr")
        )
        if depart_arrive_count < 2:
            continue

        segment_row = raw.iloc[row_idx].tolist()
        subheader_row = raw.iloc[row_idx + 1].tolist()
        n_cols = len(segment_row)

        # Build column groups with Dep/Arr tracked separately per segment.
        # Each entry is (segment_with_dep_arr_suffix, [col_idxs]) so that
        # e.g. "T3 International Dep." and "T3 International Arr." become
        # two distinct groups — the segment-to-terminal mapping then maps
        # each to its own terminal label (T3 Int Dep vs T3 Int Arr).
        column_groups = []
        current_segment = None
        # Track current sub-header (Dep/Arr) so we can append it to the key
        current_sub: str | None = None
        current_cols: list[int] = []
        # Detect which column is the date/day column — usually col 0, but
        # some files (e.g. DEL Traffic June-26) have a blank col 0 and the
        # actual "Day" label in col 1. Find the first col whose subheader
        # says "day"/"date"/"month", falling back to col 0.
        _date_col = 0
        for _ci in range(min(4, n_cols)):
            _sv = str(subheader_row[_ci]).strip().lower() if pd.notna(subheader_row[_ci]) else ""
            _sgv = str(segment_row[_ci]).strip().lower() if pd.notna(segment_row[_ci]) else ""
            if _sv in ("day", "date", "month") or _sgv in ("day", "date", "month"):
                _date_col = _ci
                break

        def _flush_group():
            if current_segment is not None and current_cols:
                # Compose the full key: "T3 International Dep." etc.
                # If no sub-header was seen, use the bare segment name.
                key = f"{current_segment} {current_sub}" if current_sub else current_segment
                column_groups.append((key, list(current_cols)))

        for col_idx in range(_date_col + 1, n_cols):  # skip the date/day column
            seg_val = segment_row[col_idx]
            seg_str = str(seg_val).strip() if pd.notna(seg_val) else None

            if seg_str:
                _flush_group()
                if (seg_str.lower().startswith("sub total")
                        or seg_str.lower().startswith("g.total")
                        or seg_str.lower().startswith("g total")):
                    current_segment = None
                    current_sub = None
                    current_cols = []
                    continue
                current_segment = seg_str
                current_sub = None
                current_cols = []

            sub_val = subheader_row[col_idx] if col_idx < len(subheader_row) else None
            sub_str = str(sub_val).strip() if pd.notna(sub_val) else None
            sub_lower = sub_str.lower() if sub_str else None

            if current_segment is not None and sub_lower in ("dep.", "dep", "arr.", "arr"):
                # Each Dep/Arr column is its own group so the terminal label
                # can distinguish "T3 International Dep." from "T3 International Arr."
                _flush_group()
                current_sub = sub_str
                current_cols = [col_idx]

        _flush_group()

        if not column_groups:
            continue

        return {
            "segment_row_idx": row_idx,
            "subheader_row_idx": row_idx + 1,
            "data_start_row_idx": row_idx + 2,
            "column_groups": column_groups,
            "date_col_idx": _date_col,
        }

    return None


_LOCATION_KEYWORD_SCAN: list[tuple[str, str]] = [
    # Longest / most specific patterns first so "hyderabad" beats "hyd"
    # when both could match (avoids false substring matches).
    ("hyderabad", "Hyderabad"), ("rgia", "Hyderabad"),
    ("delhi", "Delhi"), ("new delhi", "Delhi"), ("indira gandhi", "Delhi"),
    ("igia", "Delhi"),
    ("dabolim", "Goa"), ("mopa", "Goa"),
    # "goa" before the short codes so we don't need a word-boundary for it
    # (it never appears as a substring of another word in airport context).
    ("goa", "Goa"),
    # Short IATA-style codes — matched as whole words to avoid e.g.
    # "DELHI" matching "del" as a prefix.
    (r"\bdel\b", "Delhi"),
    (r"\bhyd\b", "Hyderabad"),
    (r"\bgox\b", "Goa"), (r"\bgoi\b", "Goa"),
]
_LOCATION_KEYWORD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), canon)
    for pat, canon in _LOCATION_KEYWORD_SCAN
]


def _resolve_location_for_sheet(sheet_name: str, raw: pd.DataFrame, layout: dict) -> Optional[str]:
    """
    Figure out which location a cross-tab sheet belongs to.

    Priority:
      1. Exact key match against _SHEET_NAME_TO_LOCATION (fast path for
         the clean "DEL" / "HYD" / "GOX" convention).
      2. Scan the full sheet name for any known location keyword or IATA
         code anywhere inside it — handles descriptive names like
         "Gox Traffic June 26", "DEL Traffic June-26", "Hyderabad Monthly".
      3. Cell A1 fallback (some exports put the airport code in A1).
    """
    key = sheet_name.strip().lower()

    # 1. Exact match.
    if key in _SHEET_NAME_TO_LOCATION:
        return _SHEET_NAME_TO_LOCATION[key]

    # 2. Keyword scan of the sheet name.
    for pat, canon in _LOCATION_KEYWORD_PATTERNS:
        if pat.search(sheet_name):
            return canon

    # 3. Cell A1 fallback.
    first_cell = raw.iloc[0, 0] if raw.shape[0] > 0 else None
    if pd.notna(first_cell):
        first_str = str(first_cell).strip()
        first_key = first_str.lower()
        if first_key in _SHEET_NAME_TO_LOCATION:
            return _SHEET_NAME_TO_LOCATION[first_key]
        if first_key in _KNOWN_LOCATIONS:
            return _normalize_location(first_key)
        for pat, canon in _LOCATION_KEYWORD_PATTERNS:
            if pat.search(first_str):
                return canon

    return None


def _detect_cross_tab_granularity(
    raw: pd.DataFrame, layout: dict, sheet_name: str = ""
) -> tuple[Optional[str], Optional[tuple]]:
    """
    Decide whether a cross-tab sheet's data rows are monthly or daily.
    Three patterns are recognised in column 0:

      * Small integers 1-31    → daily, anchor month from context
      * Month labels "Apr'24"  → monthly
      * Actual date objects    → daily with real dates; anchor=None signals
                                 "use col0 directly" to _parse_cross_tab_daily

    Returns (granularity, anchor_year_month):
      - anchor_year_month is a (year, month) tuple for the integer-day case,
        None with granularity="daily" for the real-date case (sentinel that
        tells the parser to use col0 dates as-is), or None with
        granularity=None when the column format is unrecognised.
    """
    data_start = layout.get("data_start_row_idx", 0)
    date_col = layout.get("date_col_idx", 0)
    col0_sample = raw.iloc[data_start : data_start + 10, date_col].tolist()

    # Non-blank values only for classification.
    non_blank = [v for v in col0_sample if pd.notna(v)]
    if not non_blank:
        # date col is all-blank — but data may still be valid if other
        # cols are populated (some exports omit the date entirely).
        return "daily", None

    date_like = sum(1 for v in non_blank if isinstance(v, (dt.datetime, dt.date)))
    numeric_like = sum(1 for v in non_blank if _looks_like_day_number(v))
    month_like = sum(1 for v in non_blank if _parse_month_label(v) is not None)

    if date_like >= len(non_blank) * 0.6:
        # Col0 holds actual date/datetime objects — use them directly.
        return "daily", None  # None = "use col0 dates as-is"

    if numeric_like >= month_like and numeric_like > 0:
        anchor = _infer_month_from_context(raw, sheet_name=sheet_name)
        return "daily", anchor

    if month_like > 0:
        return "monthly", None

    return None, None


def _looks_like_day_number(value) -> bool:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return False
    return 1 <= n <= 31 and float(value) == n


def _infer_month_from_context(raw: pd.DataFrame, sheet_name: str = "") -> Optional[tuple]:
    """
    For a daily-grain sheet, look for a month/year label to anchor the
    day-of-month values to real calendar dates.

    Checked in order:
      1. The sheet name itself — the most reliable source when sheets are
         named like "Gox Traffic June 26", "HYD Traffic June-26", etc.
         Uses _parse_month_label's existing "Jun 26" / "June-26" parsing.
      2. The first two cell rows of the sheet (legacy: some exports put a
         title like "May-26" or "May 2026" in A1/A2).
    """
    # 1. Sheet name: strip everything before the last word-boundary
    #    group that looks like a month+year token, then try to parse it.
    if sheet_name:
        # Try the whole name first (handles "June 26", "June-26" at end)
        parsed = _parse_month_label(sheet_name.strip())
        if parsed is not None:
            return parsed
        # Try just the trailing portion after the last space/hyphen boundary
        # e.g. "Gox Traffic June 26" → try "June 26"
        parts = re.split(r"[\s\-_]+", sheet_name.strip())
        for start in range(len(parts) - 1, 0, -1):
            candidate = " ".join(parts[start:])
            parsed = _parse_month_label(candidate)
            if parsed is not None:
                return parsed

    # 2. Title rows inside the sheet.
    for row_idx in range(min(2, len(raw))):
        for cell in raw.iloc[row_idx].tolist():
            if pd.isna(cell):
                continue
            parsed = _parse_month_label(str(cell))
            if parsed is not None:
                return parsed
    return None


def _parse_month_label(value) -> Optional[tuple]:
    """
    Parse a month label like "Apr'24", "May-26", "May 2026" into (year,
    month). Returns None if it doesn't look like a month label, or if it
    looks like a fiscal-year rollup label ("Fy24-25") which must NOT be
    treated as a real month.
    """
    text = str(value).strip()
    if _FY_ROLLUP_RE.match(text):
        return None
    cleaned = text.replace("-", " ").replace("’", "'")
    match = _MONTH_LABEL_RE.match(cleaned.replace("'", " ").strip())
    if not match:
        return None
    month_str, year_str = match.group(1)[:3].lower(), match.group(2)
    month_num = _MONTH_ABBR_TO_NUM.get(month_str)
    if month_num is None:
        return None
    year_num = int(year_str)
    if year_num < 100:
        year_num += 2000
    return (year_num, month_num)


def _parse_cross_tab_monthly(raw: pd.DataFrame, layout: dict, location: str) -> pd.DataFrame:
    """Parse a cross-tab sheet's monthly-grain data rows into long-format traffic rows."""
    records = []
    for row_idx in range(layout["data_start_row_idx"], len(raw)):
        label = raw.iloc[row_idx, 0]
        if pd.isna(label):
            continue
        year_month = _parse_month_label(str(label))
        if year_month is None:
            continue  # skips Fy24-25 rollups and any trailing non-month rows
        year, month = year_month
        period_start = dt.date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        period_end = dt.date(year, month, last_day)

        for segment_name, col_idxs in layout["column_groups"]:
            terminal = _segment_to_terminal(segment_name, location)
            total = 0.0
            any_value = False
            for col_idx in col_idxs:
                raw_val = raw.iloc[row_idx, col_idx] if col_idx < raw.shape[1] else None
                num = _parse_traffic_number(raw_val)
                if num is not None:
                    total += num
                    any_value = True
            if not any_value:
                continue
            records.append(
                {
                    "date": period_start,
                    "period_end": period_end,
                    "granularity": "monthly",
                    "location": location,
                    "terminal": terminal,
                    "traffic": total,
                }
            )

    if not records:
        return pd.DataFrame(columns=["date", "period_end", "granularity", "location", "terminal", "traffic"])

    df = pd.DataFrame.from_records(records)
    # Multiple segments can map to the same terminal label — sum them rather
    # than leaving duplicate (date, location, terminal) rows.
    # Note: T3 Dom, T3 Int, and T3 Arr are now kept as distinct terminal
    # labels so this groupby-sum does NOT collapse them together. dropna=False is
    # essential here: terminal="" is fine (empty string groups normally),
    # but pandas' groupby silently DROPS any row whose key is NaN/NaT by
    # default, which would discard every row whenever any key column held
    # a missing value — terminal is "" not NaN here, but it's safer not to
    # rely on that distinction holding for every future caller.
    df = df.groupby(
        ["date", "period_end", "granularity", "location", "terminal"], as_index=False, dropna=False
    )["traffic"].sum()
    return df


def _parse_cross_tab_daily(
    raw: pd.DataFrame, layout: dict, location: str, anchor_year_month: Optional[tuple]
) -> pd.DataFrame:
    """
    Parse a cross-tab sheet's daily-grain data rows into long-format traffic rows.

    Three col-0 formats are handled:
      * Small integers 1-31        → combined with anchor_year_month to form a date
      * Actual date/datetime objs  → used directly (anchor_year_month is None)
      * Blank / NaN                → row date is inferred from the row's position
                                     using anchor_year_month (row i → day i+1 within
                                     the anchor month), as some exports simply omit
                                     the date column while ordering rows 1-30/31.
    """
    _EMPTY = pd.DataFrame(
        columns=["date", "period_end", "granularity", "location", "terminal", "traffic"]
    )

    use_real_dates = anchor_year_month is None
    if not use_real_dates:
        year, month = anchor_year_month
        last_day = calendar.monthrange(year, month)[1]
    else:
        year = month = last_day = None

    date_col = layout.get("date_col_idx", 0)  # which column holds day/date values
    records = []
    day_counter = 0  # for blank-date-col sheets: tracks which day we're on

    # Some files (e.g. DEL Traffic June-26) have an extra "full label" row
    # immediately after the segment/subheader rows, e.g.:
    #   R2: "T3 International" | "T3 Domestic" | ...   ← segment_row
    #   R3: "Dep." | "Arr." | ...                       ← subheader_row
    #   R4: "T3 International Dep." | "T3 International Arr." | ...  ← EXTRA
    #   R5: 1 | 29421 | 30267 | ...                     ← first real data
    # Detect this by checking whether the first "data" row's date cell looks
    # like a header label (string) rather than a number or date.
    actual_data_start = layout["data_start_row_idx"]
    first_label = raw.iloc[actual_data_start, date_col] if actual_data_start < len(raw) else None
    if first_label is not None and isinstance(first_label, str) and first_label.strip().lower() in (
        "day", "date", "month", "т3 international dep.", "t3 international dep."
    ):
        actual_data_start += 1
    # More general: if the date cell is a non-empty string (not a number/date), skip it
    elif first_label is not None and isinstance(first_label, str) and first_label.strip():
        # Verify by checking if the NEXT row looks like data
        next_label = raw.iloc[actual_data_start + 1, date_col] if actual_data_start + 1 < len(raw) else None
        if next_label is not None and _looks_like_day_number(next_label):
            actual_data_start += 1

    for row_idx in range(actual_data_start, len(raw)):
        label = raw.iloc[row_idx, date_col]

        # --- Determine the date for this row --------------------------------
        row_date: Optional[dt.date] = None

        if isinstance(label, (dt.datetime, dt.date)):
            # Real date object — use directly.
            row_date = label.date() if isinstance(label, dt.datetime) else label

        elif pd.isna(label):
            # Blank col0: use position within the anchor month (day_counter).
            if not use_real_dates and anchor_year_month is not None:
                day_counter += 1
                if day_counter <= last_day:
                    row_date = dt.date(year, month, day_counter)
            # If use_real_dates and col0 is blank, skip (no date available).

        elif _looks_like_day_number(label):
            # Integer day-of-month.
            if not use_real_dates and anchor_year_month is not None:
                day_num = int(float(label))
                if day_num <= last_day:
                    row_date = dt.date(year, month, day_num)

        elif isinstance(label, str) and label.strip().lower() in (
            "total", "full month", "avg. daily", "average", "avg", "growth",
            "pax count", "arrival", "departure", "arrivals", "departures",
        ):
            # Summary / footer rows — stop parsing data here.
            break

        else:
            # Non-date, non-integer label (e.g. column header repeated) — skip.
            continue

        if row_date is None:
            continue

        # --- Extract traffic values for this row ----------------------------
        for segment_name, col_idxs in layout["column_groups"]:
            terminal = _segment_to_terminal(segment_name, location)
            total = 0.0
            any_value = False
            for col_idx in col_idxs:
                raw_val = raw.iloc[row_idx, col_idx] if col_idx < raw.shape[1] else None
                num = _parse_traffic_number(raw_val)
                if num is not None:
                    total += num
                    any_value = True
            if not any_value:
                continue
            records.append(
                {
                    "date": row_date,
                    "period_end": pd.NaT,
                    "granularity": "daily",
                    "location": location,
                    "terminal": terminal,
                    "traffic": total,
                }
            )

    if not records:
        return _EMPTY

    df = pd.DataFrame.from_records(records)
    # dropna=False: period_end is pd.NaT for every daily row — without this,
    # pandas groupby silently drops all rows whose key contains NaT.
    df = df.groupby(
        ["date", "period_end", "granularity", "location", "terminal"],
        as_index=False, dropna=False,
    )["traffic"].sum()
    return df


def _segment_to_terminal(segment_name: str, location: str) -> str:
    """
    Map a cross-tab column-group label (e.g. "T3 International", "Domestic")
    to a canonical terminal ("T1"/"T2"/"T3") for Delhi, or "" (whole-
    airport, no real per-terminal breakdown) for Hyderabad/Goa, whose
    "International"/"Domestic" segments are flight-type categories, not
    physical terminals.
    """
    if location != "Delhi":
        return ""
    key = segment_name.strip().lower()
    return _DELHI_SEGMENT_TO_TERMINAL.get(key, "")


def _parse_traffic_number(value) -> Optional[float]:
    """
    Parse a traffic cell that might be a plain number, or a string with
    Indian comma grouping and/or a stray non-breaking space (seen in the
    real file, e.g. '\\xa010,64,438' for 1,064,438) — strips both before
    converting to float. Returns None for blank/non-numeric cells.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", "").replace(",", "").strip()
    if text in ("", "-", "—"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean_terminal_value(value) -> str:
    """
    Normalize a terminal cell to a clean string, with missing/blank/NaN
    values becoming "" (empty string) rather than the literal text "nan" —
    handles None, float NaN, and pandas NA uniformly, which a plain
    .astype(str) pass does not (it can leave real NaN floats in place
    depending on pandas version/dtype, rather than the string "nan").
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("nan", "none", ""):
        return ""
    return text


def _normalize_location(raw_value: str) -> str:
    key = str(raw_value).strip().lower()
    return _LOCATION_NORMALIZATION.get(key, str(raw_value).strip())


def _find_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Case-insensitive, whitespace-tolerant column lookup against aliases."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower_map:
            return lower_map[key]
    for candidate in candidates:
        key = candidate.strip().lower()
        for lower_col, original_col in lower_map.items():
            if key in lower_col:
                return original_col
    return None
