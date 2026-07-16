# Encalm Group — Revenue Analytics

A Streamlit web app for analyzing Encalm Group's airport business revenue
(lounges, spa, Atithya, Encalm Eats, Encalm Sky Plates, and subsidiaries
across Delhi, Hyderabad, and Goa). Upload daily PDF or Excel revenue
reports, compare any historical periods, track AOP achievement, and
generate one-click management summaries — all backed by a permanent SQLite
database so nothing is lost between sessions or page navigation.

## Features

- **Universal document processing — reports in ANY layout** — files no longer have to match a predefined format. Uploads that don't match a known layout (and all CSV/TSV/TXT files) go through automatic schema detection: the app locates the real table inside the document (skipping title rows, handling multi-row and merged-cell headers), identifies which column is Date / Location / Segment / Outlet / PAX / Revenue using both header synonyms ("Txn Dt", "Airport", "Business Line", "Guests", "Sales (INR)", …) and the actual content of the values (a column full of dates is Date whatever its header says; values like DEL/HYD/GOI are Locations), melts wide layouts (dates-as-columns or locations-as-columns) into the standard long form, and standardizes everything — Indian day-first dates, ₹/Rs./comma-formatted amounts, parenthesised negatives, "Rs. in Lakhs" unit declarations, airport-code location aliases. Missing fields are recovered from context where safe (report date from the title or file name, location from sheet names or section labels, segment from outlet-name keywords) — and every recovery is reported, never silent. The result is validated and scored: low-confidence parses are **rejected with an explanation** instead of importing questionable data, budget/AOP-looking files are refused as revenue actuals, and successful imports show a field-by-field mapping report (what was mapped from where, at what confidence) so you can verify it before trusting the numbers. The standardized dataset then flows through the exact same validation, canonical segment mapping, dedupe, and analytics as every predefined format — no manual mapping, no code changes for new report formats.
- **Upload multiple PDF or Excel daily reports at once** — drag and drop (or click to browse) several files into one drop zone; each is read, validated, and saved independently, with its date taken from the file's own contents rather than a manual label, so a batch upload doesn't require labeling each file "today"/"yesterday"/etc.
- **Clear upload status at every step** — every upload (daily reports, historical import, AOP, traffic) shows which file was selected, a progress spinner while it's being read/validated/saved, then an explicit ✅ success summary (file name, rows saved, duplicates skipped, detected report date) or a ❌ failure message naming exactly which stage failed (reading the file, validating its contents, or saving to the database) and why — never a silent or ambiguous result. A multi-file batch also shows a one-line summary ("3 of 4 files processed successfully").
- **Preview uploaded data** — on the **Previous Uploads** page, look at the actual stored rows for a single date or across a date range, with revenue/PAX totals, a location filter, and (for a range) a choice between daily totals or full row-level detail — useful for spot-checking what's actually in the database before trusting a comparison built on top of it.
- **Bulk-import historical workbooks** — both long-format (one row per date+location+segment+outlet) and wide pivot/cross-tab exports (one row per date with repeated PAX./Revenue. columns per outlet), auto-detected regardless of sheet name
- **Everything persists** — switching pages never loses data; the database is the single source of truth
- **Three top-level businesses** — EHPL (Encalm Hospitality Private Ltd, the largest segment, covering Lounges/Atithya/Others), Sky Plates, and Encalm Eats. The finer Lounges/Atithya/Others detail is preserved as a `business_unit` for EHPL rows and used on the Service Categories page
- **Airport traffic, Penetration %, and SPP** — upload a traffic file on the **Traffic & Terminal Analysis** page in either of two formats: a simple flat table (Date, Location, Traffic, optionally Terminal), or the cross-tab format (one sheet per airport, Terminal × Dep./Arr. columns, monthly OR daily grain — both auto-detected). Once loaded, the app computes Penetration % (PAX ÷ Traffic) and SPP (Revenue ÷ Traffic) by location, with Week/Month/Year-wise variance, plus a plain-English narrative explaining *why* revenue or penetration moved (e.g. "Revenue increased despite traffic decline due to higher SPP"). A dedicated Terminal-wise section breaks Delhi's outlets down by T1/T2/T3 against terminal-level traffic — see the note below on this mapping. When a comparison period mixes daily and monthly data (or has a genuine gap), the page flags whether figures are estimated/incomplete rather than presenting them as exact.
- **AOP (budget) targets** — upload an AOP workbook on the main page to load forward-looking revenue targets. Two formats are recognized automatically: the original per-outlet/monthly layout (Geographical Segment / Business Segment / Unit-ID columns covering FY26-27 onward), and a simpler daily-total-per-location layout (e.g. a PivotTable export with one row per day and one column per airport). If a workbook has more than one sheet that looks like AOP data, you're asked which one to import rather than the app silently picking one. The importer reads only individual outlet/day rows (never the file's own subtotal/Grand Total rows) and reports every skipped row with a reason — out-of-scope location/business line, or no outlet mapping yet — rather than silently dropping anything. AOP variance on Executive Summary automatically prorates a month's target by however many days of that month are actually present in the selected period, and falls back from the per-outlet/monthly source to the daily-total source (or blends both) depending on what's actually loaded for a given period.
- **Color-coded growth/decline** — every percentage change in every table and metric across the app is green for growth, red for decline, neutral gray for flat/undefined, consistently
- **Intelligent comparison** — pick Day-wise, Week-wise, Month-wise, or Year-wise comparison from a dropdown on Executive Summary, Revenue Comparison, and Business Insights. Week/Month/Year-wise also let you choose "Full Period" (the whole calendar week/month/year on both sides) or "To-Date" (same partial range, e.g. Monday through today, on both sides), plus pick exactly which week/month/year (or, for Day-wise, exactly which date) to compare against. Daily uploads are unaffected — the app still stores one day of data at a time; this only changes how it's aggregated for comparison.
- **AOP variance tracking** — actual vs target, with achievement %
- **Top/bottom performer analysis** and **Volume-driven vs Spend-driven** revenue attribution
- **One-click management narrative** — rule-based, deterministic, no external API calls
- **Per-business breakdowns** — EHPL (and its Lounges/Atithya/Others sub-units), Sky Plates, Encalm Eats
- Ready for **Penetration %** and **SPP** metrics once airport traffic data is added (a future data source)

