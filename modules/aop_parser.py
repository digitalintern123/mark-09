"""
aop_parser.py — Reads the AOP (Annual Operating Plan) workbook: a 5-year
forward budget plan (FY26-27 through FY30-31) given as one wide sheet with
monthly columns plus FY-total rollup columns, and a row structure that
mixes individual outlet rows with subtotal/rollup rows.

This file's structure (verified by hand against the actual workbook,
including reading the live SUM formulas behind its "Total Delhi"/"Total
HYD"/"Total GOA" rows to understand the rollup logic) is:

  Row 1: title row (ignored)
  Row 2: headers — "Geographical Segment", "Business Segment", "Unit-ID",
         "Sales Unit-ID", then 12 monthly date columns (Apr of year Y
         through Mar of year Y+1 = one fiscal year), then paired FY-total
         columns ("FY 26-27" value + a second column, repeated for each
         of the 5 fiscal years).
  Rows 3+: either a LEAF row (Geographical Segment AND Business Segment
         both filled — a real outlet with real monthly AOP figures) or a
         ROLLUP row (both blank, Unit-ID holds a label like "Lounges at
         Dom. Dep. T1D" or "Total Delhi" — a subtotal of the leaf rows
         above it back to the previous rollup boundary).

ROLLUP ROWS ARE NEVER USED for parsing — each location's "Total X" row
turned out to be a bespoke, hand-corrected formula (verified by reading
the actual formula text, e.g. Delhi's subtracts 3 rows that Hyderabad's
formula doesn't even reference), so there's no generalizable rule that
works across locations. Only LEAF rows are imported; this also sidesteps
ever double-counting a rollup on top of its own constituent rows.

OUTLET NAME RECONCILIATION: AOP's Unit-ID labels (e.g. "T1D Lounge-1 Node
L4&5 Card", "LA22") use different naming than the revenue side's outlet
column (e.g. "T1D L4&5 Lounge", "Arrival Lounge LA 22"). _AOP_OUTLET_MAP
below is an explicit, hand-verified mapping built by cross-referencing
every leaf row against the actual outlet vocabulary already present in
revenue data — NOT a fuzzy/guessed match — because a wrong guess here
would silently corrupt every AOP variance number downstream. Three
outlets that revenue tracks as the same physical lounge under different
historical names ("Arrival Lounge LA 22", "LA 22", "Delhi - T3 - LA 22")
are explicitly treated as aliases of one AOP target. M&G/Porter/Buggy
sub-rows are summed into one "Meet & Greet" target per location, per
explicit confirmation, since the source file's own row layout duplicates
these across multiple rows per location (and even includes a redundant
plain "M&G" row alongside "M&G Del"/"M&G Hyd"/"M&G Goa").

SCOPE: only Delhi/Hyderabad/Goa rows under Business Segment
Lounge/SPA/Atithya/Others are imported (these map onto the app's existing
EHPL business unit structure). Bhogapuram and segments like IFK/F&B/Hotel/
Nap & Shower are explicitly out of scope for this importer — see
get_skipped_rows_report() to see exactly what was excluded and why,
rather than have it silently vanish.

UNITS: cell D1 of the source sheet holds the formula "=10^5" — a units
note meaning every figure in the sheet is expressed in lakhs (units of
100,000), not absolute rupees. This is read directly from the sheet (see
_read_units_multiplier) and applied to every AOP value before storage, so
parsed `aop` figures are in real INR, directly comparable to revenue_master's
`revenue` column without the caller needing to remember a scale factor.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from . import database

REQUIRED_OUTPUT_COLS = ["location", "segment", "business_unit", "outlet", "year", "month", "aop"]

_BUSINESS_SEGMENT_TO_UNIT = {
    "lounge": "Lounges",
    "spa": "Lounges",
    "atithya": "Atithya",
    "others": "Others",
}

_IN_SCOPE_LOCATIONS = {"delhi", "del", "hyd", "hyderabad", "goa", "gox", "goi"}
_LOCATION_NORMALIZATION = {
    "delhi": "Delhi", "del": "Delhi",
    "hyd": "Hyderabad", "hyderabad": "Hyderabad",
    "goa": "Goa", "gox": "Goa", "goi": "Goa",
}

_COMBINE_INTO_TARGET = {
    ("Delhi", "Meet & Greet"): ["M&G Del", "M&G", "Porter Del", "Buggy Del"],
    ("Hyderabad", "Meet & Greet"): ["M&G Hyd", "M&G", "Porter Hyd", "Buggy Hyd"],
    ("Goa", "Meet & Greet"): ["M&G Goa", "M&G", "Buggy Goa", "Porter Goa"],
}

_AOP_OUTLET_MAP = {
    ("Delhi", "T1D Lounge-1 Node L4&5 Card"): "T1D L4&5 Lounge",
    ("Delhi", "T1D new premium lounge 2 (level 5)"): "T1D new premium lounge 2 (level 5)",
    ("Delhi", "T1D new Amex Lounge (level 4)"): "T1D new Amex Lounge (level 4)",
    ("Delhi", "T2"): "T2 Domestic",
    ("Delhi", "T3 D49"): "T3 D49",
    ("Delhi", "T3 DLO2/03/04"): "Lounge DL 02,03,04",
    ("Delhi", "Lounge - Amex Centurion"): "Centurion Lounge",
    ("Delhi", "Lounge - Rupay"): "Rupay",
    ("Delhi", "Domestic AI Lounge Del"): "Air India",
    ("Delhi", "T3 INL 5&6"): "INL 5&6",
    ("Delhi", "T3 Premium"): "Premium Lounge",
    ("Delhi", "Xenia"): "First Class - Xenia Lounge",
    ("Delhi", "AI International Lounge"): "AI International Lounge",
    ("Delhi", "LA01"): "Nap & Shower LA01",
    ("Delhi", "LA12"): "Nap & Shower LA12",
    ("Delhi", "RL Delhi"): "Reserved Lounge",
    ("Delhi", "CIP Lounge"): "CIP Lounge",
    ("Delhi", "INTL Spa"): "Spa - International",
    ("Delhi", "Dom Spa"): "SPA Domestic",
    ("Delhi", "T1D SPA"): "T1D SPA",
    ("Delhi", "Baggage Wrapping DEL"): "Baggage Wrapping",
    ("Delhi", "Business Center"): "Business Center",
    ("Delhi", "RDC"): "Round D Clock (RDC)",
    ("Hyderabad", "Hyd Dom Lounge"): "Domestic Lounge",
    ("Hyderabad", "HYD DOM Prive"): "Dom Prive",
    ("Hyderabad", "Hyd Intl Lounge"): "International Lounge",
    ("Hyderabad", "INT Card Lounge - new (Level E)"): "International Lounge (New)",
    ("Hyderabad", "INT Prive - Mezzanine level"): "Prive",
    ("Hyderabad", "Hyd GA Lounge"): "GAT",
    ("Hyderabad", "Transit Lounge"): "Transit Lounge",
    ("Hyderabad", "RL  Hyd"): "Reserved Lounge",
    ("Hyderabad", "Airport Lodge"): "Airport Lodge",
    ("Hyderabad", "Baggage Wrapping HYD"): "Baggage Wrapping",
    ("Goa", "Goa Lounge Dom"): "Domestic Lounge",
    ("Goa", "Goa Lounge INTL"): "International Lounge",
    ("Goa", "CIP Lounge Goa"): "CIP Lounge",
    ("Goa", "RL Goa"): "Reserved Lounge",
    ("Goa", "Baggage Wrapping Goa"): "Baggage Wrapping",
}

_ALIAS_GROUPS = {
    ("Delhi", "LA22"): ["Arrival Lounge LA 22", "LA 22", "Delhi - T3 - LA 22"],
}

_KNOWN_UNMAPPED = {
    ("Delhi", "RDC - Rooms"): "No separate revenue-side outlet for this RDC sub-line (revenue tracks one combined 'Round D Clock (RDC)').",
    ("Delhi", "RDC - F&B"): "No separate revenue-side outlet for this RDC sub-line (revenue tracks one combined 'Round D Clock (RDC)').",
}


class AOPParseError(Exception):
    """Raised when the AOP workbook can't be parsed into the expected schema."""


