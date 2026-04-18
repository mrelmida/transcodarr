# web/routers/media.py
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi import HTTPException
from urllib.parse import quote
from threading import Thread
from pathlib import Path
import os, re, json, time, logging

from web.shared_state import (
    VIDEO_EXTS, get_media_cache, load_cache, save_cache,
    scan_pending_movies, scan_pending_tv,
    scan_processing_movies, scan_processing_tv,
    scan_reencode_progress, scan_movies_incremental, scan_tv_incremental,
    background_scan, apply_filters, find_video_for_meta, parse_sxe,
    has_sentinel, remove_sentinel, enrich_state,
    bytes_to_gb, format_timestamp,
)
from transcodarr_core.database import (
    get_all_movies, get_all_tv_episodes,
    set_ignored, remove_ignored, is_ignored, get_all_ignored, get_ignored_paths,
)
from transcodarr_core.metadata import fetch_movie_metadata, fetch_series_metadata

router = APIRouter()


@router.get("/media/movies")
def api_media_movies(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=0),
    refresh: str = Query(default=""),
    sort: str = Query(default=""),
    sort_order: str = Query(default="asc"),
):
    """Return cached movies instantly, trigger background refresh if needed."""
    s = request.app.state.settings
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(s)
    root = Path(_mp["movies_output"])
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    media_cache = get_media_cache()

    if not media_cache["movies"]["items"]:
        load_cache("movies")

    items = list(media_cache["movies"]["items"])

    reencode_map = scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    if watch_root:
        pending_items = scan_pending_movies(watch_root, temp_root)
        items = pending_items + items

    if temp_root:
        processing_items = scan_processing_movies(temp_root, watch_root)
        items = processing_items + items

    do_refresh = refresh == "1"
    cache_age = int(time.time()) - media_cache["movies"]["last_scan"]
    if do_refresh or not media_cache["movies"]["items"] or cache_age > 60:
        if not media_cache["movies"]["scanning"]:
            t = Thread(target=background_scan, args=("movies", root), daemon=True)
            t.start()

    items = apply_filters(items, q=q, limit=limit, sort=sort, sort_order=sort_order)
    scanning = media_cache["movies"]["scanning"]
    return {"items": items, "count": len(items), "scanning": scanning}


@router.get("/media/tv")
def api_media_tv(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=0),
    refresh: str = Query(default=""),
    sort: str = Query(default=""),
    sort_order: str = Query(default="asc"),
):
    """Return cached TV instantly, trigger background refresh if needed."""
    s = request.app.state.settings
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(s)
    root = Path(_mp["tv_output"])
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    media_cache = get_media_cache()

    if not media_cache["tv"]["items"]:
        load_cache("tv")

    items = list(media_cache["tv"]["items"])

    reencode_map = scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    if watch_root:
        pending_items = scan_pending_tv(watch_root, temp_root)
        items = pending_items + items

    if temp_root:
        processing_items = scan_processing_tv(temp_root, watch_root)
        items = processing_items + items

    do_refresh = refresh == "1"
    cache_age = int(time.time()) - media_cache["tv"]["last_scan"]
    if do_refresh or not media_cache["tv"]["items"] or cache_age > 60:
        if not media_cache["tv"]["scanning"]:
            t = Thread(target=background_scan, args=("tv", root), daemon=True)
            t.start()

    items = apply_filters(items, q=q, limit=limit, sort=sort, sort_order=sort_order)
    scanning = media_cache["tv"]["scanning"]
    return {"items": items, "count": len(items), "scanning": scanning}


@router.get("/media/pending")
def api_media_pending(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=0),
    media_type: str = Query(default="all"),
    sort: str = Query(default=""),
    sort_order: str = Query(default="asc"),
):
    """Return only pending/queued/processing files from the watch folder.

    Query params:
      ?q=korra           — fuzzy search filter
      ?limit=10          — max items returned
      ?media_type=movie  — "movie", "tv", or "all" (default)

    Designed for external agents to discover files awaiting transcode.
    """
    s = request.app.state.settings
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    items = []

    if watch_root:
        if media_type in ("movie", "all"):
            items.extend(scan_pending_movies(watch_root, temp_root))
        if media_type in ("tv", "all"):
            items.extend(scan_pending_tv(watch_root, temp_root))

    if temp_root:
        if media_type in ("movie", "all"):
            items.extend(scan_processing_movies(temp_root, watch_root))
        if media_type in ("tv", "all"):
            items.extend(scan_processing_tv(temp_root, watch_root))

    # Filter out ignored items
    items = [i for i in items if not i.get("ignored")]

    items = apply_filters(items, q=q, limit=limit, sort=sort, sort_order=sort_order)
    return {"items": items, "count": len(items)}


