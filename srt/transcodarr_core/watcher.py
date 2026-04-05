# srt/transcodarr_core/watcher.py
from __future__ import annotations
import logging, threading, time, os
from typing import Callable, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAVE_WATCHDOG = True
except Exception:
    HAVE_WATCHDOG = False

from transcodarr_core.config import Settings
from transcodarr_core import core_walk_and_process, core_transcode_file

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov")
IGNORE_EXTS = (".part", ".tmp", ".!qb", ".!qB", ".crdownload")

class _Kick:
    """Thread-safe 'run requested' flag with coalescing."""
    def __init__(self):
        self._event = threading.Event()
    def set(self): self._event.set()
    def clear(self): self._event.clear()
    def wait(self, timeout: float): return self._event.wait(timeout)
    def is_set(self): return self._event.is_set()

def _looks_interesting(path: str) -> bool:
    p = path.lower()
    if any(p.endswith(ext.lower()) for ext in IGNORE_EXTS): return False
    if p.endswith(VIDEO_EXTS) or os.path.isdir(path): return True
    return False

if HAVE_WATCHDOG:
    class _MovieHandler(FileSystemEventHandler):
        def __init__(self, kick: _Kick):
            super().__init__()
            self.kick = kick
        # Trigger on create/move/close-write to catch completed torrent moves
        def on_created(self, e):
            if _looks_interesting(e.src_path): self.kick.set()
        def on_moved(self, e):
            if _looks_interesting(e.dest_path): self.kick.set()
        def on_modified(self, e):
            # some downloaders emit close-write as modified; coalesce anyway
            if _looks_interesting(getattr(e, "src_path", "")): self.kick.set()
else:
    _MovieHandler = None  # type: ignore

def start_watchdog(
    *,
    settings: Optional[Settings] = None,
    stop_flag_fn: Optional[Callable[[], bool]] = None,
    debounce_sec: float = 20.0,
) -> None:
    """
    Runs a small loop:
      - Watch s.WATCH_FOLDER for new/moved/modified files
      - Debounce for `debounce_sec`
      - Call walk_and_process(transcode_file, settings)
    Safe to run in a background thread.
    """
    s = settings or Settings()

    # Safety net: don't run if auto workers disabled
    from transcodarr_core.config import get_setting
    try:
        auto_workers = int(get_setting("AUTO_WORKERS", s.AUTO_WORKERS))
    except (ValueError, TypeError):
        auto_workers = s.AUTO_WORKERS
    if auto_workers <= 0:
        logging.info("[Watchdog] AUTO_WORKERS=0, watchdog will not run.")
        return
    kick = _Kick()

    # Always do one pass on start
    kick.set()

    observer = None
    if HAVE_WATCHDOG:
        from transcodarr_core.config import get_media_paths
        _mpaths = get_media_paths(s)
        handler = _MovieHandler(kick)
        observer = Observer()
        for _wpath in [_mpaths["movies_watch"], _mpaths["tv_watch"]]:
            if os.path.isdir(_wpath):
                observer.schedule(handler, _wpath, recursive=True)
                logging.info(f"[Watchdog] Watching: {_wpath} (recursive)")
        observer.start()
    else:
        logging.warning("[Watchdog] python-watchdog not installed; falling back to 60s polling.")
        debounce_sec = max(debounce_sec, 60.0)

    try:
        while True:
            if stop_flag_fn and stop_flag_fn():
                logging.info("[Watchdog] Stop flag detected. Exiting watcher.")
                break

            # Wait for a kick; loop wakes periodically to honor stop flag
            kicked = kick.wait(timeout=1.0)
            if not kicked:
                continue  # no event yet

            # Debounce: group bursts of events (file create -> move -> close-write)
            time.sleep(debounce_sec)

            # Clear the kick and run a single scan
            kick.clear()
            logging.info("[Watchdog] Triggered. Starting walk_and_process()...")
            try:
                core_walk_and_process(transcode_file_fn=core_transcode_file, settings=s, stop_flag_fn=stop_flag_fn)
            except Exception as e:
                logging.error(f"[Watchdog] walk_and_process() failed: {e}")
                import traceback
                logging.error(traceback.format_exc())
                logging.info("[Watchdog] Continuing to watch for new files...")

    finally:
        if observer:
            observer.stop()
            observer.join(timeout=5.0)
        logging.info("[Watchdog] Stopped.")