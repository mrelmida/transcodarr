# srt/transcodarr_core/worker_pool.py
"""
Worker Pool Manager for parallel transcoding.

Uses ThreadPoolExecutor for concurrent processing. FFmpeg runs as subprocess
so Python GIL doesn't limit parallelism.

Architecture:
- MANUAL_WORKERS workers for UI-triggered manual transcodes (0 = disabled)
- AUTO_WORKERS workers for automatic watchdog transcodes (0 = disabled)
- Total concurrent ffmpeg processes = MANUAL_WORKERS + AUTO_WORKERS
"""
import logging
import threading
import time
import os
import sys
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, List, Any
from pathlib import Path

# Global registry of active FFmpeg subprocesses keyed by source file path.
# Each entry stores {"proc": subprocess, "pool": "auto"|"manual"|"unknown"}.
_active_procs: Dict[str, dict] = {}
_active_procs_lock = threading.Lock()


def _norm(path: str) -> str:
    """Canonical key for proc registry — must be identical at register and lookup."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _current_pool_type() -> str:
    """Detect pool type from the current thread name prefix."""
    name = threading.current_thread().name
    if name.startswith("auto_worker"):
        return "auto"
    if name.startswith("manual_worker"):
        return "manual"
    return "unknown"


def register_proc(proc, source_path: str = "") -> None:
    """Register an active subprocess (called from run_ffmpeg_with_progress)."""
    with _active_procs_lock:
        key = _norm(source_path) if source_path else str(id(proc))
        _active_procs[key] = {"proc": proc, "pool": _current_pool_type()}


def unregister_proc(proc, source_path: str = "") -> None:
    """Unregister a subprocess when it finishes."""
    with _active_procs_lock:
        key = _norm(source_path) if source_path else str(id(proc))
        _active_procs.pop(key, None)


def terminate_all_procs() -> int:
    """Terminate all tracked FFmpeg subprocesses. Returns count killed."""
    with _active_procs_lock:
        entries = list(_active_procs.values())
    killed = 0
    for entry in entries:
        try:
            entry["proc"].terminate()
            killed += 1
        except Exception:
            pass
    return killed


def terminate_procs_by_pool(pool_type: str) -> int:
    """Terminate tracked FFmpeg subprocesses belonging to a specific pool. Returns count killed."""
    with _active_procs_lock:
        entries = [(k, v) for k, v in _active_procs.items() if v["pool"] == pool_type]
    killed = 0
    for _key, entry in entries:
        try:
            entry["proc"].terminate()
            killed += 1
        except Exception:
            pass
    return killed


def terminate_proc_for_file(file_path: str) -> bool:
    """Terminate the FFmpeg subprocess for a specific file. Returns True if killed."""
    key = _norm(file_path)
    with _active_procs_lock:
        entry = _active_procs.get(key)
    if entry:
        try:
            entry["proc"].terminate()
            logging.info("[WORKER_POOL] Terminated FFmpeg for: %s", file_path)
            return True
        except Exception:
            pass
    logging.warning("[WORKER_POOL] No active proc for: %s (registry keys: %s)",
                    file_path, list(_active_procs.keys()))
    return False


def _flush_log_handlers():
    """Flush all handlers on the root logger (mainly for stdout visibility)."""
    for handler in logging.root.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _log_info(msg, *args):
    """Log info message and flush handlers (for worker thread visibility)."""
    logging.info(msg, *args)
    _flush_log_handlers()


def _verify_file_logging():
    """Verify that file logging is working and log diagnostic info."""
    log_file = getattr(logging.root, '_transcodarr_log_file', None)
    if log_file:
        try:
            file_exists = os.path.exists(log_file)
            file_size = os.path.getsize(log_file) if file_exists else 0
            logging.info("[LOGGING_DIAG] Log file: %s (exists=%s, size=%d bytes)", log_file, file_exists, file_size)
        except Exception as e:
            logging.warning("[LOGGING_DIAG] Error checking log file: %s", e)
    else:
        logging.warning("[LOGGING_DIAG] No log file path stored on root logger")

    # Log handler info
    handlers = []
    for h in logging.root.handlers:
        handler_info = type(h).__name__
        if hasattr(h, 'baseFilename'):
            handler_info += f"({h.baseFilename})"
        handlers.append(handler_info)
    logging.info("[LOGGING_DIAG] Root logger handlers: %s", handlers)


def _log_warning(msg, *args):
    """Log warning message and flush handlers."""
    logging.warning(msg, *args)
    _flush_log_handlers()


def _log_error(msg, *args):
    """Log error message and flush handlers."""
    logging.error(msg, *args)
    _flush_log_handlers()


class JobStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TranscodeJob:
    """Represents a manual transcode job."""
    job_id: str
    file_path: str
    media_type: str  # "movie" or "tv"
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    future: Optional[Future] = None

    # Metadata for display
    title: Optional[str] = None
    year: Optional[int] = None
    show: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None

    def to_dict(self) -> Dict:
        """Convert job to dictionary for API response."""
        return {
            "job_id": self.job_id,
            "file_path": self.file_path,
            "media_type": self.media_type,
            "status": self.status.value,
            "progress": self.progress,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "title": self.title,
            "year": self.year,
            "show": self.show,
            "season": self.season,
            "episode": self.episode,
            "elapsed": time.time() - self.started_at if self.started_at and not self.completed_at else None,
        }


class WorkerPoolManager:
    """
    Manages dual pools of workers for parallel transcoding.

    - manual_workers: handle UI-triggered manual transcodes (0 = disabled)
    - auto_workers: handle automatic watchdog transcodes (0 = disabled)
    - Jobs are tracked with status updates
    """

    def __init__(
        self,
        manual_workers: int = 0,
        auto_workers: int = 2,
        transcode_fn: Optional[Callable[[str, Any], None]] = None,
        settings: Any = None,
    ):
        """
        Initialize the worker pool.

        Args:
            manual_workers: Workers for UI-triggered transcodes (0 = disabled)
            auto_workers: Workers for automatic watchdog transcodes (0 = disabled)
            transcode_fn: Function to call for transcoding (transcode_file)
            settings: Settings object to pass to transcode function
        """
        self.manual_workers = max(0, manual_workers)
        self.auto_workers = max(0, auto_workers)
        self.transcode_fn = transcode_fn
        self.settings = settings

        self._manual_executor: Optional[ThreadPoolExecutor] = None
        self._auto_executor: Optional[ThreadPoolExecutor] = None
        self._jobs: Dict[str, TranscodeJob] = {}
        self._jobs_lock = threading.Lock()
        self._job_counter = 0
        self._running = False
        self._processing_files: set = set()  # Files currently being processed
        self._processing_lock = threading.Lock()

        # Auto-job tracking
        self._auto_active_count = 0
        self._auto_active_lock = threading.Lock()

        logging.info(
            "[WORKER_POOL] Initialized with %d manual workers, %d auto workers",
            self.manual_workers, self.auto_workers
        )

    def start(self) -> None:
        """Start the worker pool executors."""
        if self._running:
            return

        if self.manual_workers > 0:
            self._manual_executor = ThreadPoolExecutor(
                max_workers=self.manual_workers,
                thread_name_prefix="manual_worker"
            )
        if self.auto_workers > 0:
            self._auto_executor = ThreadPoolExecutor(
                max_workers=self.auto_workers,
                thread_name_prefix="auto_worker"
            )
        self._running = True

        _log_info("[WORKER_POOL] Started with %d manual workers, %d auto workers",
                  self.manual_workers, self.auto_workers)
        _verify_file_logging()

    def reconfigure(self, manual_workers: int, auto_workers: int) -> None:
        """
        Resize the worker pools at runtime.

        Running jobs on old executors finish in the background.
        New executors are created with the updated sizes.
        """
        manual_workers = max(0, manual_workers)
        auto_workers = max(0, auto_workers)

        if manual_workers == self.manual_workers and auto_workers == self.auto_workers:
            return  # No change

        logging.info(
            "[WORKER_POOL] Reconfiguring: manual %d->%d, auto %d->%d",
            self.manual_workers, manual_workers, self.auto_workers, auto_workers
        )

        # Shut down old executors (don't wait — running jobs finish in background)
        if self._manual_executor and manual_workers != self.manual_workers:
            self._manual_executor.shutdown(wait=False)
            self._manual_executor = None
        if self._auto_executor and auto_workers != self.auto_workers:
            self._auto_executor.shutdown(wait=False)
            self._auto_executor = None

        self.manual_workers = manual_workers
        self.auto_workers = auto_workers

        # Create new executors if running
        if self._running:
            if manual_workers > 0 and self._manual_executor is None:
                self._manual_executor = ThreadPoolExecutor(
                    max_workers=manual_workers,
                    thread_name_prefix="manual_worker"
                )
            if auto_workers > 0 and self._auto_executor is None:
                self._auto_executor = ThreadPoolExecutor(
                    max_workers=auto_workers,
                    thread_name_prefix="auto_worker"
                )

        logging.info(
            "[WORKER_POOL] Reconfigured: %d manual workers, %d auto workers",
            self.manual_workers, self.auto_workers
        )

    def stop_auto(self) -> None:
        """
        Cancel queued auto jobs, kill running auto FFmpeg processes, and shut
        down the auto executor.  Call start_auto() to recreate it.
        Manual workers and their processes are left untouched.
        """
        if not self._auto_executor:
            return
        logging.info("[WORKER_POOL] Stopping auto executor...")
        # Kill only auto-pool FFmpeg subprocesses (leave manual alone)
        killed = terminate_procs_by_pool("auto")
        if killed:
            logging.info("[WORKER_POOL] Terminated %d auto FFmpeg process(es)", killed)
        self._auto_executor.shutdown(wait=False, cancel_futures=True)
        self._auto_executor = None
        with self._auto_active_lock:
            self._auto_active_count = 0
        logging.info("[WORKER_POOL] Auto executor stopped")

    def start_auto(self) -> None:
        """Recreate the auto executor after a stop_auto()."""
        if self._auto_executor or self.auto_workers <= 0:
            return
        self._auto_executor = ThreadPoolExecutor(
            max_workers=self.auto_workers,
            thread_name_prefix="auto_worker"
        )
        logging.info("[WORKER_POOL] Auto executor restarted (%d workers)", self.auto_workers)

    def stop(self, wait: bool = True) -> None:
        """
        Stop the worker pool.

        Args:
            wait: If True, wait for running jobs to complete
        """
        if not self._running:
            return

        self._running = False
        logging.info("[WORKER_POOL] Shutting down (wait=%s)...", wait)

        if self._manual_executor:
            self._manual_executor.shutdown(wait=wait, cancel_futures=not wait)
            self._manual_executor = None
        if self._auto_executor:
            self._auto_executor.shutdown(wait=wait, cancel_futures=not wait)
            self._auto_executor = None

        logging.info("[WORKER_POOL] Stopped")

    def is_file_processing(self, file_path: str) -> bool:
        """
        Check if a file is currently being processed by any worker.

        Used by main loop to avoid double-processing.
        """
        with self._processing_lock:
            # Normalize path for comparison
            normalized = str(Path(file_path).resolve())
            return normalized in self._processing_files

    def _add_processing_file(self, file_path: str) -> None:
        """Add a file to the processing set."""
        with self._processing_lock:
            normalized = str(Path(file_path).resolve())
            self._processing_files.add(normalized)

    def _remove_processing_file(self, file_path: str) -> None:
        """Remove a file from the processing set."""
        with self._processing_lock:
            normalized = str(Path(file_path).resolve())
            self._processing_files.discard(normalized)

    def get_active_job_count(self) -> int:
        """Get the number of currently running jobs."""
        with self._jobs_lock:
            return sum(1 for job in self._jobs.values() if job.status == JobStatus.RUNNING)

    def get_queued_job_count(self) -> int:
        """Get the number of queued jobs."""
        with self._jobs_lock:
            return sum(1 for job in self._jobs.values() if job.status == JobStatus.QUEUED)

    def can_accept_job(self) -> bool:
        """Check if the manual pool can accept a new job."""
        return self._running and self.manual_workers > 0 and self.get_active_job_count() < self.manual_workers

    def submit_manual_job(
        self,
        file_path: str,
        media_type: str,
        title: Optional[str] = None,
        year: Optional[int] = None,
        show: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[TranscodeJob]:
        """
        Submit a manual transcode job.

        Args:
            file_path: Path to the video file
            media_type: "movie" or "tv"
            title: Display title (for movies)
            year: Year (for movies)
            show: Show name (for TV)
            season: Season number (for TV)
            episode: Episode number (for TV)

        Returns:
            TranscodeJob if submitted successfully, None if pool is busy
        """
        if not self._running or not self._manual_executor:
            logging.warning("[WORKER_POOL] Pool not running, cannot submit job")
            return None

        # Check if file exists
        if not os.path.exists(file_path):
            logging.warning("[WORKER_POOL] File not found: %s", file_path)
            return None

        # Check if file is already being processed
        if self.is_file_processing(file_path):
            logging.warning("[WORKER_POOL] File already being processed: %s", file_path)
            return None

        # Check if we can accept a new job
        if not self.can_accept_job():
            logging.warning("[WORKER_POOL] All workers busy, cannot accept job")
            return None

        # Create job
        with self._jobs_lock:
            self._job_counter += 1
            job_id = f"job_{self._job_counter}_{int(time.time())}"

            job = TranscodeJob(
                job_id=job_id,
                file_path=file_path,
                media_type=media_type,
                title=title,
                year=year,
                show=show,
                season=season,
                episode=episode,
            )
            self._jobs[job_id] = job

        # Mark file as processing
        self._add_processing_file(file_path)

        # Submit to manual executor
        future = self._manual_executor.submit(self._run_job, job)
        job.future = future

        logging.info("[WORKER_POOL] Submitted job %s for: %s", job_id, file_path)
        return job

    def submit_batch_job(self, items: List[dict]) -> Optional[TranscodeJob]:
        """
        Submit a batch of files to be transcoded sequentially by one worker.

        The first item is submitted as a real job; the remaining items are queued
        internally and each one starts only after the previous one finishes.
        This occupies exactly one manual-worker slot for the entire batch.

        Args:
            items: list of dicts with keys file_path, media_type, title, year, show, season, episode
        Returns:
            The first TranscodeJob, or None if pool cannot accept.
        """
        if not items:
            return None
        if not self._running or not self._manual_executor:
            logging.warning("[WORKER_POOL] Pool not running, cannot submit batch")
            return None
        if self.manual_workers <= 0:
            logging.warning("[WORKER_POOL] Manual workers disabled, cannot submit batch")
            return None
        if not self.can_accept_job():
            logging.warning("[WORKER_POOL] All workers busy, cannot submit batch")
            return None

        # Filter out files already processing
        valid = [it for it in items if not self.is_file_processing(it["file_path"]) and os.path.exists(it["file_path"])]
        if not valid:
            return None

        # Create a wrapper job for the first item that will chain through the rest
        first = valid[0]
        with self._jobs_lock:
            self._job_counter += 1
            job_id = f"batch_{self._job_counter}_{int(time.time())}"
            job = TranscodeJob(
                job_id=job_id,
                file_path=first["file_path"],
                media_type=first.get("media_type", "movie"),
                title=first.get("title"),
                year=first.get("year"),
                show=first.get("show"),
                season=first.get("season"),
                episode=first.get("episode"),
            )
            self._jobs[job_id] = job

        self._add_processing_file(first["file_path"])

        future = self._manual_executor.submit(self._run_batch, job, valid)
        job.future = future

        logging.info("[WORKER_POOL] Submitted batch job %s (%d files)", job_id, len(valid))
        return job

    def _run_batch(self, first_job: TranscodeJob, items: List[dict]) -> None:
        """Run a batch of transcode items sequentially in one worker thread."""
        import traceback
        total = len(items)
        for idx, item in enumerate(items):
            fp = item["file_path"]

            if idx == 0:
                job = first_job
            else:
                # Create a sub-job for tracking
                with self._jobs_lock:
                    self._job_counter += 1
                    job_id = f"batch_{self._job_counter}_{int(time.time())}"
                    job = TranscodeJob(
                        job_id=job_id,
                        file_path=fp,
                        media_type=item.get("media_type", "movie"),
                        title=item.get("title"),
                        year=item.get("year"),
                        show=item.get("show"),
                        season=item.get("season"),
                        episode=item.get("episode"),
                    )
                    self._jobs[job_id] = job
                # Skip if already processing (could have been queued individually in the meantime)
                if self.is_file_processing(fp):
                    job.status = JobStatus.FAILED
                    job.error = "File already processing"
                    job.completed_at = time.time()
                    continue
                if not os.path.exists(fp):
                    job.status = JobStatus.FAILED
                    job.error = "File not found"
                    job.completed_at = time.time()
                    continue
                self._add_processing_file(fp)

            try:
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                _log_info("[WORKER_POOL] ========== Batch %d/%d ==========", idx + 1, total)
                _log_info("[WORKER_POOL] File: %s", fp)

                if self.transcode_fn and self.settings:
                    self.transcode_fn(fp, self.settings)

                job.status = JobStatus.COMPLETED
                job.completed_at = time.time()
                elapsed = job.completed_at - job.started_at
                _log_info("[WORKER_POOL] Batch item %d/%d done in %.1fs", idx + 1, total, elapsed)

            except Exception as e:
                job.status = JobStatus.FAILED
                job.error = str(e)
                job.completed_at = time.time()
                _log_error("[WORKER_POOL] Batch item %d/%d FAILED: %s", idx + 1, total, e)
                _log_error("[WORKER_POOL] %s", traceback.format_exc())

            finally:
                self._remove_processing_file(fp)

        _log_info("[WORKER_POOL] ========== Batch complete (%d items) ==========", total)

    def _run_job(self, job: TranscodeJob) -> None:
        """Execute a transcode job."""
        import traceback
        try:
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            _log_info("[WORKER_POOL] ========================================")
            _log_info("[WORKER_POOL] Starting manual transcode job %s", job.job_id)
            _log_info("[WORKER_POOL] File: %s", job.file_path)
            _log_info("[WORKER_POOL] ========================================")

            if self.transcode_fn and self.settings:
                _log_info("[WORKER_POOL] Calling transcode function...")
                self.transcode_fn(job.file_path, self.settings)
                _log_info("[WORKER_POOL] Transcode function returned successfully")
            else:
                _log_warning("[WORKER_POOL] No transcode function configured!")
                _log_warning("[WORKER_POOL] transcode_fn=%s, settings=%s", self.transcode_fn, self.settings)

            job.status = JobStatus.COMPLETED
            job.completed_at = time.time()
            elapsed = job.completed_at - job.started_at
            _log_info("[WORKER_POOL] ========================================")
            _log_info("[WORKER_POOL] Completed job %s in %.1fs (%.1f min)", job.job_id, elapsed, elapsed / 60)
            _log_info("[WORKER_POOL] ========================================")

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.completed_at = time.time()
            _log_error("[WORKER_POOL] ========================================")
            _log_error("[WORKER_POOL] Job %s FAILED: %s", job.job_id, e)
            _log_error("[WORKER_POOL] Traceback:\n%s", traceback.format_exc())
            _log_error("[WORKER_POOL] ========================================")

        finally:
            self._remove_processing_file(job.file_path)

    def get_job(self, job_id: str) -> Optional[TranscodeJob]:
        """Get a job by ID."""
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def get_all_jobs(self, include_completed: bool = True, limit: int = 100) -> List[TranscodeJob]:
        """
        Get all jobs.

        Args:
            include_completed: Include completed/failed jobs
            limit: Maximum number of jobs to return
        """
        with self._jobs_lock:
            jobs = list(self._jobs.values())

        if not include_completed:
            jobs = [j for j in jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)]

        # Sort by created_at descending (newest first)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def get_jobs_for_file(self, file_path: str) -> List[TranscodeJob]:
        """Get all jobs for a specific file path."""
        normalized = str(Path(file_path).resolve())
        with self._jobs_lock:
            return [
                j for j in self._jobs.values()
                if str(Path(j.file_path).resolve()) == normalized
            ]

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a queued job.

        Returns True if cancelled, False if job was already running/completed.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            if job.status != JobStatus.QUEUED:
                return False

            job.status = JobStatus.CANCELLED
            job.completed_at = time.time()

            if job.future:
                job.future.cancel()

            self._remove_processing_file(job.file_path)

        logging.info("[WORKER_POOL] Cancelled job %s", job_id)
        return True

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Remove old completed/failed jobs from memory.

        Returns the number of jobs removed.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0

        with self._jobs_lock:
            to_remove = [
                job_id for job_id, job in self._jobs.items()
                if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
                and job.completed_at and job.completed_at < cutoff
            ]

            for job_id in to_remove:
                del self._jobs[job_id]
                removed += 1

        if removed:
            logging.info("[WORKER_POOL] Cleaned up %d old jobs", removed)

        return removed

    # ---- Auto-job methods ----

    def can_accept_auto_job(self) -> bool:
        """Check if the auto pool can accept a new job."""
        if not self._running or self.auto_workers <= 0:
            return False
        with self._auto_active_lock:
            return self._auto_active_count < self.auto_workers

    def submit_auto_job(self, file_path: str, fn: Callable, *args) -> Optional[Future]:
        """
        Submit a job to the auto-worker pool.

        Args:
            file_path: Path to the file (for tracking in _processing_files)
            fn: Callable to execute (e.g. transcode_file or copy_compatible_file)
            *args: Arguments to pass to fn

        Returns:
            Future if submitted, None if pool unavailable
        """
        if not self._running or not self._auto_executor or self.auto_workers <= 0:
            return None

        if self.is_file_processing(file_path):
            logging.info("[WORKER_POOL] Auto: file already processing: %s", file_path)
            return None

        if not self.can_accept_auto_job():
            return None

        self._add_processing_file(file_path)
        with self._auto_active_lock:
            self._auto_active_count += 1

        future = self._auto_executor.submit(self._run_auto_job, file_path, fn, *args)
        logging.info("[WORKER_POOL] Auto: submitted job for %s", file_path)
        return future

    def _run_auto_job(self, file_path: str, fn: Callable, *args) -> None:
        """Execute an auto-pool job."""
        import traceback
        try:
            _log_info("[WORKER_POOL] Auto: starting %s", file_path)
            fn(*args)
            _log_info("[WORKER_POOL] Auto: completed %s", file_path)
        except Exception as e:
            _log_error("[WORKER_POOL] Auto: FAILED %s: %s", file_path, e)
            _log_error("[WORKER_POOL] Traceback:\n%s", traceback.format_exc())
        finally:
            self._remove_processing_file(file_path)
            with self._auto_active_lock:
                self._auto_active_count -= 1

    def get_active_auto_job_count(self) -> int:
        """Get the number of currently running auto jobs."""
        with self._auto_active_lock:
            return self._auto_active_count

    def get_status(self) -> Dict:
        """Get worker pool status."""
        return {
            "running": self._running,
            "manual_workers": self.manual_workers,
            "auto_workers": self.auto_workers,
            "active_manual_jobs": self.get_active_job_count(),
            "active_auto_jobs": self.get_active_auto_job_count(),
            "queued_jobs": self.get_queued_job_count(),
            "can_accept": self.can_accept_job(),
            "processing_files": list(self._processing_files),
        }


# Global singleton instance (initialized by Flask app)
_worker_pool: Optional[WorkerPoolManager] = None


def get_worker_pool() -> Optional[WorkerPoolManager]:
    """Get the global worker pool instance."""
    return _worker_pool


def set_worker_pool(pool: WorkerPoolManager) -> None:
    """Set the global worker pool instance."""
    global _worker_pool
    _worker_pool = pool


def cleanup_stale_temp_files(temp_folder: str) -> int:
    """
    Clean up stale temp files from a previous run that was interrupted.

    Removes:
    - *.tmp.mp4 files (incomplete transcodes)
    - *.progress.json files (stale progress markers)

    Call this on startup before processing begins.

    Returns the number of files cleaned up.
    """
    if not temp_folder or not os.path.exists(temp_folder):
        return 0

    cleaned = 0
    temp_path = Path(temp_folder)

    # Find and remove stale .tmp.mp4 files
    for tmp_file in temp_path.rglob("*.tmp.mp4"):
        try:
            logging.info("[CLEANUP] Removing stale temp file: %s", tmp_file)
            tmp_file.unlink()
            cleaned += 1
        except Exception as e:
            logging.warning("[CLEANUP] Failed to remove %s: %s", tmp_file, e)

    # Find and remove stale .progress.json files
    for progress_file in temp_path.rglob("*.progress.json"):
        try:
            logging.info("[CLEANUP] Removing stale progress file: %s", progress_file)
            progress_file.unlink()
            cleaned += 1
        except Exception as e:
            logging.warning("[CLEANUP] Failed to remove %s: %s", progress_file, e)

    # Try to remove empty directories
    for dirpath in sorted(temp_path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # Only removes if empty
                logging.debug("[CLEANUP] Removed empty dir: %s", dirpath)
            except OSError:
                pass  # Not empty, skip

    if cleaned > 0:
        logging.info("[CLEANUP] Cleaned up %d stale temp files on startup", cleaned)

    return cleaned