@router.get("/media/ignored/rich")
def api_media_ignored_rich(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=0),
    media_type: str = Query(default="all"),
):
    """Return ignored files with full media metadata.

    Like /media/pending but for ignored items. Pulls from both watch folder
    (pending+ignored) and output folder (ready+ignored), returning only items
    whose ignored flag is set.

    Query params:
      ?q=korra           — fuzzy search filter
      ?limit=10          — max items returned
      ?media_type=movie  — "movie", "tv", or "all" (default)
    """
    s = request.app.state.settings
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    items = []

    # Pending items from watch folder (have ignored field already)
    if watch_root:
        if media_type in ("movie", "all"):
            items.extend(scan_pending_movies(watch_root, temp_root))
        if media_type in ("tv", "all"):
            items.extend(scan_pending_tv(watch_root, temp_root))

    # Keep only ignored items
    items = [i for i in items if i.get("ignored")]

    items = apply_filters(items, q=q, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/media/tv/debug")
def api_media_tv_debug(request: Request):
    """Debug endpoint to diagnose pending TV detection issues."""
    s = request.app.state.settings
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    debug_info = {
        "config": {
            "WATCH_FOLDER": s.WATCH_FOLDER,
            "MEDIA_TEMP_FOLDER": s.MEDIA_TEMP_FOLDER,
        },
        "paths_checked": [],
        "meta_files_found": [],
        "matching_results": [],
    }

    if not watch_root:
        debug_info["error"] = "WATCH_FOLDER not configured"
        return debug_info

    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(s)
    tv_root_direct = Path(_mp["tv_watch"])
    tv_root_processing = watch_root / "_processing" / tv_root_direct.name

    debug_info["paths_checked"] = [
        {"path": str(tv_root_direct), "exists": tv_root_direct.exists()},
        {"path": str(tv_root_processing), "exists": tv_root_processing.exists()},
    ]

    tv_root = tv_root_direct if tv_root_direct.exists() else tv_root_processing
    if not tv_root.exists():
        debug_info["error"] = f"Neither {tv_root_direct} nor {tv_root_processing} exists"
        return debug_info

    debug_info["tv_root_used"] = str(tv_root)

    meta_files = list(tv_root.rglob("*.meta.json"))
    debug_info["meta_files_found"] = [str(m) for m in meta_files]

    for meta_file in meta_files:
        folder = meta_file.parent
        meta_stem = meta_file.stem
        if meta_stem.endswith(".meta"):
            meta_stem = meta_stem[:-5]

        video_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]

        result = {
            "meta_file": str(meta_file),
            "meta_stem": meta_stem,
            "folder": str(folder),
            "video_files_in_folder": [{"name": v.name, "stem": v.stem} for v in video_files],
            "exact_stem_match": None,
            "episode_code_match": None,
        }

        for vf in video_files:
            if vf.stem == meta_stem:
                result["exact_stem_match"] = vf.name
                break

        meta_ep = parse_sxe(meta_file.name)
        result["meta_episode_code"] = meta_ep
        if meta_ep[0] is not None:
            for vf in video_files:
                video_ep = parse_sxe(vf.name)
                if video_ep == meta_ep:
                    result["episode_code_match"] = {"video": vf.name, "episode_code": video_ep}
                    break

        result["would_match"] = result["exact_stem_match"] is not None or result["episode_code_match"] is not None
        debug_info["matching_results"].append(result)

    return debug_info


