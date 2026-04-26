# web/routers/events.py
# Server-Sent Events (SSE) streams that replace the frontend's polling timers.
# Each endpoint is a long-lived connection that pushes deltas at a coalesced cadence.
import asyncio
import json
import logging

import psutil
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from web.shared_state import (
    compute_movies_view, compute_tv_view, maybe_trigger_background_scan,
    read_log_tail, is_running_lock,
    _stats_lock, _stats_timestamps, _cpu_history, _ram_history,
)

router = APIRouter()

PING_INTERVAL = 15  # seconds — sse-starlette emits a comment to defeat proxy buffering


def _index_by_path(items):
    return {it.get("path"): it for it in items if it.get("path")}


def _serialize(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _diff_items(prev, curr):
    """Return {"changed": [items_with_changed_or_new_signature], "removed": [paths]}."""
    changed = []
    for path, item in curr.items():
        prev_item = prev.get(path)
        if prev_item is None or _serialize(item) != _serialize(prev_item):
            changed.append(item)
    removed = [path for path in prev if path not in curr]
    return {"changed": changed, "removed": removed}


@router.get("/events/status")
async def events_status(request: Request):
    """Stream control status + worker pool status. ~1s tick."""
    async def gen():
        last_status = None
        last_workers = None
        while True:
            if await request.is_disconnected():
                break
            try:
                s = request.app.state.settings
                running = is_running_lock(request.app.state.run_lock_path)
                status_payload = {
                    "status": "running" if running else "stopped",
                    "running": running,
                    "watch_folder": s.WATCH_FOLDER,
                    "output_folder": s.OUTPUT_FOLDER,
                }
                if status_payload != last_status:
                    last_status = status_payload
                    yield {"event": "status", "data": json.dumps(status_payload)}

                worker_pool = request.app.state.worker_pool
                if worker_pool:
                    workers_payload = worker_pool.get_status()
                    if workers_payload != last_workers:
                        last_workers = workers_payload
                        yield {"event": "workers", "data": json.dumps(workers_payload)}
            except Exception as e:
                logging.warning("[SSE/status] %s", e)
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen(), ping=PING_INTERVAL)


@router.get("/events/media")
async def events_media(request: Request):
    """Stream movies/tv table deltas + scanning state. ~2s tick."""
    async def gen():
        last_movies = {}
        last_tv = {}
        last_scanning_movies = None
        last_scanning_tv = None
        first_run = True
        s = request.app.state.settings

        try:
            maybe_trigger_background_scan(s, "movies")
            maybe_trigger_background_scan(s, "tv")
        except Exception:
            pass

        while True:
            if await request.is_disconnected():
                break
            try:
                # File I/O happens in a thread to avoid blocking the event loop
                movies_items, movies_scanning = await asyncio.to_thread(compute_movies_view, s)
                tv_items, tv_scanning = await asyncio.to_thread(compute_tv_view, s)

                curr_movies = _index_by_path(movies_items)
                curr_tv = _index_by_path(tv_items)

                if first_run:
                    yield {"event": "movies_delta", "data": json.dumps({
                        "changed": list(curr_movies.values()),
                        "removed": [],
                        "scanning": movies_scanning,
                    })}
                    yield {"event": "tv_delta", "data": json.dumps({
                        "changed": list(curr_tv.values()),
                        "removed": [],
                        "scanning": tv_scanning,
                    })}
                    last_movies = curr_movies
                    last_tv = curr_tv
                    last_scanning_movies = movies_scanning
                    last_scanning_tv = tv_scanning
                    first_run = False
                else:
                    movies_diff = _diff_items(last_movies, curr_movies)
                    if movies_diff["changed"] or movies_diff["removed"] or movies_scanning != last_scanning_movies:
                        movies_diff["scanning"] = movies_scanning
                        yield {"event": "movies_delta", "data": json.dumps(movies_diff)}
                        last_movies = curr_movies
                        last_scanning_movies = movies_scanning

                    tv_diff = _diff_items(last_tv, curr_tv)
                    if tv_diff["changed"] or tv_diff["removed"] or tv_scanning != last_scanning_tv:
                        tv_diff["scanning"] = tv_scanning
                        yield {"event": "tv_delta", "data": json.dumps(tv_diff)}
                        last_tv = curr_tv
                        last_scanning_tv = tv_scanning

                # Periodically refresh the cache; same 60s threshold as the REST handler
                try:
                    maybe_trigger_background_scan(s, "movies")
                    maybe_trigger_background_scan(s, "tv")
                except Exception:
                    pass
            except Exception as e:
                logging.warning("[SSE/media] %s", e)
            await asyncio.sleep(2.0)

    return EventSourceResponse(gen(), ping=PING_INTERVAL)


@router.get("/events/logs")
async def events_logs(request: Request):
    """Stream log tail. ~500ms tick, 64 KB max chunk."""
    async def gen():
        log_path = request.app.state.log_path
        pos = 0
        inode = None
        # Tell the client to clear its buffer on first connect
        yield {"event": "log_chunk", "data": json.dumps({
            "text": "", "pos": 0, "inode": None, "reset": True,
        })}

        MAX_CHUNK = 64 * 1024
        while True:
            if await request.is_disconnected():
                break
            try:
                d = await asyncio.to_thread(read_log_tail, log_path, pos, inode)
                text = d.get("text", "") or ""
                if len(text) > MAX_CHUNK:
                    text = text[-MAX_CHUNK:]
                if text or d.get("reset") or (d.get("inode") and d.get("inode") != inode):
                    yield {"event": "log_chunk", "data": json.dumps({
                        "text": text,
                        "pos": d.get("pos", pos),
                        "inode": d.get("inode"),
                        "reset": d.get("reset", False),
                    })}
                pos = d.get("pos", pos)
                inode = d.get("inode") or inode
            except Exception as e:
                logging.warning("[SSE/logs] %s", e)
            await asyncio.sleep(0.5)

    return EventSourceResponse(gen(), ping=PING_INTERVAL)


@router.get("/events/system")
async def events_system(request: Request):
    """Stream CPU/RAM/disk stats. ~2s tick. Only used while the stats view is open."""
    async def gen():
        while True:
            if await request.is_disconnected():
                break
            try:
                with _stats_lock:
                    timestamps = list(_stats_timestamps)
                    cpu = list(_cpu_history)
                    ram = list(_ram_history)

                cur_cpu = psutil.cpu_percent(interval=0)
                cur_mem = psutil.virtual_memory()
                output_folder = request.app.state.settings.OUTPUT_FOLDER
                try:
                    cur_disk = psutil.disk_usage(output_folder)
                    disk_info = {
                        "total": cur_disk.total,
                        "used": cur_disk.used,
                        "free": cur_disk.free,
                        "percent": cur_disk.percent,
                    }
                except Exception:
                    disk_info = None

                yield {"event": "stats", "data": json.dumps({
                    "current": {
                        "cpu_percent": cur_cpu,
                        "ram_percent": cur_mem.percent,
                        "ram_used": cur_mem.used,
                        "ram_total": cur_mem.total,
                        "disk": disk_info,
                    },
                    "history": {"timestamps": timestamps, "cpu": cpu, "ram": ram},
                })}
            except Exception as e:
                logging.warning("[SSE/system] %s", e)
            await asyncio.sleep(2.0)

    return EventSourceResponse(gen(), ping=PING_INTERVAL)
