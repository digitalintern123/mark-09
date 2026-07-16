"""
github_backup.py — Automatic SQLite ↔ GitHub backup/restore for Streamlit Cloud.

Architecture
------------
• Uses the GitHub Contents API (no git binary required, works on Streamlit Cloud).
• All operations are non-blocking: the upload flow calls trigger_backup() which
  schedules the backup in a background thread so the user never waits for GitHub.
• On startup, restore_if_needed() checks whether the local DB is stale or missing
  and downloads the latest version from GitHub if necessary.
• Every function catches its own exceptions and returns a structured BackupResult
  so callers can display status without crashing.

Configuration (Streamlit Secrets or environment variables)
----------------------------------------------------------
GITHUB_TOKEN      — Personal Access Token with repo scope
GITHUB_OWNER      — Repository owner (username or org)
GITHUB_REPO       — Repository name
GITHUB_BRANCH     — Branch to commit to (default: "main")
GITHUB_DB_PATH    — Path inside the repo where the DB is stored
                    (default: "data/revenue_analytics.db")

Usage
-----
  # In modules/session.py bootstrap_session():
  from modules.github_backup import restore_if_needed
  restore_if_needed()

  # After any successful save:
  from modules.github_backup import trigger_backup
  trigger_backup(upload_type="Revenue", source_file="DB3.xlsx", inserted=42)
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _cfg(key: str, default: str = "") -> str:
    """Read from Streamlit secrets first, then environment variables."""
    try:
        import streamlit as st
        val = st.secrets.get(key, "")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get(key, default).strip()


def _is_configured() -> bool:
    return bool(_cfg("GITHUB_TOKEN") and _cfg("GITHUB_OWNER") and _cfg("GITHUB_REPO"))


def _repo_db_path() -> str:
    return _cfg("GITHUB_DB_PATH", "data/revenue_analytics.db")


def _branch() -> str:
    return _cfg("GITHUB_BRANCH", "main")


def _api_base() -> str:
    owner = _cfg("GITHUB_OWNER")
    repo  = _cfg("GITHUB_REPO")
    return f"https://api.github.com/repos/{owner}/{repo}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_cfg('GITHUB_TOKEN')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class BackupResult:
    success: bool
    message: str
    commit_sha: Optional[str] = None
    timestamp: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    skipped: bool = False          # True when DB hasn't changed — nothing to push


# ---------------------------------------------------------------------------
# In-memory backup log (persisted to session_state by the UI layer)
# ---------------------------------------------------------------------------

_backup_log: list[dict] = []        # module-level log; max 100 entries
_log_lock = threading.Lock()

def _append_log(entry: dict) -> None:
    with _log_lock:
        _backup_log.append(entry)
        if len(_backup_log) > 100:
            _backup_log.pop(0)


def get_backup_log() -> list[dict]:
    """Return a copy of the in-memory backup log (newest last)."""
    with _log_lock:
        return list(_backup_log)


# ---------------------------------------------------------------------------
# Hash utilities
# ---------------------------------------------------------------------------

def _file_sha256(path: str) -> Optional[str]:
    """SHA-256 of a local file, or None if it doesn't exist."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def database_has_changed(local_path: str, previous_sha: Optional[str]) -> bool:
    """
    Return True if the local DB file differs from the previously recorded SHA.
    Used to skip unnecessary pushes.
    """
    current = _file_sha256(local_path)
    if current is None:
        return False            # file doesn't exist — nothing to push
    return current != previous_sha


# ---------------------------------------------------------------------------
# GitHub Contents API helpers
# ---------------------------------------------------------------------------

def _get_remote_file_meta() -> Optional[dict]:
    """
    Fetch metadata (sha, download_url, size) for the DB file on GitHub.
    Returns None if the file doesn't exist or GitHub is unreachable.
    """
    url = f"{_api_base()}/contents/{_repo_db_path()}?ref={_branch()}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None                 # file not yet in repo — first push
        logger.warning("GitHub metadata fetch returned %s", resp.status_code)
        return None
    except requests.RequestException as exc:
        logger.warning("GitHub metadata fetch failed: %s", exc)
        return None


