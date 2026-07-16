"""
confidence_engine.py — Confidence scoring for extractions.

Computes a 0..1 confidence score for each parse attempt based on:
  * How many required roles were mapped (and by what method)
  * Average per-role confidence from the column mapper
  * Validation check pass rate
  * Data completeness (non-null revenue, date, location rates)

Thresholds:
  ≥ 0.80  Safe to import automatically
  0.55–0.79  Import with warnings surfaced to the user
  < 0.55  Invoke AI fallback (if available) or fail loudly

AI fallback uses a local Ollama model (Llama 3 / Mistral / Phi-3) to
re-examine the raw grid and return structured JSON field mapping.
It never sends data to external APIs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .column_mapper import MappingResult, REQUIRED_ROLES, ALL_ROLES
from .validator import ValidationReport

log = logging.getLogger(__name__)

THRESHOLD_AUTO   = 0.80   # import automatically
THRESHOLD_WARN   = 0.55   # import with user-visible warnings
# Below THRESHOLD_WARN → try AI fallback, then fail if still below


@dataclass
class ConfidenceResult:
    score: float                    # 0..1
    level: str                      # "high" | "medium" | "low"
    breakdown: dict[str, float]     # component scores
    needs_ai_fallback: bool
    message: str


def compute(
    mapping: MappingResult,
    validation: ValidationReport,
    data: pd.DataFrame,
) -> ConfidenceResult:
    """Compute an overall confidence score for one extraction attempt."""
    components: dict[str, float] = {}

    # 1. Role coverage: how many required roles were assigned
    n_required = len(REQUIRED_ROLES)
    n_assigned = sum(1 for r in REQUIRED_ROLES if r in mapping.assignments)
    components["role_coverage"] = n_assigned / n_required

    # 2. Average confidence of assigned roles
    if mapping.assignments:
        components["role_confidence"] = sum(
            a.confidence for a in mapping.assignments.values()
        ) / len(mapping.assignments)
    else:
        components["role_confidence"] = 0.0

    # 3. Method quality: content-only assignments are weaker than combined
    method_weights = {"exact": 1.0, "combined": 0.9, "fuzzy": 0.75,
                      "content": 0.6, "recovered": 0.5}
    if mapping.assignments:
        method_score = sum(
            method_weights.get(a.method, 0.5)
            for a in mapping.assignments.values()
        ) / len(mapping.assignments)
    else:
        method_score = 0.0
    components["method_quality"] = method_score

    # 4. Validation pass rate
    if validation.checks:
        passed = sum(1 for c in validation.checks.values() if c.passed)
        components["validation"] = passed / len(validation.checks)
    else:
        components["validation"] = 0.5

    # 5. Data completeness
    if not data.empty:
        for col, weight in [("revenue", 0.4), ("date", 0.3), ("location", 0.2)]:
            if col in data.columns:
                non_null = data[col].notna().mean()
                components[f"completeness_{col}"] = non_null * weight
            else:
                components[f"completeness_{col}"] = 0.0
    else:
        for col in ("revenue", "date", "location"):
            components[f"completeness_{col}"] = 0.0

    # Weighted composite
    weights = {
        "role_coverage":          0.25,
        "role_confidence":        0.25,
        "method_quality":         0.15,
        "validation":             0.15,
        "completeness_revenue":   0.10,
        "completeness_date":      0.05,
        "completeness_location":  0.05,
    }
    score = sum(components.get(k, 0.0) * w for k, w in weights.items())
    score = max(0.0, min(1.0, score))

    if score >= THRESHOLD_AUTO:
        level = "high"
        message = f"Confidence {score:.0%} — safe to import."
        needs_ai = False
    elif score >= THRESHOLD_WARN:
        level = "medium"
        message = f"Confidence {score:.0%} — imported with warnings. Please review the field mapping."
        needs_ai = False
    else:
        level = "low"
        message = f"Confidence {score:.0%} — below threshold. Attempting AI-assisted extraction."
        needs_ai = True

    return ConfidenceResult(
        score=score, level=level, breakdown=components,
        needs_ai_fallback=needs_ai, message=message,
    )


# ── AI Fallback (Ollama — local, no external API calls) ──────────────────

def ai_extract(
    grid: pd.DataFrame,
    context_lines: list[str],
    file_name: str,
    model: str = "llama3",
) -> Optional[dict]:
    """
    Ask a local Ollama model to identify columns in the grid and return
    a JSON mapping {role: column_header}.

    Returns the parsed JSON dict, or None if Ollama is not available or
    the response cannot be parsed.  Data is NEVER sent to external APIs.
    """
    try:
        import requests
        # Ollama default endpoint
        url = "http://localhost:11434/api/generate"

        # Sample: first 5 rows + headers only (keep prompt small)
        sample = grid.head(5).to_csv(index=False)
        context_str = "\n".join(context_lines[:5]) if context_lines else "(none)"

        prompt = f"""You are a data extraction assistant. Analyse this table sample and identify which column corresponds to each business field.

