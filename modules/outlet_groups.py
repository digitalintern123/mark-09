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
    # Hyderabad — display name mappings
    # Atithya (M&G)
    "Meet & Greet (Hyderabad)":            "Atithya",
    "M&G Hyd":                             "Atithya",
    "GAT (Hyderabad)":                     "Atithya",
    "GAT":                                 "Atithya",
    "Atithya":                             "Atithya",
    # Domestic Lounge
    "Domestic Lounge (Hyderabad)":         "Domestic Lounge",
    "Hyd Dom Lounge":                      "Domestic Lounge",
    "HYD DOM Prive":                       "Domestic Lounge",
    "RL Domestic Arrival D":               "Domestic Lounge",
    "RL Dom Dep E":                        "Domestic Lounge",
    "RL Dom Dep F":                        "Domestic Lounge",
    # International Lounge
    "International Lounge (Hyderabad)":    "International Lounge",
    "Hyd Intl Lounge":                     "International Lounge",
    "Hyd Intl Lounge - Closing":           "International Lounge",
    "INT Card Lounge":                     "International Lounge",
    "INT Card Lounge - new (Level E) - Upcoming": "International Lounge",
    "Airport Lodge (Hyderabad)":           "International Lounge",
    "Airport Lodge":                       "International Lounge",
    "Hyd GA Lounge":                       "International Lounge",
    "RL Int Arrival D":                    "International Lounge",
    # Encalm Prive
    "Prive (Hyderabad)":                   "Encalm Prive",
    "INT Prive - Mezzanine level":         "Encalm Prive",
    # Reserved Lounge (HYD)
    "Reserved Lounge (Hyderabad)":         "Reserved Lounge (HYD)",
    "Reserved Lounge Hyd":                 "Reserved Lounge (HYD)",
    # Baggage Wrapping
    "Baggage Wrapping (Hyderabad)":        "Baggage Wrapping",
    # Sky Plates
    "Encalm Sky Plates (Hyderabad)":       "Sky Plates",
    "Sky Plates (Hyderabad)":              "Sky Plates",
    "Sky Plates Hyd":                      "Sky Plates",
    "Encalm Sky Plates":                   "Sky Plates",
    # Porter
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
    # Row 1: Atithya (M&G) — shown as first row in HYD Excel
    "Atithya": [
        "Meet & Greet (Hyderabad)",
        "M&G Hyd",
        "GAT (Hyderabad)",
        "GAT",
        "Atithya",
    ],
    # Row 2: Domestic Lounge — all domestic area outlets
    "Domestic Lounge": [
        "Domestic Lounge (Hyderabad)",
        "Hyd Dom Lounge",
        "HYD DOM Prive",
        "RL Domestic Arrival D",
        "RL Dom Dep E",
        "RL Dom Dep F",
    ],
    # Row 3: International Lounge — all international area outlets
    "International Lounge": [
        "International Lounge (Hyderabad)",
        "Hyd Intl Lounge",
        "Hyd Intl Lounge - Closing",
        "INT Card Lounge",
        "INT Card Lounge - new (Level E) - Upcoming",
        "Airport Lodge (Hyderabad)",
        "Airport Lodge",
        "Hyd GA Lounge",
        "RL Int Arrival D",
    ],
    # Row 4: Encalm Prive — Prive + INT Prive Mezzanine
    "Encalm Prive": [
        "Prive (Hyderabad)",
        "INT Prive - Mezzanine level",
    ],
    # Subtotal row
    "Total (International + Prive)": [],   # computed = International Lounge + Encalm Prive
    # Row 5: Baggage Wrapping
    "Baggage Wrapping": [
        "Baggage Wrapping (Hyderabad)",
    ],
    # Row 6: Sky Plates
    "Sky Plates": [
        "Encalm Sky Plates (Hyderabad)",
        "Sky Plates (Hyderabad)",
        "Sky Plates Hyd",
        "Encalm Sky Plates",
    ],
    # Row 7: Porter
    "Porter": [
        "Porter (Hyderabad)",
    ],
}

GOA_GROUPS: dict[str, list[str]] = {
    "Domestic": [
        "Domestic Lounge (Goa)",
        "Domestic Lounge",          # short name stored in DB
        "Goa Lounge Dom",
        "RL Dom Departure",
        "RL Dom Arrival",
    ],
    "International": [
        "International Lounge (Goa)",
        "International Lounge",     # short name stored in DB
        "Reserved Lounge (Goa)",
        "Reserved Lounge Goa",
        "Reserved Lounge",          # short name stored in DB
        "Goa Lounge INTL",
        "Prive (Goa)",
        "CIP Lounge",               # stored in DB as short name
        "RL Int Arrival",
    ],
    "Ancillary": [
        "Baggage Wrapping (Goa)",
        "Baggage Wrapping",         # short name stored in DB
        "Meet & Greet (Goa)",
        "Meet & Greet",             # short name stored in DB
        "M&G Goa",
        "M&G",                      # short name stored in DB
        "Porter (Goa)",
        "Porter",                   # short name stored in DB
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

HYD_SUBTOTALS: list[tuple[str, list[str]]] = [
    ("Total (International + Prive)", ["International Lounge", "Encalm Prive"]),
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
        # HYD overrides
        ("international lounge", "hyderabad"):  "International Lounge",
        ("domestic lounge", "hyderabad"):       "Domestic Lounge",
        ("prive", "hyderabad"):                 "Encalm Prive",
        ("encalm prive", "hyderabad"):          "Encalm Prive",
        ("m&g", "hyderabad"):                   "Atithya",
        ("meet & greet", "hyderabad"):          "Atithya",
        ("meet & greet (hyderabad)", "hyderabad"): "Atithya",
        ("m&g hyd", "hyderabad"):               "Atithya",
        ("gat (hyderabad)", "hyderabad"):        "Atithya",
        ("gat", "hyderabad"):                   "Atithya",
        ("atithya", "hyderabad"):               "Atithya",
        ("enwrap", "hyderabad"):                "Baggage Wrapping",
        ("baggage wrapping", "hyderabad"):      "Baggage Wrapping",
        ("baggage wrapping (hyderabad)", "hyderabad"): "Baggage Wrapping",
        ("encalm sky plates (hyderabad)", "hyderabad"): "Sky Plates",
        ("sky plates (hyderabad)", "hyderabad"):         "Sky Plates",
        ("sky plates hyd", "hyderabad"):                 "Sky Plates",
        ("encalm sky plates", "hyderabad"):              "Sky Plates",
        ("sky plates", "hyderabad"):                     "Sky Plates",
        ("porter", "hyderabad"):                "Porter",
        ("porter (hyderabad)", "hyderabad"):    "Porter",
        # GOA overrides — short names actually stored in DB
        ("domestic lounge", "goa"):             "Domestic Lounge",
        ("international lounge", "goa"):        "International Lounge",
        ("baggage wrapping", "goa"):            "Enwrap",
        ("enwrap", "goa"):                      "Enwrap",
        ("meet & greet", "goa"):                "M&G",
        ("m&g", "goa"):                         "M&G",
        ("porter", "goa"):                      "Porter",
        ("cip lounge", "goa"):                  "CIP Lounge",
        ("reserved lounge goa", "goa"):         "Reserved Lounge (Goa)",
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
