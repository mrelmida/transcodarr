# web/routers/webhooks.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pathlib import Path
from datetime import datetime, timezone
import logging

from web.shared_state import remap_path, write_meta_json

router = APIRouter()


@router.post("/webhook/radarr")
async def webhook_radarr(request: Request):
    """Receive Radarr webhook (On Import / On Upgrade)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "MovieAdded", "Grab", "Test"):
        logging.debug("[WEBHOOK/RADARR] Ignoring event: %s", event_type)
        return {"status": "ignored", "event": event_type}

    if event_type == "Test":
        logging.info("[WEBHOOK/RADARR] Test event received successfully")
        return {"status": "ok", "message": "Test successful"}

    movie = payload.get("movie") or {}
    movie_file = payload.get("movieFile") or {}

    title = movie.get("title", "")
    year = movie.get("year")
    imdb_id = movie.get("imdbId")
    tmdb_id = movie.get("tmdbId")
    radarr_movie_id = movie.get("id")
    movie_path = movie.get("folderPath") or movie.get("path", "")
    file_path = movie_file.get("path", "")
    file_rel = movie_file.get("relativePath", "")

    s = request.app.state.settings
    path_from = getattr(s, "RADARR_PATH_FROM", "") or ""
    path_to = getattr(s, "RADARR_PATH_TO", "") or ""
    movie_path = remap_path(movie_path, path_from, path_to)
    file_path = remap_path(file_path, path_from, path_to)

    if not file_path and not movie_path:
        return JSONResponse({"error": "No file path in payload"}, status_code=400)

    meta = {
        "kind": "movie",
        "event_type": event_type,
        "title": title,
        "year": year,
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "radarr_movie_id": radarr_movie_id,
        "movie_path": movie_path,
        "file_path": file_path,
        "file_rel": file_rel,
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }

    if file_path:
        out_dir = Path(file_path).parent
        stem = Path(file_path).stem
    else:
        out_dir = Path(movie_path)
        stem = f"{title} ({year})" if year else title

    try:
        out_file = write_meta_json(out_dir, stem, meta)
        return {"status": "ok", "file": str(out_file)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/webhook/sonarr")
async def webhook_sonarr(request: Request):
    """Receive Sonarr webhook (On Import / On Upgrade)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "EpisodeFileDelete", "Grab", "Test"):
        logging.debug("[WEBHOOK/SONARR] Ignoring event: %s", event_type)
        return {"status": "ignored", "event": event_type}

    if event_type == "Test":
        logging.info("[WEBHOOK/SONARR] Test event received successfully")
        return {"status": "ok", "message": "Test successful"}

    series = payload.get("series") or {}
    episodes = payload.get("episodes") or []
    episode_file = payload.get("episodeFile") or {}

    series_title = series.get("title", "")
    series_path = series.get("path", "")
    tvdb_id = series.get("tvdbId")
    imdb_id = series.get("imdbId")
    sonarr_series_id = series.get("id")

    file_path = episode_file.get("path", "")
    file_rel = episode_file.get("relativePath", "")

    s = request.app.state.settings
    path_from = getattr(s, "SONARR_PATH_FROM", "") or ""
    path_to = getattr(s, "SONARR_PATH_TO", "") or ""
    series_path = remap_path(series_path, path_from, path_to)
    file_path = remap_path(file_path, path_from, path_to)

    if not file_path:
        return JSONResponse({"error": "No file path in payload"}, status_code=400)

    season_num = None
    ep_numbers = []
    ep_titles = []
    ep_imdb_ids = []
    ep_tvdb_ids = []
    ep_tmdb_ids = []

    for ep in episodes:
        if season_num is None:
            season_num = ep.get("seasonNumber")
        ep_numbers.append(ep.get("episodeNumber"))
        ep_titles.append(ep.get("title", ""))
        if ep.get("imdbId"):
            ep_imdb_ids.append(ep.get("imdbId"))
        if ep.get("tvdbId"):
            ep_tvdb_ids.append(ep.get("tvdbId"))
        if ep.get("tmdbId"):
            ep_tmdb_ids.append(ep.get("tmdbId"))

    first_title = ep_titles[0] if ep_titles else ""
    first_imdb = ep_imdb_ids[0] if ep_imdb_ids else None

    meta = {
        "kind": "episode",
        "event_type": event_type,
        "series": {
            "title": series_title,
            "path": series_path,
            "tvdb_id": tvdb_id,
            "imdb_id": imdb_id,
            "sonarr_series_id": sonarr_series_id,
        },
        "episode": {
            "season": season_num,
            "episodes": ep_numbers,
            "titles": ep_titles,
            "ids": {
                "imdb": ep_imdb_ids,
                "tvdb": ep_tvdb_ids,
                "tmdb": ep_tmdb_ids,
            },
            "first_imdb_id": first_imdb,
        },
        "file": {
            "path": file_path,
            "relative": file_rel,
        },
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }

    out_dir = Path(file_path).parent
    ep_str = f"S{season_num:02d}" if season_num is not None else "S00"
    for n in ep_numbers:
        if n is not None:
            ep_str += f"E{n:02d}"
    stem = f"{ep_str} - {first_title}" if first_title else ep_str

    try:
        out_file = write_meta_json(out_dir, stem, meta)
        return {"status": "ok", "file": str(out_file)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
