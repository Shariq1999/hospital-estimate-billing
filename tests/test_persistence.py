"""
tests/test_persistence.py - Test suite for cloud SQLite persistence hook.
Validates:
1. Cloud storage credentials retrieval from environment / secrets.
2. Cold boot database restoration from remote Supabase / S3 bucket.
3. Skipped restore when local database already exists with valid data.
4. Graceful handling of offline execution, missing credentials, and HTTP 404s.
5. WAL-safe hot database backup generation and upload with x-upsert: true.
6. Network failure resilience ensuring local database operations are never interrupted.
"""

import io
import os
import sqlite3
import tempfile
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import persistence


@pytest.fixture
def mock_cloud_env(monkeypatch):
    """Set up mock cloud storage environment variables."""
    monkeypatch.setenv("SUPABASE_STORAGE_URL", "https://xyzproject.supabase.co/storage/v1")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "mock-service-key-xyz123")
    monkeypatch.setenv("SUPABASE_BUCKET", "hospital-backups")
    monkeypatch.setenv("SUPABASE_FILENAME", "test_hospital.db")


# ==============================================================================
# 1. Credentials and URL Parsing Tests
# ==============================================================================

def test_get_cloud_credentials_and_configuration(mock_cloud_env):
    creds = persistence.get_cloud_credentials()
    assert creds["url"] == "https://xyzproject.supabase.co/storage/v1"
    assert creds["key"] == "mock-service-key-xyz123"
    assert creds["bucket"] == "hospital-backups"
    assert creds["filename"] == "test_hospital.db"

    assert persistence.is_cloud_storage_configured(creds) is True

    obj_url = persistence.get_storage_object_url(creds)
    assert obj_url == "https://xyzproject.supabase.co/storage/v1/object/hospital-backups/test_hospital.db"


def test_is_cloud_storage_unconfigured_by_default(monkeypatch):
    monkeypatch.delenv("SUPABASE_STORAGE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_BUCKET", raising=False)

    creds = persistence.get_cloud_credentials()
    assert persistence.is_cloud_storage_configured(creds) is False


# ==============================================================================
# 2. Cold Boot Database Restoration Tests
# ==============================================================================

def test_restore_db_from_cloud_success_on_cold_boot(mock_cloud_env, tmp_path):
    target_db_path = str(tmp_path / "cold_boot.db")
    assert not os.path.exists(target_db_path)

    # Prepare mock SQLite binary payload
    mock_db_content = b"SQLite format 3\x00mock-binary-sqlite-header-and-payload"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = mock_db_content
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        restored = persistence.restore_db_from_cloud(target_db_path)
        assert restored is True
        assert os.path.exists(target_db_path)

        with open(target_db_path, "rb") as f:
            saved_content = f.read()
        assert saved_content == mock_db_content

        # Verify outgoing request details
        assert mock_urlopen.call_count == 1
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_method() == "GET"
        assert "Authorization" in req.headers
        assert req.headers["Authorization"] == "Bearer mock-service-key-xyz123"
        assert req.headers["Apikey"] == "mock-service-key-xyz123"


def test_restore_db_from_cloud_skipped_when_local_db_exists(mock_cloud_env, tmp_path):
    existing_db = tmp_path / "existing.db"
    existing_db.write_bytes(b"existing-database-content-12345")

    with patch("urllib.request.urlopen") as mock_urlopen:
        restored = persistence.restore_db_from_cloud(str(existing_db))
        assert restored is False
        mock_urlopen.assert_not_called()


def test_restore_db_from_cloud_graceful_failures(mock_cloud_env, tmp_path):
    target_db = str(tmp_path / "non_existent.db")

    # 1. HTTP 404 (Snapshot not created yet)
    http_404 = urllib.error.HTTPError(
        url="https://xyz.supabase.co",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b"Not found"),
    )
    with patch("urllib.request.urlopen", side_effect=http_404):
        assert persistence.restore_db_from_cloud(target_db) is False
        assert not os.path.exists(target_db)

    # 2. URLError (Network offline / unreachable)
    url_err = urllib.error.URLError("Connection refused")
    with patch("urllib.request.urlopen", side_effect=url_err):
        assert persistence.restore_db_from_cloud(target_db) is False
        assert not os.path.exists(target_db)

    # 3. Unconfigured credentials
    with patch("persistence.is_cloud_storage_configured", return_value=False):
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert persistence.restore_db_from_cloud(target_db) is False
            mock_urlopen.assert_not_called()


# ==============================================================================
# 3. WAL-Safe Hot Database Backup & Sync Tests
# ==============================================================================

