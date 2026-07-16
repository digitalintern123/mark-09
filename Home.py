"""
Home.py — Encalm Revenue Analytics: main entry point + Page 1 (Upload & Analyze).

This file is the Streamlit multipage app's entry point (run via
`streamlit run Home.py`). The other pages live in pages/ and are
auto-discovered by Streamlit's navigation; the sidebar label for this file
itself is "Home", taken directly from the filename. Shared helpers (DB
init, session-state access) live in modules/session.py so every page
imports the same logic rather than each page reinventing it.
"""

from __future__ import annotations

import os

import streamlit as st

from modules import data_processor, database
from modules.app_logger import safe_run, log_exception, show_friendly_error


from modules.session import (
    bootstrap_session,
    clear_session,
    get_active_date,
    set_active_date,
)

st.set_page_config(
    page_title="Encalm Revenue Analytics",
    page_icon="📊",
    layout="wide",
)

bootstrap_session()

# Logo: drop a file named "encalm_logo.png" into the assets/ folder (see
# assets/README.md) and it will appear here automatically — nothing else
# to change. Silently skipped if the file isn't there yet, rather than
# showing a broken-image icon.
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "encalm_logo.png")
if os.path.exists(LOGO_PATH):
    st.image(LOGO_PATH, width=220)

st.title("📊 Encalm Group — Revenue Analytics")
st.caption("Upload revenue reports, compare periods, and track AOP performance. Re-upload data if the app restarts.")

# ---------------------------------------------------------------------------
# Upload Panel
# ---------------------------------------------------------------------------

st.header("📤 Upload & Analyze")
st.caption("Upload PDF, Excel, or CSV reports — the app auto-detects the date, format, and field mapping.")

uploaded_files = st.file_uploader(
    "Daily revenue reports — drop multiple files at once, or click to browse",
    type=[
        "pdf", "xlsx", "xls", "xlsm", "csv", "tsv", "txt",
        "docx", "html", "htm", "xml", "json",
        "png", "jpg", "jpeg", "tiff", "tif",
        "msg",
    ],
    accept_multiple_files=True,
    key="upload_multi",
)

daily_chosen_sheets = {}
if uploaded_files:
    from modules import upload_status

    for i, f in enumerate(uploaded_files):
        upload_status.render_file_selected(f.name, getattr(f, "size", None))
        sheet_names = upload_status.get_sheet_names(f)
        if sheet_names:
            daily_chosen_sheets[f.name] = upload_status.render_sheet_selector(
                f.name, sheet_names, key_prefix=f"daily_{i}", allow_auto_detect=True
            )

process_clicked = st.button(
    "🚀 Process & Analyze", type="primary", disabled=not uploaded_files
)

if process_clicked and uploaded_files:
    from modules import upload_status

    any_success = False
    latest_success_date = None
    pending_results = []
    for file_obj in uploaded_files:
        with st.spinner(f"⏳ Upload in progress — processing {file_obj.name}..."):
            result = data_processor.process_uploaded_file(
                file_obj,
                file_obj.name,
                save_to_db=True,
                excel_sheet_name=daily_chosen_sheets.get(file_obj.name),
            )
        pending_results.append((file_obj.name, result))
        if result.success:
            any_success = True
            if result.report_date and (latest_success_date is None or result.report_date > latest_success_date):
                latest_success_date = result.report_date

    if latest_success_date:
        set_active_date(latest_success_date)

    # Stash results in session_state rather than rendering them directly
    # here: a rerun is triggered right after (so the rest of the page
    # picks up the freshly-saved data), and a rerun wipes anything
    # rendered in this pass before the user ever sees it. Rendering from
    # session_state on the NEXT run is what makes the message actually
    # stick around instead of flashing for under a second.
    st.session_state["_last_upload_results"] = pending_results
    if any_success:
        st.toast("Processing complete.", icon="✅")
    st.rerun()