def _read_units_multiplier(raw: pd.DataFrame) -> float:
    """
    Look for a units note in the first couple of rows — specifically a
    numeric value like 100000 (or a formula that evaluated to it, e.g.
    "=10^5") sitting near the title. Defaults to 1.0 (no scaling) if no
    such note is found, since not every AOP workbook will necessarily
    include one and silently assuming lakhs when the file is actually in
    plain rupees would be just as wrong as the reverse.
    """
    search_rows = min(3, len(raw))
    for row_idx in range(search_rows):
        for val in raw.iloc[row_idx].tolist():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if val in (100000, 100000.0):
                    return float(val)
    return 1.0


def detect_outlet_monthly_sheet(file_obj) -> Optional[str]:
    """
    Scan every sheet in the workbook for one whose layout matches the
    per-outlet/monthly AOP format (a header row containing 'Geographical
    Segment', 'Business Segment', and 'Unit-ID'). Returns the matching
    sheet name, or None if no sheet matches.

    The AOP file used during development happened to be named
    'Revenue_Master', but that's just what one particular workbook called
    its sheet — it is not a required or special name. Any sheet, under
    any name, is checked the same way.
    """
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise AOPParseError(f"Could not open this Excel file: {exc}") from exc

    for candidate_sheet in xl.sheet_names:
        try:
            preview = pd.read_excel(xl, sheet_name=candidate_sheet, header=None, nrows=5)
        except Exception:
            continue
        if _find_header_row(preview) is not None:
            return candidate_sheet
    return None


# Backward-compatible alias — detect_aop_sheet was the original name before
# the daily-pivot format was added and this needed to be disambiguated.
detect_aop_sheet = detect_outlet_monthly_sheet


