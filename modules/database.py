"""
database.py — SQLite persistence layer for the Revenue Analytics platform.

Design notes:
- The database is the single source of truth for all revenue data.
- st.session_state should only ever hold lightweight references (selected
  dates, UI state) — never DataFrames of revenue data. This module is what
  every page calls to actually fetch data, so navigating between pages never
  loses anything.
- A UNIQUE constraint on (date, segment, outlet, location) makes re-uploading
  the same day's report a safe no-op: duplicate rows are skipped, not errored.
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
from typing import Optional

import pandas as pd
from sqlalchemy import (
    Column,
    Date,
    Float,
    Integer,
    String,
    UniqueConstraint,
    and_ as sql_and,
    create_engine,
    func,
    or_ as sql_or,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

# FIX (Bug 5): import the single canonical month-shift implementation.
# Previously database.py defined its own private _safe_month_shift(); that
# duplicate is removed below and replaced with this shared version.
from .date_utils import safe_month_shift as _safe_month_shift

# ── Database path ─────────────────────────────────────────────────────────
# On Streamlit Cloud the container filesystem is ephemeral — the DB is wiped
# on every restart/deploy. To persist data across restarts, set the env var
# REVENUE_DB_PATH to a path outside the container (e.g. a mounted volume).
# Locally it defaults to the repo root.
_DB_ENV = os.environ.get("REVENUE_DB_PATH", "").strip()

if _DB_ENV:
    # Explicit env var — honour it but ensure the parent directory exists.
    DB_PATH = _DB_ENV
else:
    # On Streamlit Cloud the repo root (/mount/src/) is read-only — SQLite
    # cannot create or write a DB file there even if os.makedirs succeeds.
    # Use a true write test (write + read back a byte) to confirm the path
    # is genuinely writable before committing to it. /tmp is always safe.
    _repo_root = os.path.dirname(os.path.dirname(__file__))
    _candidates = [
        os.path.join(_repo_root, "data", "revenue_analytics.db"),
        os.path.join(_repo_root, "revenue_analytics.db"),
        "/tmp/revenue_analytics.db",
    ]
    DB_PATH = "/tmp/revenue_analytics.db"
    for _candidate in _candidates:
        try:
            _dir = os.path.dirname(_candidate)
            if _dir:
                os.makedirs(_dir, exist_ok=True)
            # True write test: open in read+write binary mode so the OS
            # actually attempts a write syscall — catches read-only mounts
            # that allow open() but reject writes (Streamlit Cloud behaviour).
            _test_path = _dir + "/.write_test"
            with open(_test_path, "wb") as _fh:
                _fh.write(b"x")
            os.remove(_test_path)
            DB_PATH = _candidate
            break
        except OSError:
            continue

# Always ensure the parent directory exists regardless of how DB_PATH was set.
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
os.makedirs(_db_dir, exist_ok=True)

ENGINE = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={
        "check_same_thread": False,
        # FIX (Improvement 6): add a 30-second busy timeout so concurrent
        # Streamlit sessions that arrive at a write at the same moment
        # wait for the lock to clear rather than immediately raising
        # "database is locked". SQLite serialises all writes, so this is
        # a low-cost safety net with no behaviour change under normal load.
        "timeout": 30,
    },
)
SessionLocal = sessionmaker(bind=ENGINE)

def _read_sql(query) -> pd.DataFrame:
    """
    Compatibility wrapper for pd.read_sql.

    pandas + SQLAlchemy 2.x on Python 3.14 no longer accepts a bare Engine
    as the second argument to pd.read_sql when an ORM select() object is
    passed — it requires an explicit Connection context.  Using a raw SQL
    string with ENGINE.connect() is the correct approach for both old and
    new versions.

    This helper centralises that pattern so every call site stays clean.
    """
    with ENGINE.connect() as conn:
        return pd.read_sql(query, conn)


Base = declarative_base()


class RevenueMaster(Base):
    """
    One row = one (date, segment, outlet, location) revenue record.

    `segment` is the top-level business: "EHPL" (Encalm Hospitality
    Private Ltd — the umbrella business covering lounges, meet & greet,
    and other airport services), "Sky Plates", or "Encalm Eats".

    `business_unit` preserves the finer-grained category within EHPL
    (Lounges / Atithya / Others) for rows where segment == "EHPL", so that
    detail isn't lost even though those three are no longer separate
    top-level segments. For Sky Plates and Encalm Eats rows,
    business_unit is the same as segment (there's no further split).
    """

    __tablename__ = "revenue_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    segment = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)
    outlet = Column(String, nullable=False)
    location = Column(String, nullable=False)
    pax = Column(Float, nullable=True)
    revenue = Column(Float, nullable=True)
    aop = Column(Float, nullable=True)
    traffic = Column(Float, nullable=True)
    source_file = Column(String, nullable=True)
    uploaded_at = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("date", "segment", "outlet", "location", name="uq_revenue_row"),
    )


class AirportTraffic(Base):
    """
    One row = total airport visitor traffic for one (date, location,
    terminal), at a given granularity. Traffic is recorded terminal-wise
    (e.g. Delhi T1/T2/T3), not just airport-wide — so `terminal` is part
    of the row identity. `terminal` is "" (empty string, not NULL — see
    save_traffic_dataframe for why NULL specifically breaks the dedup
    constraint) for locations/files that don't break traffic out by
    terminal; in that case treat the whole airport as one terminal for
    that location.

    `granularity` is "daily" or "monthly": some traffic files report one
    row per day, others report one row per month (with `date` set to the
    1st of that month and `period_end` set to the month's last day) — both
    are supported and stored side by side rather than forcing monthly
    figures into a misleading daily average. Analysis code should prefer
    daily rows when available for a given date range and fall back to
    monthly rows otherwise (see database.load_traffic_for_date_range).

    Traffic is airport-wide visitor count (everyone who passed through
    that terminal that day/month), not outlet-level like revenue_master —
    so it's kept in its own table and joined against revenue_master at
    query time via the outlet -> terminal mapping in
    modules/terminal_mapping.py, rather than duplicated across every
    outlet row.

    PAX (in revenue_master) and traffic (here) are deliberately different
    things: traffic = total airport/terminal visitors that day; PAX =
    customers who actually used Encalm's services. Penetration % = PAX ÷
    Traffic is the metric that relates the two.
    """

    __tablename__ = "airport_traffic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    period_end = Column(Date, nullable=True)
    granularity = Column(String, nullable=False, default="daily")
    location = Column(String, nullable=False)
    terminal = Column(String, nullable=True)
    traffic = Column(Float, nullable=False)
    source_file = Column(String, nullable=True)
    uploaded_at = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("date", "location", "terminal", "granularity", name="uq_traffic_row"),
    )


class AOPTarget(Base):
    """
    One row = the AOP (Annual Operating Plan) revenue target for one
    (location, outlet, year, month) — a forward-looking budget figure,
    independent of whether revenue data exists yet for that period (the
    AOP plan in this app currently covers FY26-27 through FY30-31, years
    that mostly haven't happened yet).

    Kept separate from revenue_master's per-row `aop` column (which is
    still supported for historical Excel imports that already embed an
    AOP figure per row) because a forward plan needs to exist on its own
    timeline — AOP variance is computed by joining live revenue against
    this table for whichever (year, month) the revenue actually falls in,
    not by expecting AOP to already be attached to each revenue row.

    `business_unit` mirrors revenue_master's same-named column (Lounges /
    Atithya / Others) so AOP variance can be sliced the same way revenue
    already is. `segment` is "EHPL" for everything currently imported
    (Sky Plates / Encalm Eats AOP isn't in the source file yet).
    """

    __tablename__ = "aop_target"

    id = Column(Integer, primary_key=True, autoincrement=True)
    location = Column(String, nullable=False)
    segment = Column(String, nullable=False)
    business_unit = Column(String, nullable=True)
    outlet = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    aop = Column(Float, nullable=False)
    source_file = Column(String, nullable=True)
    uploaded_at = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("location", "outlet", "year", "month", name="uq_aop_target_row"),
    )


class AOPTargetDaily(Base):
    """
    One row = the AOP (Annual Operating Plan) revenue target for one
    (location, date) — a second, simpler AOP source format: a daily
    total per location with no outlet/segment breakdown at all (e.g. a
    pivot-table export with one row per calendar day and one column per
    location). This is kept in its own table, separate from AOPTarget
    (the per-outlet/monthly format), because the two have genuinely
    different grains — this one is daily and location-only, the other is
    monthly and outlet-level — and merging them into one table would mean
    inventing a fake "no outlet" sentinel value, which is more confusing
    than just keeping two tables.

    AOP variance calculations should prefer whichever of the two sources
    actually has data for the period being compared, and combine them
    when both are partially available (see database.get_aop_target_for_range).
    """

    __tablename__ = "aop_target_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    location = Column(String, nullable=False)
    date = Column(Date, nullable=False, index=True)
    aop = Column(Float, nullable=False)
    source_file = Column(String, nullable=True)
    uploaded_at = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("location", "date", name="uq_aop_target_daily_row"),
    )


class UploadHistory(Base):
    """One row per file upload event, used for the Previous Uploads page."""

    __tablename__ = "upload_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_name = Column(String, nullable=False)
    report_date = Column(Date, nullable=True)
    row_count = Column(Integer, nullable=False, default=0)
    # `total_revenue` is kept as the primary numeric total column so all
    # existing callers (_record_upload_history, get_upload_history, the
    # Previous Uploads page) continue to work without any changes.
    total_revenue = Column(Float, nullable=True)
    # FIX (Improvement 7): `primary_total` is added as a separate real
    # column — NOT a synonym (synonyms break pd.read_sql on Python 3.14).
    # It mirrors total_revenue and is written alongside it in
    # _record_upload_history so both are always kept in sync.
    primary_total = Column(Float, nullable=True)
    total_pax = Column(Float, nullable=True)
    uploaded_at = Column(String, nullable=False)
    status = Column(String, nullable=False, default="Available")
    # "Revenue" | "AOP" | "Traffic" — which upload pipeline produced this
    # entry. Defaults to "Revenue" so pre-existing rows (recorded before
    # this column existed, back when only revenue uploads were logged at
    # all) still display correctly without a data backfill.
    upload_type = Column(String, nullable=False, default="Revenue")


# ---------------------------------------------------------------------------
# Segment / business-unit canonicalization
# ---------------------------------------------------------------------------
#
# Encalm Group's real top-level businesses are EHPL (Encalm Hospitality
# Private Ltd — the largest segment, covering lounges, meet & greet/Atithya
# services, and other airport services), Sky Plates, and Encalm Eats.
# Historically (and in some source files), these were modeled as four
# flat, sibling "segments": Lounges, Atithya, Others, Subsidiary — with
# Subsidiary itself bundling both Sky Plates and Encalm Eats together.
#
# This mapping is applied once, here, at the point data is saved — rather
# than duplicated across every parser — so every row in the database
# always has the correct three-way `segment` (EHPL / Sky Plates / Encalm
# Eats) plus a `business_unit` that preserves the old Lounges/Atithya/
# Others detail for EHPL rows (and just mirrors segment for the other two).
_LEGACY_SEGMENT_TO_EHPL = {"lounges", "atithya", "others"}
_OUTLET_TO_NEW_SEGMENT = {
    "encalm sky plates": "Sky Plates",
    "sky plates": "Sky Plates",
    "encalm eats": "Encalm Eats",
}


def canonicalize_segment_and_business_unit(raw_segment: str, outlet: str) -> tuple[str, str]:
    """
    Map a (possibly legacy) segment label + outlet name onto the canonical
    (segment, business_unit) pair used everywhere in the app.

    Rules:
      - Lounges / Atithya / Others (any case)  -> segment="EHPL", business_unit=<original title-cased>
      - Subsidiary, with outlet "Encalm Sky Plates"/"Sky Plates" -> segment="Sky Plates", business_unit="Sky Plates"
      - Subsidiary, with outlet "Encalm Eats"                    -> segment="Encalm Eats", business_unit="Encalm Eats"
      - Already-canonical EHPL / Sky Plates / Encalm Eats        -> passed through unchanged
        (business_unit defaults to the outlet-derived mapping if the
        segment is Sky Plates/Encalm Eats, else to the raw segment itself
        for EHPL rows that already arrive correctly tagged)
      - Anything else (unrecognized segment) -> passed through as-is, with
        business_unit equal to the raw segment, so unexpected future
        segments stay visible rather than silently mis-bucketed.
    """
    seg_key = str(raw_segment).strip().lower()
    outlet_key = str(outlet).strip().lower()

    if seg_key in _LEGACY_SEGMENT_TO_EHPL:
        return "EHPL", str(raw_segment).strip().title()

    if seg_key == "subsidiary":
        new_segment = _OUTLET_TO_NEW_SEGMENT.get(outlet_key)
        if new_segment:
            return new_segment, new_segment
        # Unrecognized outlet under the legacy Subsidiary segment — keep
        # it visible rather than guessing which of the two it belongs to.
        return str(raw_segment).strip(), str(raw_segment).strip()

    if seg_key == "ehpl":
        return "EHPL", str(raw_segment).strip()

    if seg_key in ("sky plates", "encalm sky plates"):
        return "Sky Plates", "Sky Plates"

    if seg_key == "encalm eats":
        return "Encalm Eats", "Encalm Eats"

    return str(raw_segment).strip(), str(raw_segment).strip()


def init_db() -> None:
    """
    Create tables if they don't already exist. Safe to call repeatedly, and
    safe to call concurrently from multiple processes/threads (e.g. several
    Streamlit sessions starting up at once, or Streamlit's own first-launch
    double-execution on some platforms).

    SQLAlchemy's create_all() normally checks "does this table exist?" and
    then issues CREATE TABLE if not — but those are two separate steps, so
    on a brand-new database file, two near-simultaneous callers can both
    see "doesn't exist yet" and both try to create it, and the loser gets
    an OperationalError. We catch that specific race and treat it as
    success, since the end state (table exists) is exactly what we wanted.

    Also runs `_migrate_schema()` afterwards, which adds any columns that
    were introduced after a database file was first created (e.g.
    business_unit) and re-tags any rows still using legacy segment names
    (Lounges/Atithya/Others/Subsidiary) onto the current EHPL/Sky Plates/
    Encalm Eats structure — so upgrading the app's code doesn't strand
    existing data in the old shape or crash on a missing column.
    """
    try:
        Base.metadata.create_all(ENGINE)
    except OperationalError as exc:
        if "already exists" in str(exc).lower():
            # Another process/thread won the race and created it first —
            # the table exists, which is the only thing we actually cared
            # about, so this is not a real failure.
            pass
        else:
            raise
    _migrate_schema()


def _migrate_schema() -> None:
    """
    Idempotent, additive-only schema migration. Safe to call on every
    startup: each step checks the current state before acting, so running
    it against an already-migrated database is a fast no-op.
    """
    with ENGINE.begin() as conn:
        existing_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(revenue_master)")).fetchall()
        }
        if "business_unit" not in existing_columns:
            conn.execute(text("ALTER TABLE revenue_master ADD COLUMN business_unit VARCHAR"))

        # airport_traffic may not exist yet on a database created before
        # this table was introduced — PRAGMA table_info on a non-existent
        # table just returns no rows (not an error), so this check is safe
        # either way and create_all() above will have already made the
        # table if this is a fresh database.
        traffic_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(airport_traffic)")).fetchall()
        }
        needs_rebuild = False
        if traffic_columns and "terminal" not in traffic_columns:
            conn.execute(text("ALTER TABLE airport_traffic ADD COLUMN terminal VARCHAR"))
            needs_rebuild = True
        if traffic_columns and "granularity" not in traffic_columns:
            conn.execute(
                text("ALTER TABLE airport_traffic ADD COLUMN granularity VARCHAR DEFAULT 'daily'")
            )
            conn.execute(text("UPDATE airport_traffic SET granularity = 'daily' WHERE granularity IS NULL"))
            needs_rebuild = True
        if traffic_columns and "period_end" not in traffic_columns:
            conn.execute(text("ALTER TABLE airport_traffic ADD COLUMN period_end DATE"))

        if needs_rebuild:
            # The table's UNIQUE constraint predates terminal/granularity
            # tracking — that constraint can't be altered in place in
            # SQLite, so rebuild the table under the current (date,
            # location, terminal, granularity) constraint, preserving any
            # rows already present.
            _rebuild_airport_traffic_constraint(conn)

        # upload_history may not exist yet on a database created before
        # this table was introduced — same non-existent-table safety as
        # airport_traffic above.
        upload_history_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(upload_history)")).fetchall()
        }
        if upload_history_columns and "upload_type" not in upload_history_columns:
            conn.execute(text("ALTER TABLE upload_history ADD COLUMN upload_type VARCHAR DEFAULT 'Revenue'"))
            conn.execute(text("UPDATE upload_history SET upload_type = 'Revenue' WHERE upload_type IS NULL"))

        # FIX (Improvement 7): add primary_total as a real column (not a
        # synonym — synonyms conflict with pd.read_sql on Python 3.14+).
        # Backfill it from total_revenue so existing upload history rows
        # show the correct value immediately after the migration.
        if upload_history_columns and "primary_total" not in upload_history_columns:
            conn.execute(text("ALTER TABLE upload_history ADD COLUMN primary_total FLOAT"))
            conn.execute(text("UPDATE upload_history SET primary_total = total_revenue"))

    _migrate_legacy_segments()


def _rebuild_airport_traffic_constraint(conn) -> None:
    """
    Rebuild airport_traffic with the current (date, location, terminal,
    granularity) UNIQUE constraint, preserving existing rows. SQLite can't
    ALTER a table's UNIQUE constraint directly, so this does the standard
    create-new / copy / drop-old / rename dance inside the same
    transaction as the caller.
    """
    current_columns = {
        row[1] for row in conn.execute(text("PRAGMA table_info(airport_traffic)")).fetchall()
    }
    has_period_end = "period_end" in current_columns

    conn.execute(
        text(
            """
            CREATE TABLE airport_traffic_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                period_end DATE,
                granularity VARCHAR NOT NULL DEFAULT 'daily',
                location VARCHAR NOT NULL,
                terminal VARCHAR,
                traffic FLOAT NOT NULL,
                source_file VARCHAR,
                uploaded_at VARCHAR,
                CONSTRAINT uq_traffic_row UNIQUE (date, location, terminal, granularity)
            )
            """
        )
    )
    period_end_select = "period_end" if has_period_end else "NULL"
    conn.execute(
        text(
            f"""
            INSERT INTO airport_traffic_new (id, date, period_end, granularity, location, terminal, traffic, source_file, uploaded_at)
            SELECT id, date, {period_end_select}, COALESCE(granularity, 'daily'), location, terminal, traffic, source_file, uploaded_at FROM airport_traffic
            """
        )
    )
    conn.execute(text("DROP TABLE airport_traffic"))
    conn.execute(text("ALTER TABLE airport_traffic_new RENAME TO airport_traffic"))


def _migrate_legacy_segments() -> None:
    """
    Re-tag any rows still carrying the old flat segment names (Lounges,
    Atithya, Others, Subsidiary) onto the current three-segment structure
    (EHPL / Sky Plates / Encalm Eats), filling in business_unit at the
    same time. Only touches rows that need it — already-canonical rows
    (segment already EHPL/Sky Plates/Encalm Eats with business_unit set)
    are left untouched, so this is cheap to run on every startup.
    """
    legacy_segments = ["Lounges", "Atithya", "Others", "Subsidiary", "lounges", "atithya", "others", "subsidiary"]

    # FIX (Bug 4): the original query used:
    #   WHERE segment IN (...) OR business_unit IS NULL
    # The bare "OR business_unit IS NULL" matched every canonical row
    # (EHPL / Sky Plates / Encalm Eats) written before the business_unit
    # column existed, causing a full table scan + unnecessary UPDATE batch
    # on every app startup once all rows had already been migrated.
    #
    # The corrected condition restricts the business_unit IS NULL branch
    # to rows whose segment is NOT already one of the three canonical
    # values, so previously-migrated rows are never touched again.
    canonical_segments = ("EHPL", "Sky Plates", "Encalm Eats")
    canonical_placeholders = ",".join(f":canon{i}" for i in range(len(canonical_segments)))
    canonical_params = {f"canon{i}": s for i, s in enumerate(canonical_segments)}

    with ENGINE.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, segment, outlet FROM revenue_master
                WHERE segment IN ({legacy_ph})
                   OR (
                       business_unit IS NULL
                       AND segment NOT IN ({canon_ph})
                   )
                """.format(
                    legacy_ph=",".join(f":seg{i}" for i in range(len(legacy_segments))),
                    canon_ph=canonical_placeholders,
                )
            ),
            {**{f"seg{i}": s for i, s in enumerate(legacy_segments)}, **canonical_params},
        ).fetchall()

        if not rows:
            return

        update_sql = text(
            "UPDATE revenue_master SET segment = :segment, business_unit = :business_unit WHERE id = :id"
        )
        updates = []
        for row_id, raw_segment, outlet in rows:
            new_segment, business_unit = canonicalize_segment_and_business_unit(
                raw_segment or "", outlet or ""
            )
            updates.append({"id": row_id, "segment": new_segment, "business_unit": business_unit})

        conn.execute(update_sql, updates)