## Project Structure

```
Revenue_Analytics_System/
├── Home.py                     # Main page: Upload & Analyze, historical import, DB management
├── pages/
│   ├── 1_Previous_Uploads.py
│   ├── 2_Executive_Summary.py
│   ├── 3_Revenue_Comparison.py
│   ├── 4_Traffic_and_Terminal.py
│   ├── 5_Outlet_Performance.py
│   ├── 6_Business_Insights.py
│   └── 7_Service_Categories.py
├── modules/
│   ├── __init__.py
│   ├── database.py             # SQLite persistence (SQLAlchemy)
│   ├── pdf_parser.py           # Encalm PDF format parser
│   ├── excel_parser.py         # Revenue_Dashboard.xlsx + generic Excel parser
│   ├── traffic_parser.py       # Airport traffic file parser (Date/Location/Traffic/Terminal)
│   ├── aop_parser.py           # AOP (budget) workbook parser — both per-outlet/monthly and daily-total formats
│   ├── terminal_mapping.py     # Outlet -> terminal mapping (provisional, see note above)
│   ├── data_processor.py       # File-type detection + orchestration
│   ├── upload_status.py        # Shared "file selected / in progress / success / failed" status UI
│   ├── auth.py                  # Per-person login (hashed passwords via Streamlit secrets)
│   ├── generate_password_hash.py  # CLI helper to generate a secrets.toml credential line
│   ├── revenue_analysis.py     # Comparison engine, AOP variance, driver classification, period resolution, Penetration%/SPP
│   ├── comparison_widget.py    # Shared Week/Month/Year comparison-type selector UI
│   ├── insights.py             # Rule-based management narrative
│   ├── formatting.py           # Shared number formatting (₹, PAX, %)
│   ├── table_style.py          # Green/red growth-decline color coding for tables & metrics
│   └── session.py              # Lightweight session-state helpers
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example     # Copy to secrets.toml (gitignored) and fill in real credential hashes
├── requirements.txt
├── packages.txt
├── .gitignore
└── README.md
```

## Running Locally

```bash
pip install -r requirements.txt
streamlit run Home.py
```

The app will open at `http://localhost:8501`. A SQLite database file
(`revenue_analytics.db`) is created automatically in the project root on
first run — this is git-ignored and is *your* data store, not something to
commit.

### Access control (set this up before anyone else uses the app)