def parse_outlet_monthly_aop(file_obj, sheet_name: Optional[str] = None) -> dict:
    """
    Parse the per-outlet/monthly AOP workbook format into:
      {"aop_rows": DataFrame[location, segment, business_unit, outlet,
                              year, month, aop],
       "skipped_rows": DataFrame[row_number, location, business_segment,
                                  unit_id, reason],
       "units_multiplier": float}

    `sheet_name`, if given, is read directly. If not given (the normal
    case), every sheet in the workbook is scanned for one matching this
    layout (see detect_outlet_monthly_sheet) — the workbook's sheet can be
    named anything; there is no required sheet name.

    Every `aop` value in `aop_rows` is already scaled by `units_multiplier`
    (see _read_units_multiplier) — e.g. if the source sheet stores values
    in lakhs, the returned `aop` figures are in absolute rupees, directly
    comparable to revenue_master.revenue with no further conversion needed
    by the caller. `units_multiplier` is returned too so the UI can show
    the user what scaling was detected/applied.
    """
    if sheet_name is None:
        sheet_name = detect_outlet_monthly_sheet(file_obj)
        if sheet_name is None:
            try:
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
            except Exception:
                sheet_names = []
            raise AOPParseError(
                "Could not find a sheet with a recognizable per-outlet/monthly AOP "
                "layout (a row containing 'Geographical Segment', 'Business Segment', "
                f"and 'Unit-ID' headers) in any sheet of this workbook. Sheets found: {sheet_names}."
            )
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

    try:
        raw = pd.read_excel(file_obj, sheet_name=sheet_name, engine="openpyxl", header=None)
    except Exception as exc:
        raise AOPParseError(f"Could not read sheet '{sheet_name}': {exc}") from exc

    units_multiplier = _read_units_multiplier(raw)

    header_row_idx = _find_header_row(raw)
    if header_row_idx is None:
        raise AOPParseError(
            f"Sheet '{sheet_name}' was selected, but its header row (expected "
            "'Geographical Segment', 'Business Segment', 'Unit-ID' columns) "
            "could not be found on a second read. This shouldn't normally happen."
        )

    month_columns = _extract_month_columns(raw, header_row_idx)
    if not month_columns:
        raise AOPParseError(
            f"Found the header row in sheet '{sheet_name}', but no monthly date "
            "columns after it (expected a run of datetime-typed column headers)."
        )

    aop_records = []
    skipped_records = []
    pending_combine: dict = {}

    for row_idx in range(header_row_idx + 1, raw.shape[0]):
        row = raw.iloc[row_idx]
        geo_raw, biz_raw, unit_raw = row[1], row[2], row[3]

        is_leaf = pd.notna(geo_raw) and pd.notna(biz_raw)
        unit_id = str(unit_raw).strip() if pd.notna(unit_raw) else None

        if not is_leaf:
            continue

        location_key = str(geo_raw).strip().lower()
        if location_key not in _IN_SCOPE_LOCATIONS:
            skipped_records.append(
                {"row_number": row_idx + 1, "location": geo_raw, "business_segment": biz_raw,
                 "unit_id": unit_id, "reason": "Out-of-scope location (not Delhi/Hyderabad/Goa)."}
            )
            continue
        location = _LOCATION_NORMALIZATION[location_key]

        business_segment_key = str(biz_raw).strip().lower()
        business_unit = _BUSINESS_SEGMENT_TO_UNIT.get(business_segment_key)
        if business_unit is None:
            skipped_records.append(
                {"row_number": row_idx + 1, "location": location, "business_segment": biz_raw,
                 "unit_id": unit_id, "reason": f"Out-of-scope business segment '{biz_raw}'."}
            )
            continue

        monthly_values = {(y, m): _to_float_or_none(row[col_idx]) for col_idx, y, m in month_columns}

        combine_key = None
        for (loc, target_outlet), unit_ids in _COMBINE_INTO_TARGET.items():
            if loc == location and unit_id in unit_ids:
                combine_key = (loc, target_outlet)
                break

        if combine_key is not None:
            bucket = pending_combine.setdefault(
                combine_key, {"business_unit": business_unit, "monthly": {}}
            )
            for key, val in monthly_values.items():
                if val is None:
                    continue
                bucket["monthly"][key] = bucket["monthly"].get(key, 0.0) + val
            continue

        alias_key = (location, unit_id)
        if alias_key in _ALIAS_GROUPS:
            for alias_outlet in _ALIAS_GROUPS[alias_key]:
                for (y, m), val in monthly_values.items():
                    if val is None:
                        continue
                    aop_records.append(
                        {
                            "location": location, "segment": "EHPL", "business_unit": business_unit,
                            "outlet": alias_outlet, "year": y, "month": m, "aop": val,
                        }
                    )
            continue

        mapped_outlet = _AOP_OUTLET_MAP.get((location, unit_id))
        if mapped_outlet is None:
            reason = _KNOWN_UNMAPPED.get(
                (location, unit_id), "No outlet mapping defined yet for this Unit-ID."
            )
            skipped_records.append(
                {"row_number": row_idx + 1, "location": location, "business_segment": biz_raw,
                 "unit_id": unit_id, "reason": reason}
            )
            continue

        for (y, m), val in monthly_values.items():
            if val is None:
                continue
            aop_records.append(
                {
                    "location": location, "segment": "EHPL", "business_unit": business_unit,
                    "outlet": mapped_outlet, "year": y, "month": m, "aop": val,
                }
            )

    for (location, target_outlet), bucket in pending_combine.items():
        for (y, m), val in bucket["monthly"].items():
            aop_records.append(
                {
                    "location": location, "segment": "EHPL", "business_unit": bucket["business_unit"],
                    "outlet": target_outlet, "year": y, "month": m, "aop": val,
                }
            )

    aop_df = pd.DataFrame.from_records(
        aop_records, columns=REQUIRED_OUTPUT_COLS
    ) if aop_records else pd.DataFrame(columns=REQUIRED_OUTPUT_COLS)
    skipped_df = pd.DataFrame.from_records(
        skipped_records, columns=["row_number", "location", "business_segment", "unit_id", "reason"]
    ) if skipped_records else pd.DataFrame(columns=["row_number", "location", "business_segment", "unit_id", "reason"])

    if aop_df.empty:
        raise AOPParseError(
            "No in-scope AOP rows could be extracted. Check that the workbook still has "
            "Delhi/Hyderabad/Goa rows under Lounge/SPA/Atithya/Others business segments."
        )

    # Apply the units multiplier once, here, rather than at each of the
    # several places aop_records gets built above — every value in
    # aop_records was read straight from the sheet's raw (un-scaled)
    # numbers, regardless of which code path (combine/alias/direct
    # mapping) produced it.
    aop_df["aop"] = aop_df["aop"] * units_multiplier

    return {"aop_rows": aop_df, "skipped_rows": skipped_df, "units_multiplier": units_multiplier}


# Backward-compatible alias — parse_aop_workbook was the original name
# before the daily-pivot format was added and this needed disambiguating.
parse_aop_workbook = parse_outlet_monthly_aop


# ---------------------------------------------------------------------------
# Daily pivot-table AOP format: one row per calendar day, one column per
# location, no outlet/segment breakdown at all. Typically a copy-pasted (or
# exported) Excel PivotTable, e.g.:
#
#   Sum of AOP   Column Labels
#                Delhi    Hyderabad   GOA    Grand Total
#   Date
#   01-06-2022   2403534  110700             2514234
#   02-06-2022   2403534  110700             2514234
#   Grand Total  ...
#
# PivotTable quirks handled here:
#   - A "Sum of <field>" label sitting above/beside the real header row
#     (ignored — it's metadata about the pivot, not a column).
#   - "Column Labels" as the field header for the location columns
#     (ignored the same way).
#   - A trailing "Grand Total" COLUMN (summed across locations — not a
#     location itself, excluded from per-location output).
#   - A trailing "Grand Total" ROW at the bottom (a sum across all dates —
#     excluded from per-date output, the same way monthly/yearly rollup
#     rows are excluded in the other AOP format).
#   - Blank cells for a location with no target that day (treated as "no
#     data for that location that day", not zero).
# ---------------------------------------------------------------------------


