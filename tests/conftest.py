"""
Shared fixtures for Transcodarr smoke tests.

Mocks out heavy dependencies (database, worker pool, logging setup)
so tests run fast without Postgres, FFmpeg, or external services.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Environment — set before any app code is imported
# ---------------------------------------------------------------------------
os.environ.setdefault("WATCH_FOLDER", "/tmp/test-watch")
os.environ.setdefault("OUTPUT_FOLDER", "/tmp/test-output")
os.environ.setdefault("MEDIA_TEMP_FOLDER", "/tmp/test-temp")
os.environ.setdefault("FLASK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_API_KEY", "test-api-key")


def _make_mock_worker_pool():
    """Return a MagicMock that quacks like WorkerPoolManager."""
    pool = MagicMock()
    pool.manual_workers = 2
    pool.auto_workers = 1
    pool._running = True
    pool.get_status.return_value = {
        "running": True,
        "manual_workers": 2,
        "auto_workers": 1,
        "active_manual_jobs": 0,
        "active_auto_jobs": 0,
        "queued_jobs": 0,
        "can_accept": True,
        "processing_files": [],
    }
    pool.get_all_jobs.return_value = []
    pool.can_accept_job.return_value = True
    pool.get_active_job_count.return_value = 0

    mock_job = MagicMock()
    mock_job.id = "test-job-001"
    mock_job.to_dict.return_value = {
        "id": "test-job-001",
        "file_path": "/tmp/test.mkv",
        "status": "queued",
        "media_type": "movie",
    }
    pool.submit_manual_job.return_value = mock_job

    return pool


@pytest.fixture()
def client(tmp_path):
    """Starlette test client with lifespan triggered and mocks active."""
    from starlette.testclient import TestClient

    log_file = str(tmp_path / "transcode.log")
    lock_file = str(tmp_path / "transcodarr.run")

    # Write an empty log file so tail endpoints work
    open(log_file, "w").close()

    with (
        patch("transcodarr_core.config.get_setting", return_value=None),
        patch("transcodarr_core.logging_setup.setup_logging"),
        patch("transcodarr_core.logging_setup.archive_and_clear_once"),
        patch("transcodarr_core.worker_pool.WorkerPoolManager") as MockPool,
        patch("transcodarr_core.worker_pool.set_worker_pool"),
        patch("transcodarr_core.worker_pool.cleanup_stale_temp_files", return_value=0),
        patch("web.shared_state.start_stats_collector"),
    ):
        mock_pool = _make_mock_worker_pool()
        MockPool.return_value = mock_pool

        from web.app import app as application

        # Enter TestClient as context manager to trigger lifespan
        with TestClient(application) as c:
            # Override state after lifespan has set it
            application.state.log_path = log_file
            application.state.run_lock_path = lock_file
            application.state.worker_pool = mock_pool
            yield c


@pytest.fixture()
def app(client):
    """The FastAPI app (accessed via client fixture to ensure lifespan ran)."""
    return client.app