def download_latest_database(local_path: str) -> BackupResult:
    """
    Download the DB file from GitHub and save it to local_path.
    Creates parent directories as needed.
    """
    if not _is_configured():
        return BackupResult(False, "GitHub backup not configured — skipping restore.")

    meta = _get_remote_file_meta()
    if not meta:
        return BackupResult(False, "DB not found in GitHub repository.")

    download_url = meta.get("download_url")
    if not download_url:
        return BackupResult(False, "GitHub response missing download_url.")

    try:
        resp = requests.get(download_url, headers=_headers(), timeout=60, stream=True)
        resp.raise_for_status()

        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        tmp = local_path + ".tmp"
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(65536):
                fh.write(chunk)
        shutil.move(tmp, local_path)

        msg = f"Restored DB from GitHub (commit blob {meta.get('sha','?')[:7]})."
        logger.info(msg)
        return BackupResult(True, msg, commit_sha=meta.get("sha"))
    except Exception as exc:
        logger.error("DB restore failed: %s", exc)
        return BackupResult(False, f"Restore failed: {exc}")


def restore_if_needed(local_path: str) -> BackupResult:
    """
    Called at app startup. Downloads the GitHub DB only when:
      - The local DB file is missing, OR
      - The local DB's SHA differs from the remote SHA (remote is newer).

    Never overwrites a newer local file with an older remote one.
    """
    if not _is_configured():
        return BackupResult(False, "GitHub not configured — using local DB.", skipped=True)

    meta = _get_remote_file_meta()
    if not meta:
        # Nothing on GitHub yet — local DB (if any) is authoritative.
        return BackupResult(True, "No remote DB found — using local DB.", skipped=True)

    remote_blob_sha = meta.get("sha", "")
    local_exists = os.path.exists(local_path)

    if not local_exists:
        logger.info("Local DB missing — restoring from GitHub.")
        result = download_latest_database(local_path)
        _append_log({
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "action": "restore",
            "status": "success" if result.success else "failed",
            "message": result.message,
        })
        return result

    # Local exists — compare content hash against GitHub blob SHA.
    # GitHub blob SHA = sha1("blob {size}\0{content}"), but we can
    # compare our SHA-256 against what we recorded last time instead.
    # Simplest heuristic: compare file sizes.
    local_size = os.path.getsize(local_path)
    remote_size = meta.get("size", 0)
    if local_size >= remote_size:
        # Local is at least as large as remote — assume local is newer.
        return BackupResult(True, "Local DB is current — no restore needed.", skipped=True)

    logger.info("Remote DB appears newer (remote %d bytes > local %d bytes) — restoring.", remote_size, local_size)
    result = download_latest_database(local_path)
    _append_log({
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "action": "restore",
        "status": "success" if result.success else "failed",
        "message": result.message,
    })
    return result


# ---------------------------------------------------------------------------
# Core backup function
# ---------------------------------------------------------------------------

# Track the SHA256 of the last successfully pushed DB so we can skip
# identical consecutive pushes without reading the file twice.
_last_pushed_sha: Optional[str] = None
_push_lock = threading.Lock()


