"""
app_logger.py — Centralised error logging and user-friendly error display.

All unhandled exceptions in pages flow through this module:
  - Users see a clear, business-friendly message (no tracebacks)
  - Developers get full details in encalm_analytics.log

Usage:
    from modules.app_logger import log_exception, safe_run, show_friendly_error
    
    # Option 1 — wrap a whole section
    with safe_run("Loading revenue data"):
        df = database.load_for_date_range(start, end)
    
    # Option 2 — catch manually and log
    try:
        result = do_something()
    except Exception as e:
        log_exception(e, context="building comparison table")
        show_friendly_error("traffic")
"""
from __future__ import annotations

import logging
import traceback
import datetime as dt
from contextlib import contextmanager
from typing import Optional

import streamlit as st

# ---------------------------------------------------------------------------
# Logger setup — writes to file only; never to the Streamlit UI
# ---------------------------------------------------------------------------
_logger = logging.getLogger("encalm_analytics")
if not _logger.handlers:
    _handler = logging.FileHandler("encalm_analytics.log", encoding="utf-8")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Friendly message catalogue
# ---------------------------------------------------------------------------
_MESSAGES: dict[str, dict] = {
    "no_data": {
        "level": "info",
        "title": "No data available for the selected period.",
        "body": (
            "There are no records matching your current filters. "
            "Try selecting a different date, location, or comparison period, "
            "or upload the relevant revenue report on the Home page."
        ),
    },
    "no_comparison": {
        "level": "info",
        "title": "No comparison data found.",
        "body": (
            "The selected comparison period has no data yet. "
            "Revenue analysis for the current period is shown above. "
            "Upload the comparison period's report to enable variance metrics."
        ),
    },
    "no_traffic": {
        "level": "info",
        "title": "Traffic data is not available for this period.",
        "body": (
            "Revenue analysis has been completed, but Traffic, Penetration %, "
            "and SPP metrics cannot be calculated until airport traffic data is uploaded. "
            "Go to the Home page → Traffic Data Import to upload a traffic file."
        ),
    },
    "no_aop": {
        "level": "info",
        "title": "Budget (AOP) data is not available.",
        "body": (
            "Revenue comparisons are shown, but AOP targets and variance metrics "
            "cannot be calculated. Upload an AOP workbook on the Home page to enable these."
        ),
    },
    "traffic_columns": {
        "level": "warning",
        "title": "Traffic and performance metrics could not be generated.",
        "body": (
            "Possible reasons:\n"
            "- Traffic data has not been uploaded for one or both selected periods.\n"
            "- Terminal mapping is incomplete for some outlets.\n"
            "- The required Traffic, Penetration %, or SPP columns could not be built.\n\n"
            "Please verify that traffic data has been uploaded and outlet-to-terminal "
            "mapping is complete. Revenue and PAX figures are unaffected."
        ),
    },
    "missing_columns": {
        "level": "warning",
        "title": "Some required information is missing from the uploaded file.",
        "body": (
            "The application expected fields such as Revenue, PAX, Traffic, or Date, "
            "but they could not be identified. "
            "Please review the uploaded file or upload a compatible report."
        ),
    },
    "mapping_failure": {
        "level": "warning",
        "title": "Traffic mapping could not be completed for some outlets.",
        "body": (
            "These outlets have been excluded from Traffic-based calculations. "
            "Traffic/PEN/SPP columns will show '—' for affected rows. "
            "Update the outlet-to-terminal mapping in terminal_mapping.py to fix this."
        ),
    },
    "empty_result": {
        "level": "info",
        "title": "No records found for the selected filters.",
        "body": (
            "Try selecting a different date, location, business unit, or comparison period."
        ),
    },
    "corrupted_file": {
        "level": "error",
        "title": "The uploaded file could not be processed.",
        "body": (
            "The file may be corrupted or in an unsupported format. "
            "Please verify the file and try again. "
            "Supported formats: PDF, Excel (.xlsx/.xls), CSV."
        ),
    },
    "db_error": {
        "level": "error",
        "title": "A database error occurred.",
        "body": (
            "The application could not read or write data. "
            "This is usually temporary — please refresh the page and try again. "
            "If the problem persists, contact your system administrator."
        ),
    },
    "chart_error": {
        "level": "warning",
        "title": "This chart could not be rendered.",
        "body": (
            "The data may be incomplete or in an unexpected format. "
            "The tables above still show the full data."
        ),
    },
    "comparison_error": {
        "level": "warning",
        "title": "The comparison table could not be built.",
        "body": (
            "One or both periods may have incomplete data, or some required columns "
            "could not be generated. Revenue and PAX figures for the current period "
            "are still available above."
        ),
    },
    "generic": {
        "level": "warning",
        "title": "An unexpected issue occurred in this section.",
        "body": (
            "This section could not be displayed. The rest of the page is unaffected. "
            "Please refresh the page or try a different selection."
        ),
    },
}


def show_friendly_error(error_type: str = "generic") -> None:
    """Display a business-friendly Streamlit message for the given error type."""
    msg = _MESSAGES.get(error_type, _MESSAGES["generic"])
    level = msg["level"]
    text = f"**{msg['title']}**\n\n{msg['body']}"
    if level == "info":
        st.info(text)
    elif level == "warning":
        st.warning(text)
    else:
        st.error(text)


def log_exception(
    exc: Exception,
    context: str = "",
    filters: Optional[dict] = None,
) -> None:
    """
    Log a full exception to file (never shown to users).
    `context` describes where the error happened (e.g. "building Tab 1 comparison").
    `filters` captures the current user selections for debugging.
    """
    tb = traceback.format_exc()
    filter_str = str(filters) if filters else "none"
    _logger.error(
        f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] "
        f"Context: {context or 'unknown'} | "
        f"Filters: {filter_str} | "
        f"Exception: {type(exc).__name__}: {exc}\n{tb}"
    )


def _classify_exception(exc: Exception) -> str:
    """Map a Python exception type to a friendly error category."""
    name = type(exc).__name__
    msg = str(exc).lower()

    if name == "KeyError" or "not in index" in msg or "not in columns" in msg:
        if "traffic" in msg or "pen" in msg or "spp" in msg:
            return "traffic_columns"
        return "missing_columns"

    if name in ("FileNotFoundError", "PermissionError"):
        return "corrupted_file"

    if "operational" in msg or "sqlite" in msg.lower() or name == "OperationalError":
        return "db_error"

    if name in ("EmptyDataError",) or "empty" in msg:
        return "empty_result"

    if "traffic" in msg or "terminal" in msg:
        return "traffic_columns"

    if name in ("ValueError", "TypeError") and "column" in msg:
        return "missing_columns"

    return "generic"


@contextmanager
def safe_run(
    context: str = "",
    error_type: Optional[str] = None,
    filters: Optional[dict] = None,
    reraise: bool = False,
):
    """
    Context manager that catches any exception, logs it, and shows a
    friendly Streamlit message instead of a traceback.

    Usage:
        with safe_run("building traffic table", error_type="traffic_columns"):
            ... code that may raise ...

    If `error_type` is None, the exception type is auto-classified.
    If `reraise` is True, the exception is re-raised after logging
    (useful when the caller needs to know something failed).
    """
    try:
        yield
    except Exception as exc:
        log_exception(exc, context=context, filters=filters)
        etype = error_type or _classify_exception(exc)
        show_friendly_error(etype)
        if reraise:
            raise