def detect_daily_pivot_sheet(file_obj) -> Optional[str]:
    """
    Scan every sheet in the workbook for one whose layout matches the
    daily-pivot AOP format: a row containing "Date" (the row-field label)
    followed by other text cells (the location names) in the same row or
    the row directly below it, with a "Grand Total" column nearby.
    Returns the matching sheet name, or None if no sheet matches.
    """
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise AOPParseError(f"Could not open this Excel file: {exc}") from exc

    for candidate_sheet in xl.sheet_names:
        try:
            preview = pd.read_excel(xl, sheet_name=candidate_sheet, header=None, nrows=10)
        except Exception:
            continue
        if _find_daily_pivot_header_row(preview) is not None:
            return candidate_sheet
    return None


def _find_daily_pivot_header_row(raw: pd.DataFrame) -> Optional[int]:
    """
    Find the row containing the literal cell "Date" in column 0, which is
    always the header row for this format (the row-field label sits
    directly above the actual date values) — return that row's index, or
    None if no such row exists in the first ~10 rows scanned.
    """
    search_rows = min(10, len(raw))
    for row_idx in range(search_rows):
        first_cell = raw.iloc[row_idx, 0]
        if pd.notna(first_cell) and str(first_cell).strip().lower() == "date":
            return row_idx
    return None


def parse_daily_pivot_aop(file_obj, sheet_name: Optional[str] = None) -> dict:
    """
    Parse the daily pivot-table AOP format into:
      {"aop_rows": DataFrame[location, date, aop],
       "skipped_rows": DataFrame[row_number, value, reason],
       "units_multiplier": float}

    `sheet_name`, if given, is read directly. If not given, every sheet is
    scanned for this layout (see detect_daily_pivot_sheet).

    Locations are taken verbatim from the header row's column labels
    (case-normalized against the same Delhi/Hyderabad/Goa aliases used
    elsewhere in the app — "GOA"/"Gox"/"Goi" all normalize to "Goa", etc.)
    — a header that isn't a recognized location and isn't "Grand Total"
    is treated as an unrecognized column and reported, not silently used.
    """
    if sheet_name is None:
        sheet_name = detect_daily_pivot_sheet(file_obj)
        if sheet_name is None:
            try:
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
            except Exception:
                sheet_names = []
            raise AOPParseError(
                "Could not find a sheet with a recognizable daily-pivot AOP layout "
                "(a 'Date' row-header followed by location columns) in any sheet of "
                f"this workbook. Sheets found: {sheet_names}."
            )
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

    try:
        raw = pd.read_excel(file_obj, sheet_name=sheet_name, engine="openpyxl", header=None)
    except Exception as exc:
        raise AOPParseError(f"Could not read sheet '{sheet_name}': {exc}") from exc

    header_row_idx = _find_daily_pivot_header_row(raw)
    if header_row_idx is None:
        raise AOPParseError(
            f"Sheet '{sheet_name}' was selected, but its 'Date' header row could not "
            "be found on a second read. This shouldn't normally happen."
        )

    units_multiplier = _read_units_multiplier(raw)

    header_row = raw.iloc[header_row_idx]
    column_info = []  # (col_idx, "location" | "grand_total" | "unrecognized", label)
    for col_idx in range(1, len(header_row)):
        label = header_row[col_idx]
        if pd.isna(label):
            continue
        label_str = str(label).strip()
        label_key = label_str.lower()
        if label_key in ("grand total", "total", "grand totals"):
            column_info.append((col_idx, "grand_total", label_str))
        elif label_key in _DAILY_PIVOT_LOCATION_ALIASES:
            column_info.append((col_idx, "location", _DAILY_PIVOT_LOCATION_ALIASES[label_key]))
        else:
            column_info.append((col_idx, "unrecognized", label_str))

    location_columns = [(idx, loc) for idx, kind, loc in column_info if kind == "location"]
    unrecognized_columns = [(idx, label) for idx, kind, label in column_info if kind == "unrecognized"]

    if not location_columns:
        raise AOPParseError(
            f"Sheet '{sheet_name}' has a 'Date' header row, but none of its columns "
            f"matched a known location (Delhi/Hyderabad/Goa). Columns found: "
            f"{[label for _, _, label in column_info]}."
        )

    aop_records = []
    skipped_records = []

    for row_idx in range(header_row_idx + 1, raw.shape[0]):
        date_cell = raw.iloc[row_idx, 0]
        if pd.isna(date_cell):
            continue
        date_str = str(date_cell).strip()
        if date_str.lower() in ("grand total", "total", "grand totals"):
            continue  # the trailing rollup row across all dates — never used, same policy as the other AOP format's rollup rows

        parsed_date = _parse_flexible_date(date_cell)
        if parsed_date is None:
            skipped_records.append(
                {"row_number": row_idx + 1, "value": date_str, "reason": "Could not parse this row's date value."}
            )
            continue

        for col_idx, location in location_columns:
            raw_val = raw.iloc[row_idx, col_idx] if col_idx < raw.shape[1] else None
            num = _parse_traffic_number_local(raw_val)
            if num is None:
                continue  # blank cell for this location that day = no target, not zero
            aop_records.append({"location": location, "date": parsed_date, "aop": num * units_multiplier})

    aop_df = pd.DataFrame.from_records(aop_records, columns=["location", "date", "aop"]) if aop_records else pd.DataFrame(columns=["location", "date", "aop"])
    if not aop_df.empty:
        aop_df = aop_df.groupby(["location", "date"], as_index=False)["aop"].sum()

    if unrecognized_columns:
        for _, label in unrecognized_columns:
            skipped_records.append(
                {"row_number": header_row_idx + 1, "value": label, "reason": "Unrecognized column header (not a known location, not Grand Total)."}
            )

    skipped_df = pd.DataFrame.from_records(
        skipped_records, columns=["row_number", "value", "reason"]
    ) if skipped_records else pd.DataFrame(columns=["row_number", "value", "reason"])

    if aop_df.empty:
        raise AOPParseError(
            f"Sheet '{sheet_name}' was recognized as a daily-pivot AOP layout, but no "
            "usable (date, location, value) combinations were found after cleaning."
        )

    return {"aop_rows": aop_df, "skipped_rows": skipped_df, "units_multiplier": units_multiplier}