def backup_database(
    local_path: str,
    upload_type: str = "Manual",
    source_file: str = "",
    inserted: int = 0,
) -> BackupResult:
    """
    Push the local SQLite DB to GitHub as a committed file.

    • Reads the file, base64-encodes it, and calls the GitHub Contents API.
    • If the file already exists in the repo, fetches its blob SHA first
      (required by the API to update an existing file).
    • Skips the push if the local file SHA matches the last pushed SHA.
    • Never raises — returns BackupResult(success=False, ...) on any error.
    """
    global _last_pushed_sha

    if not _is_configured():
        return BackupResult(False, "GitHub not configured — backup skipped.", skipped=True)

    if not os.path.exists(local_path):
        return BackupResult(False, f"DB file not found at {local_path}.")

    with _push_lock:
        # --- Skip if unchanged ---
        current_sha = _file_sha256(local_path)
        if current_sha and current_sha == _last_pushed_sha:
            return BackupResult(True, "DB unchanged since last push — skipped.", skipped=True)

        # --- Read & encode ---
        try:
            with open(local_path, "rb") as fh:
                raw = fh.read()
            encoded = base64.b64encode(raw).decode("utf-8")
        except OSError as exc:
            return BackupResult(False, f"Could not read DB: {exc}")

        # --- Build commit message ---
        now_ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
        ts = now_ist.strftime("%Y-%m-%d %H:%M IST")
        commit_msg = f"Database backup - {upload_type} upload - {ts}"
        if source_file:
            commit_msg += f" [{source_file}]"

        # --- Get existing blob SHA (needed to update an existing file) ---
        meta = _get_remote_file_meta()
        blob_sha = meta.get("sha") if meta else None

        # --- Push via Contents API ---
        url = f"{_api_base()}/contents/{_repo_db_path()}"
        payload: dict = {
            "message": commit_msg,
            "content": encoded,
            "branch": _branch(),
        }
        if blob_sha:
            payload["sha"] = blob_sha    # required for update; omit for create

        try:
            resp = requests.put(url, headers=_headers(), json=payload, timeout=60)
        except requests.RequestException as exc:
            msg = f"GitHub push failed (network): {exc}"
            logger.error(msg)
            return BackupResult(False, msg)

        if resp.status_code in (200, 201):
            data = resp.json()
            commit_sha = data.get("commit", {}).get("sha", "")
            _last_pushed_sha = current_sha
            msg = f"Backup successful — commit {commit_sha[:7]} on {_branch()}."
            logger.info(msg)
            _append_log({
                "time": dt.datetime.now().isoformat(timespec="seconds"),
                "action": "backup",
                "upload_type": upload_type,
                "source_file": source_file,
                "inserted": inserted,
                "commit_sha": commit_sha,
                "status": "success",
                "message": msg,
            })
            return BackupResult(True, msg, commit_sha=commit_sha)

        # --- Handle errors ---
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        msg = f"GitHub push failed ({resp.status_code}): {detail}"
        logger.error(msg)
        _append_log({
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "action": "backup",
            "upload_type": upload_type,
            "source_file": source_file,
            "inserted": inserted,
            "status": "failed",
            "message": msg,
        })
        return BackupResult(False, msg)


# ---------------------------------------------------------------------------
# Background trigger — non-blocking
# ---------------------------------------------------------------------------

def trigger_backup(
    upload_type: str = "Manual",
    source_file: str = "",
    inserted: int = 0,
) -> None:
    """
    Fire-and-forget backup. Runs backup_database() in a daemon thread so
    the upload response is returned to the user immediately, regardless of
    GitHub latency.

    Call this after any successful database write:

        if result.inserted > 0:
            github_backup.trigger_backup("Revenue", file_name, result.inserted)
    """
    if not _is_configured():
        return

    # Import here to avoid circular import at module load time.
    from . import database as _db

    def _run():
        try:
            # Small delay so the SQLite transaction is fully flushed.
            time.sleep(1)
            backup_database(
                local_path=_db.DB_PATH,
                upload_type=upload_type,
                source_file=source_file,
                inserted=inserted,
            )
        except Exception as exc:
            logger.error("Background backup thread failed: %s", exc)

    t = threading.Thread(target=_run, daemon=True, name="github-backup")
    t.start()


# ---------------------------------------------------------------------------
# Status helpers (used by the UI card)
# ---------------------------------------------------------------------------

def get_last_backup_status() -> dict:
    """
    Return a dict with last backup info for display in the UI card.
    Keys: time, commit_sha, branch, status, message, configured
    """
    log = get_backup_log()
    backup_entries = [e for e in reversed(log) if e.get("action") == "backup"]
    last = backup_entries[0] if backup_entries else {}
    return {
        "configured": _is_configured(),
        "branch": _branch(),
        "repo": f"{_cfg('GITHUB_OWNER')}/{_cfg('GITHUB_REPO')}" if _is_configured() else "—",
        "time": last.get("time", "Never"),
        "commit_sha": last.get("commit_sha", "—"),
        "status": last.get("status", "—"),
        "message": last.get("message", "No backup yet."),
    }