# Render the outcome of the most recent upload, if any — persisted across
# the rerun above via session_state so it's actually visible to the user,
# not just flashed for one frame before the rerun clears it. Stays on
# screen until the next upload attempt replaces it.
if "_last_upload_results" in st.session_state:
    from modules import upload_status

    results = st.session_state["_last_upload_results"]
    n_success = sum(1 for _, r in results if r.success)
    n_failed = len(results) - n_success
    if len(results) > 1:
        st.caption(f"**Batch summary:** {n_success} of {len(results)} file(s) processed successfully" + (f", {n_failed} failed" if n_failed else "") + ".")

    for file_name, result in results:
        date_line = [f"**Report date:** {result.report_date}"] if result.success and result.report_date else []
        upload_status.render_result_from_process_result(
            result, extra_lines=date_line, expected_format=upload_status.FORMAT_DAILY_REPORT
        )
        if result.success and result.df is not None:
            with st.expander(f"Preview extracted data — {file_name}"):
                st.dataframe(result.df, use_container_width=True)
    if st.button("✖️ Dismiss", key="dismiss_upload_results"):
        del st.session_state["_last_upload_results"]
        st.rerun()



st.divider()

# ---------------------------------------------------------------------------
# Historical Excel Import
# ---------------------------------------------------------------------------

st.header("📚 Historical Excel Import")
st.caption("Bulk-import a historical revenue workbook — sheet and format are detected automatically.")

hist_files = st.file_uploader(
    "Upload workbook(s)", type=["xlsx", "xls"], key="upload_historical",
    accept_multiple_files=True,
)

hist_chosen_sheets = {}
if hist_files:
    from modules import excel_parser, upload_status

    for i, hist_file in enumerate(hist_files):
        upload_status.render_file_selected(hist_file.name, getattr(hist_file, "size", None))
        hist_sheet_names = upload_status.get_sheet_names(hist_file)
        if hist_sheet_names:
            hist_detected_sheet = None
            try:
                if hasattr(hist_file, "seek"):
                    hist_file.seek(0)
                long_match = excel_parser.detect_long_format_sheet(hist_file)
                if long_match is not None:
                    hist_detected_sheet = long_match["sheet_name"]
            except excel_parser.ExcelParseError:
                pass
            if hist_detected_sheet is None:
                try:
                    if hasattr(hist_file, "seek"):
                        hist_file.seek(0)
                    pivot_match = excel_parser.detect_wide_pivot_sheet(hist_file)
                    if pivot_match is not None:
                        hist_detected_sheet = pivot_match["sheet_name"]
                except excel_parser.ExcelParseError:
                    pass
            if hasattr(hist_file, "seek"):
                hist_file.seek(0)
            if hist_detected_sheet:
                st.caption(f"💡 **{hist_file.name}** — auto-detect will likely use sheet **'{hist_detected_sheet}'**.")
            hist_chosen_sheets[hist_file.name] = upload_status.render_sheet_selector(
                hist_file.name,
                hist_sheet_names,
                key_prefix=f"historical_{i}",
                label=f"📑 Sheet for **{hist_file.name}**",
                allow_auto_detect=True,
            )

if st.button("📥 Import Historical Data", disabled=not hist_files) and hist_files:
    from modules import upload_status
    pending_hist = []
    _hist_latest_date = None
    for hist_file in hist_files:
        with st.spinner(f"⏳ Importing '{hist_file.name}'..."):
            result = data_processor.process_uploaded_file(
                hist_file,
                hist_file.name,
                save_to_db=True,
                excel_sheet_name=hist_chosen_sheets.get(hist_file.name),
            )
        pending_hist.append(result)
        if result.success and result.report_date and (
            _hist_latest_date is None or result.report_date > _hist_latest_date
        ):
            _hist_latest_date = result.report_date
    if _hist_latest_date:
        set_active_date(_hist_latest_date)
    st.session_state["_last_historical_result"] = pending_hist
    st.rerun()

if "_last_historical_result" in st.session_state:
    from modules import upload_status
    for _hist_result in st.session_state["_last_historical_result"]:
        upload_status.render_result_from_process_result(
            _hist_result, expected_format=upload_status.FORMAT_HISTORICAL_EXCEL
        )
        if _hist_result.success and _hist_result.df is not None:
            with st.expander(f"📋 Preview saved data — {_hist_result.file_name}", expanded=True):
                st.dataframe(_hist_result.df, use_container_width=True)
    if st.button("✖️ Dismiss", key="dismiss_historical_result"):
        del st.session_state["_last_historical_result"]
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# AOP (Annual Operating Plan) Import
# ---------------------------------------------------------------------------

