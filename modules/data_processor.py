"""
data_processor.py — File-type detection and orchestration layer.

This is the single entry point the Streamlit UI calls when a file is
uploaded: it figures out whether the file is a PDF or an Excel workbook,
delegates to the right parser, validates+cleans the result, and (optionally)
saves it to the database. Keeping this logic out of Home.py keeps the UI code
focused on layout/display only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from . import aop_parser, database, excel_parser, pdf_parser, traffic_parser, universal_parser

REQUIRED_COLUMNS = ["date", "segment", "outlet", "location", "pax", "revenue"]


@dataclass
class ProcessResult:
    """Structured result returned to the UI after processing one file."""

    success: bool
    file_name: str
    message: str
    stage: str = "processing"  # "reading" | "validating" | "saving" | "processing" — which step this result reflects, for status displays
    df: Optional[pd.DataFrame] = None
    report_date: Optional[dt.date] = None
    total_revenue: float = 0.0
    total_pax: float = 0.0
    inserted: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def detect_file_type(file_name: str) -> str:
    """Return format token based on file extension."""
    lower = file_name.lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        return "excel"
    if lower.endswith((".csv", ".tsv", ".txt")):
        return "text"
    # Formats handled by the new ingestion layer
    if lower.endswith((".docx", ".doc")):
        return "docx"
    if lower.endswith((".html", ".htm")):
        return "html"
    if lower.endswith(".xml"):
        return "xml"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith((".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp")):
        return "image"
    if lower.endswith(".msg"):
        return "msg"
    return "unknown"


def process_uploaded_file(
    file_obj,
    file_name: str,
    save_to_db: bool = True,
    excel_sheet_name: Optional[str] = None,
) -> ProcessResult:
    """
    Main entry point for processing a single uploaded file (PDF or Excel).

    For Excel files, this no longer assumes any particular sheet name or
    requires the caller to say whether the file is "the historical
    workbook" vs "a daily report" — it scans every sheet in the workbook,
    finds whichever one looks like a long-format revenue table (by header
    content, not by name), and parses that one. This means a workbook can
    be named anything and have its data sheet named anything ("Data",
    "DATABASE", "Sheet1", ...) and still work, as long as one sheet has a
    row with Date + Location/Business/Outlet headers somewhere in it.

    `excel_sheet_name`, if given, skips auto-detection and reads that exact
    sheet via the fuzzy generic-column parser instead — use this if a user
    explicitly wants to point at one sheet rather than let the app guess.
    """
    file_type = detect_file_type(file_name)
    warnings: list[str] = []

    # ── Early traffic detection ─────────────────────────────────────────────
    # If the file's first row contains columns like DOM_ARR_PAX / INT_ARR_PAX,
    # it's an airport traffic export — route directly to traffic_parser so it
    # never touches the revenue parser.
    _traffic_col_hints = {"dom_arr_pax", "dom_dep_pax", "int_arr_pax", "int_dep_pax",
                          "dom_total_pax", "total_int_pax"}
    if file_type == "excel":
        try:
            import openpyxl as _opxl
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            _wb_peek = _opxl.load_workbook(file_obj, read_only=True, data_only=True)
            _first_row = []
            for _ws_peek in _wb_peek.worksheets:
                for _row_peek in _ws_peek.iter_rows(max_row=1, values_only=True):
                    _first_row = [str(v).strip().lower() for v in _row_peek if v is not None]
                break
            if any(h in _traffic_col_hints for h in _first_row):
                from .traffic_parser import parse_traffic_auto
                if hasattr(file_obj, "seek"):
                    file_obj.seek(0)
                _tdf = parse_traffic_auto(file_obj, file_name)
                if _tdf is not None and not _tdf.empty:
                    if save_to_db:
                        _sv = database.save_traffic_dataframe(_tdf, file_name)
                        return ProcessResult(
                            success=True, file_name=file_name, stage="complete",
                            message=(
                                f"'{file_name}' recognised as a **traffic export file** "
                                f"and saved to the traffic database. "
                                f"{_sv.get('inserted',0):,} rows inserted, "
                                f"{_sv.get('skipped',0):,} duplicates skipped. "
                                f"Go to **Traffic & Terminal Analysis** to see the results."
                            ),
                            df=_tdf,
                        )
                    return ProcessResult(success=True, file_name=file_name,
                                         stage="complete", message="Traffic file parsed.", df=_tdf)
        except Exception:
            pass
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

    if file_type == "text":
        # Delimited text has no predefined layout — it goes straight to
        # automatic schema detection.
        return _process_with_universal(file_obj, file_name, save_to_db, prior_error=None)

    try:
        if file_type == "pdf":
            df = pdf_parser.parse_pdf(file_obj, source_file=file_name)
            discrepancies = _safe_cross_validate(df, file_obj)
            if discrepancies:
                warnings.append(
                    f"Detail-table totals differ from the page-1 summary for "
                    f"{len(discrepancies)} segment/location combination(s). "
                    f"Data was still imported — please spot-check the source PDF."
                )
        elif file_type == "excel":
            if excel_sheet_name:
                # User explicitly named a sheet — still try the smart
                # detectors first, scoped to just this sheet, since a
                # workbook can have a sheet in a supported complex layout
                # (long-format or wide-pivot) that the naive fuzzy parser
                # can't handle at all; only fall back to the fuzzy column
                # matcher if this specific sheet doesn't match either
                # recognized layout.
                df = None
                try:
                    if hasattr(file_obj, "seek"):
                        file_obj.seek(0)
                    long_match = excel_parser.detect_long_format_sheet(file_obj)
                except excel_parser.ExcelParseError:
                    long_match = None
                if long_match is not None and long_match["sheet_name"] == excel_sheet_name:
                    if hasattr(file_obj, "seek"):
                        file_obj.seek(0)
                    df = excel_parser.parse_revenue_dashboard(
                        file_obj, sheet_name=excel_sheet_name, header_row_idx=long_match["header_row_idx"]
                    )

                if df is None:
                    try:
                        if hasattr(file_obj, "seek"):
                            file_obj.seek(0)
                        pivot_matches = excel_parser.detect_all_wide_pivot_sheets(file_obj)
                    except excel_parser.ExcelParseError:
                        pivot_matches = []
                    matching_pivot = next(
                        (m for m in pivot_matches if m["sheet_name"] == excel_sheet_name), None
                    )
                    if matching_pivot is not None:
                        if hasattr(file_obj, "seek"):
                            file_obj.seek(0)
                        df = excel_parser.parse_wide_pivot_sheet(file_obj, matching_pivot)

                if df is None:
                    if hasattr(file_obj, "seek"):
                        file_obj.seek(0)
                    df = excel_parser.parse_generic_excel(file_obj, sheet_name=excel_sheet_name)
            else:
                df, detected_description = _parse_excel_with_detection(file_obj)
                if detected_description:
                    warnings.append(f"Detected and used {detected_description} for revenue data.")
        else:
            # New ingestion-layer formats (DOCX, HTML, XML, JSON, image, MSG)
            # AND any truly unknown extension — let the ingestion layer decide.
            return _process_with_ingestion_layer(file_obj, file_name, save_to_db)
    except (pdf_parser.PDFParseError, excel_parser.ExcelParseError) as exc:
        # The file didn't match any PREDEFINED layout — fall back to the
        # universal parser, which auto-detects the schema of arbitrary
        # layouts. This is what makes new/unknown report formats work
        # without any manual mapping or code changes.
        return _process_with_universal(file_obj, file_name, save_to_db, prior_error=str(exc))
    except Exception as exc:  # pragma: no cover — last-resort safety net
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=f"Unexpected error while processing '{file_name}': {exc}",
        )

    df = _validate_and_clean(df)
    if df.empty:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="validating",
            message=(
                f"'{file_name}' was read, but contained no usable revenue rows after "
                f"validation — check that it has the expected columns (Date, Segment/"
                f"Business, Outlet, Location, PAX, Revenue) and that they aren't all blank."
            ),
        )

    report_date = df["date"].iloc[0] if "date" in df.columns and not df.empty else None
    total_revenue = float(pd.to_numeric(df["revenue"], errors="coerce").sum())
    total_pax = float(pd.to_numeric(df["pax"], errors="coerce").sum())

    # Some source formats (the wide pivot/cross-tab layout) can carry a
    # genuine per-day AOP figure per outlet alongside PAX/Revenue in the
    # very same sheet — see excel_parser.parse_wide_pivot_sheet's
    # "aop_daily" column. When present, this is extracted here and saved
    # into the same aop_target table a dedicated AOP workbook upload
    # would populate (aggregated to monthly per outlet, the grain that
    # table is keyed on), rather than being silently discarded — so a
    # single historical import can carry Revenue and AOP together and
    # have both actually usable throughout the app (AOP variance columns,
    # Executive Summary's AOP section, etc.), not just Revenue.
    aop_inserted, aop_skipped = 0, 0
    if save_to_db and "aop_daily" in df.columns and df["aop_daily"].notna().any():
        aop_source = df.dropna(subset=["aop_daily"]).copy()
        aop_source["_year"] = pd.to_datetime(aop_source["date"]).dt.year
        aop_source["_month"] = pd.to_datetime(aop_source["date"]).dt.month
        monthly = aop_source.groupby(
            ["location", "outlet", "segment", "_year", "_month"], as_index=False
        )["aop_daily"].sum()

        canon = monthly.apply(
            lambda r: database.canonicalize_segment_and_business_unit(r["segment"], r["outlet"]), axis=1
        )
        monthly["segment"] = [c[0] for c in canon]
        monthly["business_unit"] = [c[1] for c in canon]
        monthly = monthly.rename(columns={"_year": "year", "_month": "month", "aop_daily": "aop"})

        try:
            aop_save_result = database.save_aop_targets(
                monthly[["location", "segment", "business_unit", "outlet", "year", "month", "aop"]],
                source_file=file_name,
            )
            aop_inserted, aop_skipped = aop_save_result["inserted"], aop_save_result["skipped"]
            if aop_inserted:
                msg = (
                    f"Also found {aop_inserted:,} AOP target(s) embedded in this file (per-outlet, "
                    f"summed to monthly) — saved alongside the revenue data."
                )
                if aop_skipped:
                    msg += f" {aop_skipped:,} were already in the database and skipped."
                warnings.append(msg)
        except Exception as exc:
            warnings.append(
                f"Found AOP figures embedded in this file, but saving them failed: {exc} "
                f"(revenue data was still imported normally)."
            )

    inserted, skipped = 0, 0
    if save_to_db:
        try:
            result = database.save_dataframe(df, source_file=file_name)
        except Exception as exc:
            return ProcessResult(
                success=False,
                file_name=file_name,
                stage="saving",
                message=(
                    f"'{file_name}' was parsed successfully ({len(df):,} row(s)), but "
                    f"saving it to the database failed: {exc}"
                ),
            )
        inserted, skipped = result["inserted"], result["skipped"]

    message_parts = [f"Parsed {len(df):,} row(s) from '{file_name}'."]
    if save_to_db:
        message_parts.append(f"{inserted:,} new row(s) saved.")
        if skipped:
            message_parts.append(f"{skipped:,} duplicate row(s) skipped (already in database).")

    return ProcessResult(
        success=True,
        file_name=file_name,
        stage="saving" if save_to_db else "validating",
        message=" ".join(message_parts),
        df=df,
        report_date=report_date,
        total_revenue=total_revenue,
        total_pax=total_pax,
        inserted=inserted,
        skipped=skipped,
        warnings=warnings,
    )


def process_uploaded_file_auto(
    file_obj, file_name: str, save_to_db: bool = True
) -> ProcessResult:
    """
    Single entry point for a unified "Upload" button: figures out, on its
    own, which of the four file types this app understands a given upload
    is — a revenue report (daily or historical-bulk; both already share
    one pipeline, see process_uploaded_file), an AOP target workbook, or
    airport traffic data — and routes it to the right one, so the person
    uploading doesn't have to pre-sort files into separate boxes by type.

    Strategy: try each pipeline's *full* parse (not just a cheap header
    sniff) in a fixed order, and return the first one that actually
    succeeds. Order matters:
      1. Revenue is tried first. A revenue dashboard sheet can legitimately
         carry an extra Traffic column right alongside Date/Segment/
         Outlet/Location/PAX/Revenue (the revenue pipeline already
         understands that as an optional column) — trying traffic
         detection first could otherwise misroute a combined revenue+
         traffic sheet into the traffic-only pipeline and silently drop
         the segment/outlet/PAX/revenue data.
      2. AOP is tried next. Both of its layouts are keyed on very
         specific headers ("Geographical Segment"/"Business Segment"/
         "Unit-ID", or a "Date" row-label pivot whose other columns are
         location *names*, not "Location"/"Business"/"Outlet" header
         text) — this vocabulary doesn't overlap with revenue's or
         traffic's, so it's very unlikely to accidentally match either.
      3. Traffic is tried last, as the most narrowly-scoped format (Date
         + Location + Traffic headers, nothing else) — the format most
         likely to be a false-positive match for something else if tried
         earlier.
    Each attempt either fully succeeds (parsed AND saved) or fails
    cleanly with no partial/incorrect save — a failed attempt never
    writes anything to the database, so trying multiple pipelines in
    sequence is safe.

    PDFs are always treated as revenue reports — no other file type in
    this app is ever a PDF.
    """
    file_type = detect_file_type(file_name)

    if file_type in ("pdf", "text"):
        # PDFs try the Encalm layout first, then automatic schema detection;
        # delimited text goes straight to automatic schema detection.
        return process_uploaded_file(file_obj, file_name, save_to_db=save_to_db)

    if file_type != "excel":
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=(
                f"'{file_name}' is not a recognized file type. Please upload a PDF, "
                f"Excel (.xlsx/.xls/.xlsm), or CSV/TSV file."
            ),
        )

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    revenue_result = process_uploaded_file(file_obj, file_name, save_to_db=save_to_db)
    if revenue_result.success:
        return revenue_result

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    aop_result = _try_process_as_aop(file_obj, file_name, save_to_db=save_to_db)
    if aop_result.success:
        return aop_result

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    traffic_result = _try_process_as_traffic(file_obj, file_name, save_to_db=save_to_db)
    if traffic_result.success:
        return traffic_result

    # Nothing matched — prefer whichever attempt got furthest (reached
    # "saving" rather than failing at "reading") as the most informative
    # single failure message, but always list what every pipeline said,
    # since the person still needs to know their file genuinely didn't
    # match any supported layout.
    for candidate in (revenue_result, aop_result, traffic_result):
        if candidate.stage == "saving":
            return candidate
    return ProcessResult(
        success=False,
        file_name=file_name,
        stage="reading",
        message=(
            f"Could not recognize '{file_name}' as any supported file type. "
            f"As a revenue report: {revenue_result.message} "
            f"As an AOP workbook: {aop_result.message} "
            f"As traffic data: {traffic_result.message}"
        ),
    )


def _try_process_as_aop(file_obj, file_name: str, save_to_db: bool) -> ProcessResult:
    """Attempt the AOP pipeline; never raises — always returns a ProcessResult."""
    try:
        parsed = aop_parser.parse_aop_auto(file_obj)
    except aop_parser.AOPParseError as exc:
        return ProcessResult(success=False, file_name=file_name, stage="reading", message=str(exc))
    except Exception as exc:  # pragma: no cover — last-resort safety net
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=f"Unexpected error while reading '{file_name}' as an AOP workbook: {exc}",
        )

    format_label = {
        "outlet_monthly": "per-outlet/monthly",
        "daily_outlet_pivot": "daily outlet-level pivot",
        "daily_pivot": "daily-total-per-location",
    }.get(parsed["format"], parsed["format"])
    row_count = len(parsed["aop_rows"])

    if not save_to_db:
        return ProcessResult(
            success=True,
            file_name=file_name,
            stage="validating",
            message=f"Detected an AOP target workbook ({format_label} format). Parsed {row_count:,} row(s).",
        )

    try:
        # outlet_monthly and daily_outlet_pivot both produce the same
        # (location, segment, business_unit, outlet, year, month, aop)
        # row shape and save into the same aop_target table — the latter
        # is just a richer source format that gets aggregated to monthly
        # per-outlet totals during parsing (see aop_parser.
        # parse_daily_outlet_pivot_aop). Only the plain daily_pivot
        # format (location-only, no outlet breakdown) uses the separate
        # aop_target_daily table.
        if parsed["format"] in ("outlet_monthly", "daily_outlet_pivot"):
            save_result = database.save_aop_targets(parsed["aop_rows"], file_name)
        else:
            save_result = database.save_aop_targets_daily(parsed["aop_rows"], file_name)
    except Exception as exc:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="saving",
            message=(
                f"'{file_name}' was parsed as an AOP workbook ({row_count:,} row(s)), "
                f"but saving it to the database failed: {exc}"
            ),
        )

    message_parts = [
        f"Detected an AOP target workbook ({format_label} format). Parsed {row_count:,} row(s).",
        f"{save_result['inserted']:,} new AOP target(s) saved.",
    ]
    if save_result["skipped"]:
        message_parts.append(f"{save_result['skipped']:,} duplicate target(s) skipped (already in database).")

    warnings: list[str] = []
    skipped_rows = parsed.get("skipped_rows")
    if skipped_rows is not None and not skipped_rows.empty:
        warnings.append(
            f"{len(skipped_rows)} row(s) in this AOP workbook were out of scope or "
            f"unmapped (not a recognized location/segment) and were not imported."
        )
    units_multiplier = parsed.get("units_multiplier", 1.0)
    if units_multiplier and units_multiplier != 1.0:
        warnings.append(
            f"Detected a ×{units_multiplier:,.0f} units scale in this workbook "
            f"(e.g. values stored in lakhs) — applied automatically."
        )

    return ProcessResult(
        success=True,
        file_name=file_name,
        stage="saving",
        message=" ".join(message_parts),
        df=parsed["aop_rows"],
        inserted=save_result["inserted"],
        skipped=save_result["skipped"],
        warnings=warnings,
    )


def _try_process_as_traffic(file_obj, file_name: str, save_to_db: bool) -> ProcessResult:
    """Attempt the traffic pipeline; never raises — always returns a ProcessResult."""
    try:
        parsed = traffic_parser.parse_traffic_auto(file_obj, source_file=file_name)
    except traffic_parser.TrafficParseError as exc:
        return ProcessResult(success=False, file_name=file_name, stage="reading", message=str(exc))
    except Exception as exc:  # pragma: no cover — last-resort safety net
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=f"Unexpected error while reading '{file_name}' as traffic data: {exc}",
        )

    row_count = len(parsed)

    if not save_to_db:
        return ProcessResult(
            success=True,
            file_name=file_name,
            stage="validating",
            message=f"Detected an airport traffic data file. Parsed {row_count:,} row(s).",
        )

    try:
        save_result = database.save_traffic_dataframe(parsed, file_name)
    except Exception as exc:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="saving",
            message=(
                f"'{file_name}' was parsed as traffic data ({row_count:,} row(s)), "
                f"but saving it to the database failed: {exc}"
            ),
        )

    message_parts = [
        f"Detected an airport traffic data file. Parsed {row_count:,} row(s).",
        f"{save_result['inserted']:,} new row(s) saved.",
    ]
    if save_result["skipped"]:
        message_parts.append(f"{save_result['skipped']:,} duplicate row(s) skipped (already in database).")

    return ProcessResult(
        success=True,
        file_name=file_name,
        stage="saving",
        message=" ".join(message_parts),
        df=parsed,
        inserted=save_result["inserted"],
        skipped=save_result["skipped"],
    )


def _process_with_ingestion_layer(
    file_obj, file_name: str, save_to_db: bool
) -> ProcessResult:
    """
    Route a file through the new universal ingestion layer
    (modules/ingestion/parser_factory.py).

    Handles: DOCX, HTML, XML, JSON, PNG/JPG/TIFF images, Outlook MSG,
    scanned PDFs, and any other format the ingestion layer recognises.

    The ingestion layer returns an IngestionResult whose .df already has
    the standard revenue schema.  We pass that df through the existing
    _validate_and_clean → save_dataframe pipeline so all downstream
    analytics, canonical segment mapping, and dedupe work unchanged.
    """
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    try:
        from .ingestion import parse as ingestion_parse, IngestionResult
        result: IngestionResult = ingestion_parse(file_obj, file_name)
    except Exception as exc:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=f"Ingestion layer error for '{file_name}': {exc}",
        )

    if not result.success or result.df is None:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=(
                f"Could not extract revenue data from '{file_name}'. "
                + " | ".join(result.errors[:3])
            ),
        )

    # Build the warnings list: mapping report + validation + other warnings.
    ui_warnings: list[str] = []
    ui_warnings.extend(result.mapping_report)
    if result.validation_summary:
        ui_warnings.append("Validation: " + result.validation_summary.replace("\n", "; "))
    ui_warnings.extend(result.warnings)

    # Pass through the same validate-and-clean step every other parser uses.
    try:
        df = _validate_and_clean(result.df)
    except ValueError as exc:
        return ProcessResult(
            success=False, file_name=file_name, stage="validating",
            message=str(exc),
        )

    if df.empty:
        return ProcessResult(
            success=False, file_name=file_name, stage="validating",
            message=(
                f"'{file_name}' was parsed by the ingestion layer "
                f"({result.source_description}) but produced no usable rows after validation."
            ),
        )

    report_date  = df["date"].iloc[0] if "date" in df.columns and not df.empty else None
    total_revenue = float(pd.to_numeric(df["revenue"], errors="coerce").sum())
    total_pax     = float(pd.to_numeric(df["pax"],     errors="coerce").sum())

    inserted = skipped = 0
    if save_to_db:
        try:
            save_result = database.save_dataframe(df, source_file=file_name)
        except Exception as exc:
            return ProcessResult(
                success=False, file_name=file_name, stage="saving",
                message=(
                    f"'{file_name}' was parsed ({len(df):,} row(s)) "
                    f"but saving to the database failed: {exc}"
                ),
            )
        inserted, skipped = save_result["inserted"], save_result["skipped"]

    message_parts = [
        f"Ingestion layer parsed '{file_name}' "
        f"({result.source_description}, confidence {result.confidence:.0%}): "
        f"{len(df):,} row(s)."
    ]
    if save_to_db:
        message_parts.append(f"{inserted:,} new row(s) saved.")
        if skipped:
            message_parts.append(f"{skipped:,} duplicate(s) skipped.")

    return ProcessResult(
        success=True,
        file_name=file_name,
        stage="saving" if save_to_db else "validating",
        message=" ".join(message_parts),
        df=df,
        report_date=report_date,
        total_revenue=total_revenue,
        total_pax=total_pax,
        inserted=inserted,
        skipped=skipped,
        warnings=ui_warnings,
    )


def _process_with_universal(
    file_obj, file_name: str, save_to_db: bool, prior_error: Optional[str]
) -> ProcessResult:
    """
    Run the universal (automatic schema detection) parser on a file, then
    push its output through the SAME validation + save path every other
    revenue parser uses — so canonical segment mapping, dedupe, and the
    analytics engine all behave identically regardless of the source layout.

    `prior_error` is the message from the predefined-layout parsers (if
    they were tried first and failed), included in the failure message so
    the person sees the full story when neither approach worked.
    """
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    # ── Pre-check: is this a traffic file? ──────────────────────────────────
    # Traffic files (e.g. DOM_ARR_PAX, INT_ARR_PAX columns) don't have
    # Revenue / Outlet / Location columns so they will always fail the
    # universal revenue parser. Detect them early and route to traffic_parser.
    try:
        from .traffic_parser import parse_traffic_auto, TrafficParseError
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        _traffic_result = parse_traffic_auto(file_obj, file_name)
        if _traffic_result is not None and not _traffic_result.empty:
            from . import database as _db
            if save_to_db:
                _save_res = _db.save_traffic_dataframe(_traffic_result, file_name)
                return ProcessResult(
                    success=True,
                    file_name=file_name,
                    stage="complete",
                    message=(
                        f"'{file_name}' was recognised as a **traffic file** and imported "
                        f"into the traffic database. "
                        f"{_save_res.get('inserted', 0):,} rows inserted, "
                        f"{_save_res.get('skipped', 0):,} duplicates skipped. "
                        f"Go to **Traffic & Terminal Analysis** to see the results."
                    ),
                    df=_traffic_result,
                )
    except Exception:
        pass  # Not a traffic file — continue to universal parser

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    try:
        parsed = universal_parser.parse_universal(file_obj, file_name)
    except universal_parser.UniversalParseError as exc:
        msg = str(exc)
        if prior_error:
            msg = (
                f"'{file_name}' did not match any predefined report layout "
                f"({prior_error}), and automatic schema detection also could not "
                f"understand it: {msg}"
            )
        return ProcessResult(success=False, file_name=file_name, stage="reading", message=msg)
    except Exception as exc:  # pragma: no cover — last-resort safety net
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="reading",
            message=f"Unexpected error during automatic schema detection of '{file_name}': {exc}",
        )

    warnings: list[str] = []
    if prior_error:
        warnings.append(
            "This file did not match any predefined report layout — it was "
            "imported via automatic schema detection instead. Please review "
            "the detected mapping below."
        )
    warnings.extend(parsed.report_lines())
    warnings.extend(parsed.warnings)

    try:
        df = _validate_and_clean(parsed.df)
    except ValueError as exc:
        return ProcessResult(
            success=False, file_name=file_name, stage="validating", message=str(exc)
        )
    if df.empty:
        return ProcessResult(
            success=False,
            file_name=file_name,
            stage="validating",
            message=(
                f"'{file_name}' was understood by automatic schema detection, but "
                f"no usable revenue rows remained after validation."
            ),
        )

    report_date = df["date"].iloc[0] if not df.empty else None
    total_revenue = float(pd.to_numeric(df["revenue"], errors="coerce").sum())
    total_pax = float(pd.to_numeric(df["pax"], errors="coerce").sum())

    inserted, skipped = 0, 0
    if save_to_db:
        try:
            result = database.save_dataframe(df, source_file=file_name)
        except Exception as exc:
            return ProcessResult(
                success=False,
                file_name=file_name,
                stage="saving",
                message=(
                    f"'{file_name}' was parsed via automatic schema detection "
                    f"({len(df):,} row(s)), but saving it to the database failed: {exc}"
                ),
            )
        inserted, skipped = result["inserted"], result["skipped"]

    message_parts = [
        f"Automatically detected the schema of '{file_name}' "
        f"(confidence {parsed.confidence:.0%}) and parsed {len(df):,} row(s)."
    ]
    if save_to_db:
        message_parts.append(f"{inserted:,} new row(s) saved.")
        if skipped:
            message_parts.append(f"{skipped:,} duplicate row(s) skipped (already in database).")

    return ProcessResult(
        success=True,
        file_name=file_name,
        stage="saving" if save_to_db else "validating",
        message=" ".join(message_parts),
        df=df,
        report_date=report_date,
        total_revenue=total_revenue,
        total_pax=total_pax,
        inserted=inserted,
        skipped=skipped,
        warnings=warnings,
    )


def _parse_excel_with_detection(file_obj) -> tuple[pd.DataFrame, Optional[str]]:
    """
    Try, in order:
      1. A long-format sheet anywhere in the workbook (any sheet name).
      2. Every wide pivot/cross-tab sheet anywhere in the workbook (any
         sheet name) — one row per date with PAX./Revenue. column-pairs
         repeated per outlet under merged Location/Segment/Outlet header
         rows. All matching sheets are parsed and combined (not just the
         largest), since some workbooks split this layout across several
         sheets (e.g. one per month) — see
         excel_parser.detect_all_wide_pivot_sheets for why. Rows that
         appear in more than one matching sheet for the same (date,
         segment, outlet, location) are de-duplicated, keeping whichever
         sheet was processed first (smallest/most-specific sheets first).
      3. The generic fuzzy-column parser on the first sheet, as a last
         resort for a small ad-hoc spreadsheet without a perfectly clean
         header row.

    Returns (dataframe, description_of_what_was_detected_or_None).
    """
    try:
        long_match = excel_parser.detect_long_format_sheet(file_obj)
    except excel_parser.ExcelParseError:
        long_match = None

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    if long_match is not None:
        df = excel_parser.parse_revenue_dashboard(
            file_obj, sheet_name=long_match["sheet_name"], header_row_idx=long_match["header_row_idx"]
        )
        return df, f"long-format sheet '{long_match['sheet_name']}'"

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        pivot_matches = excel_parser.detect_all_wide_pivot_sheets(file_obj)
    except excel_parser.ExcelParseError:
        pivot_matches = []

    if pivot_matches:
        frames = []
        sheet_names_used = []
        for match in pivot_matches:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            try:
                frame = excel_parser.parse_wide_pivot_sheet(file_obj, match)
            except Exception:
                continue
            if frame is not None and not frame.empty:
                frames.append(frame)
                sheet_names_used.append(match["sheet_name"])

        if frames:
            combined = pd.concat(frames, ignore_index=True)
            before_count = len(combined)
            combined = combined.drop_duplicates(
                subset=["date", "segment", "outlet", "location"], keep="first"
            ).reset_index(drop=True)
            dropped_count = before_count - len(combined)

            sheets_desc = ", ".join(f"'{s}'" for s in sheet_names_used)
            description = f"wide pivot sheet(s) {sheets_desc}"
            if dropped_count:
                description += (
                    f" ({dropped_count:,} row(s) that appeared in more than one sheet for the "
                    "same date/outlet were de-duplicated)"
                )
            return combined, description

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    df = excel_parser.parse_generic_excel(file_obj)
    return df, None


def _safe_cross_validate(df: pd.DataFrame, file_obj) -> Optional[dict]:
    """Re-validate against page 1's summary, tolerating a fresh read of file_obj."""
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        return pdf_parser.cross_validate_against_summary(df, file_obj)
    except Exception:
        return None