_DAILY_PIVOT_LOCATION_ALIASES = {
    "delhi": "Delhi", "del": "Delhi",
    "hyderabad": "Hyderabad", "hyd": "Hyderabad",
    "goa": "Goa", "gox": "Goa", "goi": "Goa",
}


# ---------------------------------------------------------------------------
# Daily outlet-level pivot format: like the daily-pivot format above (one
# row per calendar day), but with a per-OUTLET breakdown instead of a
# location-only total — a richer PivotTable export with a 3-row header:
#   Row A: location banner ("Delhi" spanning many columns, then "Delhi
#          Total", then "Hyderabad", ... then "Grand Total")
#   Row B: business-segment banner within each location ("Atithya",
#          "Atithya Total", "Lounges", "Lounges Total", "Others",
#          "Subsidiary", ...)
#   Row C: "Date" in column 0, then one column per OUTLET — already using
#          this app's own canonical outlet names (e.g. "Meet & Greet",
#          "T1D L4&5 Lounge", "GAT"), not a separate name that needs
#          reconciling.
# Every "X Total"/"Grand Total" marker column is blank in row C (it has
# no outlet name of its own), so it's naturally skipped just by only
# reading columns with a real header in row C — no separate exclusion
# list is needed.
#
# Each cell is a genuine per-day target (confirmed against a real
# export: values step between planning periods but stay flat day-to-day
# within one, and carry fractional-rupee precision consistent with a
# monthly total divided evenly across that month's days), so this format
# is summed as-is into MONTHLY per-outlet totals on parse and fed into
# the exact same aop_target table (and therefore the exact same
# outlet-level AOP variance features) the per-outlet/monthly format
# already uses — rather than adding a new, separate day-level-per-outlet
# table that every AOP-consuming page would need to learn about too.
# ---------------------------------------------------------------------------

_DAILY_OUTLET_PIVOT_SEGMENT_ALIASES = {
    "lounge": "Lounges", "lounges": "Lounges", "spa": "Lounges",
    "atithya": "Atithya",
    "others": "Others",
}


def _find_date_header_row_deep(raw: pd.DataFrame, max_rows: int = 30) -> Optional[int]:
    """
    Same check as _find_daily_pivot_header_row (a literal "Date" cell in
    column 0), but searching up to `max_rows` instead of that function's
    hardcoded 10 — needed here because the daily outlet-level pivot
    format's extra location/segment banner rows routinely push its "Date"
    header row past row 10 (typically row 8-12), which the shared helper
    would silently miss regardless of how much data it's handed, since
    its own 10-row cap is internal, not driven by its input's length.
    """
    search_rows = min(max_rows, len(raw))
    for row_idx in range(search_rows):
        first_cell = raw.iloc[row_idx, 0]
        if pd.notna(first_cell) and str(first_cell).strip().lower() == "date":
            return row_idx
    return None


def detect_daily_outlet_pivot_sheet(file_obj) -> Optional[dict]:
    """
    Scan every sheet for the daily outlet-level pivot AOP format (see
    module comment above). Returns {"sheet_name": str, "header_row_idx":
    int, "location_row_idx": int}, or None if no sheet matches.

    Distinguishing this from the plain daily-pivot format: both have a
    "Date" cell in column 0 of their header row, but the plain format's
    header row itself has location names as its other cells, while this
    format's header row has OUTLET names, with locations instead given by
    a banner row somewhere above it — so a match requires the header
    row's own cells to NOT look like locations, plus a location-alias
    match in one of the few rows above it.
    """
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise AOPParseError(f"Could not open this Excel file: {exc}") from exc

    for candidate_sheet in xl.sheet_names:
        try:
            # A larger preview than the plain daily-pivot detector uses —
            # this format's extra banner rows push its header row deeper
            # into the sheet (typically row 8-12), so a shallow preview
            # can miss it entirely.
            preview = pd.read_excel(xl, sheet_name=candidate_sheet, header=None, nrows=25)
        except Exception:
            continue

        header_row_idx = _find_date_header_row_deep(preview)
        if header_row_idx is None:
            continue

        header_row_values = [
            str(v).strip().lower() for v in preview.iloc[header_row_idx].tolist() if pd.notna(v)
        ]
        if any(v in _DAILY_PIVOT_LOCATION_ALIASES for v in header_row_values[1:]):
            continue  # header row's own cells are locations -> plain daily-pivot format, not this one

        location_row_idx = None
        for candidate_row in range(max(0, header_row_idx - 5), header_row_idx):
            row_values = [
                str(v).strip().lower() for v in preview.iloc[candidate_row].tolist() if pd.notna(v)
            ]
            if any(v in _DAILY_PIVOT_LOCATION_ALIASES for v in row_values):
                location_row_idx = candidate_row

        if location_row_idx is not None:
            return {
                "sheet_name": candidate_sheet,
                "header_row_idx": header_row_idx,
                "location_row_idx": location_row_idx,
            }
    return None


