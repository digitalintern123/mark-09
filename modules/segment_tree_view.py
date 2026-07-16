"""
segment_tree_view.py — Renders the hierarchical Location → Service Category
→ Segment comparison view as a custom HTML/CSS component, used by the
"Segment Summary" tab on the Revenue Comparison page.

Why custom HTML instead of st.dataframe: the brief calls for expandable
cards with a clear three-level hierarchy (Location, then Service, then
Segment), icons, and a corporate dashboard look — none of which a flat
table can express. This module is pure rendering: it takes already-
computed comparison rows and turns them into markup. All number
formatting and comparison math stays in revenue_analysis.py / formatting.py
so this file has no business logic of its own.
"""

from __future__ import annotations

import html

import pandas as pd

from .formatting import format_money, format_pax, format_pct

LOCATION_ICONS = {
    "Delhi": "🏛️",
    "Hyderabad": "🕌",
    "Goa": "🏖️",
}
DEFAULT_LOCATION_ICON = "📍"

SEGMENT_ICONS = {
    "Lounges": "🛋️",
    "Atithya": "🤝",
    "Others": "🧾",
    "Subsidiary": "🍴",
}
DEFAULT_SEGMENT_ICON = "🏷️"

_TREND_UP = "up"
_TREND_DOWN = "down"
_TREND_FLAT = "flat"


def _trend_class(pct: float | None) -> str:
    if pct is None or pd.isna(pct):
        return _TREND_FLAT
    try:
        if not (pct == pct) or pct in (float("inf"), float("-inf")):
            return _TREND_FLAT
    except TypeError:
        return _TREND_FLAT
    if pct > 0.05:
        return _TREND_UP
    if pct < -0.05:
        return _TREND_DOWN
    return _TREND_FLAT


def _esc(value) -> str:
    return html.escape(str(value))


def render_segment_tree(
    location_summary: pd.DataFrame,
    segment_summary_by_location: pd.DataFrame,
    current_label: str,
    compare_label: str,
) -> str:
    """
    Build the full tree-view HTML for one rendering pass.

    location_summary: rows of {location, current_revenue, compare_revenue,
        revenue_pct_change, current_pax, compare_pax, pax_pct_change}
        (one row per location — i.e. revenue_analysis.compare_locations()).
    segment_summary_by_location: rows of {location, segment,
        current_revenue, compare_revenue, revenue_pct_change, current_pax,
        compare_pax, pax_pct_change} — i.e.
        revenue_analysis.compare_periods(..., group_cols=["location","segment"]).
    current_label / compare_label: human-readable period labels (e.g.
        "Jun 2026 (full month)") shown in the card header so the numbers
        have context without repeating it on every row.
    """
    locations = list(location_summary["location"]) if not location_summary.empty else []

    cards = []
    for loc in locations:
        loc_row = location_summary[location_summary["location"] == loc].iloc[0]
        seg_rows = segment_summary_by_location[
            segment_summary_by_location["location"] == loc
        ].sort_values("current_revenue", ascending=False)
        cards.append(_render_location_card(loc, loc_row, seg_rows, current_label, compare_label))

    body = "\n".join(cards) if cards else _empty_state()

    return f"""
<div class="segtree-root">
  {_STYLE_BLOCK}
  <div class="segtree-legend">
    <span class="segtree-legend-item"><span class="segtree-dot segtree-dot-up"></span>Growing (&gt;5%)</span>
    <span class="segtree-legend-item"><span class="segtree-dot segtree-dot-flat"></span>Stable (±5%)</span>
    <span class="segtree-legend-item"><span class="segtree-dot segtree-dot-down"></span>Declining (&lt;-5%)</span>
  </div>
  <div class="segtree-cards">
    {body}
  </div>
</div>
""".strip()


