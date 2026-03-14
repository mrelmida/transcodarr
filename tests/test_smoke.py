"""
Smoke tests for Transcodarr.

Validates app startup, core API endpoints respond correctly,
and basic job creation flow. Not comprehensive — just the happy path.
"""

import json


# ── App startup ──────────────────────────────────────────────────────────────

def test_app_creates_successfully(app):
    """create_app() returns a Flask app with key config populated."""
    assert app is not None
    assert app.config["SETTINGS"] is not None
    assert app.config["WORKER_POOL"] is not None


# ── UI ───────────────────────────────────────────────────────────────────────

def test_ui_home(client):
    """GET / returns the main UI page."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Transcodarr" in resp.data


# ── Status & control endpoints ───────────────────────────────────────────────

def test_api_status(client):
    """GET /api/status returns status JSON."""
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] in ("running", "stopped")
    assert "watch_folder" in data


def test_api_stop(client):
    """POST /api/stop returns a stopping response."""
    resp = client.post("/api/stop")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "stopping"


# ── Workers ──────────────────────────────────────────────────────────────────

def test_workers_status(client):
    """GET /api/workers/status returns pool info."""
    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["running"] is True
    assert "manual_workers" in data


# ── Jobs ─────────────────────────────────────────────────────────────────────

def test_list_jobs_empty(client):
    """GET /api/transcode/jobs returns an empty list initially."""
    resp = client.get("/api/transcode/jobs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["jobs"] == []
    assert data["count"] == 0


def test_manual_transcode_missing_path(client):
    """POST /api/transcode/manual without file_path returns 400."""
    resp = client.post(
        "/api/transcode/manual",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "file_path" in resp.get_json()["error"]


# ── Logs ─────────────────────────────────────────────────────────────────────

def test_logs_tail(client):
    """GET /api/logs/tail returns log payload."""
    resp = client.get("/api/logs/tail")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "pos" in data
    assert "text" in data
