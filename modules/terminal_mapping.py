"""
terminal_mapping.py — Maps each revenue outlet to its physical airport
terminal, so revenue/PAX can be compared against terminal-level traffic
rather than whole-airport traffic.

Updated from the Master Sheet outlet mapping reference (Jul 2026).

Delhi terminal structure:
  T1  — Domestic, low-cost carriers (IndiGo, SpiceJet etc.)
  T2  — Domestic overflow
  T3  — Combined: T3 Domestic (Air India domestic, full-service) +
                  T3 International (all international flights)

T3 is split into three distinct traffic pools per the reference report:
  T3 Dom  — T3 domestic departures      → outlets: T3D49, Air India, Centurion, Lounge Rupay, Dom Spa
  T3 Int  — T3 international departures → outlets: INL5&6, Premium, Xenia, AI Intl, INTL Spa
  T3 Arr  — Total Arrival T3            → outlets: LA01, LA12, LA22, RL Delhi

Ancillary outlet traffic basis per master sheet + reference report:
  LA01, LA12, LA22  → Total Arrival T3   (mapped to T3 Arr)
  RL Delhi           → T3 Arrivals        (mapped to T3 Arr)
  CIP Lounge         → T3                 (mapped to T3 Dom — closest match)
  Dom Spa            → T3 Domestic        (mapped to T3 Dom)
  INTL Spa           → T3 International   (mapped to T3 Int)
  T1D SPA            → T1                 (mapped to T1)
  Buggy Del          → Total T3 DOM+INT   (mapped to T3 generic — no split available)
  Porter Del         → T1+T2+T3 Arrivals  (mapped to None — airport-wide)
  Baggage Wrapping   → All Dept           (mapped to None — airport-wide)
  Business Centre    → no specific traffic (mapped to None)
  M&G, RDC, Encalm  → airport-wide       (mapped to None)

Hyderabad and Goa are single-terminal airports — all outlets use the
whole-airport traffic total (Main Terminal).
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

# Canonical terminal labels — must match `terminal` column values in airport_traffic.
#
# Per Business Plan (June 2026), Delhi traffic is split into 6 distinct pools:
#   T1 Dep     — Terminal 1 DEPARTURES  (T1D Lounge, Amex T1, T1D SPA, Rupay)
#   T2 Dep     — T2 DEPARTURES          (T2 Lounge)
#   T3 Dom Dep — T3 domestic DEP        (T3D49, DLO2/03/04, Air India, Centurion, Rupay T3, Dom Spa)
#   T3 Int Dep — T3 international DEP   (INL5&6, Premium, Xenia, AI Intl, INTL Spa)
#   T3 Dom Arr ─┐ both summed together  (LA01/LA12/LA22/RL Delhi — "Total Arrival T3")
#   T3 Int Arr ─┘
#
# Special composite sentinels (NOT stored in DB — derived at query time):
#   "T3 Arr"   → T3 Dom Arr + T3 Int Arr           (LA outlets)
#   "All Dep"  → T1 Dep + T2 Dep + T3 Dom Dep + T3 Int Dep (Enwrap/Baggage Wrapping)
#   "All"      → all six pools summed               (M&G / Atithya)
#   "T3 Dom+Int Dep+T2 Dep" → T3 Dom Dep + T3 Int Dep + T2 Dep (approximate for Buggy)
#   "T3"       → generic T3 fallback for old/monthly data without Dep/Arr split
#
# Hyderabad uses "Domestic" and "International" (whole terminal, no Dep/Arr split).
# Goa uses the same.
TERMINAL_1         = "T1 Dep"        # T1 departures
TERMINAL_2         = "T2 Dep"        # T2 departures
TERMINAL_3         = "T3"            # generic T3 fallback
TERMINAL_3_DOM_DEP = "T3 Dom Dep"    # T3 domestic departures
TERMINAL_3_INT_DEP = "T3 Int Dep"    # T3 international departures
TERMINAL_3_DOM_ARR = "T3 Dom Arr"    # T3 domestic arrivals
TERMINAL_3_INT_ARR = "T3 Int Arr"    # T3 international arrivals
# Convenience aliases
TERMINAL_3_DOM = TERMINAL_3_DOM_DEP
TERMINAL_3_INT = TERMINAL_3_INT_DEP
MAIN_TERMINAL      = "Main Terminal"
DEFAULT_TERMINAL_FALLBACK = "Unmapped"

# Composite pool sentinels (resolved in database.join_revenue_with_traffic_by_outlet)
_SENTINEL_T3_ARR   = "T3 Arr"     # T3 Dom Arr + T3 Int Arr  (LA outlets)
_SENTINEL_ALL_DEP  = "All Dep"    # T1 Dep + T2 Dep + T3 Dom Dep + T3 Int Dep (Enwrap)
_SENTINEL_ALL      = "All"        # all 6 pools = entire airport (M&G)
_SENTINEL_T3_TOTAL = "T3 Total"   # all 4 T3 pools: Dom Dep + Int Dep + Dom Arr + Int Arr (Buggy)
_SENTINEL_PORTER       = "Porter Pool"    # T1Dep+T1Arr + T2Dep+T2Arr + T3DomDep + T3IntDep (no T3IntArr)
_SENTINEL_T3_DEP_TOTAL = "T3 Dom+Int Dep" # T3 Dom Dep + T3 Int Dep combined (RL T3 Departure)

# ---------------------------------------------------------------------------
# Delhi outlet → terminal mapping
# Source: Master Sheet outlet mapping reference (Jul 2026)
# None = airport-wide service, no single terminal denominator applies
# ---------------------------------------------------------------------------
_DELHI_OUTLET_TO_TERMINAL: dict[str, Optional[str]] = {

    # ── Terminal 1 — Departures only (per YOY Pax Delhi: T1D Lounge uses T1 Dep = 1,375,341) ──
    "T1D Lounge":                           TERMINAL_1,     # "T1 Dep"
    "T1D L4&5 Lounge":                      TERMINAL_1,
    "T1D Lounge-1 Node L4&5 Card":          TERMINAL_1,
    "T1D new Amex Lounge (level 4)":        TERMINAL_1,
    "T1D new premium lounge 2 (level 5)":   TERMINAL_1,
    "T1D SPA":                              TERMINAL_1,
    "Rupay":                                TERMINAL_3_DOM_DEP,  # T3 Domestic (Rupay Lounge is at T3D)

    # ── Terminal 2 — Departures only ────────────────────────────────────────
    "T2 Domestic":                          TERMINAL_2,     # "T2 Dep"
    "Lounge DL 02,03,04":                   TERMINAL_3_DOM_DEP,  # same as T3 DLO2/03/04 → T3 Domestic Dep
    "Lounge DL 02&03":                      TERMINAL_3_DOM_DEP,  # T3 Domestic Dep
    "Regular Lounge":                       TERMINAL_2,
    "T2 Lounge":                            TERMINAL_2,

    # ── Terminal 1 — new RL outlets from All Services file ─────────────────
    "RL T1 Departure":                      TERMINAL_1,       # T1D departure lounge
    "RL T1 Arrivals":                       "T1 Arr",         # T1D arrivals lounge

    # ── Terminal 3 Domestic — T3 Dom DEPARTURES traffic ────────────────────
    # Per Business Plan: T3 Dom departure PAX is the denominator for these
    # outlets (11,22,458 Jun 2025 / 5,73,080 Jun 2026 in the reference report)
    "T3 D49":                               TERMINAL_3_DOM_DEP,
    "T3 DLO2/03/04":                        TERMINAL_3_DOM_DEP,
    "Lounge - Amex Centurion":              TERMINAL_3_DOM_DEP,
    "Centurion Lounge":                     TERMINAL_3_DOM_DEP,
    "Lounge - Rupay":                       TERMINAL_3_DOM_DEP,
    "Domestic AI Lounge Del":               TERMINAL_3_DOM_DEP,
    "Air India":                            TERMINAL_3_DOM_DEP,
    "SPA Domestic":                         TERMINAL_3_INT_DEP,  # Transitioning to International (per All Services file)
    "Dom Spa":                              TERMINAL_3_INT_DEP,  # Dom Spa Transitioning to INL

    # ── Terminal 3 International — T3 Int DEPARTURES traffic ─────────────
    # Per Business Plan: T3 Int departure PAX is the denominator for these
    # outlets (8,31,713 Jun 2025 / 8,43,621 Jun 2026 in the reference report)
    "T3 INL 5&6":                           TERMINAL_3_INT_DEP,
    "INL 5&6":                              TERMINAL_3_INT_DEP,
    "T3 Premium":                           TERMINAL_3_INT_DEP,
    "T3 Premium (Prive)":                   TERMINAL_3_INT_DEP,  # variant name from All Services file
    "Premium Lounge":                       TERMINAL_3_INT_DEP,
    "Xenia":                                TERMINAL_3_INT_DEP,
    "First Class - Xenia Lounge":           TERMINAL_3_INT_DEP,
    "AI International Lounge":              TERMINAL_3_INT_DEP,
    "International Lounge":                 TERMINAL_3_INT_DEP,
    "International Lounge (New)":           TERMINAL_3_INT_DEP,
    "Visitor Lounge":                       TERMINAL_3_INT_DEP,
    "Spa - International":                  TERMINAL_3_INT_DEP,
    "INTL Spa":                             TERMINAL_3_INT_DEP,

    # ── Terminal 3 Arrivals — Total Arrival T3 (Dom Arr + Int Arr) ───────
    # Extra name variants from All Services file
    # Per Business Plan: LA01/LA12/LA22 use TOTAL ARRIVAL T3 as denominator
    # = T3 Dom Arr + T3 Int Arr (19,31,152 Jun 2025 / 14,21,421 Jun 2026)
    # The terminal_mapping returns a special sentinel so the traffic query
    # sums both T3 Dom Arr and T3 Int Arr rows from airport_traffic.
    "Arrival Lounge LA 22":                 "T3 Arr",   # sentinel: sum Dom+Int arrivals
    "LA 22":                                "T3 Arr",
    "Delhi - T3 - LA 22":                   "T3 Arr",
    "Nap & Shower LA01":                    "T3 Arr",             # Total T3 Arrival = T3 Dom Arr + T3 Int Arr
    "LA 12":                                "T3 Arr",
    "Nap & Shower LA12":                    "T3 Arr",             # Total T3 Arrival = T3 Dom Arr + T3 Int Arr
    "Reserved Lounge":                      "T3 Arr",
    "RL Delhi":                             "T3 Arr",
    # ── Terminal 3 RL outlets from All Services file ────────────────────────
    "RL T3 Departure":                      "T3 Dom+Int Dep",     # T3 departure area — T3 Dom Dep + T3 Int Dep combined
    "RL T3 Domestic Arrival":               TERMINAL_3_DOM_ARR,   # T3 domestic arrivals only
    "RL T3 International arrival":          TERMINAL_3_INT_ARR,   # T3 international arrivals only
    # Name variants from All Services file
    "Dom Spa Transitioning to INL":         TERMINAL_3_INT_DEP,   # alias for Dom Spa
    "LA01":                                 "T3 Arr",             # bare LA01
    "LA12":                                 "T3 Arr",             # bare LA12
    "LA22":                                 "T3 Arr",             # bare LA22
    "LA12 - Closing":                       "T3 Arr",             # closing variant
    "Lounge - Amex Centurion - Closing":    TERMINAL_3_DOM_DEP,   # closing variant
    "T2":                                   TERMINAL_2,            # bare T2
    "M&G Del":                              _SENTINEL_ALL,         # Delhi M&G short form
    "M&G":                                  _SENTINEL_ALL,         # M&G short form
    "Meet & Greet (Delhi)":                 _SENTINEL_ALL,         # with location suffix
    "Porter Del":                           "Porter Pool",         # Porter Delhi variant
    "Porter (Delhi)":                       "Porter Pool",
    "Buggy Del":                            "T3 Total",            # Buggy Delhi variant
    "Buggy Service (Delhi)":               "T3 Total",
    "Baggage Wrapping (Delhi)":             _SENTINEL_ALL_DEP,     # with location suffix
    "Baggage Wrapping Del":                 _SENTINEL_ALL_DEP,
    "Business Center (Delhi)":              None,
    "RDC - F&B + Rooms":                    None,                  # combined RDC name
    "RDC":                                  None,                  # bare RDC
    "Encalm Sky Plates (Delhi)":            None,
    "Encalm Sky Plates (Hyderabad)":        None,
    "Encalm Eats (Delhi)":                  None,

    # CIP Lounge — not currently operational; excluded from traffic metrics
    "CIP Lounge":                           None,
    # ── Ancillary services — confirmed traffic pools ──────────────────────
    # Baggage Wrapping (Enwrap) → All Dept = T1+T2+T3Dom+T3Int ALL departures
    # M&G / Atithya             → Total airport = all 6 pools (Dep+Arr all terminals)
    # Buggy                     → Total T3 = all 4 T3 pools (Dom Dep + Int Dep + Dom Arr + Int Arr)
    # Porter                    → T1 Dep+Arr + T2 Dep+Arr + T3 Dom Dep + T3 Int Dep
    #                             (excludes T3 Int Arr — no Porter in T3 International Arrivals)
    "Baggage Wrapping":                     _SENTINEL_ALL_DEP,    # all terminal departures
    "Meet & Greet":                         _SENTINEL_ALL,         # whole airport total
    "Buggy Service":                        "T3 Total",            # all 4 T3 pools
    "Porter":                               "Porter Pool",         # T1+T2+T3Dom+T3IntDep (no T3IntArr)
    # No specific traffic pool — excluded from penetration/SPP
    "Business Center":                      None,
    "Bar":                                  None,
    # Non-airport services
    "Round D Clock (RDC)":                  None,
    "RDC - Rooms":                          None,
    "RDC - F&B":                            None,
    "Encalm Eats":                          None,
    "Encalm Sky Plates":                    None,
    "Special Events":                       None,
}

# ---------------------------------------------------------------------------
# Hyderabad — traffic split into Domestic and International
# Domestic Lounge → Domestic traffic pool
# International Lounge / Prive → International traffic pool
# Ancillary (M&G, Enwrap, Porter, Airport Lodge) → whole airport total
# ---------------------------------------------------------------------------
_HYDERABAD_OUTLET_TO_TERMINAL: dict[str, Optional[str]] = {
    # ── Domestic terminal ──────────────────────────────────────────────────
    "Domestic Lounge":                   "Domestic",   # short name used in revenue data
    "Domestic Lounge (Hyderabad)":       "Domestic",
    "Hyd Dom Lounge":                    "Domestic",
    "HYD DOM Prive":                     "Domestic",
    "Dom Prive":                         "Domestic",
    "RL Domestic Arrival D":             "Domestic",
    "RL Dom Dep E":                      "Domestic",
    "RL Dom Dep F":                      "Domestic",
    # ── International terminal ─────────────────────────────────────────────
    "International Lounge":              "International",  # short name in revenue data
    "International Lounge (Hyderabad)":  "International",
    "Hyd Intl Lounge":                   "International",
    "Hyd Intl Lounge - Closing":         "International",
    "Prive":                             "International",  # short name in revenue data
    "Prive (Hyderabad)":                 "International",
    "INT Prive - Mezzanine level":       "International",
    "INT Card Lounge - new (Level E) - Upcoming": "International",
    "INT Card Lounge":                   "International",
    "RL Int Arrival D":                  "International",
    "Airport Lodge (Hyderabad)":         "International",
    "Airport Lodge":                     "International",
    "Hyd GA Lounge":                     "International",
    "GAT":                               "International",  # General Aviation Terminal
    "Reserved Lounge":                   "International",  # reserved lounge in intl area
    # ── No traffic pool ────────────────────────────────────────────────────
    "Encalm Sky Plates":                 None,
    "Encalm Sky Plates (Hyderabad)":     None,
    "Transit Hotel":                     None,
    # ── Whole airport ─────────────────────────────────────────────────────
    "Baggage Wrapping":                  "All",   # short name in revenue data
    "Baggage Wrapping (Hyderabad)":      "All",
    "Meet & Greet":                      "All",   # short name in revenue data
    "Meet & Greet (Hyderabad)":          "All",
    "M&G Hyd":                           "All",
    "Porter":                            "All",   # short name in revenue data
    "Porter (Hyderabad)":                "All",
}

# ---------------------------------------------------------------------------
# Goa — traffic split into Domestic and International
# ---------------------------------------------------------------------------
_GOA_OUTLET_TO_TERMINAL: dict[str, Optional[str]] = {
    # ── Domestic terminal ──────────────────────────────────────────────────
    "Domestic Lounge":                   "Domestic",   # short name in revenue data
    "Domestic Lounge (Goa)":             "Domestic",
    "Goa Lounge Dom":                    "Domestic",
    "RL Dom Departure":                  "Domestic",
    "RL Dom Arrival":                    "Domestic",
    # ── International terminal ─────────────────────────────────────────────
    "International Lounge":              "International",  # short name in revenue data
    "International Lounge (Goa)":        "International",
    "Goa Lounge INTL":                   "International",
    "Prive":                             "International",  # short name in revenue data
    "Prive (Goa)":                       "International",
    "CIP Lounge Goa":                    None,
    "RL Int Arrival":                    "International",
    # ── Whole airport ─────────────────────────────────────────────────────
    "Baggage Wrapping":                  "All",   # short name in revenue data
    "Baggage Wrapping (Goa)":            "All",
    "Meet & Greet":                      "All",   # short name in revenue data
    "Meet & Greet (Goa)":               "All",
    "M&G Goa":                           "All",
    "Porter":                            "All",   # short name in revenue data
    "Porter (Goa)":                      "All",
}


def get_terminal_for_outlet(outlet: str, location: str) -> Optional[str]:
    """
    Return the terminal label for a given outlet+location.

    - Delhi outlet with an assigned terminal  → "T1" / "T2" / "T3"
    - Delhi outlet mapped to None             → None (airport-wide service;
      terminal-specific traffic does not apply)
    - Hyderabad / Goa                         → "Main Terminal"
    - Outlet not yet in the mapping           → "Unmapped" (visible in the
      Terminal Analysis page so new outlets can be spotted and added)
    """
    location_key = location.strip()
    outlet_key = outlet.strip()

    if location_key == "Delhi":
        mapping = _DELHI_OUTLET_TO_TERMINAL
    elif location_key == "Hyderabad":
        mapping = _HYDERABAD_OUTLET_TO_TERMINAL
    elif location_key == "Goa":
        mapping = _GOA_OUTLET_TO_TERMINAL
    else:
        return DEFAULT_TERMINAL_FALLBACK

    if outlet_key in mapping:
        return mapping[outlet_key]
    # Hyderabad/Goa outlets not in the per-outlet mapping fall back to
    # whole-airport total rather than "Unmapped" — these are single-
    # terminal airports so a fallback to total is always safe.
    if location_key in ("Hyderabad", "Goa"):
        return MAIN_TERMINAL
    return DEFAULT_TERMINAL_FALLBACK


def add_terminal_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a `terminal` column to a revenue DataFrame by mapping each row's
    (outlet, location) through get_terminal_for_outlet(). Rows that map to
    None (airport-wide services) keep terminal=None; rows for an outlet
    not yet in the mapping get "Unmapped" so they're visible rather than
    silently dropped from terminal-level views.
    """
    if df is None or df.empty:
        return df
    work = df.copy()
    work["terminal"] = work.apply(
        lambda r: get_terminal_for_outlet(r["outlet"], r["location"]), axis=1
    )
    return work


def get_known_terminals_for_location(location: str) -> list[str]:
    """Distinct physical terminal labels stored in airport_traffic for a location."""
    location_key = location.strip()
    if location_key == "Delhi":
        # Return all actual stored terminal labels (not sentinels)
        return sorted([
            "T1 Dep", "T1 Arr",
            "T2 Dep", "T2 Arr",
            "T3 Dom Dep", "T3 Dom Arr",
            "T3 Int Dep", "T3 Int Arr",
            "T3",   # fallback for older data without Dep/Arr split
        ])
    if location_key in ("Hyderabad", "Goa"):
        return ["Domestic", "International", MAIN_TERMINAL]
    return []


def get_unmapped_outlets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return the distinct (location, outlet) pairs in `df` that currently
    fall back to "Unmapped" — a diagnostic helper for the Terminal
    Analysis page, so new outlets that show up in revenue data but aren't
    in the mapping yet are easy to spot and add.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["location", "outlet"])
    tagged = add_terminal_column(df)
    unmapped = tagged[tagged["terminal"] == DEFAULT_TERMINAL_FALLBACK]
    return unmapped[["location", "outlet"]].drop_duplicates().reset_index(drop=True)