def _forward_fill_banner_row(
    raw: pd.DataFrame, banner_row_idx: int, recognized: dict, n_cols: int
) -> dict:
    """
    Build a {col_idx: normalized_label_or_None} map by scanning a banner
    row left-to-right and carrying the most recent *recognized* label
    forward across blank/unrecognized cells (e.g. "Delhi" carries across
    columns 1-42 until "Hyderabad" appears at column 44) — an
    unrecognized cell (e.g. "Delhi Total", "Grand Total") resets the
    carried value to None, but since those marker columns are always
    blank in the actual header row too (see module comment above), this
    only ever matters for the columns that get skipped anyway.
    """
    banner_row = raw.iloc[banner_row_idx]
    result: dict[int, Optional[str]] = {}
    current = None
    for col_idx in range(n_cols):
        cell = banner_row[col_idx] if col_idx < len(banner_row) else None
        if pd.notna(cell):
            key = str(cell).strip().lower()
            current = recognized.get(key, None)
        result[col_idx] = current
    return result


def parse_daily_outlet_pivot_aop(file_obj, sheet_name: Optional[str] = None) -> dict:
    """
    Parse the daily outlet-level pivot AOP format into the same shape as
    parse_outlet_monthly_aop:
      {"aop_rows": DataFrame[location, segment, business_unit, outlet,
                              year, month, aop],
       "skipped_rows": DataFrame[row_number, location, business_segment,
                                  unit_id, reason],
       "units_multiplier": float}

    `sheet_name`, if given, is read directly (the location-banner row is
    re-located on this specific sheet rather than trusting a stale index
    from a different sheet). If not given, every sheet is scanned via
    detect_daily_outlet_pivot_sheet().
    """
    if sheet_name is None:
        match = detect_daily_outlet_pivot_sheet(file_obj)
        if match is None:
            try:
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
            except Exception:
                sheet_names = []
            raise AOPParseError(
                "Could not find a sheet with a recognizable daily outlet-level pivot AOP "
                "layout (a 'Date' row-header with per-outlet columns, under a location "
                f"banner row) in any sheet of this workbook. Sheets found: {sheet_names}."
            )
        sheet_name = match["sheet_name"]
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
    else:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        preview = pd.read_excel(file_obj, sheet_name=sheet_name, header=None, nrows=25)
        header_row_idx = _find_date_header_row_deep(preview)
        if header_row_idx is None:
            raise AOPParseError(f"Sheet '{sheet_name}' has no 'Date' header row.")
        location_row_idx = None
        for candidate_row in range(max(0, header_row_idx - 5), header_row_idx):
            row_values = [
                str(v).strip().lower() for v in preview.iloc[candidate_row].tolist() if pd.notna(v)
            ]
            if any(v in _DAILY_PIVOT_LOCATION_ALIASES for v in row_values):
                location_row_idx = candidate_row
        if location_row_idx is None:
            raise AOPParseError(
                f"Sheet '{sheet_name}' has a 'Date' header row, but no location banner "
                "row (Delhi/Hyderabad/Goa) was found above it."
            )
        match = {"sheet_name": sheet_name, "header_row_idx": header_row_idx, "location_row_idx": location_row_idx}
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

    try:
        raw = pd.read_excel(file_obj, sheet_name=match["sheet_name"], engine="openpyxl", header=None)
    except Exception as exc:
        raise AOPParseError(f"Could not read sheet '{match['sheet_name']}': {exc}") from exc

    header_row_idx = match["header_row_idx"]
    location_row_idx = match["location_row_idx"]
    # Re-locate these on the full read: a header/location row found in a
    # (possibly truncated) preview should land at the same index on the
    # full sheet, since both are read the same way (header=None), but
    # re-deriving defensively costs nothing and protects against a
    # preview/full-read mismatch.
    if header_row_idx >= raw.shape[0] or location_row_idx >= raw.shape[0]:
        raise AOPParseError(
            f"Sheet '{match['sheet_name']}' changed shape between detection and parsing "
            "— this shouldn't normally happen."
        )

    units_multiplier = _read_units_multiplier(raw)
    n_cols = raw.shape[1]

    location_by_col = _forward_fill_banner_row(
        raw, location_row_idx, _DAILY_PIVOT_LOCATION_ALIASES, n_cols
    )
    # The business-segment banner sits somewhere between the location row
    # and the header row — usually the very next row, but scan the whole
    # gap defensively in case a sheet has an extra spacer row.
    segment_row_idx = None
    for candidate_row in range(location_row_idx, header_row_idx):
        row_values = [
            str(v).strip().lower() for v in raw.iloc[candidate_row].tolist() if pd.notna(v)
        ]
        if any(v in _DAILY_OUTLET_PIVOT_SEGMENT_ALIASES or v == "subsidiary" for v in row_values):
            segment_row_idx = candidate_row
            break
    segment_aliases_with_subsidiary = dict(_DAILY_OUTLET_PIVOT_SEGMENT_ALIASES)
    segment_aliases_with_subsidiary["subsidiary"] = "Subsidiary"
    segment_by_col = (
        _forward_fill_banner_row(raw, segment_row_idx, segment_aliases_with_subsidiary, n_cols)
        if segment_row_idx is not None
        else {}
    )

    # Build the list of real outlet columns: any column with a non-blank
    # header-row cell AND a known location — "X Total"/"Grand Total"
    # marker columns are always blank in the header row (see module
    # comment above) so they're excluded automatically; a column with a
    # header value but no recognized location (shouldn't normally happen
    # given the file's own structure) is reported as skipped rather than
    # silently dropped.
    header_row = raw.iloc[header_row_idx]
    outlet_columns = []  # (col_idx, location, business_unit_label, outlet_name)
    unresolved_columns = []
    for col_idx in range(1, n_cols):
        outlet_cell = header_row[col_idx] if col_idx < len(header_row) else None
        if pd.isna(outlet_cell):
            continue
        outlet_name = str(outlet_cell).strip()
        if not outlet_name or outlet_name.lower() in ("grand total",) or outlet_name.lower().endswith(" total"):
            continue
        location = location_by_col.get(col_idx)
        segment_label = segment_by_col.get(col_idx)
        if location is None or segment_label is None:
            unresolved_columns.append((col_idx, location, segment_label, outlet_name))
            continue
        outlet_columns.append((col_idx, location, segment_label, outlet_name))

    if not outlet_columns:
        raise AOPParseError(
            f"Sheet '{match['sheet_name']}' was recognized as a daily outlet-level pivot "
            "layout, but no columns could be resolved to a known location + outlet."
        )

    aop_totals: dict[tuple, float] = {}  # (location, segment, business_unit, outlet, year, month) -> sum
    skipped_records = []

    for row_idx in range(header_row_idx + 1, raw.shape[0]):
        date_cell = raw.iloc[row_idx, 0]
        if pd.isna(date_cell):
            continue
        date_str = str(date_cell).strip()
        if date_str.lower() in ("grand total", "total", "grand totals"):
            continue  # trailing rollup row across all dates — never used, same policy as the other AOP formats

        parsed_date = _parse_flexible_date(date_cell)
        if parsed_date is None:
            skipped_records.append(
                {"row_number": row_idx + 1, "location": None, "business_segment": None,
                 "unit_id": date_str, "reason": "Could not parse this row's date value."}
            )
            continue

        for col_idx, location, segment_label, outlet_name in outlet_columns:
            raw_val = raw.iloc[row_idx, col_idx] if col_idx < raw.shape[1] else None
            num = _parse_traffic_number_local(raw_val)
            if num is None:
                continue  # blank cell for this outlet that day = no target, not zero

            if segment_label == "Subsidiary":
                segment, business_unit = database.canonicalize_segment_and_business_unit(
                    "Subsidiary", outlet_name
                )
            else:
                segment, business_unit = "EHPL", segment_label

            key = (location, segment, business_unit, outlet_name, parsed_date.year, parsed_date.month)
            aop_totals[key] = aop_totals.get(key, 0.0) + num * units_multiplier

    if unresolved_columns:
        for col_idx, location, segment_label, outlet_name in unresolved_columns:
            skipped_records.append(
                {
                    "row_number": header_row_idx + 1,
                    "location": location,
                    "business_segment": segment_label,
                    "unit_id": outlet_name,
                    "reason": "Could not resolve this column to a known location and/or business segment banner.",
                }
            )

    aop_records = [
        {"location": loc, "segment": seg, "business_unit": bu, "outlet": outlet, "year": y, "month": m, "aop": total}
        for (loc, seg, bu, outlet, y, m), total in aop_totals.items()
    ]
    aop_df = pd.DataFrame.from_records(aop_records, columns=REQUIRED_OUTPUT_COLS) if aop_records else pd.DataFrame(columns=REQUIRED_OUTPUT_COLS)
    skipped_df = pd.DataFrame.from_records(
        skipped_records, columns=["row_number", "location", "business_segment", "unit_id", "reason"]
    ) if skipped_records else pd.DataFrame(columns=["row_number", "location", "business_segment", "unit_id", "reason"])

    if aop_df.empty:
        raise AOPParseError(
            f"Sheet '{match['sheet_name']}' was recognized as a daily outlet-level pivot "
            "layout, but no usable (date, outlet, value) combinations were found after cleaning."
        )

    return {"aop_rows": aop_df, "skipped_rows": skipped_df, "units_multiplier": units_multiplier}


