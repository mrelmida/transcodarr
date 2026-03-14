# srt/transcodarr_core/logging_setup.py
import logging, os, sys, time
from logging.handlers import RotatingFileHandler

_SENTINEL = "/tmp/transcodarr_log_boot_rotated"


class FlushingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes after every emit.

    Uses direct file opening instead of the inherited stream to ensure
    reliable writes across threads and after file recreation.
    """
    def emit(self, record):
        try:
            # Check if rotation is needed before writing
            if os.path.exists(self.baseFilename):
                if os.path.getsize(self.baseFilename) >= self.maxBytes:
                    self._do_rotation()

            msg = self.format(record)
            # Direct file open/write bypasses stream state issues
            with open(self.baseFilename, 'a', encoding='utf-8') as f:
                f.write(msg + self.terminator)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            self.handleError(record)

    def _do_rotation(self):
        """Rotate log files: .log -> .log.1 -> .log.2 etc."""
        try:
            # Rotate existing backups
            for i in range(self.backupCount - 1, 0, -1):
                src = f"{self.baseFilename}.{i}"
                dst = f"{self.baseFilename}.{i + 1}"
                if os.path.exists(src):
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.rename(src, dst)

            # Rotate current log to .1
            dst = f"{self.baseFilename}.1"
            if os.path.exists(dst):
                os.remove(dst)
            if os.path.exists(self.baseFilename):
                os.rename(self.baseFilename, dst)
        except Exception:
            pass  # Rotation failure shouldn't stop logging


def archive_and_clear_once(log_file="logs/transcode.log", archive_dir="logs/archive"):
    if os.path.exists(_SENTINEL):
        return
    os.makedirs(archive_dir, exist_ok=True)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if os.path.exists(log_file) and os.path.getsize(log_file) > 0:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        os.rename(log_file, os.path.join(archive_dir, f"transcodarr_{ts}.log"))
    # Start clean
    open(log_file, "w").close()
    # mark done
    open(_SENTINEL, "w").close()

def setup_logging(log_file="logs/transcode.log"):
    """
    Idempotent logging configuration for both web & CLI.
    - Writes to stdout (so `docker logs` works)
    - Writes to file with rotation and immediate flush
    - Safe to call multiple times
    - Thread-safe for worker pool logging
    """
    root = logging.getLogger()
    if getattr(root, "_transcodarr_configured", False):
        return

    # Ensure log directory exists
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # File rotation (5MB x 3 backups) with immediate flush for thread safety
    fh = FlushingRotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)

    # Stdout handler also flushes immediately
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    root.addHandler(fh)
    root.addHandler(sh)

    # Ensure Gunicorn/Flask logs bubble up to root once
    logging.getLogger("gunicorn.error").propagate = True
    logging.getLogger("gunicorn.access").propagate = True
    logging.getLogger("werkzeug").propagate = True

    root._transcodarr_configured = True

    # Store the log file path for reference and log startup info
    root._transcodarr_log_file = log_file
    root.info("[LOGGING] Initialized with file: %s", log_file)
    root.info("[LOGGING] Handlers: %s", [type(h).__name__ for h in root.handlers])