Context / title lines:
{context_str}

Table sample (CSV format):
{sample}

Return ONLY a JSON object mapping these field names to the exact column header in the table.
Use null if a field is not present.

Fields to identify:
- date: The transaction or report date
- location: Airport or city (Delhi, Hyderabad, Goa)
- segment: Business line (Lounges, Atithya, Others, EHPL, Sky Plates, Encalm Eats)
- outlet: Specific outlet or unit name
- pax: Passenger count / footfall
- revenue: Revenue / sales / collection amount
- aop: Budget / target / AOP amount (if present)
- traffic: Airport traffic / total passengers (if present)

Example response:
{{"date": "Business Date", "location": "Airport", "segment": "Business Line", "outlet": "Unit Name", "pax": "PAX", "revenue": "Net Sales", "aop": null, "traffic": null}}

Respond with ONLY the JSON, no explanation."""

        resp = requests.post(
            url,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")

        # Extract JSON from the response
        json_match = _extract_json(raw)
        if json_match:
            parsed = json.loads(json_match)
            log.info("AI fallback produced mapping: %s", parsed)
            return parsed
    except requests.exceptions.ConnectionError:
        log.debug("Ollama not running — AI fallback unavailable.")
    except Exception as exc:
        log.warning("AI fallback failed: %s", exc)
    return None


def apply_ai_mapping(
    ai_result: dict,
    headers: list[str],
    data: pd.DataFrame,
) -> MappingResult:
    """
    Convert AI-returned {role: header_string} into a MappingResult,
    matching AI header strings to actual column indices by fuzzy lookup.
    """
    from rapidfuzz import process as fuzz_process
    from .column_mapper import (
        MappingResult, ColumnAssignment, ALL_ROLES,
    )

    assignments: dict = {}
    unmapped: list[str] = []
    header_lower = {h.lower().strip(): i for i, h in enumerate(headers)}

    for role in ALL_ROLES:
        ai_col = ai_result.get(role)
        if not ai_col:
            continue
        ai_col_norm = str(ai_col).lower().strip()

        # Exact match first
        if ai_col_norm in header_lower:
            j = header_lower[ai_col_norm]
        else:
            # Fuzzy match
            match = fuzz_process.extractOne(
                ai_col_norm, list(header_lower.keys()), score_cutoff=75
            )
            if match:
                j = header_lower[match[0]]
            else:
                unmapped.append(ai_col)
                continue

        assignments[role] = ColumnAssignment(
            role=role, source_col=headers[j], col_index=j,
            header_score=0.0, content_score=0.0,
            total_score=0.7, method="ai_fallback",
            confidence=0.70,
        )

    remaining = [h for i, h in enumerate(headers) if i not in {a.col_index for a in assignments.values()}]
    return MappingResult(
        assignments=assignments,
        unmapped_cols=remaining + unmapped,
    )


def _extract_json(text: str) -> Optional[str]:
    """Extract a JSON object from free-form text."""
    import re
    m = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    return m.group(0) if m else None