_KNOWN_ACRONYM_SEGMENTS = {"ehpl"}


def _title_case_preserving_acronyms(value: str) -> str:
    """
    Title-case a segment label, except for known acronyms (currently just
    "EHPL") which should stay all-caps rather than becoming "Ehpl".
    """
    if value.strip().lower() in _KNOWN_ACRONYM_SEGMENTS:
        return value.strip().upper()
    return value.title()


def _validate_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize column names/types and drop unusable rows. This is the
    common cleanup step run regardless of which parser produced the
    DataFrame, so the database layer always receives a consistent shape.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Parsed data is missing required column(s): {missing}")

    df["segment"] = df["segment"].astype(str).str.strip().apply(_title_case_preserving_acronyms)
    df["outlet"] = df["outlet"].astype(str).str.strip()
    df["location"] = df["location"].astype(str).str.strip().str.title()

    df["pax"] = pd.to_numeric(df["pax"], errors="coerce")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    if "aop" in df.columns:
        df["aop"] = pd.to_numeric(df["aop"], errors="coerce")
    else:
        df["aop"] = pd.NA
    if "traffic" in df.columns:
        df["traffic"] = pd.to_numeric(df["traffic"], errors="coerce")
    else:
        df["traffic"] = pd.NA

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])

    # A row with neither PAX nor Revenue carries no information — drop it.
    df = df[~(df["pax"].isna() & df["revenue"].isna())]

    df = df.drop_duplicates(subset=["date", "segment", "outlet", "location"], keep="last")
    df = df.reset_index(drop=True)
    return df