st.header("🎯 AOP Target Import")
st.caption("Upload the AOP budget workbook — monthly or daily formats are detected automatically.")

_AOP_FORMAT_LABELS = {
    "outlet_monthly": "per-outlet/monthly",
    "daily_outlet_pivot": "daily outlet-level pivot",
    "daily_pivot": "daily-total-per-location",
}

aop_files = st.file_uploader(
    "AOP workbook(s) (.xlsx)", type=["xlsx", "xls"], key="aop_upload",
    accept_multiple_files=True,
)

aop_chosen_sheets = {}  # file_name → chosen sheet
if aop_files:
    from modules import aop_parser, upload_status

    for i, aop_file in enumerate(aop_files):
        upload_status.render_file_selected(aop_file.name, getattr(aop_file, "size", None))
        try:
            candidates = aop_parser.list_aop_candidate_sheets(aop_file)
        except aop_parser.AOPParseError as exc:
            upload_status.render_result(
                False, aop_file.name, str(exc), stage="reading", expected_format=upload_status.FORMAT_AOP
            )
            candidates = []
        valid_candidates = [c for c in candidates if c["format"] is not None]
        if candidates:
            if not valid_candidates:
                sheet_list = ", ".join(c["sheet_name"] for c in candidates)
                upload_status.render_result(
                    False, aop_file.name,
                    f"None of this workbook's sheets ({sheet_list}) match a recognized AOP layout.",
                    stage="reading", expected_format=upload_status.FORMAT_AOP,
                )
            all_sheet_names = [c["sheet_name"] for c in candidates]
            format_by_sheet = {c["sheet_name"]: c["format"] for c in candidates}
            default_sheet = valid_candidates[0]["sheet_name"] if valid_candidates else None
            if len(all_sheet_names) == 1:
                aop_chosen_sheets[aop_file.name] = all_sheet_names[0]
                fmt = format_by_sheet.get(all_sheet_names[0])
                if fmt:
                    st.caption(f"📄 **{aop_file.name}** — sheet **'{all_sheet_names[0]}'** ({_AOP_FORMAT_LABELS.get(fmt, fmt)} format).")
            else:
                option_labels = {
                    name: (
                        f"{name} ({_AOP_FORMAT_LABELS.get(format_by_sheet[name], format_by_sheet[name])} format)"
                        if format_by_sheet.get(name) else f"{name} (format not recognized)"
                    )
                    for name in all_sheet_names
                }
                default_index = all_sheet_names.index(default_sheet) if default_sheet in all_sheet_names else 0
                aop_chosen_sheets[aop_file.name] = st.selectbox(
                    f"📑 Sheet for **{aop_file.name}**",
                    options=all_sheet_names,
                    index=default_index,
                    format_func=lambda s: option_labels[s],
                    key=f"aop_sheet_picker_{i}",
                )

if st.button("📥 Import AOP Targets", disabled=not aop_chosen_sheets):
    from modules import aop_parser, upload_status
    pending_aop = []
    for aop_file in aop_files:
        chosen_sheet = aop_chosen_sheets.get(aop_file.name)
        if not chosen_sheet:
            continue
        if hasattr(aop_file, "seek"):
            aop_file.seek(0)
        with st.spinner(f"⏳ Importing '{aop_file.name}'..."):
            try:
                parsed = aop_parser.parse_aop_auto(aop_file, sheet_name=chosen_sheet)
                if parsed["format"] in ("outlet_monthly", "daily_outlet_pivot"):
                    save_result = database.save_aop_targets(parsed["aop_rows"], aop_file.name)
                else:
                    save_result = database.save_aop_targets_daily(parsed["aop_rows"], aop_file.name)
                pending_aop.append({"success": True, "file_name": aop_file.name,
                    "inserted": save_result["inserted"], "skipped": save_result["skipped"],
                    "rows_parsed": len(parsed["aop_rows"]),
                    "units_multiplier": parsed["units_multiplier"],
                    "skipped_rows": parsed["skipped_rows"], "aop_rows": parsed["aop_rows"]})
            except aop_parser.AOPParseError as exc:
                pending_aop.append({"success": False, "file_name": aop_file.name,
                    "message": str(exc), "stage": "reading"})
            except Exception as exc:
                pending_aop.append({"success": False, "file_name": aop_file.name,
                    "message": str(exc), "stage": "saving"})
    st.session_state["_last_aop_result"] = pending_aop
    st.rerun()