def reset_db() -> None:
    """Danger zone: drop and recreate all tables, wiping all stored data."""
    Base.metadata.drop_all(ENGINE)
    try:
        Base.metadata.create_all(ENGINE)
    except OperationalError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


# ---------------------------------------------------------------------------
# Writing data
# ---------------------------------------------------------------------------

def save_dataframe(
    df: pd.DataFrame,
    source_file: str,
    record_upload: bool = True,
) -> dict:
    """
    Insert a normalized revenue DataFrame into revenue_master.

    Expects columns: date, segment, outlet, location, pax, revenue, aop, traffic
    (aop/traffic optional, may be all-NaN).

    Duplicate (date, segment, outlet, location) rows are skipped silently —
    this makes re-uploading the same report a harmless no-op. Uses a bulk
    "INSERT OR IGNORE" so large historical imports (50K+ rows) complete in
    a couple of seconds rather than minutes.

    Returns a dict summary: {"inserted": int, "skipped": int, "total_rows": int}
    """
    if df.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": 0}

    required_cols = {"date", "segment", "outlet", "location"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")

    now_str = dt.datetime.now().isoformat(timespec="seconds")

    # FIX (Bug 8): replaced the slow `for _, row in df.iterrows()` loop
    # with a vectorized approach:
    #   1. String-strip the key columns with .str.strip() (vectorized).
    #   2. Apply canonicalize_segment_and_business_unit once per row via
    #      .apply() — this call cannot be fully vectorized because the
    #      function contains conditional branching on individual values,
    #      but a single .apply() pass is significantly faster than the
    #      Python-level iteration + dict-append in iterrows().
    #   3. Convert numeric columns to float via .apply(_to_float_or_none)
    #      rather than calling it inside the row loop.
    #   4. Use .to_dict("records") to produce the final list in one step.
    work = df.copy()

    # Vectorized string normalisation for the four core string columns.
    work["segment"]  = work["segment"].astype(str).str.strip()
    work["outlet"]   = work["outlet"].astype(str).str.strip()
    work["location"] = work["location"].astype(str).str.strip()

    # Apply segment canonicalization — returns (segment, business_unit) per row.
    canon = work.apply(
        lambda r: canonicalize_segment_and_business_unit(r["segment"], r["outlet"]),
        axis=1,
    )
    work["segment"]       = canon.apply(lambda t: t[0])
    work["business_unit"] = canon.apply(lambda t: t[1])

    # Normalise date to ISO string.
    work["date"] = work["date"].apply(lambda v: _to_date(v).isoformat())

    # Normalise optional float columns; missing columns default to None.
    for col in ("pax", "revenue", "aop", "traffic"):
        if col in work.columns:
            work[col] = work[col].apply(_to_float_or_none)
        else:
            work[col] = None

    # Attach the upload-context fields.
    work["source_file"]  = source_file
    work["uploaded_at"]  = now_str

    # Select only the columns the INSERT expects, in order, to ensure no
    # extra columns from the caller's DataFrame sneak into the records list.
    records = work[
        ["date", "segment", "business_unit", "outlet", "location",
         "pax", "revenue", "aop", "traffic", "source_file", "uploaded_at"]
    ].to_dict("records")

    insert_sql = text(
        """
        INSERT OR IGNORE INTO revenue_master
            (date, segment, business_unit, outlet, location, pax, revenue, aop, traffic, source_file, uploaded_at)
        VALUES
            (:date, :segment, :business_unit, :outlet, :location, :pax, :revenue, :aop, :traffic, :source_file, :uploaded_at)
        """
    )

    with ENGINE.begin() as conn:
        before = conn.execute(select(func.count(RevenueMaster.id))).scalar() or 0
        conn.execute(insert_sql, records)
        after = conn.execute(select(func.count(RevenueMaster.id))).scalar() or 0

    inserted = after - before
    skipped = len(records) - inserted

    if record_upload:
        report_date = _to_date(df["date"].iloc[0]) if "date" in df.columns else None
        # Sum revenue/PAX for only the rows this call actually inserted,
        # not the whole parsed file — every record just inserted carries
        # this exact (source_file, uploaded_at) pair (uploaded_at is a
        # fresh timestamp generated above, per call), so this query
        # identifies exactly the new rows even when some/all of the
        # file's other rows were skipped as duplicates. Without this, a
        # re-upload where every row is a duplicate would still log the
        # file's full revenue/PAX total next to "0 rows saved," which
        # looks like new data was added when nothing was.
        if inserted > 0:
            with ENGINE.begin() as conn:
                inserted_revenue, inserted_pax = conn.execute(
                    select(
                        func.coalesce(func.sum(RevenueMaster.revenue), 0.0),
                        func.coalesce(func.sum(RevenueMaster.pax), 0.0),
                    ).where(
                        RevenueMaster.source_file == source_file,
                        RevenueMaster.uploaded_at == now_str,
                    )
                ).one()
        else:
            inserted_revenue, inserted_pax = 0.0, 0.0
        _record_upload_history(
            file_name=source_file,
            report_date=report_date,
            row_count=inserted,
            total_revenue=float(inserted_revenue),
            total_pax=float(inserted_pax),
            upload_type="Revenue",
        )

    upload_type_label = "Revenue"

    # Trigger async GitHub backup after every successful write.
    from . import github_backup as _ghb  # local import avoids circular dependency
    if inserted > 0:
        _ghb.trigger_backup(
            upload_type=upload_type_label,
            source_file=source_file,
            inserted=inserted,
        )
    return {"inserted": inserted, "skipped": skipped, "total_rows": len(df)}


def _record_upload_history(
    file_name: str,
    report_date: Optional[dt.date],
    row_count: int,
    total_revenue: float,
    total_pax: float,
    upload_type: str = "Revenue",
) -> None:
    session = SessionLocal()
    try:
        entry = UploadHistory(
            file_name=file_name,
            report_date=report_date,
            row_count=row_count,
            total_revenue=total_revenue,
            # FIX (Improvement 7): keep primary_total in sync with
            # total_revenue so both columns always hold the same value.
            primary_total=total_revenue,
            total_pax=total_pax,
            uploaded_at=dt.datetime.now().isoformat(timespec="seconds"),
            status="Available",
            upload_type=upload_type,
        )
        session.add(entry)
        session.commit()
    finally:
        session.close()


def _to_date(value) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    return pd.to_datetime(value).date()


def _to_float_or_none(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Reading data
# ---------------------------------------------------------------------------

def load_all() -> pd.DataFrame:
    """Load the entire revenue_master table as a DataFrame."""
    return _read_sql(select(RevenueMaster))


def load_for_date(target_date: dt.date) -> pd.DataFrame:
    """Load all revenue rows for a single date."""
    query = select(RevenueMaster).where(RevenueMaster.date == target_date)
    return _read_sql(query)


def load_for_dates(dates: list[dt.date]) -> pd.DataFrame:
    """Load all revenue rows for a list of dates in one query."""
    if not dates:
        return pd.DataFrame()
    query = select(RevenueMaster).where(RevenueMaster.date.in_(dates))
    return _read_sql(query)


def load_for_date_range(start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    """
    Load all revenue rows with date in [start_date, end_date] inclusive.
    This is what powers Month-wise and Year-wise comparison — those modes
    aggregate a whole range of dates into one period rather than comparing
    two single days.
    """
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    query = select(RevenueMaster).where(
        RevenueMaster.date >= start_date, RevenueMaster.date <= end_date
    )
    return _read_sql(query)


# ---------------------------------------------------------------------------
# Airport traffic: save, load, and join against revenue
# ---------------------------------------------------------------------------

def save_traffic_dataframe(df: pd.DataFrame, source_file: str) -> dict:
    """
    Insert a (date, location, terminal, traffic, granularity, period_end)
    DataFrame into airport_traffic. `terminal` and `period_end` are
    optional in the input (default to "" and None respectively);
    `granularity` defaults to "daily" if not present. Duplicate (date,
    location, terminal, granularity) rows are skipped silently, same
    dedup behavior as save_dataframe() for revenue — re-uploading a
    traffic file you've already loaded is a harmless no-op that reports
    how many rows were skipped.
    """
    if df.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": 0}

    required_cols = {"date", "location", "traffic"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Traffic DataFrame is missing required columns: {missing}")

    now_str = dt.datetime.now().isoformat(timespec="seconds")

    # FIX (Bug 8 extension): replace iterrows() with vectorized apply().
    # The per-row logic (NULL-safe terminal, period_end, granularity) is
    # preserved exactly — only the iteration mechanism changes.
    work_t = df.copy()

    # Drop rows with no usable traffic value before building records.
    work_t["_traffic"] = work_t["traffic"].apply(_to_float_or_none)
    work_t = work_t[work_t["_traffic"].notna()].copy()

    if work_t.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    # Use "" rather than NULL for "no terminal breakdown" rows: SQLite's
    # UNIQUE constraint treats every NULL as distinct from every other
    # NULL (by SQL standard), so two NULL-terminal rows for the same
    # (date, location) would NOT be caught as duplicates by INSERT OR
    # IGNORE — silently creating duplicate rows on every re-upload.
    # Empty string is a normal, comparable value, so the constraint
    # works correctly.
    def _norm_terminal(v) -> str:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        s = str(v).strip()
        return "" if s.lower() == "nan" else s

    def _norm_period_end(v) -> object:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return None
        try:
            return _to_date(v).isoformat()
        except Exception:
            return None

    def _norm_granularity(v) -> str:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return "daily"
        return str(v).strip().lower() or "daily"

    work_t["date"]       = work_t["date"].apply(lambda v: _to_date(v).isoformat())
    work_t["location"]   = work_t["location"].astype(str).str.strip()
    work_t["terminal"]   = work_t["terminal"].apply(_norm_terminal) if "terminal" in work_t.columns else ""
    work_t["period_end"] = work_t["period_end"].apply(_norm_period_end) if "period_end" in work_t.columns else None
    work_t["granularity"] = work_t["granularity"].apply(_norm_granularity) if "granularity" in work_t.columns else "daily"
    work_t["traffic"]    = work_t["_traffic"]
    work_t["source_file"]  = source_file
    work_t["uploaded_at"]  = now_str

    records = work_t[
        ["date", "period_end", "granularity", "location", "terminal",
         "traffic", "source_file", "uploaded_at"]
    ].to_dict("records")

    if not records:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    insert_sql = text(
        """
        INSERT OR IGNORE INTO airport_traffic
            (date, period_end, granularity, location, terminal, traffic, source_file, uploaded_at)
        VALUES
            (:date, :period_end, :granularity, :location, :terminal, :traffic, :source_file, :uploaded_at)
        """
    )

    with ENGINE.begin() as conn:
        before = conn.execute(select(func.count(AirportTraffic.id))).scalar() or 0
        conn.execute(insert_sql, records)
        after = conn.execute(select(func.count(AirportTraffic.id))).scalar() or 0

    inserted = after - before
    skipped = len(records) - inserted

    # Same "only the rows this call actually inserted" accuracy fix used
    # for revenue uploads (see save_dataframe): sum traffic for just the
    # (source_file, uploaded_at) pair this call just wrote, not the whole
    # file, so a re-upload where every row is a duplicate logs 0, not a
    # misleading full-file total next to "0 rows saved".
    if inserted > 0:
        with ENGINE.begin() as conn:
            inserted_traffic = conn.execute(
                select(func.coalesce(func.sum(AirportTraffic.traffic), 0.0)).where(
                    AirportTraffic.source_file == source_file,
                    AirportTraffic.uploaded_at == now_str,
                )
            ).scalar() or 0.0
    else:
        inserted_traffic = 0.0

    report_date = _to_date(df["date"].iloc[0]) if "date" in df.columns and not df.empty else None
    _record_upload_history(
        file_name=source_file,
        report_date=report_date,
        row_count=inserted,
        total_revenue=float(inserted_traffic),
        total_pax=0.0,
        upload_type="Traffic",
    )

    upload_type_label = "Traffic"

    # Trigger async GitHub backup after every successful write.
    from . import github_backup as _ghb  # local import avoids circular dependency
    if inserted > 0:
        _ghb.trigger_backup(
            upload_type=upload_type_label,
            source_file=source_file,
            inserted=inserted,
        )
    return {"inserted": inserted, "skipped": skipped, "total_rows": len(df)}


def load_traffic_for_date_range(start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    """Load all airport_traffic rows with date in [start_date, end_date] inclusive."""
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    query = select(AirportTraffic).where(
        AirportTraffic.date >= start_date, AirportTraffic.date <= end_date
    )
    return _read_sql(query)


def load_traffic_all() -> pd.DataFrame:
    """Load the entire airport_traffic table."""
    return _read_sql(select(AirportTraffic))


def get_available_traffic_dates() -> list[dt.date]:
    """Distinct dates present in airport_traffic, sorted ascending."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AirportTraffic.date).distinct().order_by(AirportTraffic.date)
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def get_available_terminals() -> list[str]:
    """Distinct non-empty terminal labels present in airport_traffic, sorted."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AirportTraffic.terminal).distinct().where(
                AirportTraffic.terminal.is_not(None), AirportTraffic.terminal != ""
            )
        ).all()
        return sorted(r[0] for r in rows if r[0])
    finally:
        session.close()


def get_traffic_total_for_range(
    start_date: dt.date, end_date: dt.date, location: Optional[str] = None
) -> pd.DataFrame:
    """
    Return the best-available traffic total for [start_date, end_date]
    inclusive, grouped by location (and terminal, kept separate) — this is
    the function that actually handles daily vs monthly granularity
    correctly, which a naive per-row join cannot do.

    Strategy per location:
      1. If daily rows fully cover every date in the range, sum the daily
         rows — this is the most accurate option.
      2. Otherwise, fall back to monthly rows whose [date, period_end]
         span overlaps the requested range. A monthly row is prorated by
         the fraction of its days that fall inside the requested range
         (e.g. requesting just the first half of a month against a
         monthly total prorates that total roughly in half) — this is an
         approximation flagged via the `is_estimated` column, since a
         monthly total has no real daily shape to draw from.
      3. If both exist for an overlapping period, daily takes precedence
         for the days it actually covers, and monthly only fills the
         remaining gap days that its own period actually spans.
      4. Any requested date with neither a daily row nor a monthly row
         covering it (a genuine data gap, e.g. a month nobody uploaded
         traffic for at all) is counted in `missing_days` and excluded
         from the total rather than silently treated as zero or silently
         papered over by an unrelated month's monthly figure.

    Returns columns: location, terminal, traffic, is_estimated (bool, True
    if any part of that location+terminal's total came from a prorated
    monthly figure rather than real daily data), missing_days (int, count
    of requested dates with no data at all — callers should warn the user
    when this is > 0, since the returned `traffic` is understated by
    however much those missing days would have contributed).
    """
    # Use the overlap-aware query unconditionally, not just as a fallback
    # for an empty result: a monthly row's own `date` (the 1st of its
    # month) can fall *before* start_date while its `period_end` still
    # overlaps the requested range (e.g. a row dated 2024-05-01 with
    # period_end 2024-05-31 must still be included when start_date is
    # 2024-05-27) — load_traffic_for_date_range's simple date>=/date<=
    # filter would silently exclude that row even though plenty of OTHER
    # rows in the range make the overall query result non-empty, which is
    # exactly the case an empty-result-only fallback would miss.
    all_traffic = _load_traffic_overlapping_range(start_date, end_date, location)

    if all_traffic.empty:
        return pd.DataFrame(columns=["location", "terminal", "traffic", "is_estimated"])

    results = []
    total_days = (end_date - start_date).days + 1

    for (loc, term), group in all_traffic.groupby(["location", "terminal"], dropna=False):
        daily_rows = group[group["granularity"] == "daily"]
        monthly_rows = group[group["granularity"] == "monthly"]

        daily_dates_covered = set(daily_rows["date"])
        all_dates_in_range = {start_date + dt.timedelta(days=i) for i in range(total_days)}
        missing_dates = all_dates_in_range - daily_dates_covered

        traffic_total = daily_rows["traffic"].sum() if not daily_rows.empty else 0.0
        is_estimated = False
        unfilled_dates = set(missing_dates)

        if missing_dates and not monthly_rows.empty:
            # Fill gaps using prorated monthly figures, one missing date
            # at a time, attributing each missing date to whichever
            # monthly row's [date, period_end] span contains it. Only
            # dates that actually fall inside some monthly row's period
            # are removed from `unfilled_dates` — a monthly row covering
            # an unrelated month (e.g. March, when the gap is in April)
            # must NOT cause those April dates to be treated as resolved.
            for _, mrow in monthly_rows.iterrows():
                period_start = mrow["date"]
                period_end = mrow["period_end"] if pd.notna(mrow["period_end"]) else period_start
                days_in_period = (period_end - period_start).days + 1
                if days_in_period <= 0:
                    continue
                per_day_estimate = mrow["traffic"] / days_in_period
                covered_by_this_row = {d for d in missing_dates if period_start <= d <= period_end}
                if not covered_by_this_row:
                    continue
                is_estimated = True
                traffic_total += per_day_estimate * len(covered_by_this_row)
                unfilled_dates -= covered_by_this_row

        results.append(
            {
                "location": loc,
                "terminal": term,
                "traffic": traffic_total,
                "is_estimated": is_estimated,
                "missing_days": len(unfilled_dates),
            }
        )

    return pd.DataFrame(results)


def _load_traffic_overlapping_range(
    start_date: dt.date, end_date: dt.date, location: Optional[str] = None
) -> pd.DataFrame:
    """
    Wider traffic query that also catches monthly rows whose period
    overlaps [start_date, end_date] even if the monthly row's own `date`
    (the 1st of its month) falls before start_date — a simple date>=/date<=
    filter on `date` alone would miss e.g. a monthly row dated 2026-06-01
    / period_end 2026-06-30 when asked for just 2026-06-15 onward, since
    2026-06-01 < 2026-06-15.

    Daily rows (period_end IS NULL) are bounded normally on both sides
    (date >= start_date AND date <= end_date); monthly rows (period_end
    IS NOT NULL) use [date, period_end] as their span and are included if
    that span overlaps [start_date, end_date] at all.
    """
    daily_condition = sql_and(
        AirportTraffic.period_end.is_(None),
        AirportTraffic.date >= start_date,
        AirportTraffic.date <= end_date,
    )
    monthly_condition = sql_and(
        AirportTraffic.period_end.is_not(None),
        AirportTraffic.date <= end_date,
        AirportTraffic.period_end >= start_date,
    )
    conditions = [sql_or(daily_condition, monthly_condition)]
    if location is not None:
        conditions.append(AirportTraffic.location == location)
    query = select(AirportTraffic).where(*conditions)
    return _read_sql(query)


def join_revenue_with_traffic(revenue_df: pd.DataFrame, traffic_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Aggregate a revenue DataFrame (potentially many outlet/date rows) down
    to one row per location — Revenue and PAX summed across every
    outlet/date in `revenue_df` — with a single, range-correct Traffic
    total attached for that location (see get_traffic_total_for_range).

    IMPORTANT — this deliberately returns one row per location, NOT one
    row per original revenue_df row. Traffic is airport-wide, not
    outlet- or day-specific, so there is exactly one correct Traffic
    figure per location for the whole date range revenue_df spans.
    Earlier this function stamped that same range-total onto every
    outlet/date row instead, on the assumption a caller would dedupe it
    back down before summing (as revenue_analysis.
    location_level_summary_with_traffic does for genuine per-date traffic
    data) — but a dedupe keyed on (date, location) keeps one row per
    distinct *date*, not per location, so it still summed the same
    range-total once per day in the range, multiplying Traffic (and
    therefore silently shrinking Penetration % and SPP) by however many
    days the comparison period covered. Returning exactly one row per
    location makes that miscount impossible.

    Uses get_traffic_total_for_range() internally, so daily vs monthly
    granularity is handled correctly (see that function's docstring) — if
    `traffic_df` is explicitly given instead (e.g. by a caller that
    already loaded a specific slice), it's used as-is via a simple sum,
    skipping the daily/monthly reconciliation logic, so prefer leaving
    `traffic_df` as None unless you know what you're passing.

    Returns columns: location, date (a representative date, kept only so
    this shape matches what revenue_analysis.
    location_level_summary_with_traffic expects), revenue, pax, traffic,
    traffic_is_estimated, traffic_missing_days. Rows whose location has
    no matching traffic at all get NaN traffic, not 0 — this matters for
    has_traffic_data() and the penetration/SPP calculations downstream,
    which need to distinguish "no traffic data yet" from "traffic was
    genuinely zero that day".
    """
    if revenue_df is None or revenue_df.empty:
        return revenue_df

    work = revenue_df.copy()
    if "traffic" in work.columns:
        work = work.drop(columns=["traffic"])

    dates = pd.to_datetime(work["date"]).dt.date
    start_date, end_date = dates.min(), dates.max()

    location_totals = work.groupby("location", as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )
    location_totals["date"] = start_date

    if traffic_df is None:
        traffic_totals = get_traffic_total_for_range(start_date, end_date)
        if traffic_totals.empty:
            location_totals["traffic"] = pd.NA
            location_totals["traffic_is_estimated"] = False
            location_totals["traffic_missing_days"] = 0
            return location_totals
        traffic_by_location = traffic_totals.groupby("location", as_index=False).agg(
            traffic=("traffic", "sum"),
            traffic_is_estimated=("is_estimated", "any"),
            traffic_missing_days=("missing_days", "sum"),
        )
    else:
        if traffic_df.empty:
            location_totals["traffic"] = pd.NA
            location_totals["traffic_is_estimated"] = False
            location_totals["traffic_missing_days"] = 0
            return location_totals
        traffic_by_location = traffic_df.groupby("location", as_index=False)["traffic"].sum()
        traffic_by_location["traffic_is_estimated"] = False
        traffic_by_location["traffic_missing_days"] = 0

    merged = location_totals.merge(traffic_by_location, on="location", how="left")
    return merged


def join_revenue_with_traffic_by_outlet(
    revenue_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Outlet-level traffic join: attaches the CORRECT terminal-specific traffic
    figure to each outlet row based on terminal_mapping.get_terminal_for_outlet.

    Unlike join_revenue_with_traffic (which returns one row per location with
    the whole-airport total), this function returns one row per
    (outlet, location) with the traffic figure for that outlet's specific
    terminal pool — e.g. T3 Dom Dep for T3D49, T3 Int Dep for INL5&6,
    T3 Arr (= T3 Dom Arr + T3 Int Arr) for LA01/LA12/LA22.

    Special terminal sentinels handled:
      "T3 Arr" → sums T3 Dom Arr + T3 Int Arr from airport_traffic
      "T3"     → sums ALL T3 rows (Dom Dep + Int Dep + Dom Arr + Int Arr)
      ""       → NaN traffic (airport-wide services with no single pool)
      "Unmapped" → NaN traffic

    Returns columns: outlet, location, revenue, pax, traffic,
    traffic_is_estimated, traffic_missing_days.
    """
    from . import terminal_mapping as tm

    if revenue_df is None or revenue_df.empty:
        return pd.DataFrame(columns=["outlet", "location", "revenue", "pax",
                                      "traffic", "traffic_is_estimated", "traffic_missing_days"])

    work = revenue_df.copy()
    dates = pd.to_datetime(work["date"]).dt.date
    start_date, end_date = dates.min(), dates.max()

    # Get all terminal-level traffic for this date range
    traffic_totals = get_traffic_total_for_range(start_date, end_date)
    # Build lookup: (location, terminal) → traffic info
    traffic_lookup: dict[tuple, dict] = {}
    for _, row in traffic_totals.iterrows():
        key = (row["location"], str(row.get("terminal", "")))
        traffic_lookup[key] = {
            "traffic": row["traffic"],
            "is_estimated": row.get("is_estimated", False),
            "missing_days": row.get("missing_days", 0),
        }

    # Aggregate revenue/pax per (outlet, location)
    outlet_agg = work.groupby(["outlet", "location"], as_index=False).agg(
        revenue=("revenue", "sum"), pax=("pax", "sum")
    )

    def _get_traffic(outlet: str, location: str) -> tuple:
        """Return (traffic, is_estimated, missing_days) for this outlet."""
        terminal = tm.get_terminal_for_outlet(outlet, location)
        if not terminal or terminal in ("", "Unmapped"):
            return (float("nan"), False, 0)

        def _sum_pools(pools: list[str]) -> tuple:
            """Sum multiple terminal pools; fall back to generic labels if new labels absent."""
            total = 0.0
            estimated = False
            missing = 0
            found_any = False
            for p in pools:
                info = traffic_lookup.get((location, p), {})
                if not info or not info.get("traffic"):
                    # Fallback: old data stored without Dep/Arr split
                    fallbacks = {
                        "T1 Dep": ["T1"], "T1 Arr": ["T1"],
                        "T2 Dep": ["T2"], "T2 Arr": ["T2"],
                        "T3 Dom Dep": ["T3"], "T3 Dom Arr": ["T3"],
                        "T3 Int Dep": ["T3"], "T3 Int Arr": ["T3"],
                    }
                    for fb in fallbacks.get(p, []):
                        info = traffic_lookup.get((location, fb), {})
                        if info and info.get("traffic"):
                            break
                if info and info.get("traffic"):
                    total += info["traffic"]
                    estimated = estimated or info.get("is_estimated", False)
                    missing = max(missing, info.get("missing_days", 0))
                    found_any = True
            if not found_any or total == 0:
                return (float("nan"), False, 0)
            return (total, estimated, missing)

        # --- Composite sentinels ---
        if terminal == "T3 Arr":
            # LA outlets: T3 Dom Arr + T3 Int Arr (Total Arrival T3)
            return _sum_pools(["T3 Dom Arr", "T3 Int Arr"])

        if terminal == "All Dep":
            # Enwrap (Baggage Wrapping): all terminal departures
            return _sum_pools(["T1 Dep", "T2 Dep", "T3 Dom Dep", "T3 Int Dep"])

        if terminal == "All":
            # M&G / Atithya: entire airport
            # Delhi: all 8 terminal pools; HYD/GOA: Domestic + International
            if location in ("Hyderabad", "Goa"):
                return _sum_pools(["Domestic", "International"])
            return _sum_pools(["T1 Dep", "T1 Arr", "T2 Dep", "T2 Arr",
                                "T3 Dom Dep", "T3 Dom Arr", "T3 Int Dep", "T3 Int Arr"])

        if terminal == "T3 Total":
            # Buggy: all 4 T3 pools (Dom Dep + Int Dep + Dom Arr + Int Arr)
            return _sum_pools(["T3 Dom Dep", "T3 Int Dep", "T3 Dom Arr", "T3 Int Arr"])

        if terminal == "T3 Dom+Int Dep":
            # RL T3 Departure: serves T3 departing passengers (both Dom & Int sides)
            return _sum_pools(["T3 Dom Dep", "T3 Int Dep"])

        if terminal == "Porter Pool":
            # Porter: T1 Dep+Arr + T2 Dep+Arr + T3 Dom Dep + T3 Int Dep
            # Excludes T3 Int Arr (no Porter in T3 International Arrivals area)
            return _sum_pools(["T1 Dep", "T1 Arr", "T2 Dep", "T2 Arr",
                                "T3 Dom Dep", "T3 Int Dep"])

        if terminal in ("T3", "T3+"):
            # Generic T3 fallback (old data without Dep/Arr split)
            return _sum_pools(["T3 Dom Dep", "T3 Dom Arr", "T3 Int Dep", "T3 Int Arr"])

        # Hyderabad / Goa terminal labels
        if terminal == "Domestic":
            return _sum_pools(["Domestic"])

        if terminal == "International":
            return _sum_pools(["International"])

        if terminal == "Main Terminal":
            # Whole-airport fallback for HYD/GOA outlets without specific mapping
            return _sum_pools(["Domestic", "International", "Main Terminal"])

        # --- Single terminal pool ---
        info = traffic_lookup.get((location, terminal), {})
        if not info or not info.get("traffic"):
            # Fallback for old data without Dep/Arr split
            generic = {
                "T1 Dep": "T1", "T1 Arr": "T1",
                "T2 Dep": "T2", "T2 Arr": "T2",
                "T3 Dom Dep": "T3", "T3 Int Dep": "T3",
                "T3 Dom Arr": "T3", "T3 Int Arr": "T3",
            }
            if terminal in generic:
                info = traffic_lookup.get((location, generic[terminal]), {})
            if not info or not info.get("traffic"):
                return (float("nan"), False, 0)
        return (info["traffic"], info.get("is_estimated", False), info.get("missing_days", 0))

    # FIX (Bug 2): previously _get_traffic() was called three separate times
    # per row — once for each of traffic, traffic_is_estimated, and
    # traffic_missing_days. Each call re-runs the full terminal lookup and
    # pool-summing logic. With potentially hundreds of outlets and
    # chained fallback look-ups inside _get_traffic, the triple-call cost
    # is non-trivial. Fix: call once per row, store the tuple, then
    # extract all three values from the stored result.
    traffic_results = outlet_agg.apply(
        lambda r: _get_traffic(r["outlet"], r["location"]), axis=1
    )
    outlet_agg["traffic"]              = traffic_results.apply(lambda t: t[0])
    outlet_agg["traffic_is_estimated"] = traffic_results.apply(lambda t: t[1])
    outlet_agg["traffic_missing_days"] = traffic_results.apply(lambda t: t[2])
    return outlet_agg


# ---------------------------------------------------------------------------
# AOP targets: save, load, and join against revenue
# ---------------------------------------------------------------------------

def save_aop_targets(df: pd.DataFrame, source_file: str) -> dict:
    """
    Insert a (location, segment, business_unit, outlet, year, month, aop)
    DataFrame into aop_target. Duplicate (location, outlet, year, month)
    rows are skipped silently — re-uploading an AOP file you've already
    loaded, or re-uploading a corrected version with the same keys, is a
    safe no-op for the rows that already match (use reset/delete first if
    you need to genuinely replace a target value).
    """
    if df.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": 0}

    required_cols = {"location", "outlet", "year", "month", "aop"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"AOP DataFrame is missing required columns: {missing}")

    now_str = dt.datetime.now().isoformat(timespec="seconds")

    # FIX (Bug 8 extension): replace iterrows() with vectorized apply().
    work_a = df.copy()
    work_a["_aop"] = work_a["aop"].apply(_to_float_or_none)
    work_a = work_a[work_a["_aop"].notna()].copy()

    if work_a.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    work_a["location"] = work_a["location"].astype(str).str.strip()
    work_a["outlet"]   = work_a["outlet"].astype(str).str.strip()
    work_a["segment"]  = (
        work_a["segment"].astype(str).str.strip()
        if "segment" in work_a.columns
        else "EHPL"
    )
    work_a["business_unit"] = work_a.apply(
        lambda r: str(r["business_unit"]).strip()
        if "business_unit" in r.index and pd.notna(r.get("business_unit"))
        else None,
        axis=1,
    )
    work_a["year"]  = work_a["year"].apply(lambda v: int(v))
    work_a["month"] = work_a["month"].apply(lambda v: int(v))
    work_a["aop"]   = work_a["_aop"]
    work_a["source_file"]  = source_file
    work_a["uploaded_at"]  = now_str

    records = work_a[
        ["location", "segment", "business_unit", "outlet",
         "year", "month", "aop", "source_file", "uploaded_at"]
    ].to_dict("records")

    if not records:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    insert_sql = text(
        """
        INSERT OR IGNORE INTO aop_target
            (location, segment, business_unit, outlet, year, month, aop, source_file, uploaded_at)
        VALUES
            (:location, :segment, :business_unit, :outlet, :year, :month, :aop, :source_file, :uploaded_at)
        """
    )

    with ENGINE.begin() as conn:
        before = conn.execute(select(func.count(AOPTarget.id))).scalar() or 0
        conn.execute(insert_sql, records)
        after = conn.execute(select(func.count(AOPTarget.id))).scalar() or 0

    inserted = after - before
    skipped = len(records) - inserted

    if inserted > 0:
        with ENGINE.begin() as conn:
            inserted_aop = conn.execute(
                select(func.coalesce(func.sum(AOPTarget.aop), 0.0)).where(
                    AOPTarget.source_file == source_file,
                    AOPTarget.uploaded_at == now_str,
                )
            ).scalar() or 0.0
    else:
        inserted_aop = 0.0

    first_row = df.iloc[0]
    report_date = (
        dt.date(int(first_row["year"]), int(first_row["month"]), 1)
        if pd.notna(first_row.get("year")) and pd.notna(first_row.get("month"))
        else None
    )
    _record_upload_history(
        file_name=source_file,
        report_date=report_date,
        row_count=inserted,
        total_revenue=float(inserted_aop),
        total_pax=0.0,
        upload_type="AOP",
    )

    upload_type_label = "AOP"

    # Trigger async GitHub backup after every successful write.
    from . import github_backup as _ghb  # local import avoids circular dependency
    if inserted > 0:
        _ghb.trigger_backup(
            upload_type=upload_type_label,
            source_file=source_file,
            inserted=inserted,
        )
    return {"inserted": inserted, "skipped": skipped, "total_rows": len(df)}


def load_aop_targets_for_period(year: int, month: int) -> pd.DataFrame:
    """Load all AOP target rows for a single (year, month)."""
    query = select(AOPTarget).where(AOPTarget.year == year, AOPTarget.month == month)
    return _read_sql(query)


def load_aop_targets_for_range(start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    """
    Load all AOP target rows for every (year, month) touched by
    [start_date, end_date] inclusive — e.g. a range spanning April 5 to
    May 10 pulls both April's and May's targets.
    """
    months = set()
    cursor = dt.date(start_date.year, start_date.month, 1)
    end_marker = dt.date(end_date.year, end_date.month, 1)
    while cursor <= end_marker:
        months.add((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = dt.date(cursor.year + 1, 1, 1)
        else:
            cursor = dt.date(cursor.year, cursor.month + 1, 1)

    if not months:
        return pd.DataFrame()

    conditions = sql_or(*[
        (AOPTarget.year == y) & (AOPTarget.month == m) for y, m in months
    ])
    query = select(AOPTarget).where(conditions)
    return _read_sql(query)


def get_available_aop_year_months() -> list[tuple[int, int]]:
    """Distinct (year, month) pairs present in aop_target, sorted ascending."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AOPTarget.year, AOPTarget.month).distinct()
        ).all()
        return sorted({(r[0], r[1]) for r in rows})
    finally:
        session.close()


def save_aop_targets_daily(df: pd.DataFrame, source_file: str) -> dict:
    """
    Insert a (location, date, aop) DataFrame into aop_target_daily — the
    daily-total-per-location AOP format (no outlet/segment breakdown).
    Duplicate (location, date) rows are skipped silently, same dedup
    behavior as every other save_* function in this module.
    """
    if df.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": 0}

    required_cols = {"location", "date", "aop"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Daily AOP DataFrame is missing required columns: {missing}")

    now_str = dt.datetime.now().isoformat(timespec="seconds")

    # FIX (Bug 8 extension): replace iterrows() with vectorized apply().
    work_d = df.copy()
    work_d["_aop"] = work_d["aop"].apply(_to_float_or_none)
    work_d = work_d[work_d["_aop"].notna()].copy()

    if work_d.empty:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    work_d["location"] = work_d["location"].astype(str).str.strip()
    work_d["date"]     = work_d["date"].apply(lambda v: _to_date(v).isoformat())
    work_d["aop"]      = work_d["_aop"]
    work_d["source_file"]  = source_file
    work_d["uploaded_at"]  = now_str

    records = work_d[
        ["location", "date", "aop", "source_file", "uploaded_at"]
    ].to_dict("records")

    if not records:
        return {"inserted": 0, "skipped": 0, "total_rows": len(df)}

    insert_sql = text(
        """
        INSERT OR IGNORE INTO aop_target_daily (location, date, aop, source_file, uploaded_at)
        VALUES (:location, :date, :aop, :source_file, :uploaded_at)
        """
    )

    with ENGINE.begin() as conn:
        before = conn.execute(select(func.count(AOPTargetDaily.id))).scalar() or 0
        conn.execute(insert_sql, records)
        after = conn.execute(select(func.count(AOPTargetDaily.id))).scalar() or 0

    inserted = after - before
    skipped = len(records) - inserted

    if inserted > 0:
        with ENGINE.begin() as conn:
            inserted_aop = conn.execute(
                select(func.coalesce(func.sum(AOPTargetDaily.aop), 0.0)).where(
                    AOPTargetDaily.source_file == source_file,
                    AOPTargetDaily.uploaded_at == now_str,
                )
            ).scalar() or 0.0
    else:
        inserted_aop = 0.0

    report_date = _to_date(df["date"].iloc[0]) if "date" in df.columns and not df.empty else None
    _record_upload_history(
        file_name=source_file,
        report_date=report_date,
        row_count=inserted,
        total_revenue=float(inserted_aop),
        total_pax=0.0,
        upload_type="AOP",
    )

    upload_type_label = "AOP Daily"

    # Trigger async GitHub backup after every successful write.
    from . import github_backup as _ghb  # local import avoids circular dependency
    if inserted > 0:
        _ghb.trigger_backup(
            upload_type=upload_type_label,
            source_file=source_file,
            inserted=inserted,
        )
    return {"inserted": inserted, "skipped": skipped, "total_rows": len(df)}


def load_aop_targets_daily_for_range(start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
    """Load all aop_target_daily rows with date in [start_date, end_date] inclusive."""
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    query = select(AOPTargetDaily).where(
        AOPTargetDaily.date >= start_date, AOPTargetDaily.date <= end_date
    )
    return _read_sql(query)


def get_available_aop_daily_dates() -> list[dt.date]:
    """Distinct dates present in aop_target_daily, sorted ascending."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AOPTargetDaily.date).distinct().order_by(AOPTargetDaily.date)
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def get_aop_target_for_range(start_date: dt.date, end_date: dt.date, location: Optional[str] = None) -> dict:
    """
    Return the best-available total AOP target for [start_date, end_date]
    inclusive, per location, preferring the daily-total source
    (aop_target_daily) when it covers the range, and falling back to the
    monthly per-outlet source (aop_target, summed across outlets and
    prorated the same way join_revenue_with_aop already does) for any
    part of the range the daily source doesn't cover.

    Returns {location: {"aop_target": float, "is_estimated": bool,
    "missing_days": int}}, mirroring the same shape and meaning as
    get_traffic_total_for_range — a missing_days > 0 means part of the
    range has no AOP target from either source, and the caller should
    treat the returned total as understated rather than complete.
    """
    daily_df = load_aop_targets_daily_for_range(start_date, end_date)
    if location is not None and not daily_df.empty:
        daily_df = daily_df[daily_df["location"] == location]

    total_days = (end_date - start_date).days + 1
    all_dates_in_range = {start_date + dt.timedelta(days=i) for i in range(total_days)}

    locations_seen = set(daily_df["location"].unique()) if not daily_df.empty else set()
    if location is not None:
        locations_seen.add(location)

    monthly_df = load_aop_targets_for_range(start_date, end_date)
    if location is not None and not monthly_df.empty:
        monthly_df = monthly_df[monthly_df["location"] == location]
    if not monthly_df.empty:
        locations_seen |= set(monthly_df["location"].unique())

    results = {}
    for loc in locations_seen:
        loc_daily = daily_df[daily_df["location"] == loc] if not daily_df.empty else daily_df
        dates_covered_daily = set(loc_daily["date"]) if not loc_daily.empty else set()
        total = float(loc_daily["aop"].sum()) if not loc_daily.empty else 0.0
        missing_dates = all_dates_in_range - dates_covered_daily
        is_estimated = False

        if missing_dates:
            loc_monthly = monthly_df[monthly_df["location"] == loc] if not monthly_df.empty else monthly_df
            if not loc_monthly.empty:
                # Sum across outlets per (year, month), then prorate by
                # the fraction of that month's days actually missing from
                # the daily source and still inside the requested range.
                monthly_by_ym = loc_monthly.groupby(["year", "month"])["aop"].sum()
                for (y, m), month_total in monthly_by_ym.items():
                    days_in_month = calendar.monthrange(int(y), int(m))[1]
                    per_day = month_total / days_in_month if days_in_month else 0.0
                    covered_by_this_month = {
                        d for d in missing_dates if d.year == y and d.month == m
                    }
                    if covered_by_this_month:
                        is_estimated = True
                        total += per_day * len(covered_by_this_month)
                        missing_dates -= covered_by_this_month

        results[loc] = {
            "aop_target": total,
            "is_estimated": is_estimated,
            "missing_days": len(missing_dates),
        }

    return results


def join_revenue_with_aop(revenue_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build one row per (location, outlet, year, month) actually present in
    revenue_df, with that outlet-month's actual revenue (summed over
    exactly the dates revenue_df contains) alongside its AOP target,
    prorated to a daily rate within the month and multiplied by however
    many days of that month are actually present in `revenue_df`, so a
    partial-month revenue range (e.g. just the first 10 days) is compared
    against a proportional slice of that month's AOP target rather than
    the whole month's target.

    IMPORTANT — this deliberately returns one row per outlet-month, NOT
    one row per original revenue_df row. Earlier this function left-
    joined the (already-prorated) target onto every individual date-row
    for that outlet, on the assumption a caller would pull a single value
    per outlet-month back out. But callers (e.g. revenue_analysis.
    aop_variance) instead sum the returned column directly — and summing
    the *same* prorated total once per date-row silently multiplied every
    outlet's AOP target by however many days of that month were present
    in revenue_df. Returning exactly one row per outlet-month makes that
    miscount impossible: summing `revenue` or `aop_target` across the
    rows this function returns always yields the correct total, with
    nothing left to double-count.

    Returns columns: location, outlet, revenue (actual, summed over the
    dates present), aop_target. Rows with no matching AOP target (a new
    outlet, an out-of-scope location, or a period with no AOP data
    loaded) get NaN aop_target, not 0 — same "missing vs genuinely zero"
    distinction used throughout this module.

    This is independent of any pre-existing `aop` column already on
    individual revenue_master rows from a historical Excel import — that
    legacy per-row AOP figure lives on `revenue_df` itself and is not
    read or altered here.
    """
    if revenue_df is None or revenue_df.empty:
        return revenue_df

    work = revenue_df.copy()
    work["_year"] = pd.to_datetime(work["date"]).dt.year
    work["_month"] = pd.to_datetime(work["date"]).dt.month

    dates = pd.to_datetime(work["date"]).dt.date
    start_date, end_date = dates.min(), dates.max()

    # One row per outlet-month, with the actual revenue for exactly the
    # dates present, and how many distinct dates that is (used below to
    # prorate the AOP target by the same fraction of the month).
    group_totals = work.groupby(
        ["location", "outlet", "_year", "_month"], as_index=False
    ).agg(revenue=("revenue", "sum"), _days_present=("date", "nunique"))

    # ── Try daily-total AOP first (aop_target_daily table) ─────────────────
    # The daily table holds location-level totals per day (no outlet
    # breakdown). When present, sum the daily values that fall within the
    # revenue_df date range for each location, then broadcast the location
    # total across all outlets proportionally by outlet revenue share —
    # this is the only way to attribute a location-level daily AOP to
    # individual outlets without a per-outlet AOP workbook.
    daily_aop = load_aop_targets_daily_for_range(start_date, end_date)
    if not daily_aop.empty:
        # Sum daily AOP per location over the date range
        daily_by_loc = daily_aop.groupby("location", as_index=False)["aop"].sum()
        daily_by_loc = daily_by_loc.rename(columns={"aop": "_loc_aop_total"})

        # Compute each outlet's revenue share within its location
        loc_rev = group_totals.groupby("location", as_index=False)["revenue"].sum()
        loc_rev = loc_rev.rename(columns={"revenue": "_loc_rev_total"})
        merged = group_totals.merge(daily_by_loc, on="location", how="left")
        merged = merged.merge(loc_rev, on="location", how="left")

        # Outlet AOP = location AOP × (outlet revenue / location revenue).
        #
        # FIX (Bug 1): when a location's total revenue is zero or negative
        # (e.g. a refund-heavy period where returns net out all positive
        # sales), the original code returned None for _outlet_share which
        # caused aop_target to become pd.NA — hiding every AOP target for
        # the affected outlets and making AOP variance pages blank for
        # those periods.
        #
        # Fallback: count how many outlets exist in the location and
        # distribute the AOP equally (1 / outlet_count). This matches the
        # docstring's stated intention ("distribute equally") and ensures
        # AOP targets always appear, even in refund-heavy periods.
        # Normal revenue-proportional allocation is unchanged.

        # Count outlets per location so we can compute the equal share.
        outlet_counts_per_loc = (
            merged.groupby("location")["outlet"].transform("count")
        )
        merged["_outlet_count"] = outlet_counts_per_loc.values

        def _compute_outlet_share(r) -> object:
            """Return this outlet's share of its location's AOP total."""
            loc_total = r.get("_loc_rev_total")
            if pd.notna(loc_total) and loc_total > 0:
                # Normal case: proportional to this outlet's revenue.
                return r["revenue"] / loc_total
            # Fallback for zero/negative location revenue: equal share.
            count = r.get("_outlet_count")
            if count and count > 0:
                return 1.0 / count
            return None

        merged["_outlet_share"] = merged.apply(_compute_outlet_share, axis=1)
        merged["aop_target"] = merged.apply(
            lambda r: r["_loc_aop_total"] * r["_outlet_share"]
            if pd.notna(r.get("_loc_aop_total")) and pd.notna(r.get("_outlet_share"))
            else pd.NA,
            axis=1,
        )
        merged = merged.drop(columns=[
            "_year", "_month", "_days_present",
            "_loc_aop_total", "_loc_rev_total", "_outlet_share", "_outlet_count",
        ])
        return merged

    # ── Fall back to monthly per-outlet AOP (aop_target table) ──────────────
    aop_targets = load_aop_targets_for_range(start_date, end_date)
    if aop_targets.empty:
        group_totals["aop_target"] = pd.NA
        return group_totals.drop(columns=["_year", "_month", "_days_present"])

    aop_targets = aop_targets.rename(columns={"year": "_year", "month": "_month"})
    merged = group_totals.merge(
        aop_targets[["location", "outlet", "_year", "_month", "aop"]],
        on=["location", "outlet", "_year", "_month"],
        how="left",
    )

    merged["_days_in_month"] = merged.apply(
        lambda r: calendar.monthrange(int(r["_year"]), int(r["_month"]))[1], axis=1
    )
    merged["aop_target"] = merged["aop"] * (
        merged["_days_present"] / merged["_days_in_month"]
    )

    merged = merged.drop(columns=["_year", "_month", "_days_present", "_days_in_month", "aop"])
    return merged


def get_available_year_months() -> list[tuple[int, int]]:
    """
    Distinct (year, month) pairs present in the database, sorted ascending.
    Used to populate the Month-wise comparison dropdowns with only months
    that actually have data, rather than every month since year 1.
    """
    dates = get_available_dates()
    pairs = sorted({(d.year, d.month) for d in dates})
    return pairs


def get_available_years() -> list[int]:
    """Distinct years present in the database, sorted ascending."""
    dates = get_available_dates()
    return sorted({d.year for d in dates})


def get_available_week_starts() -> list[dt.date]:
    """
    Distinct ISO week-start (Monday) dates that have at least one day of
    data in the database, sorted ascending. Used to populate the Week-wise
    comparison dropdown with only weeks that actually have data — a week
    appears here even if only one of its seven days was uploaded, since
    that's still a usable (if partial) week for comparison.
    """
    dates = get_available_dates()
    mondays = {d - dt.timedelta(days=d.weekday()) for d in dates}
    return sorted(mondays)


def get_available_dates() -> list[dt.date]:
    """All distinct dates present in the database, sorted ascending."""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(RevenueMaster.date).distinct().order_by(RevenueMaster.date)
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def get_nearest_date(target: dt.date, available: Optional[list[dt.date]] = None) -> Optional[dt.date]:
    """Return the date in the DB closest to (but not after) `target`, or None."""
    available = available if available is not None else get_available_dates()
    candidates = [d for d in available if d <= target]
    if not candidates:
        return None
    return max(candidates)


def find_comparison_dates(current: dt.date) -> dict:
    """
    Find the best-match yesterday / last-month / last-year dates that
    actually exist in the database, relative to `current`.
    """
    available = get_available_dates()
    avail_set = set(available)

    yesterday = current - dt.timedelta(days=1)
    last_month = _safe_month_shift(current, -1)
    last_year = _safe_month_shift(current, -12)

    return {
        "yesterday": yesterday if yesterday in avail_set else get_nearest_date(yesterday, available),
        "last_month": last_month if last_month in avail_set else get_nearest_date(last_month, available),
        "last_year": last_year if last_year in avail_set else get_nearest_date(last_year, available),
    }


# FIX (Bug 5): _safe_month_shift was duplicated here and in revenue_analysis.py.
# The canonical implementation now lives in modules/date_utils.py and is
# imported at the top of this file as `_safe_month_shift`.  The local
# definition is removed to eliminate the duplicate.


def get_upload_history() -> pd.DataFrame:
    """All upload history records, most recent first."""
    query = select(UploadHistory).order_by(UploadHistory.id.desc())
    return _read_sql(query)


def get_dates_summary() -> pd.DataFrame:
    """One row per date with total revenue, total PAX, and outlet count."""
    query = (
        select(
            RevenueMaster.date,
            func.sum(RevenueMaster.revenue).label("total_revenue"),
            func.sum(RevenueMaster.pax).label("total_pax"),
            func.count(RevenueMaster.outlet.distinct()).label("outlets"),
        )
        .group_by(RevenueMaster.date)
        .order_by(RevenueMaster.date.desc())
    )
    return _read_sql(query)


def get_db_stats() -> dict:
    """Summary stats for the Database Management section."""
    session = SessionLocal()
    try:
        total_rows = session.execute(select(func.count(RevenueMaster.id))).scalar() or 0
        min_date = session.execute(select(func.min(RevenueMaster.date))).scalar()
        max_date = session.execute(select(func.max(RevenueMaster.date))).scalar()
        distinct_dates = session.execute(
            select(func.count(RevenueMaster.date.distinct()))
        ).scalar() or 0
        return {
            "total_rows": total_rows,
            "min_date": min_date,
            "max_date": max_date,
            "distinct_dates": distinct_dates,
        }
    finally:
        session.close()
