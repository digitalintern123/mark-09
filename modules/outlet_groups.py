"""
outlet_groups.py — Canonical outlet groupings and name mappings derived
from the Business Plan Excel (June 2026).

Defines:
  - OUTLET_DISPLAY_NAME: maps internal outlet names to the display names
    used in management reports
  - REVENUE_GROUPS: hierarchical groupings of outlets into subtotal buckets
    (T1+T2, T3 Domestic, T3 International, Arrivals, Ancillary) matching
    the Excel's logic exactly
  - GROUP_ORDER: display order for summary tables

Used by the Business Performance page to produce the grouped summary table
that mirrors the AOP Delhi / AOP Hyd / AOP Gox sheets.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Display name mapping: internal outlet name → Excel/report display name
# ---------------------------------------------------------------------------
OUTLET_DISPLAY_NAME: dict[str, str] = {
    # Delhi — T1
    "T1D Lounge":                          "Encalm Lounge (T1 D)",
    "T1D L4&5 Lounge":                     "Encalm Lounge (T1 D)",
    "T1D Lounge-1 Node L4&5 Card":         "Encalm Lounge (T1 D)",
    "T1D new Amex Lounge (level 4)":       "Amex Lounge T1",
    "T1D new premium lounge 2 (level 5)":  "Encalm Prive (T1)",
    "Rupay":                               "Encalm Prive (T1)",
    "Lounge Rupay (T1)":                   "Encalm Prive (T1)",
    "T1D Prive":                           "Encalm Prive (T1)",
    "T1D SPA":                             "Encalm Spa (T1 Dom)",
    # Delhi — T2
    "T2 Domestic":                         "Encalm Lounge (T2, D)",
    "Lounge DL 02,03,04":                  "Encalm Lounge (T3, DL02, 3 & 4)",
    "Lounge DL 02&03":                     "Encalm Lounge (T3, DL02, 3 & 4)",
    "T2 Lounge":                           "Encalm Lounge (T2, D)",
    # Delhi — T3 Domestic
    "T3 D49":                              "Encalm Lounge (T3 – D49)",
    "T3 DLO2/03/04":                       "Encalm Lounge (T3, DL02, 3 & 4)",
    "T3 DL02/03/04":                       "Encalm Lounge (T3, DL02, 3 & 4)",
    "T3 DL023 &4":                         "Encalm Lounge (T3, DL02, 3 & 4)",
    "T3 DL023&4":                          "Encalm Lounge (T3, DL02, 3 & 4)",
    "Lounge DL023 &4":                     "Encalm Lounge (T3, DL02, 3 & 4)",
    "Lounge DL 023 & 4":                   "Encalm Lounge (T3, DL02, 3 & 4)",
    "Lounge - Amex Centurion":             "Lounge Amex Centurion",
    "Centurion Lounge":                    "Lounge Amex Centurion",
    "Lounge - Rupay":                      "Lounge Rupay",
    "Lounge Rupay":                        "Lounge Rupay",
    "Domestic AI Lounge Del":              "Air India Lounge (T3 Dom)",
    "Air India":                           "Air India Lounge (T3 Dom)",
    "SPA Domestic":                        "Encalm Spa (T3 Dom)",
    "Dom Spa":                             "Encalm Spa (T3 Dom)",
    # Delhi — T3 International
    "INL 5&6":                             "Encalm Lounge (T3 INT)",
    "T3 INL 5&6":                          "Encalm Lounge (T3 INT)",
    "Premium Lounge":                      "Encalm Prive (T3)",
    "T3 Premium":                          "Encalm Prive (T3)",
    "Xenia":                               "Encalm Xenia",
    "First Class - Xenia Lounge":          "Encalm Xenia",
    "AI International Lounge":             "AI International",
    "International Lounge":                "Encalm Lounge (T3 INT)",
    "Spa - International":                 "Encalm Spa (T3 INT)",
    "INTL Spa":                            "Encalm Spa (T3 INT)",
    # Delhi — T3 Arrivals
    "Arrival Lounge LA 22":                "Arrival Lounge (T3, LA22)",
    "LA 22":                               "Arrival Lounge (T3, LA22)",
    "Nap & Shower LA01":                   "Nap Rooms LA01",
    "Nap & Shower LA12":                   "Nap Rooms LA12",
    "Reserved Lounge":                     "RL Delhi",
    "RL Delhi":                            "RL Delhi",
    # Delhi — Ancillary
    "Baggage Wrapping":                    "Enwrap",
    "Meet & Greet":                        "M&G",
    "Porter":                              "Porter",
    "Buggy Service":                       "Buggy",
    "CIP Lounge":                          "CIP Lounge",
    "Business Center":                     "Business Centre",
    # Hyderabad — all name variants map to the same display name
    "Reserved Lounge (Hyderabad)":         "Reserved Lounge (HYD)",
    "Reserved Lounge Hyd":                 "Reserved Lounge (HYD)",
    "Domestic Lounge (Hyderabad)":         "Domestic Lounge",
    "Hyd Dom Lounge":                      "Domestic Lounge",
    "HYD DOM Prive":                       "HYD DOM Prive",
    "RL Domestic Arrival D":               "RL Domestic Arrival",
    "RL Dom Dep E":                        "RL Dom Dep E",
    "RL Dom Dep F":                        "RL Dom Dep F",
    "International Lounge (Hyderabad)":    "International Lounge",
    "Hyd Intl Lounge":                     "International Lounge",
    "Hyd Intl Lounge - Closing":           "International Lounge",
    "INT Prive - Mezzanine level":         "INT Prive",
    "INT Card Lounge":                     "INT Card Lounge",
    "INT Card Lounge - new (Level E) - Upcoming": "INT Card Lounge",
    "Prive (Hyderabad)":                   "Encalm Prive",
    "Airport Lodge (Hyderabad)":           "Airport Lodge",
    "Airport Lodge":                       "Airport Lodge",
    "Hyd GA Lounge":                       "Hyd GA Lounge",
    "RL Int Arrival D":                    "RL Int Arrival",
    "Baggage Wrapping (Hyderabad)":        "Enwrap",
    "Meet & Greet (Hyderabad)":            "M&G",
    "M&G Hyd":                             "M&G",
    "Porter (Hyderabad)":                  "Porter",
    # Goa
    "Reserved Lounge (Goa)":               "Reserved Lounge (Goa)",
    "Reserved Lounge Goa":                 "Reserved Lounge (Goa)",
    "Domestic Lounge (Goa)":              "Domestic Lounge",
    "Goa Lounge Dom":                      "Domestic Lounge",
    "RL Dom Departure":                    "RL Dom Departure",
    "RL Dom Arrival":                      "RL Dom Arrival",
    "International Lounge (Goa)":         "International Lounge",
    "Goa Lounge INTL":                    "International Lounge",
    "Prive (Goa)":                         "Encalm Prive",
    "RL Int Arrival":                      "RL Int Arrival",
    "Baggage Wrapping (Goa)":             "Enwrap",
    "Meet & Greet (Goa)":                 "M&G",
    "M&G Goa":                             "M&G",
    "Porter (Goa)":                        "Porter",
}

# ---------------------------------------------------------------------------
# Revenue groupings — mirrors the AOP Delhi sheet's subtotal structure.
# Each group has a display label and a list of internal outlet names.
# Order within each group = display order in the summary table.
# ---------------------------------------------------------------------------

DELHI_GROUPS: dict[str, list[str]] = {
    "T1D (Lounges)": [
        "T1D Lounge",
        "T1D L4&5 Lounge",
        "T1D Lounge-1 Node L4&5 Card",
        "T1D new premium lounge 2 (level 5)",
        "T1D new Amex Lounge (level 4)",
        "Rupay",
        "T1D SPA",
    ],
    "T2 (Lounges)": [
        "T2 Domestic",

        "T2 Lounge",
    ],
    "Total (T1D + T2D)": [],   # computed as sum of T1D + T2D groups

    "T3 Domestic": [
        "T3 DLO2/03/04",
        "Lounge DL 02,03,04",
        "Lounge DL 02&03",
        "T3 DL02/03/04",
        "T3 DL023 &4",
        "T3 DL023&4",
        "Lounge DL023 &4",
        "Lounge DL 023 & 4",
        "T3 D49",
        "Lounge - Rupay",
        "Lounge - Amex Centurion",
        "Centurion Lounge",
        "Domestic AI Lounge Del",
        "Air India",
        "Dom Spa",
        "SPA Domestic",
    ],
    "Total (T3 Domestic)": [],  # computed

    "Total (T1+T2+T3 Domestic)": [],  # computed as T1D+T2D + T3 Dom

    "T3 International": [
        "INL 5&6",
        "T3 INL 5&6",
        "Premium Lounge",
        "T3 Premium",
        "Xenia",
        "First Class - Xenia Lounge",
        "AI International Lounge",
        "International Lounge",
        "Spa - International",
        "INTL Spa",
    ],
    "Total (T3 International)": [],  # computed

    "T3 Arrivals": [
        "Arrival Lounge LA 22",
        "LA 22",
        "Nap & Shower LA01",
        "Nap & Shower LA12",
        "Reserved Lounge",
        "RL Delhi",
        "CIP Lounge",
    ],
    "Total Arrivals": [],  # computed

    "Ancillary": [
        "Baggage Wrapping",
        "Meet & Greet",
        "Porter",
        "Buggy Service",
        "Business Center",
    ],
}

HYD_GROUPS: dict[str, list[str]] = {
    "Domestic": [
        "Domestic Lounge (Hyderabad)",
        "Hyd Dom Lounge",           # name variant from revenue files
        "HYD DOM Prive",
        "RL Domestic Arrival D",
        "RL Dom Dep E",
        "RL Dom Dep F",
    ],
    "International": [
        "International Lounge (Hyderabad)",
        "Reserved Lounge (Hyderabad)",
        "Reserved Lounge Hyd",
        "Hyd Intl Lounge",          # name variant
        "Hyd Intl Lounge - Closing",
        "INT Prive - Mezzanine level",
        "INT Card Lounge",
        "INT Card Lounge - new (Level E) - Upcoming",
        "Prive (Hyderabad)",
        "Airport Lodge (Hyderabad)",
        "Airport Lodge",
        "Hyd GA Lounge",
        "RL Int Arrival D",
    ],
    "Total (Int. + Prive)": [],  # computed
    "Ancillary": [
        "Baggage Wrapping (Hyderabad)",
        "Meet & Greet (Hyderabad)",
        "M&G Hyd",                  # name variant
        "Porter (Hyderabad)",
    ],
}

GOA_GROUPS: dict[str, list[str]] = {
    "Domestic": [
        "Domestic Lounge (Goa)",
        "Goa Lounge Dom",           # name variant from revenue files
        "RL Dom Departure",
        "RL Dom Arrival",
    ],
    "International": [
        "International Lounge (Goa)",
        "Reserved Lounge (Goa)",
        "Reserved Lounge Goa",
        "Goa Lounge INTL",          # name variant
        "Prive (Goa)",
        "RL Int Arrival",
    ],
    "Ancillary": [
        "Baggage Wrapping (Goa)",
        "Meet & Greet (Goa)",
        "M&G Goa",                  # name variant
        "Porter (Goa)",
    ],
}

# ---------------------------------------------------------------------------
# Subtotal computation rules: (subtotal_label, list_of_group_labels_to_sum)
# ---------------------------------------------------------------------------
DELHI_SUBTOTALS: list[tuple[str, list[str]]] = [
    ("Total (T1D + T2D)",          ["T1D (Lounges)", "T2 (Lounges)"]),
    ("Total (T3 Domestic)",         ["T3 Domestic"]),
    ("Total (T1+T2+T3 Domestic)",   ["T1D (Lounges)", "T2 (Lounges)", "T3 Domestic"]),
    ("Total (T3 International)",    ["T3 International"]),
    ("Total Arrivals",              ["T3 Arrivals"]),
]


def get_display_name(outlet: str, location: str = "") -> str:
    """
    Return the management report display name for an outlet.

    Location-aware for ambiguous outlet names that exist at multiple airports
    with different display names (e.g. 'Reserved Lounge' is 'RL Delhi' at
    Delhi but should show as 'Reserved Lounge (HYD)' at Hyderabad).
    Falls back to the outlet name itself if no mapping is found.
    """
    key = outlet.strip()
    loc = location.strip().lower()

    # Location-specific overrides for ambiguous names
    _LOCATION_OVERRIDES: dict[tuple[str, str], str] = {
        ("reserved lounge", "hyderabad"): "Reserved Lounge (HYD)",
        ("reserved lounge", "goa"):       "Reserved Lounge (Goa)",
        # 'Reserved Lounge' with no location = Delhi (default)
    }
    override = _LOCATION_OVERRIDES.get((key.lower(), loc))
    if override:
        return override

    return OUTLET_DISPLAY_NAME.get(key, key)


def get_outlet_group(outlet: str, location: str) -> str:
    """Return which group an outlet belongs to for a given location."""
    location_key = location.strip()
    outlet_key = outlet.strip()

    if location_key == "Delhi":
        groups = DELHI_GROUPS
    elif location_key == "Hyderabad":
        groups = HYD_GROUPS
    elif location_key == "Goa":
        groups = GOA_GROUPS
    else:
        return "Other"

    for group_name, outlets in groups.items():
        if outlet_key in outlets:
            return group_name
    return "Other"