def test_sync_db_to_cloud_wal_safe_hot_backup(mock_cloud_env, tmp_path):
    # 1. Create a real SQLite database in WAL mode
    db_file = str(tmp_path / "live_wal.db")
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, item TEXT);")
    conn.execute("INSERT INTO test_data (item) VALUES ('Ultrasound probe');")
    conn.commit()
    conn.close()

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        synced = persistence.sync_db_to_cloud(db_file)
        assert synced is True

        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"

        # Check required Supabase headers
        assert req.headers["Authorization"] == "Bearer mock-service-key-xyz123"
        assert req.headers["Apikey"] == "mock-service-key-xyz123"
        assert req.headers["X-upsert"] == "true"
        assert req.headers["Content-type"] == "application/octet-stream"

        # Check that uploaded payload is a valid binary SQLite database
        uploaded_bytes = req.data
        assert uploaded_bytes.startswith(b"SQLite format 3\x00")

        # Verify the uploaded backup can be read by SQLite and contains the test record
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as test_tmp:
            test_tmp.write(uploaded_bytes)
            test_tmp_path = test_tmp.name

        try:
            chk_conn = sqlite3.connect(test_tmp_path)
            row = chk_conn.execute("SELECT item FROM test_data").fetchone()
            assert row[0] == "Ultrasound probe"
            chk_conn.close()
        finally:
            if os.path.exists(test_tmp_path):
                os.remove(test_tmp_path)


def test_sync_db_to_cloud_graceful_failures(mock_cloud_env, tmp_path):
    db_file = str(tmp_path / "valid.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (x INT);")
    conn.commit()
    conn.close()

    # 1. Missing credentials
    with patch("persistence.is_cloud_storage_configured", return_value=False):
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert persistence.sync_db_to_cloud(db_file) is False
            mock_urlopen.assert_not_called()

    # 2. Non-existent database path
    assert persistence.sync_db_to_cloud(str(tmp_path / "missing.db")) is False

    # 3. HTTP Error during upload
    http_500 = urllib.error.HTTPError(
        url="https://xyz.supabase.co",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b"Storage error"),
    )
    with patch("urllib.request.urlopen", side_effect=http_500):
        assert persistence.sync_db_to_cloud(db_file) is False

    # 4. Network error during upload
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network unreachable")):
        assert persistence.sync_db_to_cloud(db_file) is False


# ==============================================================================
# 4. Database Mutation Sync Hook Verification
# ==============================================================================

def test_database_writes_trigger_cloud_sync(tmp_path):
    import database
    test_db = str(tmp_path / "notify_test.db")
    conn = database.get_connection(test_db)
    database.init_db(conn)

    with patch("persistence.trigger_cloud_sync_background") as mock_trigger:
        # 1. Master department mutation
        dept_id = database.save_department(conn, "Cardiothoracic Surgery")
        assert mock_trigger.call_count == 1
        assert mock_trigger.call_args[0][0].lower() == test_db.lower()

        # 2. Master room mutation
        mock_trigger.reset_mock()
        room_id = database.save_room_type(conn, "VIP Suite", 12000, "Luxury room")
        assert mock_trigger.call_count == 1
        assert mock_trigger.call_args[0][0].lower() == test_db.lower()

        # 3. Master doctor mutation
        mock_trigger.reset_mock()
        doc_id = database.save_doctor(
            conn, "Dr. Alice Smith", dept_id, 1500, 20, "Cardiac Specialist",
            surgeon_fee_type="fixed", surgeon_fee_amount=25000,
        )
        assert mock_trigger.call_count == 1

        # 4. PDF layout configuration update
        mock_trigger.reset_mock()
        database.save_pdf_layout(conn, {"hospital_name": "Cloud Memorial Hospital"})
        assert mock_trigger.call_count == 1

        # 5. Estimate save
        mock_trigger.reset_mock()
        est_dict = {
            "estimate_number": "EST-20261025-9999",
            "patient_name": "Test Patient",
            "room_type_id": room_id,
            "department_id": dept_id,
            "doctor_id": doc_id,
            "procedure_id": None,
            "stay_days": 2,
            "discount_pct": "0",
            "subtotal": "50000",
            "discount_amount": "0",
            "contingency_amount": "0",
            "tax_amount": "0",
            "total_amount": "50000",
            "min_estimate": "45000",
            "max_estimate": "55000",
            "line_items_json": {"items": []},
            "remarks": "Test note",
        }
        database.save_estimate(conn, est_dict)
        assert mock_trigger.call_count == 1

        # 6. Estimate outcome update
        mock_trigger.reset_mock()
        database.update_estimate_outcome(conn, "EST-20261025-9999", converted=True, actual_amount="49500")
        assert mock_trigger.call_count == 1

    conn.close()
