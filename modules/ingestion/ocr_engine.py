"""
ocr_engine.py — OCR for scanned PDFs and images.

Pipeline:
  1. Convert page/image to a NumPy array (via pdfplumber or PIL).
  2. OpenCV preprocessing: grayscale → denoise → deskew → threshold.
  3. Tesseract extraction in TSV mode (preserves cell structure).
  4. Reconstruct a table grid from bounding-box rows.

Handles: rotated pages, skewed scans, noisy/low-res images, multi-page PDFs.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Lazy imports so the module loads even if cv2/tesseract aren't available.
def _cv2():
    import cv2
    return cv2

def _pytesseract():
    import pytesseract
    return pytesseract

def _pil():
    from PIL import Image
    return Image


class OCRError(Exception):
    """Raised when OCR cannot produce usable output."""


# ── Public API ────────────────────────────────────────────────────────────

def pdf_to_grids(file_obj, max_pages: int = 100) -> list[tuple[pd.DataFrame, str]]:
    """
    OCR a scanned PDF.  Returns [(grid_df, page_description), ...].
    Each grid_df has object dtype; rows are text lines, columns are
    spatial columns inferred from Tesseract word positions.
    """
    import pdfplumber
    from PIL import Image as PILImage

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    results: list[tuple[pd.DataFrame, str]] = []
    with pdfplumber.open(file_obj) as pdf:
        pages = pdf.pages[:max_pages]
        for page_no, page in enumerate(pages, start=1):
            try:
                # pdfplumber renders the page as a PIL image at 150 dpi.
                img = page.to_image(resolution=150).original
                arr = np.array(img)
                preprocessed = _preprocess(arr)
                grid = _tesseract_to_grid(preprocessed)
                if grid is not None and not grid.empty:
                    results.append((grid, f"PDF page {page_no}"))
            except Exception as exc:
                log.warning("OCR failed for page %d: %s", page_no, exc)

    if not results:
        raise OCRError(
            "OCR produced no usable text from this PDF. "
            "Check that Tesseract is installed and the PDF quality is sufficient."
        )
    return results


def image_to_grid(file_obj, file_name: str = "") -> tuple[pd.DataFrame, str]:
    """OCR a single image file.  Returns (grid_df, description)."""
    PILImage = _pil()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    img = PILImage.open(file_obj)
    arr = np.array(img.convert("RGB"))
    preprocessed = _preprocess(arr)
    grid = _tesseract_to_grid(preprocessed)
    if grid is None or grid.empty:
        raise OCRError(f"OCR extracted no text from '{file_name}'.")
    return grid, f"image '{file_name}'"


# ── Image preprocessing ───────────────────────────────────────────────────

def _preprocess(arr: np.ndarray) -> np.ndarray:
    """
    OpenCV preprocessing pipeline:
      1. Grayscale
      2. Denoise (fast non-local means for photos, Gaussian for documents)
      3. Deskew
      4. Adaptive threshold → binary
    """
    cv2 = _cv2()

    # 1. Grayscale
    if arr.ndim == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    else:
        gray = arr.copy()

    # 2. Upscale if small (Tesseract works best at ~300 dpi)
    h, w = gray.shape[:2]
    if max(h, w) < 1500:
        scale = 2
        gray = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    # 3. Denoise
    gray = cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)

    # 4. Deskew
    gray = _deskew(gray)

    # 5. Adaptive threshold → binary (handles uneven lighting)
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=35, C=11,
    )
    return binary


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Detect page skew via Hough lines and rotate to correct it."""
    cv2 = _cv2()
    try:
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)
        if lines is None or len(lines) == 0:
            return gray
        angles = []
        for line in lines[:30]:
            rho, theta = line[0]
            angle = np.degrees(theta) - 90
            if -45 < angle < 45:
                angles.append(angle)
        if not angles:
            return gray
        median_angle = float(np.median(angles))
        if abs(median_angle) < 0.5:
            return gray
        h, w = gray.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), median_angle, 1.0)
        rotated = cv2.warpAffine(gray, M, (w, h),
                                  flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)
        return rotated
    except Exception as exc:
        log.debug("Deskew failed (non-fatal): %s", exc)
        return gray


# ── Tesseract → grid ─────────────────────────────────────────────────────

def _tesseract_to_grid(binary: np.ndarray) -> Optional[pd.DataFrame]:
    """
    Run Tesseract in TSV mode and reconstruct a table grid using the
    bounding-box information Tesseract provides per word.

    Strategy:
      - Cluster words into rows by their vertical midpoint (y-centre).
      - Within each row, cluster words into columns by their horizontal
        centre (x-centre).  Column boundaries are determined globally
        from the median word widths.
      - Fill cells by concatenating words that fall in the same (row, col).
    """
    pt = _pytesseract()
    cv2 = _cv2()

    # Tesseract reads a PIL image or file path; encode our numpy array.
    PILImage = _pil()
    pil_img = PILImage.fromarray(binary)

    try:
        tsv = pt.image_to_data(pil_img, output_type=pt.Output.DATAFRAME,
                               config="--psm 6")
    except Exception as exc:
        log.warning("Tesseract failed: %s", exc)
        return None

    # Filter to actual words
    words = tsv[tsv["conf"] > 20].copy()
    words = words[words["text"].notna() & (words["text"].str.strip() != "")]
    if words.empty:
        return None

    words["y_mid"] = words["top"] + words["height"] / 2
    words["x_mid"] = words["left"] + words["width"] / 2

    # Cluster into rows (y-midpoint within tolerance)
    row_tol = words["height"].median() * 0.6
    words_sorted = words.sort_values("y_mid")
    row_ids = []
    current_row = 0
    prev_y = None
    for y in words_sorted["y_mid"]:
        if prev_y is None or abs(y - prev_y) > row_tol:
            current_row += 1
            prev_y = y
        row_ids.append(current_row)
    words_sorted["row_id"] = row_ids

    # Cluster into columns (x-midpoint buckets)
    x_mids = words_sorted["x_mid"].values
    col_ids = _cluster_1d(x_mids, gap=words_sorted["width"].median() * 1.2)
    words_sorted["col_id"] = col_ids

    # Reconstruct grid
    max_row = words_sorted["row_id"].max()
    max_col = words_sorted["col_id"].max()
    grid: list[list[str]] = [[""] * (max_col) for _ in range(max_row)]

    for _, w in words_sorted.iterrows():
        r = int(w["row_id"]) - 1
        c = int(w["col_id"]) - 1
        cell_text = str(w["text"]).strip()
        if grid[r][c]:
            grid[r][c] += " " + cell_text
        else:
            grid[r][c] = cell_text

    df = pd.DataFrame(grid)
    # Drop all-blank rows and columns
    df = df.replace("", pd.NA).dropna(how="all").dropna(axis=1, how="all")
    df = df.fillna("").reset_index(drop=True)
    df.columns = range(len(df.columns))
    return df


def _cluster_1d(values: np.ndarray, gap: float) -> list[int]:
    """Assign each value a cluster ID based on gaps larger than `gap`."""
    sorted_vals = np.sort(np.unique(values))
    mapping: dict[float, int] = {}
    cluster = 1
    prev = None
    for v in sorted_vals:
        if prev is not None and (v - prev) > gap:
            cluster += 1
        mapping[v] = cluster
        prev = v
    return [mapping[v] for v in values]
