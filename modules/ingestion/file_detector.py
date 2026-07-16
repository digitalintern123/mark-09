"""
file_detector.py — File-type and structure detection.

Answers questions about a file BEFORE any parsing happens:
  * What format is it?  (Excel / PDF / CSV / DOCX / HTML / XML / JSON / image / MSG …)
  * Is the PDF text-based or scanned?
  * How many sheets / tables / pages does it contain?
  * What encoding does a text file use?
  * Are there merged cells?  Multi-row headers?

Nothing here modifies the file or extracts business data.
"""

from __future__ import annotations

import csv as _csv
import io
import logging
import mimetypes
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# ── Supported format tokens ───────────────────────────────────────────────
FMT_EXCEL  = "excel"
FMT_PDF    = "pdf"
FMT_CSV    = "csv"
FMT_TSV    = "tsv"
FMT_TXT    = "txt"
FMT_DOCX   = "docx"
FMT_HTML   = "html"
FMT_XML    = "xml"
FMT_JSON   = "json"
FMT_IMAGE  = "image"   # PNG / JPG / JPEG / TIFF / BMP / WEBP
FMT_MSG    = "msg"     # Outlook .msg
FMT_UNKNOWN = "unknown"

_EXT_MAP: dict[str, str] = {
    ".xlsx": FMT_EXCEL, ".xls": FMT_EXCEL, ".xlsm": FMT_EXCEL,
    ".pdf":  FMT_PDF,
    ".csv":  FMT_CSV,
    ".tsv":  FMT_TSV,
    ".txt":  FMT_TXT,
    ".docx": FMT_DOCX, ".doc": FMT_DOCX,
    ".html": FMT_HTML, ".htm": FMT_HTML,
    ".xml":  FMT_XML,
    ".json": FMT_JSON,
    ".png":  FMT_IMAGE, ".jpg": FMT_IMAGE, ".jpeg": FMT_IMAGE,
    ".tiff": FMT_IMAGE, ".tif": FMT_IMAGE, ".bmp": FMT_IMAGE,
    ".webp": FMT_IMAGE,
    ".msg":  FMT_MSG,
}

# Magic-byte signatures: (offset, bytes) → format
_MAGIC: list[tuple[int, bytes, str]] = [
    (0,  b"PK\x03\x04", FMT_EXCEL),   # ZIP container (xlsx / docx both start here)
    (0,  b"\xd0\xcf\x11\xe0", FMT_EXCEL),  # OLE2 compound doc (xls / msg)
    (0,  b"%PDF",       FMT_PDF),
    (0,  b"\xff\xd8\xff", FMT_IMAGE), # JPEG
    (0,  b"\x89PNG",    FMT_IMAGE),
    (0,  b"II*\x00",    FMT_IMAGE),   # TIFF little-endian
    (0,  b"MM\x00*",    FMT_IMAGE),   # TIFF big-endian
    (0,  b"GIF8",       FMT_IMAGE),
    (0,  b"BM",         FMT_IMAGE),   # BMP
]


@dataclass
class FileMetadata:
    """Everything detected about a file before content parsing."""
    file_name: str
    format: str                        # one of FMT_* constants
    encoding: Optional[str] = None     # for text-based formats
    sheet_count: int = 0
    sheet_names: list[str] = field(default_factory=list)
    page_count: int = 0
    table_count: int = 0               # estimated from quick scan
    is_scanned_pdf: bool = False
    has_merged_cells: bool = False
    delimiter: Optional[str] = None    # CSV/TSV detected delimiter
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def is_image_like(self) -> bool:
        return self.format == FMT_IMAGE or self.is_scanned_pdf

    @property
    def needs_ocr(self) -> bool:
        return self.is_scanned_pdf or self.format == FMT_IMAGE