if "_last_aop_result" in st.session_state:
    from modules import upload_status
    for _aop_res in st.session_state["_last_aop_result"]:
        if _aop_res["success"]:
            upload_status.render_result(True, _aop_res["file_name"], "",
                inserted=_aop_res["inserted"], skipped=_aop_res["skipped"],
                extra_lines=[f"**Rows parsed:** {_aop_res['rows_parsed']:,}",
                              f"**Units detected:** ×{_aop_res['units_multiplier']:,.0f}"])
            if not _aop_res["skipped_rows"].empty:
                with st.expander(f"⚠️ {len(_aop_res['skipped_rows'])} row(s) out of scope — {_aop_res['file_name']}"):
                    st.dataframe(_aop_res["skipped_rows"], use_container_width=True, hide_index=True)
            with st.expander(f"📋 Preview — {_aop_res['file_name']}", expanded=True):
                st.dataframe(_aop_res["aop_rows"], use_container_width=True, hide_index=True)
        else:
            upload_status.render_result(False, _aop_res["file_name"], _aop_res["message"],
                stage=_aop_res["stage"], expected_format=upload_status.FORMAT_AOP)
    if st.button("✖️ Dismiss", key="dismiss_aop_result"):
        del st.session_state["_last_aop_result"]
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Traffic Data Import
# ---------------------------------------------------------------------------

st.header("🛂 Traffic Data Import")
st.caption("Upload airport traffic files to enable Penetration % and SPP — all formats auto-detected.")

traffic_files_home = st.file_uploader(
    "Traffic file(s) (.xlsx / .xls)",
    type=["xlsx", "xls"],
    key="traffic_upload_home",
    accept_multiple_files=True,
)

if traffic_files_home:
    from modules import upload_status as _traffic_upload_status
    for _tf in traffic_files_home:
        _traffic_upload_status.render_file_selected(_tf.name, getattr(_tf, "size", None))

if st.button("📥 Import Traffic Data", disabled=not traffic_files_home) and traffic_files_home:
    from modules import traffic_parser, upload_status as _traffic_us
    pending_traffic = []
    for _tf in traffic_files_home:
        if hasattr(_tf, "seek"):
            _tf.seek(0)
        with st.spinner(f"⏳ Importing '{_tf.name}'..."):
            try:
                _traffic_parsed = traffic_parser.parse_traffic_auto(_tf, _tf.name)
                _traffic_parse_error = None
            except Exception as _exc:
                _traffic_parsed, _traffic_parse_error = None, (str(_exc), "reading")
            _traffic_save_result = None
            if _traffic_parsed is not None:
                try:
                    _traffic_save_result = database.save_traffic_dataframe(_traffic_parsed, _tf.name)
                except Exception as _exc:
                    _traffic_parse_error = (str(_exc), "saving")
        if _traffic_save_result is not None:
            pending_traffic.append({"success": True, "file_name": _tf.name,
                "inserted": _traffic_save_result["inserted"],
                "skipped": _traffic_save_result["skipped"],
                "rows_parsed": len(_traffic_parsed), "parsed_df": _traffic_parsed})
        else:
            _msg, _stage = _traffic_parse_error
            pending_traffic.append({"success": False, "file_name": _tf.name,
                "message": _msg, "stage": _stage})
    st.session_state["_last_traffic_result_home"] = pending_traffic
    st.rerun()

