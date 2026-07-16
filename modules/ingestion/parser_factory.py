"""
parser_factory.py — Main ingestion orchestrator.

parse(file_obj, file_name) is the single public function.  It:

  1. FileDetector   → detect format, encoding, sheet count, scanned-PDF flag
  2. TableDetector  → extract all candidate tables (every sheet/page/table)
  3. OCR Engine     → if scanned PDF or image, OCR first
  4. SchemaDetector → find header row, detect layout / orientation
  5. DataCleaner    → melt wide layouts, forward-fill, drop totals
  6. ColumnMapper   → assign canonical roles to columns
  7. Normalizer     → build the standard output DataFrame
  8. Validator      → check dates, revenue, duplicates
  9. ConfidenceEngine → score the extraction; invoke AI fallback if needed
 10. Return IngestionResult

The result's .df property is a DataFrame with the same schema as every
other parser in this app (date, segment, outlet, location, pax, revenue,
aop, traffic) — ready to be passed directly to data_processor._validate_and_clean()
and database.save_dataframe() without any changes downstream.

Logging:
  Every decision (table chosen, column mapped, value recovered, row dropped,
  confidence score) is written to the "ingestion" Python logger.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

log = logging.getLogger("ingestion")


@dataclass
class IngestionResult:
    """Returned to the caller (data_processor) for every file processed."""
    success: bool
    file_name: str
    df: Optional[pd.DataFrame]          # canonical schema, ready for DB
    confidence: float                   # 0..1
    confidence_level: str               # "high" | "medium" | "low"
    mapping_report: list[str]           # human-readable field-mapping lines
    validation_summary: str             # per-check summary
    warnings: list[str]
    errors: list[str]
    rows_total: int = 0
    rows_dropped: int = 0
    unit_multiplier: float = 1.0
    source_description: str = ""        # "sheet 'Data'" etc.
    duplicate_log: pd.DataFrame = field(default_factory=pd.DataFrame)

    def user_message(self) -> str:
        """One-line status message for the upload-status UI."""
        if not self.success:
            return f"❌ {'; '.join(self.errors[:2])}"
        parts = [
            f"Auto-detected schema ({self.source_description})",
            f"confidence {self.confidence:.0%}",
            f"{self.rows_total:,} row(s) extracted",
        ]
        if self.rows_dropped:
            parts.append(f"{self.rows_dropped:,} row(s) dropped (totals/blanks)")
        return " — ".join(parts) + "."


def parse(
    file_obj,
    file_name: str,
    upload_time: Optional[dt.datetime] = None,
    ai_model: str = "llama3",
    max_workers: int = 4,
) -> IngestionResult:
    """
    Universal document ingestion entry point.

    Tries every candidate table in parallel (one thread per candidate),
    picks the highest-confidence successful parse, and returns the result.
    """
    warnings: list[str] = []
    errors:   list[str] = []
    upload_time = upload_time or dt.datetime.now()

    # ── 1. File detection ────────────────────────────────────────────────
    from .file_detector import detect, FMT_UNKNOWN
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        meta = detect(file_obj, file_name)
        warnings.extend(meta.warnings)
        log.info("[%s] Detected format=%s pages=%d sheets=%d scanned=%s",
                 file_name, meta.format, meta.page_count,
                 meta.sheet_count, meta.is_scanned_pdf)
    except Exception as exc:
        errors.append(f"File detection failed: {exc}")
        return _fail(file_name, errors)

    if meta.format == FMT_UNKNOWN:
        errors.append(
            f"'{file_name}' is not a supported file format. "
            f"Supported: Excel, PDF, CSV, TSV, TXT, DOCX, HTML, XML, JSON, PNG, "
            f"JPG, TIFF, Outlook MSG."
        )
        return _fail(file_name, errors)

    # ── 2. OCR if needed ─────────────────────────────────────────────────
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    if meta.needs_ocr:
        log.info("[%s] OCR required (scanned=%s image=%s)",
                 file_name, meta.is_scanned_pdf, meta.format == "image")
        ocr_candidates = _run_ocr(file_obj, file_name, meta, warnings, errors)
        if not ocr_candidates and errors:
            return _fail(file_name, errors)
        candidates = ocr_candidates
    else:
        # ── 3. Table extraction ──────────────────────────────────────────
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        from .table_detector import extract_candidates, rank_candidates
        try:
            candidates = extract_candidates(file_obj, file_name, meta.format, meta)
            candidates = rank_candidates(candidates)
            log.info("[%s] %d candidate table(s) extracted", file_name, len(candidates))
        except Exception as exc:
            errors.append(f"Table extraction failed: {exc}")
            return _fail(file_name, errors)

    if not candidates:
        errors.append(
            f"No tables found in '{file_name}'. "
            "If this is a scanned PDF or image, ensure Tesseract is installed."
        )
        return _fail(file_name, errors)

    # ── 4–9. Parse candidates (parallel) ─────────────────────────────────
    results: list[IngestionResult] = []
    limit = min(len(candidates), 6)   # don't fan out too wide

    with ThreadPoolExecutor(max_workers=min(max_workers, limit)) as pool:
        futures = {
            pool.submit(
                _parse_one_candidate, c, file_name, upload_time, ai_model
            ): c
            for c in candidates[:limit]
        }
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result is not None:
                    results.append(result)
            except Exception as exc:
                log.debug("Candidate parse error: %s", exc)

    # Pick the highest-confidence successful result
    successes = [r for r in results if r.success and r.df is not None and not r.df.empty]
    if not successes:
        failures = [r for r in results if r.errors]
        all_errors = []
        for r in failures[:3]:
            all_errors.extend(r.errors)
        errors.append(
            f"Could not extract revenue data from '{file_name}'. "
            + (" | ".join(all_errors[:4]) if all_errors else
               "No table matched the expected revenue schema.")
        )
        return _fail(file_name, errors)

    best = max(successes, key=lambda r: (r.confidence, len(r.df) if r.df is not None else 0))
    if len(successes) > 1:
        best.warnings.append(
            f"{len(successes)} table(s) matched the revenue schema — "
            f"imported the best match ({best.source_description}). "
            "Upload others individually if they are separate datasets."
        )
    best.warnings = warnings + best.warnings
    return best


# ── Single-candidate pipeline ─────────────────────────────────────────────

def _parse_one_candidate(candidate, file_name: str,
                         upload_time: dt.datetime,
                         ai_model: str) -> Optional[IngestionResult]:
    """Run steps 4–9 for one candidate table. Returns None on hard failure."""
    from .table_detector import CandidateTable
    from .schema_detector import detect_schema
    from .data_cleaner import clean
    from .column_mapper import map_columns
    from .normalizer import build_output
    from .validator import validate
    from .confidence_engine import compute, ai_extract, apply_ai_mapping

    warnings: list[str] = []
    errors:   list[str] = []
    c: CandidateTable = candidate
    warnings.extend(c.warnings)

    # 4. Schema detection
    try:
        schema = detect_schema(c.grid)
        warnings.extend(schema.warnings)
        log.debug("[%s] %s: header_row=%d orientation=%s layout=%s",
                  file_name, c.source, schema.header_row_idx,
                  schema.orientation, schema.layout)
    except Exception as exc:
        return _one_fail(file_name, c.source, [f"Schema detection: {exc}"])

    if not schema.headers or schema.data.empty:
        return _one_fail(file_name, c.source,
                         ["Could not identify a header row in this table."])

    # 5. Data cleaning
    try:
        headers, data, unit_mult = clean(
            schema.headers, schema.data,
            schema.title_context, schema.layout, schema.orientation,
        )
        rows_before = len(schema.data)
        rows_after  = len(data)
        rows_dropped = rows_before - rows_after
        log.debug("[%s] %s: %d→%d rows after cleaning, unit_mult=%.0f",
                  file_name, c.source, rows_before, rows_after, unit_mult)
    except Exception as exc:
        return _one_fail(file_name, c.source, [f"Data cleaning: {exc}"])

    if data.empty:
        return _one_fail(file_name, c.source,
                         ["No usable data rows remained after cleaning."])

    # 6. Column mapping
    try:
        mapping = map_columns(headers, data)
        log.debug("[%s] %s: mapped roles=%s missing=%s",
                  file_name, c.source,
                  list(mapping.assignments.keys()), mapping.missing_required)
    except Exception as exc:
        return _one_fail(file_name, c.source, [f"Column mapping: {exc}"])

    # 7. Normalisation (try once; if required roles missing, attempt AI)
    df: Optional[pd.DataFrame] = None
    try:
        df, norm_warnings = build_output(
            mapping, data, schema.title_context,
            c.sheet_name, file_name, unit_mult, upload_time,
        )
        warnings.extend(norm_warnings)
    except ValueError as exc:
        # Missing required field — try AI fallback before giving up
        log.info("[%s] %s: normalisation error: %s — trying AI", file_name, c.source, exc)
        ai_result = ai_extract(c.grid, schema.title_context, file_name, model=ai_model)
        if ai_result:
            try:
                mapping = apply_ai_mapping(ai_result, headers, data)
                df, norm_warnings = build_output(
                    mapping, data, schema.title_context,
                    c.sheet_name, file_name, unit_mult, upload_time,
                )
                warnings.extend(norm_warnings)
                warnings.append(
                    "This file was processed with AI-assisted field detection "
                    "(rule-based mapping could not confidently identify all required fields). "
                    "Please review the mapping report."
                )
            except Exception as exc2:
                errors.append(str(exc2))
                return _one_fail(file_name, c.source, errors)
        else:
            errors.append(str(exc))
            return _one_fail(file_name, c.source, errors)

    if df is None or df.empty:
        return _one_fail(file_name, c.source, ["Normalisation produced no rows."])

    # 8. Validation
    try:
        val_report = validate(df)
        warnings.extend(val_report.warnings)
        log.debug("[%s] %s: validation %s", file_name, c.source,
                  "PASSED" if val_report.passed else "ISSUES: " + str(val_report.critical_failures))
    except Exception as exc:
        val_report = None
        warnings.append(f"Validation error (non-fatal): {exc}")
        from .validator import ValidationReport
        val_report = ValidationReport()

    # 9. Confidence
    try:
        conf = compute(mapping, val_report, df)
        log.info("[%s] %s: confidence=%.2f (%s)", file_name, c.source,
                 conf.score, conf.level)
    except Exception as exc:
        warnings.append(f"Confidence scoring error (non-fatal): {exc}")
        from .confidence_engine import ConfidenceResult
        conf = ConfidenceResult(0.5, "medium", {}, False, "Score unavailable")

    # If still low-confidence even after AI, accept with prominent warning
    if conf.needs_ai_fallback:
        warnings.append(
            f"⚠️ Low confidence ({conf.score:.0%}). "
            "The field mapping may be incorrect. Please verify the data before using it in analytics."
        )

    # Build mapping report lines
    mapping_lines = [
        f"Auto-detected schema ({c.source}) — overall confidence {conf.score:.0%}:"
    ]
    for a in mapping.assignments.values():
        mapping_lines.append(
            f"  • {a.role} ← '{a.source_col}' ({a.method}, {a.confidence:.0%})"
        )
    if unit_mult != 1.0:
        mapping_lines.append(f"  • values scaled ×{unit_mult:,.0f} (unit declared in title)")
    if rows_dropped:
        mapping_lines.append(f"  • {rows_dropped} subtotal/blank/invalid row(s) excluded")

    return IngestionResult(
        success=True,
        file_name=file_name,
        df=df,
        confidence=conf.score,
        confidence_level=conf.level,
        mapping_report=mapping_lines,
        validation_summary=val_report.summary() if val_report else "",
        warnings=warnings,
        errors=[],
        rows_total=len(df),
        rows_dropped=rows_dropped,
        unit_multiplier=unit_mult,
        source_description=c.source,
        duplicate_log=val_report.duplicate_log if val_report else pd.DataFrame(),
    )


# ── OCR path ──────────────────────────────────────────────────────────────

def _run_ocr(file_obj, file_name: str, meta, warnings, errors):
    from .ocr_engine import pdf_to_grids, image_to_grid, OCRError
    from .table_detector import CandidateTable, rank_candidates

    ocr_candidates: list[CandidateTable] = []
    try:
        if meta.is_scanned_pdf:
            grids = pdf_to_grids(file_obj)
        else:
            grid, source = image_to_grid(file_obj, file_name)
            grids = [(grid, source)]

        for grid, source in grids:
            ocr_candidates.append(CandidateTable(grid=grid, source=f"OCR: {source}"))
        ocr_candidates = rank_candidates(ocr_candidates)
        log.info("[%s] OCR produced %d candidate(s)", file_name, len(ocr_candidates))
    except OCRError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"OCR failed: {exc}")
    return ocr_candidates


# ── Failure helpers ───────────────────────────────────────────────────────

def _fail(file_name: str, errors: list[str]) -> IngestionResult:
    return IngestionResult(
        success=False, file_name=file_name, df=None,
        confidence=0.0, confidence_level="low",
        mapping_report=[], validation_summary="",
        warnings=[], errors=errors,
    )


def _one_fail(file_name: str, source: str, errors: list[str]) -> IngestionResult:
    log.debug("[%s] %s failed: %s", file_name, source, errors)
    return IngestionResult(
        success=False, file_name=file_name, df=None,
        confidence=0.0, confidence_level="low",
        mapping_report=[], validation_summary="",
        warnings=[], errors=errors,
        source_description=source,
    )