def _render_location_card(loc, loc_row, seg_rows, current_label, compare_label) -> str:
    icon = LOCATION_ICONS.get(loc, DEFAULT_LOCATION_ICON)
    trend = _trend_class(loc_row.get("revenue_pct_change"))
    rev_pct_str = format_pct(loc_row.get("revenue_pct_change"))
    pax_pct_str = format_pct(loc_row.get("pax_pct_change"))

    segment_html = "\n".join(
        _render_segment_row(row, current_label, compare_label) for _, row in seg_rows.iterrows()
    )
    if not segment_html:
        segment_html = '<div class="segtree-empty-segments">No segment data for this location.</div>'

    details_id = f"segtree-details-{_esc(loc).lower().replace(' ', '-')}"

    return f"""
<details class="segtree-card segtree-trend-{trend}" open>
  <summary class="segtree-card-header">
    <span class="segtree-card-title">
      <span class="segtree-icon" aria-hidden="true">{icon}</span>
      <span class="segtree-location-name">{_esc(loc)}</span>
    </span>
    <span class="segtree-card-metrics">
      <span class="segtree-metric">
        <span class="segtree-metric-label">Revenue</span>
        <span class="segtree-metric-value">{_esc(format_money(loc_row.get("current_revenue")))}</span>
        <span class="segtree-badge segtree-badge-{trend}">{_esc(rev_pct_str)}</span>
      </span>
      <span class="segtree-metric">
        <span class="segtree-metric-label">PAX</span>
        <span class="segtree-metric-value">{_esc(format_pax(loc_row.get("current_pax")))}</span>
        <span class="segtree-badge segtree-badge-{_trend_class(loc_row.get('pax_pct_change'))}">{_esc(pax_pct_str)}</span>
      </span>
      <span class="segtree-chevron" aria-hidden="true">›</span>
    </span>
  </summary>
  <div class="segtree-card-body" id="{details_id}">
    <div class="segtree-services-label">
      <span class="segtree-icon-small" aria-hidden="true">🗂️</span>Service Categories
    </div>
    <div class="segtree-segments">
      {segment_html}
    </div>
  </div>
</details>
""".strip()


def _render_segment_row(row, current_label, compare_label) -> str:
    segment = row["segment"]
    icon = SEGMENT_ICONS.get(segment, DEFAULT_SEGMENT_ICON)
    rev_trend = _trend_class(row.get("revenue_pct_change"))
    pax_trend = _trend_class(row.get("pax_pct_change"))

    return f"""
<div class="segtree-segment segtree-trend-{rev_trend}">
  <div class="segtree-segment-header">
    <span class="segtree-icon-small" aria-hidden="true">{icon}</span>
    <span class="segtree-segment-name">{_esc(segment)}</span>
    <span class="segtree-badge segtree-badge-{rev_trend}">{_esc(format_pct(row.get('revenue_pct_change')))}</span>
  </div>
  <div class="segtree-segment-grid">
    <div class="segtree-stat">
      <span class="segtree-stat-label">{_esc(current_label)}</span>
      <span class="segtree-stat-value">{_esc(format_money(row.get('current_revenue')))}</span>
      <span class="segtree-stat-sub">{_esc(format_pax(row.get('current_pax')))} PAX</span>
    </div>
    <div class="segtree-stat segtree-stat-compare">
      <span class="segtree-stat-label">{_esc(compare_label)}</span>
      <span class="segtree-stat-value">{_esc(format_money(row.get('compare_revenue')))}</span>
      <span class="segtree-stat-sub">{_esc(format_pax(row.get('compare_pax')))} PAX</span>
    </div>
    <div class="segtree-stat segtree-stat-delta">
      <span class="segtree-stat-label">PAX Δ%</span>
      <span class="segtree-badge segtree-badge-{pax_trend} segtree-badge-block">{_esc(format_pct(row.get('pax_pct_change')))}</span>
    </div>
  </div>
</div>
""".strip()


def _empty_state() -> str:
    return """
<div class="segtree-empty">
  <div class="segtree-empty-icon">🗺️</div>
  <div class="segtree-empty-title">No location data for this period</div>
  <div class="segtree-empty-sub">Try a different comparison period, or upload more data.</div>
</div>
""".strip()