def detect(file_obj, file_name: str) -> FileMetadata:
    """
    Detect everything about an uploaded file.

    file_obj must support .read() and ideally .seek(0).
    Leaves the file positioned at 0 after detection.
    """
    meta = FileMetadata(file_name=file_name, format=FMT_UNKNOWN)

    # --- size ---
    if hasattr(file_obj, "seek"):
        file_obj.seek(0, 2)
        meta.size_bytes = file_obj.tell()
        file_obj.seek(0)

    # --- format from extension + magic bytes ---
    ext = Path(file_name).suffix.lower()
    fmt_from_ext = _EXT_MAP.get(ext, FMT_UNKNOWN)
    fmt_from_magic = _detect_magic(file_obj)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    # Magic beats extension for binary formats; extension wins for text formats
    # where magic is unreliable.
    if fmt_from_magic != FMT_UNKNOWN and fmt_from_ext in (FMT_UNKNOWN, fmt_from_magic):
        meta.format = fmt_from_magic
    elif fmt_from_ext != FMT_UNKNOWN:
        meta.format = fmt_from_ext
    else:
        meta.format = fmt_from_magic

    # OLE2 magic could be xls OR .msg — distinguish by extension.
    if fmt_from_magic == FMT_EXCEL and ext == ".msg":
        meta.format = FMT_MSG

    # ZIP magic could be xlsx OR docx — distinguish by extension.
    if fmt_from_magic == FMT_EXCEL and ext in (".docx", ".doc"):
        meta.format = FMT_DOCX

    # --- format-specific deep scan ---
    try:
        if meta.format == FMT_EXCEL:
            _scan_excel(file_obj, meta)
        elif meta.format == FMT_PDF:
            _scan_pdf(file_obj, meta)
        elif meta.format in (FMT_CSV, FMT_TSV, FMT_TXT):
            _scan_text(file_obj, meta)
        elif meta.format == FMT_HTML:
            _scan_html(file_obj, meta)
    except Exception as exc:
        meta.warnings.append(f"Structure scan warning: {exc}")

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    log.debug("Detected %s: %s", file_name, meta)
    return meta


# ── Private helpers ───────────────────────────────────────────────────────

def _detect_magic(file_obj) -> str:
    try:
        header = file_obj.read(16)
        if not isinstance(header, bytes):
            return FMT_UNKNOWN
        for offset, sig, fmt in _MAGIC:
            if header[offset : offset + len(sig)] == sig:
                return fmt
        # Plain text / JSON / XML / HTML — try to peek
        try:
            text_start = header.decode("utf-8", errors="replace").lstrip()
        except Exception:
            return FMT_UNKNOWN
        if text_start.startswith("{") or text_start.startswith("["):
            return FMT_JSON
        if re.match(r"<\?xml|<[A-Za-z]", text_start):
            return FMT_XML
        if re.match(r"<!DOCTYPE html|<html", text_start, re.IGNORECASE):
            return FMT_HTML
        return FMT_UNKNOWN
    except Exception:
        return FMT_UNKNOWN


def _scan_excel(file_obj, meta: FileMetadata) -> None:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        xl = pd.ExcelFile(file_obj, engine="openpyxl")
        meta.sheet_count = len(xl.sheet_names)
        meta.sheet_names = xl.sheet_names

        # Quick merged-cell check (openpyxl only; skip for .xls)
        try:
            import openpyxl
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            wb = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
            for ws in wb.worksheets:
                if ws.merged_cells:
                    meta.has_merged_cells = True
                    break
            wb.close()
        except Exception:
            pass
    except Exception as exc:
        meta.warnings.append(f"Excel scan: {exc}")


def _scan_pdf(file_obj, meta: FileMetadata) -> None:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        import pdfplumber
        with pdfplumber.open(file_obj) as pdf:
            meta.page_count = len(pdf.pages)
            text_chars = 0
            tables = 0
            for page in pdf.pages[:min(5, len(pdf.pages))]:
                text_chars += len(page.extract_text() or "")
                tables += len(page.extract_tables() or [])
            meta.table_count = tables
            # Heuristic: scanned if < 50 chars per page on first 5 pages
            avg_chars = text_chars / max(1, min(5, meta.page_count))
            meta.is_scanned_pdf = avg_chars < 50
    except Exception as exc:
        meta.warnings.append(f"PDF scan: {exc}")


def _scan_text(file_obj, meta: FileMetadata) -> None:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    raw = file_obj.read(32768)
    if isinstance(raw, str):
        meta.encoding = "utf-8"
        sample = raw
    else:
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                sample = raw.decode(enc)
                meta.encoding = enc
                break
            except UnicodeDecodeError:
                continue
        else:
            sample = raw.decode("latin-1")
            meta.encoding = "latin-1"

    if meta.format == FMT_TSV:
        meta.delimiter = "\t"
    else:
        try:
            dialect = _csv.Sniffer().sniff(sample[:4096], delimiters=",;\t|")
            meta.delimiter = dialect.delimiter
        except Exception:
            meta.delimiter = ","


def _scan_html(file_obj, meta: FileMetadata) -> None:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        from bs4 import BeautifulSoup
        content = file_obj.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(content, "lxml")
        meta.table_count = len(soup.find_all("table"))
    except Exception as exc:
        meta.warnings.append(f"HTML scan: {exc}")