**Streamlit has no built-in login.** On Streamlit Community Cloud
specifically, anyone who has (or finds, or guesses) the app's URL can open
it, upload data, and see everything already uploaded — completely
independent of whether the GitHub repo backing the app is public or
private. A public repo does **not** expose your uploaded data (the
database lives only on the running app's server, never in the repo) but
it doesn't protect the *live app* either. This app now requires signing
in before any page renders, which is what actually closes that gap.

**1. Generate a password for each person who needs access:**

```bash
python3 modules/generate_password_hash.py
```

This prompts for a username and password (not echoed to the terminal) and
prints a line like:

```
alice = "3e2e7b71...$542cad74..."
```

That's a random salt and a SHA-256 hash, separated by `$` — never the
plaintext password, and not reversible back to it. Run this once per
person who needs an account.

**2. Add those lines to your secrets** under an `[auth.users]` section.
Where this goes depends on how you're running the app:

- **Locally:** copy `.streamlit/secrets.toml.example` to
  `.streamlit/secrets.toml` (already git-ignored — never commit the real
  file) and paste in the generated lines.
- **Streamlit Community Cloud:** open your app → **Settings → Secrets**
  and paste the same `[auth]` / `[auth.users]` block in there. This is
  stored encrypted by Streamlit and is never part of your repo.

```toml
[auth]
[auth.users]
alice = "3e2e7b71...$542cad74..."
bob   = "9f1c2e88...$a07b3dd1..."
```

**3. That's it.** Every page now shows a sign-in form first; nobody sees
any data, charts, or upload controls until they enter a username and
password that matches one of the configured users. The sidebar shows who's
signed in and has a **Log out** button. If no users are configured yet,
the app shows a clear setup message instead of a login form (so this is
obvious to fix on first deploy, rather than confusing).

Each person should have their own account (rather than one shared
login) so that "who did what" stays traceable — the signed-in username is
available throughout the app via `modules.auth.current_user()`.

### First-time setup

1. Sign in (see **Access control** above — you'll need at least one
   account configured before this step).
2. Go to the main page (**Upload & Analyze**).
3. If you have a historical revenue workbook, use the **Historical Excel
   Import** section to bulk-load it. **You don't need to know or set the
   sheet name** — the app scans every sheet in the workbook and uses
   whichever one contains a row with `Date` plus `Location`/`Business`/
   `Outlet` headers, regardless of what that sheet (or the workbook) is
   named. This can take 10–30 seconds for 50K+ rows. If you ever do want to
   force one specific sheet, there's an optional "force a specific sheet
   name" field in an expander — leave it blank for auto-detection.
4. Drag and drop one or more daily PDF/Excel reports onto the upload zone
   (or click it to browse) — you can drop several files at once and they're
   each read, validated, and saved independently. The app reads each
   file's own report date from its contents, so you don't need to label
   which file is "today's" vs "yesterday's" — just upload whatever you
   have. The app will automatically find the closest matching dates
   already in the database for comparisons.
5. Use the sidebar to navigate to any of the other six pages. All of them
   read live from the database, so you can jump between pages freely
   without re-uploading or losing your place.

#### A note on wide pivot-style workbooks

If your workbook's revenue data is laid out as a wide pivot/cross-tab table
— one row per date, with `PAX.`/`Revenue.` column-pairs repeated across
merged header rows for every outlet (Location → Segment → Outlet) — the app
now detects and parses this layout too, automatically, on any sheet name.
It's tried as a fallback whenever no long-format sheet is found first, so a
workbook that only has this pivot layout (no separate long-format sheet at
all) will still import correctly. Segment- and location-level subtotal
columns (e.g. `"Atithya PAX."`, `"Delhi Revenue."`) are recognized and
skipped automatically so totals aren't double-counted, and a trailing
"Grand Total" row (if present) is ignored.

## AOP import notes (please read before trusting AOP variance numbers)

Two AOP file formats are supported, auto-detected, and stored in two
separate database tables (`aop_target` for the per-outlet/monthly format,
`aop_target_daily` for the daily-total format) since they have genuinely
different grains — merging them into one table would mean inventing a
fake "no outlet" placeholder, which is more confusing than just keeping
two tables. `database.get_aop_target_for_range()` is what reconciles the
two when computing variance: it prefers the daily-total source when it
covers the requested range, and falls back to a prorated monthly figure
(or a genuine "no data" flag) for the gaps.

### Format 2: daily-total-per-location (e.g. a PivotTable export)

This format has no outlet or business-segment breakdown at all — one row
per calendar day, one column per airport, e.g.:

```
Sum of AOP   Column Labels
             Delhi      Hyderabad   GOA    Grand Total
Date
01-06-2022   2403534    110700             2514234
```

Handling notes:
- The "Sum of AOP" / "Column Labels" cells (PivotTable metadata, not real
  columns) are ignored — detection looks specifically for a cell reading
  exactly "Date" to find the real header row.
- The "Grand Total" **column** is excluded (it's a sum across locations,
  not a location of its own); the "Grand Total" **row** at the bottom is
  also excluded (a sum across all dates).
- A blank cell for a location on a given day means "no target for that
  location that day", not zero — it's simply not included in the output
  for that (location, date) pair.
- Dates are parsed as DD-MM-YYYY text (matching the real export this was
  built against) with a few other common formats tried as a fallback.
- Because this format has no outlet/segment dimension, it can only ever
  support a whole-location AOP comparison (e.g. on Executive Summary) —
  it cannot feed the per-outlet driver tables or the per-business-category
  AOP section on Service Categories, which need the other format's
  outlet-level detail.

### Multiple candidate sheets

If a workbook has more than one sheet that looks like AOP data (in either
format), the upload UI shows a dropdown asking which sheet to import,
rather than silently picking one — see `modules/aop_parser.py`'s
`list_aop_candidate_sheets()`.

### Format 1: per-outlet/monthly

The AOP workbook's structure required several judgment calls, made
explicit here rather than buried in code comments alone:

- **Units**: the source file's cell D1 holds `=10^5` — read as a units
  note meaning every figure in the sheet is in lakhs. The importer
  multiplies every value by 100,000 automatically; if a future AOP file
  doesn't have this convention, check `modules/aop_parser.py`'s
  `_read_units_multiplier()`, which defaults to ×1 (no scaling) when it
  doesn't find that marker.
- **Rollup rows are never used.** Each location's "Total Delhi"/"Total
  HYD"/"Total GOA" row turned out to be a bespoke formula with different,
  hand-corrected exclusions per location (verified by reading the actual
  formula text in the file) — there's no general rule that works across
  locations, so only individual outlet rows are imported.
- **Meet & Greet consolidation**: per explicit confirmation, each
  location's "M&G [City]", plain "M&G", "Porter [City]", and "Buggy
  [City]" rows are all summed into one "Meet & Greet" AOP target —the
  source file lists these as separate rows but they represent the same
  underlying service.
- **LA22 aliasing**: Delhi's AOP "LA22" row is duplicated onto all three
  names revenue data has used for this lounge over time ("Arrival Lounge
  LA 22", "LA 22", "Delhi - T3 - LA 22"), per explicit confirmation that
  these are the same physical lounge.
- **Out of scope for now**: Bhogapuram (a newer airport) and business
  lines that don't exist in the app's current segment model yet (In-Flight
  Kitchen, F&B, Hotel, Nap & Shower) are not imported. Every skipped row
  is reported with a reason in an expander after upload — check that
  report after every AOP upload, especially for any row reported as "No
  outlet mapping defined yet", since that means a real, in-scope outlet
  has no AOP target yet (one already showed up this way: Hyderabad's
  "Amex Hyd" lounge, which doesn't have a matching revenue outlet at all
  yet).
- If the underlying AOP workbook's structure changes (rows reordered,
  outlets renamed, new locations added), the hand-built mapping in
  `modules/aop_parser.py`'s `_AOP_OUTLET_MAP` will need updating —
  it's an explicit dictionary, not a fuzzy matcher, by design, since a
  wrong guess here would silently corrupt every AOP variance number.

## Traffic data and the terminal mapping (please read before relying on Terminal Analysis)

The **Traffic & Terminal Analysis** page needs a traffic file uploaded
before it shows anything beyond revenue/PAX — upload one via the expander
at the top of that page. Expected columns: `Date`, `Location`, `Traffic`,
and optionally `Terminal` (any reasonably-named variant of these headers
is recognized; see `modules/traffic_parser.py`'s `COLUMN_ALIASES` if your
file uses very different names).

**The outlet → terminal mapping (`modules/terminal_mapping.py`) is
provisional.** It was built from Delhi's outlet *naming conventions*
(T1D..., T2..., T3 D49, LA01/12/22, etc.) cross-checked against Delhi
airport's publicly known terminal layout (T1/T2 = domestic, T3 =
international + premium/full-service domestic), not from an actual
traffic file's terminal labels — at the time this was built, no real
traffic file had been provided yet. Before trusting the Terminal-wise
section's numbers:

1. Check that the terminal labels your traffic file actually uses (e.g.
   "T1" vs "Terminal 1" vs "1") match `TERMINAL_1`/`TERMINAL_2`/`TERMINAL_3`
   in `terminal_mapping.py` — the join is an exact string match.
2. Spot-check a few outlet→terminal assignments against ground truth if
   you can. Outlets whose name doesn't encode a terminal directly (e.g.
   "Premium Lounge", "Air India", "CIP Lounge") were placed in T3 based on
   business-knowledge of where those services are likely to sit, not from
   an explicit marker — worth a second look.
3. Outlets not yet in the mapping show up as "Unmapped" in an expander on
   the Terminal Analysis page rather than silently vanishing — add them to
   `_DELHI_OUTLET_TO_TERMINAL` (or the Hyderabad/Goa equivalents, currently
   empty since both are treated as single-terminal) as they appear.

## A note on outlet-name matching

The daily PDF report and the historical Excel workbook describe the same
physical outlets with different naming conventions (e.g. the PDF's
*"Domestic Lounge (DEL DLO2/3/4, HYD)"* corresponds to the Excel's
*"Lounge DL 02,03,04"* for Delhi and *"Domestic Lounge"* for Hyderabad).
`modules/pdf_parser.py` includes an explicit, verified mapping
(`_OUTLET_ALIAS_BY_LOCATION`) so that outlets from both sources roll up
under one canonical name — this is what makes outlet-level comparisons
between a PDF upload and historical Excel data actually line up. If Encalm
introduces a brand-new outlet not in this map, the parser falls back to the
PDF's own label rather than dropping the row, so new outlets stay visible
(you may just see it listed under two slightly different names until the
map is updated).

