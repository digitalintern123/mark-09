"""
upload_status.py — Shared, consistent upload-status feedback for every file
upload point in the app (daily reports, historical import, AOP import,
traffic import).

Every upload should show the same sequence so the user always knows
exactly what's happening and what happened:
  1. File selected         — confirmation of what was picked, before any
                              processing starts.
  2. Upload in progress     — a spinner while reading/parsing/saving runs.
  3. Processing completed   — (folded into step 4's success message; kept
                              as a concept in the docstring/spec, shown via
                              the same success block as step 4 since
                              splitting them into two separate boxes for
                              a single click added more clutter than
                              clarity in practice.)
  4. Upload successful, or Upload failed with reason — the final outcome,
     always shown explicitly, never left for the user to infer from the
     page just changing.

This module doesn't know anything about data_processor.ProcessResult's
internals beyond duck-typing the attributes it needs (success, file_name,
message, stage, inserted, skipped, warnings, total_revenue, total_pax,
report_date) — render_file_selected/render_result take plain values too,
so any upload point (including the AOP/traffic importers, whose results
aren't ProcessResult instances) can use the same rendering.
"""

from __future__ import annotations

import streamlit as st

_STAGE_LABELS = {
    "reading": "reading and parsing the file",
    "validating": "validating the data",
    "saving": "saving to the database",
    "processing": "processing the file",
}


def render_file_selected(file_name: str, file_size_bytes: int | None = None) -> None:
    """
    Step 1: confirm exactly which file was picked, before any processing
    starts — shown as soon as a file is selected in the uploader, so the
    user has positive confirmation of the filename even before clicking
    the process/import button.
    """
    size_str = f" ({_format_size(file_size_bytes)})" if file_size_bytes else ""
    st.caption(f"📄 File selected: **{file_name}**{size_str}")


def render_result(
    success: bool,
    file_name: str,
    message: str,
    stage: str = "processing",
    inserted: int | None = None,
    skipped: int | None = None,
    warnings: list[str] | None = None,
    extra_lines: list[str] | None = None,
    expected_format: str | None = None,
) -> None:
    """
    Steps 3+4 combined: render the final outcome of an upload — always
    one of exactly two shapes, success or failure, never left ambiguous:

    Success:
        ✅ File uploaded successfully
        File Name: <file_name>
        Status: Uploaded and processed successfully
        <any extra detail lines: rows saved, duplicates skipped, totals>

    Failure:
        ❌ File upload failed
        File Name: <file_name>
        Stage: <which step failed — reading / validating / saving>
        Reason: <the exact message>
        <expected_format spec, if given — a "here's what a valid file for
        this upload box looks like" block, always in a follow-up st.info,
        never folded into the same error box, so the "did it fail" signal
        (red) stays visually distinct from the "here's how to fix it"
        signal (blue).>

    `warnings` (non-fatal issues alongside a successful upload, e.g. a
    cross-validation discrepancy) are shown as a follow-up info box, not
    folded into the success message itself, so the headline success/failure
    state is never ambiguous at a glance.

    `expected_format`, if given, is only shown on failure — a markdown
    description of the file layout this specific upload box expects, so
    the person doesn't have to guess or dig through documentation to see
    what a valid file looks like. Every call site that has a defined
    format (see the FORMAT_* constants below) should pass it.
    """
    if success:
        lines = [
            "✅ **File uploaded successfully**",
            f"- **File Name:** {file_name}",
            "- **Status:** Uploaded and processed successfully",
        ]
        if inserted is not None:
            lines.append(f"- **Rows saved:** {inserted:,}")
        if skipped:
            lines.append(f"- **Duplicate rows skipped:** {skipped:,} (already in database)")
        for extra in extra_lines or []:
            lines.append(f"- {extra}")
        st.success("\n".join(lines))

        for warning in warnings or []:
            st.info(f"ℹ️ {warning}")
    else:
        stage_label = _STAGE_LABELS.get(stage, _STAGE_LABELS["processing"])
        lines = [
            "❌ **File upload failed**",
            f"- **File Name:** {file_name}",
            f"- **Stage:** Failed while {stage_label}",
            f"- **Reason:** {message}",
        ]
        st.error("\n".join(lines))

        if expected_format:
            st.info(
                "📋 **This box expects the following format** — please check your "
                "file against it and re-upload:\n\n" + expected_format
            )


def render_result_from_process_result(
    pr, extra_lines: list[str] | None = None, expected_format: str | None = None
) -> None:
    """
    Convenience wrapper for the common case: render_result() fed directly
    from a data_processor.ProcessResult instance, so call sites don't need
    to manually unpack every field.
    """
    render_result(
        success=pr.success,
        file_name=pr.file_name,
        message=pr.message,
        stage=getattr(pr, "stage", "processing"),
        inserted=getattr(pr, "inserted", None) if pr.success else None,
        skipped=getattr(pr, "skipped", None) if pr.success else None,
        warnings=getattr(pr, "warnings", None),
        extra_lines=extra_lines,
        expected_format=expected_format,
    )


# ---------------------------------------------------------------------------
# Defined-format specs, one per upload box — passed to render_result(...,
# expected_format=...) so a failed upload always tells the person exactly
# what a valid file looks like for that specific box, not just what went
# wrong with the one they tried.
# ---------------------------------------------------------------------------