@router.delete("/media/output")
def api_delete_output(request: Request, data: dict = Body(default={})):
    """Delete output files (and companions) by path."""
    paths = data.get("paths", [])
    if not paths or not isinstance(paths, list):
        return JSONResponse({"error": "paths array required"}, status_code=400)

    s = request.app.state.settings
    output_folder = os.path.realpath(s.OUTPUT_FOLDER)

    deleted = []
    errors = []
    companion_exts = (".nfo", ".srt", ".sub", ".idx", ".ass", ".ssa", ".meta.json",
                      ".jpg", ".png", "-thumb.jpg", "-poster.jpg")

    for p in paths:
        real = os.path.realpath(p)
        if not real.startswith(output_folder + os.sep) and real != output_folder:
            errors.append({"path": p, "error": "path outside output folder"})
            continue

        if not os.path.isfile(real):
            errors.append({"path": p, "error": "file not found"})
            continue

        try:
            os.remove(real)
            deleted.append(p)

            stem = os.path.splitext(real)[0]
            parent = os.path.dirname(real)
            for ext in companion_exts:
                companion = stem + ext
                if os.path.isfile(companion):
                    os.remove(companion)

            try:
                d = parent
                while d != output_folder and d.startswith(output_folder):
                    if not os.listdir(d):
                        os.rmdir(d)
                        d = os.path.dirname(d)
                    else:
                        break
            except OSError:
                pass

        except Exception as e:
            errors.append({"path": p, "error": str(e)})

    if deleted:
        media_cache = get_media_cache()
        media_cache["movies"]["items"] = []
        media_cache["movies"]["last_scan"] = 0
        media_cache["tv"]["items"] = []
        media_cache["tv"]["last_scan"] = 0

    return {"deleted": deleted, "errors": errors, "count": len(deleted)}


@router.get("/media/metadata/movie")
def api_movie_metadata(
    imdb_id: str = Query(default=None),
    tmdb_id: str = Query(default=None),
    title: str = Query(default=None),
    year: str = Query(default=None),
):
    """Fetch movie metadata from Radarr."""
    year_int = None
    if year:
        try:
            year_int = int(year)
        except ValueError:
            pass

    if not any([imdb_id, tmdb_id, title]):
        return JSONResponse({"error": "Must provide imdb_id, tmdb_id, or title"}, status_code=400)

    metadata = fetch_movie_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, title=title, year=year_int)
    if metadata:
        return metadata
    return JSONResponse({"error": "Metadata not found"}, status_code=404)


@router.get("/media/metadata/series")
def api_series_metadata(
    imdb_id: str = Query(default=None),
    tvdb_id: str = Query(default=None),
    tmdb_id: str = Query(default=None),
    title: str = Query(default=None),
):
    """Fetch TV series metadata from Sonarr."""
    tvdb_int = None
    tmdb_int = None
    if tvdb_id:
        try:
            tvdb_int = int(tvdb_id)
        except ValueError:
            pass
    if tmdb_id:
        try:
            tmdb_int = int(tmdb_id)
        except ValueError:
            pass

    if not any([imdb_id, tvdb_int, tmdb_int, title]):
        return JSONResponse({"error": "Must provide imdb_id, tvdb_id, tmdb_id, or title"}, status_code=400)

    metadata = fetch_series_metadata(imdb_id=imdb_id, tvdb_id=tvdb_int, tmdb_id=tmdb_int, title=title)
    if metadata:
        return metadata
    return JSONResponse({"error": "Metadata not found"}, status_code=404)


@router.get("/media/poster/{subpath:path}")
def api_media_poster(subpath: str, request: Request):
    """Serve poster.jpg from media folders."""
    s = request.app.state.settings
    if ".." in subpath:
        raise HTTPException(status_code=400)

    if subpath.startswith("watch/"):
        if not s.WATCH_FOLDER:
            raise HTTPException(status_code=404)
        subpath = subpath[6:]
        base_folder = Path(s.WATCH_FOLDER)
    elif subpath.startswith("temp/"):
        if not s.MEDIA_TEMP_FOLDER:
            raise HTTPException(status_code=404)
        subpath = subpath[5:]
        base_folder = Path(s.MEDIA_TEMP_FOLDER)
    else:
        base_folder = Path(s.OUTPUT_FOLDER)

    poster_path = base_folder / subpath

    if not poster_path.exists() or not poster_path.is_file():
        raise HTTPException(status_code=404)

    try:
        poster_path.resolve().relative_to(base_folder.resolve())
    except ValueError:
        raise HTTPException(status_code=403)

    return FileResponse(poster_path, media_type="image/jpeg")


