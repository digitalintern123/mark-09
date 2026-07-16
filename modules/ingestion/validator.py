"""
validator.py — Post-normalisation validation and duplicate detection.

Checks:
  * Required columns present
  * Date column: no NaTs, plausible range (2015-01-01 .. today+1yr)
  * Revenue: numeric, not all-NaN; flags negatives as warnings (could be refunds)
  * PAX: numeric; flags implausibly large values
  * Location: only known canonical values
  * Segment: only known canonical values
  * Outlet: non-blank
  * Duplicate rows: same (date, segment, outlet, location)

Produces a ValidationReport with per-check results, counts, and a list
of row-level issues (for the duplicate log).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

VALID_LOCATIONS = {"Delhi", "Hyderabad", "Goa"}
VALID_SEGMENTS  = {
    "EHPL", "Lounges", "Atithya", "Others",
    "Subsidiary", "Sky Plates", "Encalm Eats",
}

_PLAUSIBLE_DATE_MIN = dt.date(2015, 1, 1)
_PLAUSIBLE_DATE_MAX_DELTA = dt.timedelta(days=366)


@dataclass
class CheckResult:
    passed: bool
    message: str
    affected_rows: int = 0


@dataclass
class ValidationReport:
    checks: dict[str, CheckResult] = field(default_factory=dict)
    duplicate_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks.values())

    @property
    def critical_failures(self) -> list[str]:
        return [name for name, c in self.checks.items() if not c.passed]

    def summary(self) -> str:
        lines = []
        for name, c in self.checks.items():
            icon = "✅" if c.passed else "❌"
            lines.append(f"{icon} {name}: {c.message}")
        return "\n".join(lines)


def validate(df: pd.DataFrame) -> ValidationReport:
    """Run all validation checks on the normalized DataFrame."""
    report = ValidationReport()

    # 1. Required columns
    required = ["date", "location", "segment", "outlet", "revenue"]
    missing_cols = [c for c in required if c not in df.columns]
    report.checks["required_columns"] = CheckResult(
        passed=not missing_cols,
        message=(f"All required columns present"
                 if not missing_cols
                 else f"Missing: {missing_cols}"),
    )
    if missing_cols:
        return report  # can't proceed

    n = len(df)

    # 2. Dates
    nat_dates = df["date"].isna().sum()
    date_range_ok = True
    today = dt.date.today()
    date_max = today + _PLAUSIBLE_DATE_MAX_DELTA
    if nat_dates == 0:
        out_of_range = df["date"].apply(
            lambda d: d is not None and (d < _PLAUSIBLE_DATE_MIN or d > date_max)
        ).sum()
    else:
        out_of_range = 0

    if nat_dates > 0 or out_of_range > 0:
        parts = []
        if nat_dates:
            parts.append(f"{nat_dates} NaT/unparseable date(s)")
        if out_of_range:
            parts.append(f"{out_of_range} date(s) outside 2015–{date_max.year}")
            date_range_ok = False
        report.checks["dates"] = CheckResult(
            passed=False, message="; ".join(parts),
            affected_rows=nat_dates + out_of_range,
        )
    else:
        report.checks["dates"] = CheckResult(
            passed=True, message=f"All {n} dates valid and in range"
        )

    # 3. Revenue
    rev_null = df["revenue"].isna().sum()
    rev_neg  = (pd.to_numeric(df["revenue"], errors="coerce") < 0).sum()
    if rev_null == n:
        report.checks["revenue"] = CheckResult(
            passed=False, message="Revenue column is entirely blank", affected_rows=n
        )
    else:
        msg = f"{n - rev_null} non-null revenue values"
        if rev_neg:
            msg += f"; {rev_neg} negative (possible refunds — kept, please verify)"
            report.warnings.append(
                f"{rev_neg} row(s) have negative revenue — kept (refunds/adjustments?)."
            )
        report.checks["revenue"] = CheckResult(
            passed=True, message=msg, affected_rows=rev_null
        )

    # 4. PAX
    if "pax" in df.columns:
        pax_null = df["pax"].isna().sum()
        pax_num = pd.to_numeric(df["pax"], errors="coerce")
        pax_large = (pax_num > 1_000_000).sum()
        if pax_large:
            report.warnings.append(
                f"{pax_large} row(s) have PAX > 1,000,000 — possible unit error?"
            )
        report.checks["pax"] = CheckResult(
            passed=True,
            message=f"{n - pax_null} non-null PAX values"
                    + (f"; {pax_null} blank" if pax_null else ""),
            affected_rows=pax_null,
        )

    # 5. Location
    unknown_locs = df["location"].dropna().apply(
        lambda v: v not in VALID_LOCATIONS
    ).sum()
    if unknown_locs > 0:
        uniq_unknown = df["location"][df["location"].apply(
            lambda v: v not in VALID_LOCATIONS
        )].unique()[:5].tolist()
        report.checks["location"] = CheckResult(
            passed=False,
            message=f"{unknown_locs} row(s) have unrecognised location(s): {uniq_unknown}",
            affected_rows=unknown_locs,
        )
        report.warnings.append(
            f"Unrecognised locations: {uniq_unknown}. "
            "Expected: Delhi, Hyderabad, Goa."
        )
    else:
        report.checks["location"] = CheckResult(
            passed=True, message="All locations are valid"
        )

    # 6. Segment
    unknown_segs = df["segment"].dropna().apply(
        lambda v: v not in VALID_SEGMENTS
    ).sum()
    if unknown_segs > 0:
        uniq_unknown_s = df["segment"][df["segment"].apply(
            lambda v: v not in VALID_SEGMENTS
        )].unique()[:5].tolist()
        report.checks["segment"] = CheckResult(
            passed=False,
            message=f"{unknown_segs} row(s) have unrecognised segment(s): {uniq_unknown_s}",
            affected_rows=unknown_segs,
        )
        report.warnings.append(
            f"Unrecognised segments: {uniq_unknown_s}. Will be stored as-is."
        )
    else:
        report.checks["segment"] = CheckResult(
            passed=True, message="All segments are valid"
        )

    # 7. Outlet non-blank
    blank_outlets = df["outlet"].isna().sum() + (df["outlet"] == "").sum()
    report.checks["outlet"] = CheckResult(
        passed=blank_outlets == 0,
        message=(f"All outlets non-blank"
                 if blank_outlets == 0
                 else f"{blank_outlets} blank outlet(s)"),
        affected_rows=blank_outlets,
    )

    # 8. Duplicates
    dup_cols = ["date", "segment", "outlet", "location"]
    dup_mask = df.duplicated(subset=dup_cols, keep=False)
    dup_count = dup_mask.sum()
    if dup_count > 0:
        report.duplicate_log = df[dup_mask].copy()
        report.warnings.append(
            f"{dup_count} duplicate row(s) detected (same date/segment/outlet/location). "
            "The last occurrence will be kept; earlier duplicates will be skipped by the DB."
        )
    report.checks["duplicates"] = CheckResult(
        passed=True,
        message=(f"No duplicates" if dup_count == 0
                 else f"{dup_count} duplicate row(s) — will be de-duplicated on save"),
        affected_rows=dup_count,
    )

    return report