FORMAT_DAILY_REPORT = """\
**PDF** — the standard "Detailed Revenue" report: a `For the Day -> DD-MM-YYYY` header on page 1, and an outlet-level table on page 2.

**Excel** — one row per (date, outlet), with these columns (reasonable naming variants are accepted, e.g. "Business" for Segment):

| Column | Required | Example |
|---|---|---|
| Date | ✅ | 2026-06-26 |
| Segment / Business | ✅ | EHPL, Sky Plates, Encalm Eats |
| Outlet / Sub-Business | ✅ | Lounge A |
| Location | ✅ | Delhi, Hyderabad, Goa |
| PAX | ✅ | 120 |
| Revenue | ✅ | 45000 |
| AOP | optional | 50000 |
| Traffic | optional | 50000 |
"""

FORMAT_HISTORICAL_EXCEL = FORMAT_DAILY_REPORT + (
    "\nFor a bulk historical workbook, this can be any sheet in the file "
    "(named anything) — it's detected automatically by looking for a row "
    "with these headers, not by sheet name."
)

FORMAT_AOP = """\
One of three layouts, auto-detected — no need to say which one you're uploading:

1. **Per-outlet/monthly** — a header row with `Geographical Segment`, `Business Segment`, and `Unit-ID` columns, followed by monthly date columns (one fiscal year per 12 columns).
2. **Daily outlet-level pivot** — a `Date` row-header with one column per outlet (e.g. "Meet & Greet", "T1D L4&5 Lounge"), grouped under a location banner row (Delhi/Hyderabad/Goa) above it.
3. **Daily-total-per-location** — a `Date` row-header with one column per location (Delhi/Hyderabad/Goa) and a Grand Total column, no outlet breakdown.

Only Delhi/Hyderabad/Goa data is imported from any of the three.
"""

FORMAT_TRAFFIC = """\
**Simple flat table** — one row per day (or per day+terminal), with columns: `Date`, `Location`, `Traffic`, and optionally `Terminal`.

**Or a per-location cross-tab** — one sheet per airport (e.g. "DEL", "HYD", "GOX"), with Terminal × Departure/Arrival column pairs, either one row per month or one row per day.
"""


def get_sheet_names(file_obj) -> list[str]:
    """
    Return every sheet name in an Excel workbook, or [] for a PDF or an
    unreadable file. Always leaves the file object seeked back to 0
    afterward, so it's safe to call before any other reading of the same
    file object.
    """
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        import pandas as pd

        sheet_names = pd.ExcelFile(file_obj, engine="openpyxl").sheet_names
    except Exception:
        sheet_names = []
    finally:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
    return sheet_names


AUTO_DETECT_LABEL = "🔍 Auto-detect (recommended)"


def render_sheet_selector(
    file_name: str,
    sheet_names: list[str],
    key_prefix: str,
    label: str | None = None,
    default_sheet: str | None = None,
    allow_auto_detect: bool = False,
) -> str | None:
    """
    If a workbook has more than one sheet, show a dropdown listing every
    sheet name and return the user's choice; if it has exactly one sheet
    (and `allow_auto_detect` is False), return that sheet's name without
    asking (nothing to choose). Returns None if `sheet_names` is empty
    (e.g. this is a PDF, or the file couldn't be read).

    `allow_auto_detect`, if True, prepends an "Auto-detect (recommended)"
    choice, selected by default — picking it returns None, signaling
    "don't force a specific sheet; let the caller's own detection decide"
    instead of the sheet the person happened to have open. This matters
    a lot for callers whose own auto-detection is smarter than a fixed
    single-sheet parse: e.g. the revenue-upload path scans every sheet
    for a long-format or wide-pivot layout, including scanning several
    rows to find the real header row wherever it actually is (a workbook
    can have title/banner rows above the real headers) — forcing one
    specific sheet here bypasses all of that and falls back to a much
    more naive "assume row 0 is the header row" parse instead, which
    silently breaks on exactly that kind of file. With only one sheet
    and `allow_auto_detect=True`, nothing is asked at all (there's only
    one sheet to detect from), and None is returned so the caller's own
    detection still runs rather than being skipped.
    """
    if not sheet_names:
        return None

    if allow_auto_detect:
        if len(sheet_names) <= 1:
            return None
        options = [AUTO_DETECT_LABEL] + sheet_names
        choice = st.selectbox(
            label or f"📑 Which sheet has the data in '{file_name}'?",
            options=options,
            index=0,  # always default to Auto-detect itself — pre-selecting a
                      # specific (even correctly-detected) sheet here would
                      # force the naive single-sheet parser just the same as
                      # any other manual override; only the person choosing
                      # to override should ever leave this default.
            key=f"{key_prefix}_sheet_select",
        )
        return None if choice == AUTO_DETECT_LABEL else choice

    if len(sheet_names) == 1:
        return sheet_names[0]

    index = sheet_names.index(default_sheet) if default_sheet in sheet_names else 0
    return st.selectbox(
        label or f"📑 '{file_name}' has {len(sheet_names)} sheets — which one do you want to upload?",
        options=sheet_names,
        index=index,
        key=f"{key_prefix}_sheet_select",
    )


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
