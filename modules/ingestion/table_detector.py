"""
table_detector.py — Extract and rank candidate tables from any document.

Handles:
  Excel   — every sheet, merged cells unrolled, multi-row headers preserved
  PDF     — pdfplumber (text PDFs) + Camelot lattice/stream fallback
  CSV/TSV — single grid with delimiter sniffing
  DOCX    — every table in the document body
  HTML    — every <table> via BeautifulSoup
  XML     — repeating element groups treated as rows
  JSON    — list-of-dicts or nested structures flattened
  MSG     — body + attachment tables
  Images  — via ocr_engine (caller must detect and route)

Each extracted candidate is a CandidateTable with a raw pd.DataFrame (all
object dtype, no header assumed) and metadata about its origin.  Ranking
is done by column-count, row-count, and vocabulary hit density — the
highest-ranked candidate is the most likely revenue data table.
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class CandidateTable:
    """One raw grid extracted from a document."""
    grid: pd.DataFrame        # object dtype, no header assumed
    source: str               # e.g. "sheet 'Data'", "PDF page 3 table 1"
    sheet_name: str = ""      # for Excel
    page_no: int = 0          # for PDF
    table_index: int = 0
    relevance_score: float = 0.0   # set by rank_candidates()
    warnings: list[str] = field(default_factory=list)


# Vocabulary words that strongly suggest a revenue/traffic data table.
_REVENUE_VOCAB = {
    "date", "revenue", "sales", "pax", "passengers", "footfall", "guests",
    "outlet", "lounge", "location", "city", "airport", "segment", "business",
    "income", "amount", "turnover", "collection", "billing", "traffic",
    "domestic", "international", "terminal", "aop", "budget", "target",
    "atithya", "ehpl", "encalm", "sky plates", "eats", "goa", "delhi",
    "hyderabad", "hyd", "del", "gox",
}


# ── Public API ────────────────────────────────────────────────────────────

def extract_candidates(file_obj, file_name: str, fmt: str,
                       meta=None) -> list[CandidateTable]:
    """
    Extract every candidate table from the file.
    `fmt` is one of the FMT_* constants from file_detector.
    Returns an unranked list; call rank_candidates() afterwards.
    """
    from .file_detector import (
        FMT_EXCEL, FMT_PDF, FMT_CSV, FMT_TSV, FMT_TXT,
        FMT_DOCX, FMT_HTML, FMT_XML, FMT_JSON, FMT_IMAGE, FMT_MSG,
    )

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    if fmt == FMT_EXCEL:
        return _from_excel(file_obj)
    elif fmt == FMT_PDF:
        return _from_pdf(file_obj, meta)
    elif fmt in (FMT_CSV, FMT_TSV, FMT_TXT):
        return _from_delimited(file_obj, meta)
    elif fmt == FMT_DOCX:
        return _from_docx(file_obj)
    elif fmt == FMT_HTML:
        return _from_html(file_obj)
    elif fmt == FMT_XML:
        return _from_xml(file_obj)
    elif fmt == FMT_JSON:
        return _from_json(file_obj)
    elif fmt == FMT_MSG:
        return _from_msg(file_obj, file_name)
    elif fmt == FMT_IMAGE:
        return _from_image(file_obj, file_name)
    else:
        return []


def rank_candidates(candidates: list[CandidateTable]) -> list[CandidateTable]:
    """
    Score and sort candidates: largest / most vocabulary-rich table first.
    Decorative tables (< 2 columns or < 2 data rows) are pushed to the end.
    """
    for c in candidates:
        c.relevance_score = _score(c.grid)
    candidates.sort(key=lambda c: c.relevance_score, reverse=True)
    return candidates


# ── Extractors ────────────────────────────────────────────────────────────

def _from_excel(file_obj) -> list[CandidateTable]:
    candidates: list[CandidateTable] = []
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
    except Exception as exc:
        log.warning("Excel open failed: %s", exc)
        return candidates

    for sheet_name in xl.sheet_names:
        try:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            raw = xl.parse(sheet_name, header=None, dtype=object)
            if raw.dropna(how="all").empty:
                continue
            # Unroll merged cells: forward-fill blank cells left-to-right,
            # top-to-bottom (mimics how Excel visually displays merged cells).
            raw = _unroll_merged(raw, file_obj, sheet_name)
            candidates.append(CandidateTable(
                grid=raw.reset_index(drop=True),
                source=f"sheet '{sheet_name}'",
                sheet_name=sheet_name,
            ))
        except Exception as exc:
            log.warning("Sheet '%s' extraction failed: %s", sheet_name, exc)

    return candidates


def _unroll_merged(raw: pd.DataFrame, file_obj, sheet_name: str) -> pd.DataFrame:
    """Forward-fill cells that were merged in the source workbook."""
    try:
        import openpyxl
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        wb = openpyxl.load_workbook(file_obj, read_only=False, data_only=True)
        ws = wb[sheet_name]
        # openpyxl already expands merged cells when read_only=False,
        # so we just re-read with ffill for any remaining NaN runs.
        wb.close()
    except Exception:
        pass
    # Horizontal ffill for merged row-spans
    raw = raw.ffill(axis=1)
    return raw


def _from_pdf(file_obj, meta=None) -> list[CandidateTable]:
    """Try pdfplumber first; fall back to Camelot lattice + stream."""
    candidates: list[CandidateTable] = []

    # --- pdfplumber (text strategy) ---
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        import pdfplumber
        with pdfplumber.open(file_obj) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables() or []
                if not tables:
                    try:
                        tables = page.extract_tables({
                            "vertical_strategy": "text",
                            "horizontal_strategy": "text",
                        }) or []
                    except Exception:
                        pass
                for t_idx, table in enumerate(tables):
                    if not table:
                        continue
                    width = max(len(r) for r in table)
                    norm = [list(r) + [None] * (width - len(r)) for r in table]
                    grid = pd.DataFrame(norm, dtype=object)
                    candidates.append(CandidateTable(
                        grid=grid,
                        source=f"PDF page {page_no} table {t_idx + 1}",
                        page_no=page_no,
                        table_index=t_idx,
                    ))
    except Exception as exc:
        log.warning("pdfplumber extraction failed: %s", exc)

    if candidates:
        return candidates

    # --- Camelot fallback ---
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        import camelot, tempfile, os
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(file_obj.read())
        tmp.flush(); tmp.close()
        try:
            for flavor in ("lattice", "stream"):
                try:
                    tables = camelot.read_pdf(tmp.name, flavor=flavor, pages="all")
                    for t_idx, table in enumerate(tables):
                        grid = table.df.copy().astype(object)
                        candidates.append(CandidateTable(
                            grid=grid,
                            source=f"PDF camelot-{flavor} table {t_idx + 1}",
                            table_index=t_idx,
                        ))
                    if candidates:
                        break
                except Exception as exc:
                    log.debug("Camelot %s failed: %s", flavor, exc)
        finally:
            os.unlink(tmp.name)
    except Exception as exc:
        log.warning("Camelot fallback failed: %s", exc)

    return candidates


def _from_delimited(file_obj, meta=None) -> list[CandidateTable]:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        raw_bytes = file_obj.read()
        enc = (meta.encoding if meta and meta.encoding else None) or "utf-8"
        text = raw_bytes.decode(enc, errors="replace") if isinstance(raw_bytes, bytes) else raw_bytes
        sep = (meta.delimiter if meta and meta.delimiter else None) or ","

        import csv
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
            sep = dialect.delimiter
        except Exception:
            pass

        grid = pd.read_csv(io.StringIO(text), header=None, dtype=object,
                            sep=sep, engine="python", skip_blank_lines=False)
        return [CandidateTable(grid=grid.reset_index(drop=True), source="delimited text")]
    except Exception as exc:
        log.warning("Delimited text extraction failed: %s", exc)
        return []


def _from_docx(file_obj) -> list[CandidateTable]:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    candidates: list[CandidateTable] = []
    try:
        from docx import Document
        doc = Document(file_obj)
        for t_idx, table in enumerate(doc.tables):
            rows = []
            for row in table.rows:
                rows.append([cell.text.strip() for cell in row.cells])
            if not rows:
                continue
            grid = pd.DataFrame(rows, dtype=object)
            candidates.append(CandidateTable(
                grid=grid,
                source=f"DOCX table {t_idx + 1}",
                table_index=t_idx,
            ))
    except Exception as exc:
        log.warning("DOCX extraction failed: %s", exc)
    return candidates


def _from_html(file_obj) -> list[CandidateTable]:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    candidates: list[CandidateTable] = []
    try:
        from bs4 import BeautifulSoup
        content = file_obj.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(content, "lxml")
        for t_idx, table in enumerate(soup.find_all("table")):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if not rows:
                continue
            width = max(len(r) for r in rows)
            padded = [r + [""] * (width - len(r)) for r in rows]
            grid = pd.DataFrame(padded, dtype=object)
            candidates.append(CandidateTable(
                grid=grid,
                source=f"HTML table {t_idx + 1}",
                table_index=t_idx,
            ))
    except Exception as exc:
        log.warning("HTML extraction failed: %s", exc)
    return candidates


def _from_xml(file_obj) -> list[CandidateTable]:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    candidates: list[CandidateTable] = []
    try:
        import xml.etree.ElementTree as ET
        content = file_obj.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        root = ET.fromstring(content)

        def _collect(node, prefix=""):
            """Find the first repeating child element set and flatten."""
            child_tags = [c.tag for c in node]
            if not child_tags:
                return []
            most_common = max(set(child_tags), key=child_tags.count)
            repeated = [c for c in node if c.tag == most_common]
            if len(repeated) >= 2:
                rows = []
                for elem in repeated:
                    row = {c.tag: (c.text or "").strip() for c in elem}
                    rows.append(row)
                return rows
            for child in node:
                rows = _collect(child, prefix + child.tag + "/")
                if rows:
                    return rows
            return []

        rows = _collect(root)
        if rows:
            df = pd.DataFrame(rows, dtype=object)
            # Prepend column names as the first row so detect_schema
            # finds them as a header row (it expects all-row grids).
            header_row = pd.DataFrame([{c: c for c in df.columns}])
            grid = pd.concat([header_row, df], ignore_index=True)
            grid.columns = range(len(grid.columns))
            candidates.append(CandidateTable(grid=grid, source="XML elements"))
    except Exception as exc:
        log.warning("XML extraction failed: %s", exc)
    return candidates


def _from_json(file_obj) -> list[CandidateTable]:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    candidates: list[CandidateTable] = []
    try:
        content = file_obj.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        data = json.loads(content)
        # Flatten to list of dicts
        if isinstance(data, list):
            flat = _flatten_list(data)
        elif isinstance(data, dict):
            # Try known wrapper keys
            for key in ("data", "records", "rows", "results", "items", "report"):
                if key in data and isinstance(data[key], list):
                    flat = _flatten_list(data[key])
                    break
            else:
                flat = _flatten_list([data])
        else:
            flat = []
        if flat:
            df = pd.DataFrame(flat, dtype=object)
            # Prepend the column names as the first row so detect_schema
            # finds them as a header row (schema_detector expects all-row grids).
            header_row = pd.DataFrame([{c: c for c in df.columns}])
            grid = pd.concat([header_row, df], ignore_index=True)
            grid.columns = range(len(grid.columns))
            candidates.append(CandidateTable(grid=grid, source="JSON records"))
    except Exception as exc:
        log.warning("JSON extraction failed: %s", exc)
    return candidates


def _flatten_list(lst: list) -> list[dict]:
    """Flatten a list of possibly-nested dicts to a flat list of dicts."""
    result = []
    for item in lst:
        if isinstance(item, dict):
            result.append(_flatten_dict(item))
        elif isinstance(item, list):
            result.extend(_flatten_list(item))
    return result


def _flatten_dict(d: dict, parent_key: str = "", sep: str = ".") -> dict:
    items: dict = {}
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.update(_flatten_dict(v, new_key, sep))
        elif isinstance(v, list):
            items[new_key] = str(v)
        else:
            items[new_key] = v
    return items


def _from_msg(file_obj, file_name: str) -> list[CandidateTable]:
    """Extract tables from an Outlook .msg email (body + attachments)."""
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    candidates: list[CandidateTable] = []
    try:
        import extract_msg
        raw = file_obj.read()
        msg = extract_msg.Message(io.BytesIO(raw))

        # Body as HTML → parse tables
        body_html = getattr(msg, "htmlBody", None) or getattr(msg, "html_body", None)
        if body_html:
            if isinstance(body_html, bytes):
                body_html = body_html.decode("utf-8", errors="replace")
            html_candidates = _from_html(io.StringIO(body_html))
            for c in html_candidates:
                c.source = f"MSG body HTML table {c.table_index + 1}"
            candidates.extend(html_candidates)

        # Attachments
        attachments = getattr(msg, "attachments", [])
        for att in attachments:
            att_name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", "") or ""
            att_data = getattr(att, "data", None)
            if att_data is None:
                continue
            att_buf = io.BytesIO(att_data)
            from .file_detector import detect
            att_meta = detect(att_buf, att_name)
            att_buf.seek(0)
            sub_candidates = extract_candidates(att_buf, att_name, att_meta.format, att_meta)
            for c in sub_candidates:
                c.source = f"MSG attachment '{att_name}' → {c.source}"
            candidates.extend(sub_candidates)

        msg.close()
    except Exception as exc:
        log.warning("MSG extraction failed: %s", exc)
    return candidates


def _from_image(file_obj, file_name: str) -> list[CandidateTable]:
    """OCR a standalone image file."""
    from .ocr_engine import image_to_grid, OCRError
    try:
        grid, source = image_to_grid(file_obj, file_name)
        return [CandidateTable(grid=grid, source=source)]
    except OCRError as exc:
        log.warning("Image OCR failed: %s", exc)
        return []


# ── Scoring ───────────────────────────────────────────────────────────────

def _score(grid: pd.DataFrame) -> float:
    """
    Score a candidate table's relevance to revenue data.
    Higher = more likely to be the real data table.
    """
    if grid is None or grid.empty:
        return -1.0

    rows, cols = grid.shape
    if cols < 2 or rows < 2:
        return 0.0

    # Size score (log scale so big tables don't crush small ones unfairly)
    size_score = np.log1p(rows) * np.log1p(cols)

    # Vocabulary hit score: how many cells contain revenue-domain words
    sample = grid.head(10).values.flatten()
    vocab_hits = sum(
        1 for cell in sample
        if any(kw in str(cell).lower() for kw in _REVENUE_VOCAB)
    )
    vocab_score = vocab_hits * 3.0

    # Header-row bonus: first row likely header if it's all text
    first_row = [str(v).strip() for v in grid.iloc[0].tolist() if str(v).strip()]
    header_bonus = 2.0 if first_row and all(
        not str(v).replace(".", "").replace(",", "").replace("-", "").isdigit()
        for v in first_row
    ) else 0.0

    return size_score + vocab_score + header_bonus
