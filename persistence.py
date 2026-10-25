"""
persistence.py - Cloud SQLite persistence hook for ephemeral cloud container hosts.
Ensures hospital.db is safely retained across server restarts and sleep cycles
on platforms like Streamlit Community Cloud or Render while preserving SQLite WAL mode.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_cloud_credentials() -> Dict[str, str]:
    """
    Retrieve Supabase / S3-compatible cloud storage credentials.
    Checks Streamlit secrets first, then falls back to environment variables.
    """
    creds = {
        "url": "",
        "key": "",
        "bucket": "",
        "filename": "hospital.db",
    }

    # 1. Streamlit secrets
    try:
        import streamlit as st
        if hasattr(st, "secrets") and "cloud_storage" in st.secrets:
            sec = st.secrets["cloud_storage"]
            creds["url"] = str(sec.get("url") or sec.get("storage_url") or "").strip()
            creds["key"] = str(sec.get("key") or sec.get("service_key") or "").strip()
            creds["bucket"] = str(sec.get("bucket") or "").strip()
            creds["filename"] = str(sec.get("filename") or "hospital.db").strip()
    except Exception:
        pass

    # 2. Environment variables fallback
    if not creds["url"]:
        creds["url"] = os.environ.get("SUPABASE_STORAGE_URL", "").strip() or os.environ.get("SUPABASE_URL", "").strip()
    if not creds["key"]:
        creds["key"] = os.environ.get("SUPABASE_SERVICE_KEY", "").strip() or os.environ.get("SUPABASE_KEY", "").strip()
    if not creds["bucket"]:
        creds["bucket"] = os.environ.get("SUPABASE_BUCKET", "").strip()
    if not creds["filename"] or creds["filename"] == "hospital.db":
        creds["filename"] = os.environ.get("SUPABASE_FILENAME", "hospital.db").strip() or "hospital.db"

    return creds


def is_cloud_storage_configured(creds: Optional[Dict[str, str]] = None) -> bool:
    """Return True if required cloud storage credentials are set."""
    c = creds or get_cloud_credentials()
    return bool(c.get("url") and c.get("key") and c.get("bucket"))


def get_storage_object_url(creds: Dict[str, str]) -> str:
    """
    Construct the full REST object endpoint for upload/download.
    Supports base Supabase URLs (appending /storage/v1 if missing)
    or direct storage endpoint URLs.
    """
    raw_url = creds.get("url", "").rstrip("/")
    bucket = creds.get("bucket", "")
    filename = creds.get("filename", "hospital.db")

    if "/storage/v1" in raw_url:
        base_endpoint = raw_url
    else:
        base_endpoint = f"{raw_url}/storage/v1"

    return f"{base_endpoint}/object/{bucket}/{filename}"


def restore_db_from_cloud(db_path: str = "hospital.db") -> bool:
    """
    Download and restore database snapshot from cloud storage on cold boot.
    Runs once before init_db().
    If db_path already exists and has content (size > 0), does nothing and returns False.
    Fails gracefully (no-op returning False) during offline development or when credentials are unset.
    """
    # 1. Skip if database already exists with valid data
    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        logger.debug("Local database exists with non-zero size. Skipping cloud restore.")
        return False

    creds = get_cloud_credentials()
    if not is_cloud_storage_configured(creds):
        logger.debug("Cloud storage credentials not configured. Skipping cloud restore.")
        return False

    target_url = get_storage_object_url(creds)
    headers = {
        "Authorization": f"Bearer {creds['key']}",
        "apikey": creds["key"],
        "User-Agent": "Hospital-Estimate-Billing-Generator/2.0",
    }

    req = urllib.request.Request(
        url=target_url,
        headers=headers,
        method="GET",
    )

    try:
        logger.info(f"Attempting database restore from {target_url}...")
        with urllib.request.urlopen(req, timeout=20.0) as response:
            if response.status == 200:
                payload = response.read()
                if payload and len(payload) > 0:
                    parent_dir = os.path.dirname(os.path.abspath(db_path))
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)

                    temp_restore = f"{db_path}.restore.tmp"
                    with open(temp_restore, "wb") as f:
                        f.write(payload)
                    os.replace(temp_restore, db_path)
                    logger.info(f"Database successfully restored ({len(payload)} bytes).")
                    return True
        return False
    except urllib.error.HTTPError as he:
        if he.code == 404:
            logger.info("No remote snapshot found in cloud storage bucket (HTTP 404). Proceeding with fresh database.")
        else:
            logger.warning(f"Cloud storage restore failed with HTTP {he.code}: {he.reason}")
        return False
    except Exception as exc:
        logger.warning(f"Cloud storage restore encountered network or IO error: {exc}")
        return False


def sync_db_to_cloud(db_path: str = "hospital.db") -> bool:
    """
    Safely backup the live WAL-mode SQLite database using SQLite's native backup API
    and upload it to the remote cloud storage bucket with x-upsert: true.
    Fails gracefully (returning False) during offline development or network issues.
    """
    creds = get_cloud_credentials()
    if not is_cloud_storage_configured(creds):
        logger.debug("Cloud storage credentials not configured. Skipping cloud sync.")
        return False

    if not os.path.exists(db_path):
        logger.warning(f"Database path '{db_path}' does not exist. Skipping cloud sync.")
        return False

    target_url = get_storage_object_url(creds)

    # 1. Perform safe SQLite online backup of live WAL database into temporary file
    temp_backup_file = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            temp_backup_file = tmp.name

        src_conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            dest_conn = sqlite3.connect(temp_backup_file)
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()

        with open(temp_backup_file, "rb") as f:
            backup_bytes = f.read()

    except Exception as e:
        logger.warning(f"Failed to create online SQLite backup for sync: {e}")
        return False
    finally:
        if temp_backup_file and os.path.exists(temp_backup_file):
            try:
                os.remove(temp_backup_file)
            except OSError:
                pass

    if not backup_bytes:
        logger.warning("Online backup produced 0 bytes. Skipping upload.")
        return False

    # 2. Upload snapshot with x-upsert: true
    headers = {
        "Authorization": f"Bearer {creds['key']}",
        "apikey": creds["key"],
        "x-upsert": "true",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Hospital-Estimate-Billing-Generator/2.0",
    }

    req = urllib.request.Request(
        url=target_url,
        data=backup_bytes,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            if response.status in (200, 201):
                logger.info(f"Database successfully synchronized to cloud ({len(backup_bytes)} bytes).")
                return True
        return False
    except urllib.error.HTTPError as he:
        logger.warning(f"Cloud storage sync failed with HTTP {he.code}: {he.reason}")
        return False
    except Exception as exc:
        logger.warning(f"Cloud storage sync encountered network or IO error: {exc}")
        return False


def trigger_cloud_sync_background(db_path: str = "hospital.db") -> None:
    """Trigger sync_db_to_cloud in a background daemon thread so UI never blocks."""
    import threading
    t = threading.Thread(target=sync_db_to_cloud, args=(db_path,), daemon=True)
    t.start()