if "_last_traffic_result_home" in st.session_state:
    from modules import upload_status as _traffic_us2
    for _tres in st.session_state["_last_traffic_result_home"]:
        if _tres["success"]:
            _traffic_us2.render_result(True, _tres["file_name"], "",
                inserted=_tres["inserted"], skipped=_tres["skipped"],
                extra_lines=[f"**Rows parsed:** {_tres['rows_parsed']:,}"])
            with st.expander(f"📋 Preview — {_tres['file_name']}", expanded=True):
                st.dataframe(_tres["parsed_df"], use_container_width=True, hide_index=True)
        else:
            _traffic_us2.render_result(False, _tres["file_name"], _tres["message"],
                stage=_tres["stage"], expected_format=_traffic_us2.FORMAT_TRAFFIC)
    if st.button("✖️ Dismiss", key="dismiss_traffic_result_home"):
        del st.session_state["_last_traffic_result_home"]
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------

st.header("🗄️ Database Management")

with safe_run("Database stats", error_type="db_error"):
    stats = database.get_db_stats()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Rows Stored", f"{stats['total_rows']:,}")
m2.metric("Distinct Dates", f"{stats['distinct_dates']:,}")
m3.metric("Earliest Date", str(stats["min_date"]) if stats["min_date"] else "—")
m4.metric("Latest Date", str(stats["max_date"]) if stats["max_date"] else "—")

active_date = get_active_date()
if active_date:
    st.caption(f"Active analysis date for other pages: **{active_date}**")

mgmt_col1, mgmt_col2 = st.columns(2)
with mgmt_col1:
    if st.button("🧹 Clear Session Data"):
        clear_session()
        st.session_state["_last_mgmt_message"] = "Workspace cleared. Database history is untouched."
        st.rerun()

with mgmt_col2:
    with st.popover("🗑️ Reset Database (danger zone)"):
        st.warning(
            "This permanently deletes **all** stored revenue data and upload "
            "history. This cannot be undone."
        )
        confirm = st.checkbox("I understand this will delete everything.")
        if st.button("Permanently Delete All Data", type="primary", disabled=not confirm):
            database.reset_db()
            clear_session()
            st.session_state["_last_mgmt_message"] = "Database reset. All data has been deleted."
            st.rerun()

if "._last_mgmt_message" in st.session_state:
    st.success(st.session_state["_last_mgmt_message"])
    if st.button("✖️ Dismiss", key="dismiss_mgmt_message"):
        del st.session_state["_last_mgmt_message"]
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# GitHub Backup Status Card
# ---------------------------------------------------------------------------
from modules import github_backup

st.subheader("☁️ Database Backup")

_bk = github_backup.get_last_backup_status()

if not _bk["configured"]:
    st.info(
        "GitHub backup is not configured. "
        "Add **GITHUB_TOKEN**, **GITHUB_OWNER**, **GITHUB_REPO** to your "
        "Streamlit Secrets or environment variables to enable automatic backups.",
        icon="ℹ️",
    )
else:
    _bc1, _bc2, _bc3, _bc4 = st.columns(4)
    _bc1.metric("Repository", _bk["repo"])
    _bc2.metric("Branch", _bk["branch"])
    _bc3.metric("Last Backup", _bk["time"] if _bk["time"] != "Never" else "—")
    _bc4.metric("Commit", _bk["commit_sha"][:7] if _bk["commit_sha"] != "—" else "—")

    _status = _bk["status"]
    if _status == "success":
        st.success(f"✅ {_bk['message']}", icon="✅")
    elif _status == "failed":
        st.error(f"❌ {_bk['message']}", icon="❌")
    else:
        st.caption(_bk["message"])

    _bb1, _bb2 = st.columns([1, 5])
    with _bb1:
        if st.button("🔄 Sync Now", key="manual_backup_btn"):
            with st.spinner("Pushing database to GitHub…"):
                _result = github_backup.backup_database(
                    local_path=database.DB_PATH,
                    upload_type="Manual",
                    source_file="manual-sync",
                    inserted=1,
                )
            if _result.success:
                st.success(_result.message)
            else:
                st.error(_result.message)

    # Backup log expander
    _log = github_backup.get_backup_log()
    if _log:
        with st.expander("📋 Backup Log", expanded=False):
            import pandas as _pd
            _log_df = _pd.DataFrame(reversed(_log))
            st.dataframe(_log_df, use_container_width=True, hide_index=True)

st.divider()
st.caption("Use the sidebar to navigate between pages. AOP variance is shown directly in Revenue Comparison.")
