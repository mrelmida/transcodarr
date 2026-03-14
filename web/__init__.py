import atexit
import logging
import os
from flask import Flask
from transcodarr_core import Settings
from transcodarr_core.logging_setup import setup_logging, archive_and_clear_once
from transcodarr_core.worker_pool import WorkerPoolManager, set_worker_pool, cleanup_stale_temp_files
from transcodarr_core.pipeline import transcode_file
from .blueprints.api import api_bp
from .blueprints.ui import ui_bp
from env_flag import get_stop_flag, set_stop_flag

def create_app():
    # Use absolute paths for logs to ensure consistency across all threads
    log_file = os.path.abspath("logs/transcode.log")
    archive_dir = os.path.abspath("logs/archive")

    # rotate once on first container boot
    archive_and_clear_once(log_file, archive_dir)
    # configure handlers
    setup_logging(log_file)

    app = Flask(__name__)
    settings = Settings()
    app.config["SETTINGS"] = settings
    app.config["LOG_PATH"] = log_file  # Use absolute path for consistency
    app.config["STOP_FLAG_FN"] = get_stop_flag
    app.config["SET_STOP_FLAG_FN"] = set_stop_flag
    app.config["RUN_LOCK_PATH"] = "/tmp/transcodarr.run"
    app.config["WATCH_DEBOUNCE_SEC"] = settings.WATCH_DEBOUNCE_SEC or 20.0

    # Clean up stale temp files from previous interrupted runs
    if settings.MEDIA_TEMP_FOLDER:
        try:
            cleaned = cleanup_stale_temp_files(settings.MEDIA_TEMP_FOLDER)
            if cleaned > 0:
                logging.info("[STARTUP] Cleaned up %d stale temp files", cleaned)
        except Exception as e:
            logging.warning("[STARTUP] Failed to cleanup temp files: %s", e)

    # Initialize worker pool — read from DB first, fall back to env/defaults
    from transcodarr_core.config import get_setting
    try:
        init_mw = int(get_setting("MANUAL_WORKERS", settings.MANUAL_WORKERS))
    except (ValueError, TypeError):
        init_mw = settings.MANUAL_WORKERS
    try:
        init_aw = int(get_setting("AUTO_WORKERS", settings.AUTO_WORKERS))
    except (ValueError, TypeError):
        init_aw = settings.AUTO_WORKERS

    worker_pool = WorkerPoolManager(
        manual_workers=init_mw,
        auto_workers=init_aw,
        transcode_fn=transcode_file,
        settings=settings,
    )
    worker_pool.start()
    app.config["WORKER_POOL"] = worker_pool
    set_worker_pool(worker_pool)

    # Graceful shutdown
    @atexit.register
    def shutdown_worker_pool():
        if worker_pool:
            worker_pool.stop(wait=True)

    # register blueprints
    # mount blueprints
    app.register_blueprint(ui_bp)                 # serves "/" UI
    app.register_blueprint(api_bp, url_prefix="/api")  # <-- mount API here

    # static/template folders are under web/ by default
    return app