_STYLE_BLOCK = """
<style>
.segtree-root {
  font-family: "Source Sans Pro", "Segoe UI", system-ui, -apple-system, sans-serif;
  color: #1E293B;
}
.segtree-legend {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  margin-bottom: 14px;
  font-size: 0.8rem;
  color: #64748B;
}
.segtree-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.segtree-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}
.segtree-dot-up { background: #0F9D74; }
.segtree-dot-flat { background: #94A3B8; }
.segtree-dot-down { background: #DC2626; }

.segtree-cards {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.segtree-card {
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-left: 4px solid #94A3B8;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  overflow: hidden;
}
.segtree-card.segtree-trend-up { border-left-color: #0F9D74; }
.segtree-card.segtree-trend-down { border-left-color: #DC2626; }
.segtree-card.segtree-trend-flat { border-left-color: #94A3B8; }

.segtree-card-header {
  list-style: none;
  cursor: pointer;
  padding: 16px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  background: #F8FAFC;
  user-select: none;
  transition: background 0.12s ease;
}
.segtree-card-header:hover {
  background: #F1F5F9;
}
.segtree-card-header::-webkit-details-marker { display: none; }

.segtree-card-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 1.05rem;
  color: #0F172A;
}
.segtree-icon { font-size: 1.3rem; line-height: 1; }
.segtree-icon-small { font-size: 1rem; line-height: 1; margin-right: 6px; }

.segtree-card-metrics {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}
.segtree-metric {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding-right: 24px;
  border-right: 1px solid #E2E8F0;
}
.segtree-metric:last-of-type {
  border-right: none;
  padding-right: 0;
}
.segtree-metric-label {
  font-size: 0.72rem;
  color: #64748B;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.segtree-metric-value {
  font-weight: 600;
  font-size: 0.95rem;
  color: #0F172A;
}
.segtree-chevron {
  font-size: 1.2rem;
  color: #94A3B8;
  transition: transform 0.15s ease;
}
details[open] > .segtree-card-header .segtree-chevron {
  transform: rotate(90deg);
}

.segtree-badge {
  display: inline-block;
  font-size: 0.74rem;
  font-weight: 700;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
  letter-spacing: 0.01em;
}
.segtree-badge-up { background: #CFF1E3; color: #0B7A57; }
.segtree-badge-down { background: #FBDADA; color: #B91C1C; }
.segtree-badge-flat { background: #E7EBF0; color: #475569; }
.segtree-badge-block { display: block; text-align: center; margin-top: 6px; }

.segtree-card-body {
  padding: 20px 18px 18px 18px;
  background: #FFFFFF;
}
.segtree-services-label {
  font-size: 0.78rem;
  font-weight: 700;
  color: #64748B;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #EEF2F6;
}

.segtree-segments {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.segtree-segment {
  border: 1px solid #E2E8F0;
  border-left: 3px solid #94A3B8;
  border-radius: 8px;
  padding: 12px 14px;
  background: #FCFCFD;
  box-shadow: 0 1px 1px rgba(15, 23, 42, 0.02);
  transition: box-shadow 0.12s ease, border-color 0.12s ease;
}
.segtree-segment:hover {
  box-shadow: 0 2px 6px rgba(15, 23, 42, 0.06);
  border-color: #CBD5E1;
}
.segtree-segment.segtree-trend-up { border-left-color: #0F9D74; }
.segtree-segment.segtree-trend-down { border-left-color: #DC2626; }
.segtree-segment.segtree-trend-flat { border-left-color: #94A3B8; }

.segtree-segment-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.segtree-segment-name {
  font-weight: 600;
  font-size: 0.92rem;
  color: #1E293B;
  flex: 1;
}

.segtree-segment-grid {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 10px;
  align-items: center;
}
.segtree-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.segtree-stat-label {
  font-size: 0.68rem;
  color: #94A3B8;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.segtree-stat-value {
  font-weight: 600;
  font-size: 0.95rem;
  color: #0F172A;
}
.segtree-stat-sub {
  font-size: 0.72rem;
  color: #64748B;
}
.segtree-stat-delta {
  min-width: 64px;
}

.segtree-empty-segments {
  font-size: 0.85rem;
  color: #94A3B8;
  padding: 8px 0;
}

.segtree-empty {
  text-align: center;
  padding: 48px 20px;
  color: #64748B;
}
.segtree-empty-icon { font-size: 2.2rem; margin-bottom: 8px; }
.segtree-empty-title { font-weight: 600; font-size: 1rem; color: #1E293B; }
.segtree-empty-sub { font-size: 0.85rem; margin-top: 4px; }

@media (max-width: 640px) {
  .segtree-card-header {
    flex-direction: column;
    align-items: flex-start;
  }
  .segtree-card-metrics {
    width: 100%;
    justify-content: space-between;
    gap: 14px;
  }
  .segtree-segment-grid {
    grid-template-columns: 1fr 1fr;
  }
  .segtree-stat-delta {
    grid-column: span 2;
  }
}
</style>
"""