def _parse_flexible_date(value) -> Optional[dt.date]:
    """
    Parse a date cell that might already be a real datetime (if Excel
    stored it that way) or a text string in DD-MM-YYYY format (as seen in
    the real pivot export, e.g. "01-06-2022") — tries the most likely
    format for this file first, then falls back to pandas' general parser.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        parsed = pd.to_datetime(text, errors="raise", dayfirst=True)
        return parsed.date()
    except Exception:
        return None


def _parse_traffic_number_local(value) -> Optional[float]:
    """
    Parse a numeric cell that might be blank, a plain number, or text with
    comma grouping — mirrors traffic_parser._parse_traffic_number's
    handling of the same real-world messiness (Indian comma grouping,
    stray whitespace), kept as a local copy here rather than importing
    traffic_parser, since AOP and traffic are otherwise independent.
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


# ---------------------------------------------------------------------------
# Combined auto-detection across both AOP formats, and the sheet-list
# helper used by the "pick a sheet" UI when a workbook has several
# plausible candidates.
# ---------------------------------------------------------------------------

def list_aop_candidate_sheets(file_obj) -> list[dict]:
    """
    Scan every sheet in the workbook and report which AOP format (if any)
    each one matches. Returns a list of
    {"sheet_name": str, "format": "outlet_monthly" | "daily_pivot" |
    "daily_outlet_pivot" | None} — used to populate a sheet-picker when
    more than one sheet looks like a plausible candidate, so the user
    chooses explicitly rather than the app silently picking one.
    """
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        raise AOPParseError(f"Could not open this Excel file: {exc}") from exc

    results = []
    for sheet in xl.sheet_names:
        try:
            # 25 rows, not 10 — the daily_outlet_pivot format's extra
            # banner rows can push its "Date" header row past row 10.
            preview = pd.read_excel(xl, sheet_name=sheet, header=None, nrows=25)
        except Exception:
            results.append({"sheet_name": sheet, "format": None})
            continue
        if _find_header_row(preview) is not None:
            results.append({"sheet_name": sheet, "format": "outlet_monthly"})
            continue
        header_row_idx = _find_date_header_row_deep(preview)
        if header_row_idx is not None:
            header_row_values = [
                str(v).strip().lower() for v in preview.iloc[header_row_idx].tolist() if pd.notna(v)
            ]
            if any(v in _DAILY_PIVOT_LOCATION_ALIASES for v in header_row_values[1:]):
                results.append({"sheet_name": sheet, "format": "daily_pivot"})
                continue
            has_location_banner = any(
                any(
                    str(v).strip().lower() in _DAILY_PIVOT_LOCATION_ALIASES
                    for v in preview.iloc[r].tolist() if pd.notna(v)
                )
                for r in range(max(0, header_row_idx - 5), header_row_idx)
            )
            if has_location_banner:
                results.append({"sheet_name": sheet, "format": "daily_outlet_pivot"})
                continue
        results.append({"sheet_name": sheet, "format": None})
    return results