## Deploying to Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, and
   click **New app**.
3. Point it at your repo, branch, and `Home.py` as the main file.
4. Deploy. `requirements.txt` and `packages.txt` are picked up automatically.

**Note on `packages.txt`:** it's intentionally an empty file. None of this
project's Python dependencies need system-level (apt) packages — `pdfplumber`'s
own dependencies (`pdfminer.six`, `Pillow`, `pypdfium2`) all ship as
pre-built wheels. Streamlit Cloud's apt-get step treats every non-empty
line in this file as a literal package name, including `#` comment lines —
so don't add comments here; if you ever do need a system package, list
just its name, one per line, with no comments.

**Important:** Streamlit Community Cloud's filesystem is ephemeral — the
SQLite database will reset whenever the app restarts/redeploys (e.g. after
inactivity or a new push). For a permanent multi-user deployment, point
`modules/database.py`'s `ENGINE` at a hosted Postgres/MySQL database instead
of the local SQLite file (the SQLAlchemy models will work unchanged — only
the connection string needs to change).

## Extending with Traffic Data (Source 3)

`revenue_master.traffic` is already part of the schema and `Penetration %` /
`SPP` calculations are already implemented in `revenue_analysis.py` — they
just have no data to work with yet. Once airport traffic figures are
available:

1. Add a small parser/importer that writes `traffic` values into
   `revenue_master` for the relevant `(date, location)` rows (traffic is
   typically airport-and-day level, not per-outlet, so you may want to
   either repeat the same traffic figure across all outlets at that
   location/date, or extend the schema with a separate `airport_traffic`
   table — either approach works with the existing `penetration_and_spp_table()` function).
2. `ra.has_traffic_data(df)` already gates the UI — once real traffic
   values are present, the **Service Categories** page automatically
   switches from the "not yet available" placeholder to live Penetration %
   / SPP tables with no other code changes required.

## Troubleshooting

- **"Could not find a usable revenue layout in any sheet"** when importing
  an Excel workbook — none of the sheets matched either the long-format
  layout or the wide pivot layout the app understands. Double-check the
  workbook actually contains daily revenue data (not just a monthly/yearly
  summary pivot) on one of its sheets.
- **"Could not find the detailed outlet table"** when uploading a PDF — the
  app expects a page containing the text *"Outlet / Business"* or *"Detailed
  Revenue"*. If your PDF's layout differs significantly from Encalm's
  standard report, this parser will need adjusting.
- **A new outlet always shows "—" in comparisons** — likely a genuinely new
  outlet not yet in `_OUTLET_ALIAS_BY_LOCATION` in `pdf_parser.py`. Add an
  entry there mapping the PDF's exact outlet text (per location) to whatever
  name you'd like it to share with historical data.
- **Duplicate upload warnings** — this is expected and harmless: re-uploading
  a report you've already loaded skips every row that's already in the
  database (matched on date + segment + outlet + location) and reports how
  many were skipped.
