"""
session.py — Lightweight session-state helpers shared across all pages.

Per the persistence requirements: st.session_state must only ever hold
small references (selected dates, UI toggles) — never DataFrames of revenue
data. Every page re-loads its data from the database on each run via
modules.database, so navigating between pages never loses anything and
switching pages is always consistent with what's actually stored.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import streamlit as st

from . import database
from . import github_backup

_ACTIVE_DATE_KEY   = "active_analysis_date"
_COMPARE_DATE_KEY  = "compare_analysis_date"
_BOOTSTRAPPED_KEY  = "_db_bootstrapped"


def bootstrap_session() -> None:
    """
    Ensure the database exists and is up to date. Safe to call at the
    top of every page — the heavy work (DB init + GitHub restore) runs
    only once per session thanks to the _BOOTSTRAPPED_KEY guard.
    """
    if not st.session_state.get(_BOOTSTRAPPED_KEY):
        # Step 1: restore the DB from GitHub if local copy is missing/stale.
        # This is a no-op when GitHub is not configured or the local DB is
        # already current — and it never blocks the boot if GitHub is down.
        try:
            github_backup.restore_if_needed(database.DB_PATH)
        except Exception:
            pass  # GitHub unavailable — continue with local DB

        # Step 2: initialise (or migrate) the local SQLite schema.
        database.init_db()
        st.session_state[_BOOTSTRAPPED_KEY] = True


def get_active_date() -> Optional[dt.date]:
    """The currently selected 'main' analysis date, or None if unset."""
    return st.session_state.get(_ACTIVE_DATE_KEY)


def set_active_date(value: Optional[dt.date]) -> None:
    st.session_state[_ACTIVE_DATE_KEY] = value


def get_compare_date() -> Optional[dt.date]:
    """The currently selected 'comparison' date (Page 4), or None if unset."""
    return st.session_state.get(_COMPARE_DATE_KEY)


def set_compare_date(value: Optional[dt.date]) -> None:
    st.session_state[_COMPARE_DATE_KEY] = value


def clear_session() -> None:
    """
    Clear the active workspace (selected dates / UI state) without touching
    the database. Keeps the bootstrap flag so we don't re-run init_db
    needlessly, but resets everything else a user would consider 'workspace'
    state.
    """
    keys_to_clear = [
        k for k in st.session_state.keys()
        if k not in (_BOOTSTRAPPED_KEY,)
    ]
    for k in keys_to_clear:
        del st.session_state[k]


def default_active_date() -> Optional[dt.date]:
    """
    Fall back to the most recent date in the database if no active date has
    been explicitly selected yet — gives every page a sensible default on
    first load instead of an empty selector.
    """
    current = get_active_date()
    if current is not None:
        return current
    dates = database.get_available_dates()
    return dates[-1] if dates else None