@router.post("/media/ignore")
def api_media_ignore(data: dict = Body(default={})):
    """Add or remove a file from the ignore list (toggle)."""
    file_path = data.get("file_path")
    action = data.get("action", "toggle")

    if not file_path:
        return JSONResponse({"error": "file_path is required"}, status_code=400)

    try:
        in_database = is_ignored(file_path)
        has_sent = has_sentinel(file_path)
        currently_ignored = in_database or has_sent

        if action == "toggle":
            if currently_ignored:
                if in_database:
                    remove_ignored(file_path)
                sentinel_removed = remove_sentinel(file_path)
                return {
                    "status": "removed",
                    "ignored": False,
                    "sentinel_removed": sentinel_removed
                }
            else:
                reason = data.get("reason", "Manual ignore from UI")
                set_ignored(file_path, reason)
                return {"status": "added", "ignored": True}
        elif action == "add":
            if not in_database:
                reason = data.get("reason", "Manual ignore from UI")
                set_ignored(file_path, reason)
            return {"status": "added", "ignored": True}
        elif action == "remove":
            if in_database:
                remove_ignored(file_path)
            sentinel_removed = remove_sentinel(file_path)
            return {
                "status": "removed",
                "ignored": False,
                "sentinel_removed": sentinel_removed
            }
        else:
            return JSONResponse({"error": "Invalid action"}, status_code=400)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/media/ignored")
def api_media_ignored():
    """List all ignored files."""
    try:
        ignored = get_all_ignored()
        return {
            "items": ignored,
            "count": len(ignored),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/media/ignored/check")
def api_check_ignored(file_path: str = Query(default=None)):
    """Check if a specific file is ignored."""
    if not file_path:
        return JSONResponse({"error": "file_path is required"}, status_code=400)

    try:
        in_database = is_ignored(file_path)
        has_sent = has_sentinel(file_path)
        return {
            "file_path": file_path,
            "ignored": in_database or has_sent,
            "in_database": in_database,
            "has_sentinel": has_sent,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/media/enrich")
def api_enrich_single(data: dict = Body(default={})):
    """Enrich a single media file with metadata, NFO, and poster."""
    path = data.get("path")
    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    try:
        from transcodarr_core.enrich import enrich_media
        result = enrich_media(path)
        return {"ok": True, **result}
    except Exception as e:
        logging.error("[ENRICH] Failed to enrich %s: %s", path, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/media/enrich-all")
def api_enrich_all():
    """Start bulk enrichment of all movies/episodes missing NFOs."""
    if enrich_state["running"]:
        return JSONResponse({"error": "Enrichment already running", "status": enrich_state}, status_code=409)

    def _run_enrichment():
        from transcodarr_core.enrich import enrich_media
        from transcodarr_core.nfo import find_nfo_for_video

        enrich_state["running"] = True
        enrich_state["processed"] = 0
        enrich_state["nfo_written"] = 0
        enrich_state["posters_downloaded"] = 0
        enrich_state["errors"] = 0

        try:
            from transcodarr_core.config import Settings, get_media_paths
            _mp = get_media_paths(Settings())
            video_exts = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".webm"}

            to_enrich = []
            for root in (_mp["movies_output"], _mp["tv_output"]):
                root_p = Path(root)
                if not root_p.exists():
                    continue
                for vp in root_p.rglob("*"):
                    if vp.is_file() and vp.suffix.lower() in video_exts and not find_nfo_for_video(str(vp)):
                        to_enrich.append(str(vp))

            enrich_state["total"] = len(to_enrich)
            logging.info("[ENRICH] Starting bulk enrichment: %d files", len(to_enrich))

            for path in to_enrich:
                if not enrich_state["running"]:
                    logging.info("[ENRICH] Bulk enrichment cancelled")
                    break

                try:
                    result = enrich_media(path)
                    if result.get("nfo_written"):
                        enrich_state["nfo_written"] += 1
                    if result.get("poster_downloaded"):
                        enrich_state["posters_downloaded"] += 1
                except Exception as e:
                    logging.warning("[ENRICH] Failed to enrich %s: %s", path, e)
                    enrich_state["errors"] += 1

                enrich_state["processed"] += 1

                time.sleep(0.5)

            logging.info("[ENRICH] Bulk enrichment complete: %d/%d processed, %d NFOs, %d posters",
                         enrich_state["processed"], enrich_state["total"],
                         enrich_state["nfo_written"], enrich_state["posters_downloaded"])
        finally:
            enrich_state["running"] = False

    t = Thread(target=_run_enrichment, daemon=True)
    t.start()
    return {"ok": True, "status": "started"}


@router.get("/media/enrich-status")
def api_enrich_status():
    """Check progress of bulk enrichment."""
    return enrich_state


@router.post("/media/enrich-stop")
def api_enrich_stop():
    """Stop a running bulk enrichment."""
    if enrich_state["running"]:
        enrich_state["running"] = False
        return {"ok": True, "status": "stopping"}
    return {"ok": True, "status": "not_running"}
