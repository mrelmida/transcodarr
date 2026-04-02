"""
Tests for batch job tracking, batch stop, and sorting.

Covers:
  - batch_id on TranscodeJob
  - WorkerPoolManager batch stop flags
  - apply_filters sorting
  - Batch API endpoints
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from transcodarr_core.worker_pool import (
    TranscodeJob,
    JobStatus,
    WorkerPoolManager,
)
from web.shared_state import apply_filters


# ── TranscodeJob batch_id ───────────────────────────────────────────────────

class TestTranscodeJobBatchId:

    def test_batch_id_defaults_none(self):
        job = TranscodeJob(job_id="j1", file_path="/tmp/a.mkv", media_type="movie")
        assert job.batch_id is None

    def test_batch_id_set(self):
        job = TranscodeJob(
            job_id="j1", file_path="/tmp/a.mkv", media_type="movie",
            batch_id="batch_1_12345",
        )
        assert job.batch_id == "batch_1_12345"

    def test_batch_id_in_to_dict(self):
        job = TranscodeJob(
            job_id="j1", file_path="/tmp/a.mkv", media_type="movie",
            batch_id="batch_1_12345",
        )
        d = job.to_dict()
        assert "batch_id" in d
        assert d["batch_id"] == "batch_1_12345"

    def test_batch_id_none_in_to_dict(self):
        job = TranscodeJob(job_id="j1", file_path="/tmp/a.mkv", media_type="movie")
        d = job.to_dict()
        assert "batch_id" in d
        assert d["batch_id"] is None


# ── WorkerPoolManager batch methods ─────────────────────────────────────────

class TestWorkerPoolBatch:

    def _make_pool(self):
        pool = WorkerPoolManager(manual_workers=1, auto_workers=0)
        pool.start()
        return pool

    def test_get_batch_jobs_empty(self):
        pool = self._make_pool()
        try:
            assert pool.get_batch_jobs("nonexistent") == []
        finally:
            pool.stop()

    def test_get_batch_jobs_returns_matching(self):
        pool = self._make_pool()
        try:
            j1 = TranscodeJob(job_id="j1", file_path="/a.mkv", media_type="movie", batch_id="b1")
            j2 = TranscodeJob(job_id="j2", file_path="/b.mkv", media_type="movie", batch_id="b1")
            j3 = TranscodeJob(job_id="j3", file_path="/c.mkv", media_type="movie", batch_id="b2")
            pool._jobs = {"j1": j1, "j2": j2, "j3": j3}
            result = pool.get_batch_jobs("b1")
            assert len(result) == 2
            assert all(j.batch_id == "b1" for j in result)
        finally:
            pool.stop()

    def test_stop_batch_sets_flag(self):
        pool = self._make_pool()
        try:
            j1 = TranscodeJob(
                job_id="j1", file_path="/a.mkv", media_type="movie",
                batch_id="b1", status=JobStatus.COMPLETED,
            )
            pool._jobs = {"j1": j1}
            pool.stop_batch("b1")
            assert pool._batch_stop_flags.get("b1") is True
        finally:
            pool.stop()

    def test_stop_batch_kills_running_proc(self):
        pool = self._make_pool()
        try:
            j1 = TranscodeJob(
                job_id="j1", file_path="/a.mkv", media_type="movie",
                batch_id="b1", status=JobStatus.RUNNING,
            )
            pool._jobs = {"j1": j1}
            with patch("transcodarr_core.worker_pool.terminate_proc_for_file") as mock_kill:
                pool.stop_batch("b1")
                mock_kill.assert_called_once_with("/a.mkv")
        finally:
            pool.stop()

    def test_cancel_remaining_batch_items(self):
        pool = self._make_pool()
        try:
            remaining = [
                {"file_path": "/b.mkv", "media_type": "movie", "title": "Movie B"},
                {"file_path": "/c.mkv", "media_type": "movie", "title": "Movie C"},
            ]
            pool._cancel_remaining_batch_items(remaining, "b1")
            batch_jobs = pool.get_batch_jobs("b1")
            assert len(batch_jobs) == 2
            assert all(j.status == JobStatus.CANCELLED for j in batch_jobs)
            assert all(j.batch_id == "b1" for j in batch_jobs)
            assert all(j.completed_at is not None for j in batch_jobs)
        finally:
            pool.stop()

    def test_submit_batch_job_sets_batch_id(self, tmp_path):
        pool = self._make_pool()
        try:
            # Create real files so validation passes
            f1 = tmp_path / "a.mkv"
            f2 = tmp_path / "b.mkv"
            f1.touch()
            f2.touch()

            # Mock transcode_fn so it doesn't actually transcode
            pool.transcode_fn = MagicMock()
            pool.settings = MagicMock()

            with patch("transcodarr_core.database.is_ignored", return_value=False):
                job = pool.submit_batch_job([
                    {"file_path": str(f1), "media_type": "movie", "title": "A"},
                    {"file_path": str(f2), "media_type": "movie", "title": "B"},
                ])

            assert job is not None
            assert job.batch_id is not None
            assert job.batch_id.startswith("batch_")
            assert job.job_id == job.batch_id
        finally:
            pool.stop(wait=True)

    def test_run_batch_respects_stop_flag(self, tmp_path):
        """When stop flag is set, remaining items are cancelled."""
        pool = self._make_pool()
        try:
            f1 = tmp_path / "a.mkv"
            f2 = tmp_path / "b.mkv"
            f3 = tmp_path / "c.mkv"
            f1.touch()
            f2.touch()
            f3.touch()

            call_count = 0
            def fake_transcode(fp, settings):
                nonlocal call_count
                call_count += 1
                # After first item completes, set the stop flag
                if call_count == 1:
                    pool._batch_stop_flags["test_batch"] = True

            pool.transcode_fn = fake_transcode
            pool.settings = MagicMock()

            items = [
                {"file_path": str(f1), "media_type": "movie", "title": "A"},
                {"file_path": str(f2), "media_type": "movie", "title": "B"},
                {"file_path": str(f3), "media_type": "movie", "title": "C"},
            ]

            first_job = TranscodeJob(
                job_id="test_batch", batch_id="test_batch",
                file_path=str(f1), media_type="movie",
            )
            pool._jobs["test_batch"] = first_job
            pool._add_processing_file(str(f1))

            pool._run_batch(first_job, items, "test_batch")

            # Only the first item should have been transcoded
            assert call_count == 1
            assert first_job.status == JobStatus.COMPLETED

            # Remaining items should be cancelled
            batch_jobs = pool.get_batch_jobs("test_batch")
            cancelled = [j for j in batch_jobs if j.status == JobStatus.CANCELLED]
            assert len(cancelled) == 2

            # Stop flag should be cleaned up
            assert "test_batch" not in pool._batch_stop_flags
        finally:
            pool.stop()

    def test_run_batch_completes_all_without_stop(self, tmp_path):
        """Without stop flag, all items complete normally."""
        pool = self._make_pool()
        try:
            f1 = tmp_path / "a.mkv"
            f2 = tmp_path / "b.mkv"
            f1.touch()
            f2.touch()

            pool.transcode_fn = MagicMock()
            pool.settings = MagicMock()

            items = [
                {"file_path": str(f1), "media_type": "movie", "title": "A"},
                {"file_path": str(f2), "media_type": "movie", "title": "B"},
            ]

            first_job = TranscodeJob(
                job_id="test_batch", batch_id="test_batch",
                file_path=str(f1), media_type="movie",
            )
            pool._jobs["test_batch"] = first_job
            pool._add_processing_file(str(f1))

            pool._run_batch(first_job, items, "test_batch")

            assert first_job.status == JobStatus.COMPLETED
            assert pool.transcode_fn.call_count == 2

            batch_jobs = pool.get_batch_jobs("test_batch")
            completed = [j for j in batch_jobs if j.status == JobStatus.COMPLETED]
            assert len(completed) == 2
        finally:
            pool.stop()


# ── apply_filters sorting ───────────────────────────────────────────────────

class TestApplyFiltersSorting:

    ITEMS = [
        {"title": "Zebra Movie", "mtime": 1000, "size_gb": 5.0, "year": 2020},
        {"title": "Alpha Movie", "mtime": 3000, "size_gb": 1.0, "year": 2018},
        {"title": "Middle Movie", "mtime": 2000, "size_gb": 3.0, "year": 2022},
    ]

    def test_no_sort_preserves_order(self):
        result = apply_filters(list(self.ITEMS))
        assert [r["title"] for r in result] == ["Zebra Movie", "Alpha Movie", "Middle Movie"]

    def test_sort_mtime_asc(self):
        result = apply_filters(list(self.ITEMS), sort="mtime", sort_order="asc")
        assert [r["mtime"] for r in result] == [1000, 2000, 3000]

    def test_sort_mtime_desc(self):
        result = apply_filters(list(self.ITEMS), sort="mtime", sort_order="desc")
        assert [r["mtime"] for r in result] == [3000, 2000, 1000]

    def test_sort_title_asc(self):
        result = apply_filters(list(self.ITEMS), sort="title", sort_order="asc")
        assert [r["title"] for r in result] == ["Alpha Movie", "Middle Movie", "Zebra Movie"]

    def test_sort_title_desc(self):
        result = apply_filters(list(self.ITEMS), sort="title", sort_order="desc")
        assert [r["title"] for r in result] == ["Zebra Movie", "Middle Movie", "Alpha Movie"]

    def test_sort_size_gb_asc(self):
        result = apply_filters(list(self.ITEMS), sort="size_gb", sort_order="asc")
        assert [r["size_gb"] for r in result] == [1.0, 3.0, 5.0]

    def test_sort_year_desc(self):
        result = apply_filters(list(self.ITEMS), sort="year", sort_order="desc")
        assert [r["year"] for r in result] == [2022, 2020, 2018]

    def test_sort_with_limit(self):
        result = apply_filters(list(self.ITEMS), sort="mtime", sort_order="asc", limit=2)
        assert len(result) == 2
        assert [r["mtime"] for r in result] == [1000, 2000]

    def test_sort_with_query_and_limit(self):
        result = apply_filters(list(self.ITEMS), q="movie", sort="year", sort_order="asc", limit=2)
        assert len(result) == 2
        assert [r["year"] for r in result] == [2018, 2020]

    def test_sort_none_values_go_to_end(self):
        items = [
            {"title": "No Year", "mtime": 1000, "year": None},
            {"title": "Has Year", "mtime": 2000, "year": 2020},
        ]
        result = apply_filters(items, sort="year", sort_order="asc")
        assert result[0]["title"] == "Has Year"
        assert result[1]["title"] == "No Year"

    def test_sort_invalid_field_ignored(self):
        result = apply_filters(list(self.ITEMS), sort="invalid_field", sort_order="asc")
        assert [r["title"] for r in result] == ["Zebra Movie", "Alpha Movie", "Middle Movie"]

    def test_sort_empty_string_ignored(self):
        result = apply_filters(list(self.ITEMS), sort="", sort_order="asc")
        assert [r["title"] for r in result] == ["Zebra Movie", "Alpha Movie", "Middle Movie"]


# ── API-level batch endpoint tests ──────────────────────────────────────────

class TestBatchEndpoints:

    def test_batch_info_not_found(self, client):
        pool = client.app.state.worker_pool
        pool.get_batch_jobs.return_value = []
        resp = client.get("/api/transcode/batch/nonexistent")
        assert resp.status_code == 404

    def test_batch_stop_not_found(self, client):
        pool = client.app.state.worker_pool
        pool.get_batch_jobs.return_value = []
        resp = client.post("/api/transcode/batch/nonexistent/stop")
        assert resp.status_code == 404

    def test_batch_info_returns_jobs(self, client):
        pool = client.app.state.worker_pool
        mock_job = MagicMock()
        mock_job.batch_id = "b1"
        mock_job.to_dict.return_value = {
            "job_id": "j1", "batch_id": "b1", "file_path": "/a.mkv",
            "status": "running", "media_type": "movie",
        }
        pool.get_batch_jobs.return_value = [mock_job]

        resp = client.get("/api/transcode/batch/b1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_id"] == "b1"
        assert data["count"] == 1
        assert data["jobs"][0]["batch_id"] == "b1"

    def test_batch_stop_returns_stopping(self, client):
        pool = client.app.state.worker_pool
        mock_job = MagicMock()
        mock_job.batch_id = "b1"
        pool.get_batch_jobs.return_value = [mock_job]

        resp = client.post("/api/transcode/batch/b1/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopping"
        assert data["batch_id"] == "b1"
        pool.stop_batch.assert_called_once_with("b1")

    def test_file_stop_triggers_batch_stop(self, client):
        pool = client.app.state.worker_pool
        mock_job = MagicMock()
        mock_job.batch_id = "b1"
        mock_job.status = MagicMock()
        mock_job.status.value = "running"
        pool.get_jobs_for_file.return_value = [mock_job]
        pool.cancel_job.return_value = False

        with patch("transcodarr_core.worker_pool.terminate_proc_for_file", return_value=True):
            resp = client.post("/api/transcode/stop", json={"file_path": "/a.mkv"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_stopped"] is True
        pool.stop_batch.assert_called_once_with("b1")

    def test_file_stop_no_batch(self, client):
        pool = client.app.state.worker_pool
        mock_job = MagicMock()
        mock_job.batch_id = None
        mock_job.status = MagicMock()
        mock_job.status.value = "running"
        pool.get_jobs_for_file.return_value = [mock_job]
        pool.cancel_job.return_value = False

        with patch("transcodarr_core.worker_pool.terminate_proc_for_file", return_value=True):
            resp = client.post("/api/transcode/stop", json={"file_path": "/a.mkv"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_stopped"] is False
        pool.stop_batch.assert_not_called()


# ── API-level sorting tests ─────────────────────────────────────────────────

class TestMediaSortingEndpoints:

    def test_pending_accepts_sort_params(self, client):
        resp = client.get("/api/media/pending?sort=mtime&sort_order=asc&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_movies_accepts_sort_params(self, client):
        resp = client.get("/api/media/movies?sort=size_gb&sort_order=desc&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_tv_accepts_sort_params(self, client):
        resp = client.get("/api/media/tv?sort=year&sort_order=asc")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
