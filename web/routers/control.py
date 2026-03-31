# web/routers/control.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from threading import Thread

from web.shared_state import (
    _state, acquire_run_lock, release_run_lock, is_running_lock, _bg,
)

router = APIRouter()


@router.post("/start")
def start(request: Request):
    if _state.get("thread") and _state["thread"].is_alive():
        return JSONResponse({"status": "already running"}, status_code=400)

    lock_path = request.app.state.run_lock_path
    try:
        acquire_run_lock(lock_path)
    except FileExistsError:
        return JSONResponse({"status": "already running"}, status_code=400)

    settings = request.app.state.settings
    stop_flag_fn = request.app.state.stop_flag_fn
    set_stop_flag_fn = request.app.state.set_stop_flag_fn
    from transcodarr_core.config import get_setting
    debounce_sec = float(get_setting("WATCH_DEBOUNCE_SEC", 20.0))

    worker_pool = request.app.state.worker_pool
    if worker_pool:
        worker_pool.start_auto()

    t = Thread(
        target=_bg,
        args=(settings, stop_flag_fn, set_stop_flag_fn, lock_path, debounce_sec),
        daemon=True,
    )
    _state["thread"] = t
    t.start()
    return {"status": "started", "debounce_sec": debounce_sec}


@router.get("/status")
def api_status(request: Request):
    s = request.app.state.settings
    running = is_running_lock(request.app.state.run_lock_path)
    return {
        "status": "running" if running else "stopped",
        "watch_folder": s.WATCH_FOLDER,
        "output_folder": s.OUTPUT_FOLDER,
    }


@router.post("/stop")
def stop(request: Request):
    request.app.state.set_stop_flag_fn(True)
    worker_pool = request.app.state.worker_pool
    if worker_pool:
        worker_pool.stop_auto()
    return {"status": "stopping"}