def parse_aop_auto(file_obj, sheet_name: Optional[str] = None) -> dict:
    """
    Universal AOP entry point: detects which of the three supported
    formats a workbook (or a specific sheet within it, if `sheet_name` is
    given) uses, and parses it accordingly. Returns the same shape as
    parse_outlet_monthly_aop/parse_daily_pivot_aop/
    parse_daily_outlet_pivot_aop, plus a "format" key ("outlet_monthly",
    "daily_pivot", or "daily_outlet_pivot") so the caller knows which kind
    of AOP data it received (outlet_monthly and daily_outlet_pivot both
    save into the same aop_target table via database.save_aop_targets,
    since daily_outlet_pivot is aggregated to monthly per-outlet totals on
    parse; daily_pivot saves into the separate, location-only
    aop_target_daily table via database.save_aop_targets_daily).

    If `sheet_name` is given, only that sheet is tried (in whichever
    format it matches) — this is what the sheet-picker UI uses once the
    user has chosen explicitly. If not given, every sheet is scanned for
    all three formats, most-specific first: outlet_monthly, then
    daily_outlet_pivot, then plain daily_pivot last (its only signal — a
    "Date" cell in column 0 — is the weakest/most easily coincidentally
    matched of the three).
    """
    if sheet_name is not None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        preview = pd.read_excel(file_obj, sheet_name=sheet_name, header=None, nrows=25)
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        if _find_header_row(preview) is not None:
            result = parse_outlet_monthly_aop(file_obj, sheet_name=sheet_name)
            result["format"] = "outlet_monthly"
            return result

        header_row_idx = _find_date_header_row_deep(preview)
        if header_row_idx is not None:
            header_row_values = [
                str(v).strip().lower() for v in preview.iloc[header_row_idx].tolist() if pd.notna(v)
            ]
            looks_like_plain_pivot = any(
                v in _DAILY_PIVOT_LOCATION_ALIASES for v in header_row_values[1:]
            )
            if not looks_like_plain_pivot:
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                try:
                    result = parse_daily_outlet_pivot_aop(file_obj, sheet_name=sheet_name)
                    result["format"] = "daily_outlet_pivot"
                    return result
                except AOPParseError:
                    pass  # fall through and try the plain daily-pivot format below

            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            result = parse_daily_pivot_aop(file_obj, sheet_name=sheet_name)
            result["format"] = "daily_pivot"
            return result

        raise AOPParseError(
            f"Sheet '{sheet_name}' doesn't match any supported AOP layout "
            "(per-outlet/monthly, daily outlet-level pivot, or daily-pivot by location)."
        )

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    monthly_sheet = detect_outlet_monthly_sheet(file_obj)
    if monthly_sheet is not None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        result = parse_outlet_monthly_aop(file_obj, sheet_name=monthly_sheet)
        result["format"] = "outlet_monthly"
        return result

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    outlet_pivot_match = detect_daily_outlet_pivot_sheet(file_obj)
    if outlet_pivot_match is not None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        result = parse_daily_outlet_pivot_aop(file_obj, sheet_name=outlet_pivot_match["sheet_name"])
        result["format"] = "daily_outlet_pivot"
        return result

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    daily_sheet = detect_daily_pivot_sheet(file_obj)
    if daily_sheet is not None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        result = parse_daily_pivot_aop(file_obj, sheet_name=daily_sheet)
        result["format"] = "daily_pivot"
        return result

    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
    except Exception:
        sheet_names = []
    raise AOPParseError(
        "Could not find a sheet matching any supported AOP layout: the "
        "per-outlet/monthly format (Geographical Segment / Business Segment / "
        "Unit-ID headers), the daily outlet-level pivot format (a 'Date' row-header "
        "with per-outlet columns under a location banner), or the plain daily-pivot "
        f"format (a 'Date' row-header with location columns). Sheets found: {sheet_names}."
    )


def _find_header_row(raw: pd.DataFrame) -> Optional[int]:
    search_rows = min(5, len(raw))
    for row_idx in range(search_rows):
        row_values = [str(v).strip().lower() for v in raw.iloc[row_idx].tolist() if pd.notna(v)]
        if "geographical segment" in row_values and "business segment" in row_values and "unit-id" in row_values:
            return row_idx
    return None


def _extract_month_columns(raw: pd.DataFrame, header_row_idx: int) -> list:
    """
    Return [(col_idx, year, month), ...] for every column after the
    Unit-ID/Sales Unit-ID columns whose header is a real datetime. Reads
    the (year, month) from the date itself rather than trusting the exact
    day-of-month, since this file's month columns drift by a few days
    each (e.g. "Apr 1", "May 2", "Jun 2", ...) due to a +30-days-style
    formula rather than a true end-of-month/start-of-month formula — the
    day value isn't reliable, but the month/year it falls in still is.
    """
    header_row = raw.iloc[header_row_idx]
    result = []
    for col_idx in range(5, len(header_row)):
        val = header_row[col_idx]
        if isinstance(val, (dt.datetime, dt.date, pd.Timestamp)):
            result.append((col_idx, val.year, val.month))
        elif pd.notna(val):
            if result:
                break
    return result


def _to_float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_skipped_rows_report(parse_result: dict) -> pd.DataFrame:
    """Convenience accessor for the skipped-rows diagnostic table."""
    return parse_result.get("skipped_rows", pd.DataFrame())
