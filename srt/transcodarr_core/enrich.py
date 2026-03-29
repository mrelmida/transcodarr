# srt/transcodarr_core/enrich.py
"""
Unified enrichment: given a media file path, fetch metadata, write NFO, and
download a poster.  Used by both single-item and bulk-enrich API endpoints.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Dict, Any

from .meta import load_unified_meta, find_meta_json
from .metadata import fetch_movie_metadata, fetch_series_metadata, fetch_episode_metadata
from .nfo import find_nfo_for_video, write_nfo_from_meta, write_tvshow_nfo_if_missing
from .posters import ensure_poster


def enrich_media(video_path: str) -> Dict[str, Any]:
    """
    Enrich a single media file:
      1. Load sidecar .meta.json
      2. Fetch metadata from Radarr/Sonarr (fills cache)
      3. Fetch per-episode plot from Sonarr for TV episodes
      4. Write .nfo next to the video (with plot)
      5. Download poster into the media folder

    Returns dict with keys: nfo_written (bool), poster_downloaded (bool),
    plus any extra info for the caller.
    """
    result: Dict[str, Any] = {"nfo_written": False, "poster_downloaded": False}
    p = Path(video_path)

    if not p.exists():
        logging.warning("[ENRICH] File not found: %s", video_path)
        return result

    # --- 1. Load sidecar meta ---
    meta = load_unified_meta(video_path)
    kind = meta.get("kind", "movie")

    # --- 2. Fetch rich metadata from Radarr / Sonarr ---
    episode_plot = None
    movie_plot = None

    if kind == "episode":
        series_imdb = meta.get("series_imdb_id")
        series_tvdb = meta.get("series_tvdb_id")
        series_title = meta.get("series_title")
        if series_imdb or series_tvdb or series_title:
            try:
                fetch_series_metadata(
                    imdb_id=series_imdb,
                    tvdb_id=int(series_tvdb) if series_tvdb else None,
                    title=series_title,
                )
            except Exception as e:
                logging.debug("[ENRICH] Series metadata fetch failed: %s", e)

        # --- 3. Fetch per-episode plot from Sonarr ---
        sonarr_series_id = meta.get("sonarr_series_id")
        season = meta.get("season")
        episodes = meta.get("episodes")
        if sonarr_series_id and season is not None and episodes:
            ep_num = episodes[0]  # primary episode number
            try:
                ep_meta = fetch_episode_metadata(int(sonarr_series_id), int(season), int(ep_num))
                if ep_meta and ep_meta.get("overview"):
                    episode_plot = ep_meta["overview"]
                    logging.info("[ENRICH] Got episode plot for S%02dE%02d: %s...",
                                 int(season), int(ep_num), episode_plot[:80])
            except Exception as e:
                logging.debug("[ENRICH] Episode metadata fetch failed: %s", e)
    else:
        imdb_id = meta.get("imdb_id") or meta.get("best_imdb_id")
        tmdb_id = meta.get("tmdb_id")
        title = meta.get("title")
        year = meta.get("year")
        if imdb_id or tmdb_id or title:
            try:
                movie_meta = fetch_movie_metadata(
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    title=title,
                    year=year,
                )
                if movie_meta and movie_meta.get("description"):
                    movie_plot = movie_meta["description"]
            except Exception as e:
                logging.debug("[ENRICH] Movie metadata fetch failed: %s", e)

    # --- 4. Write NFO ---
    meta_json_path = find_meta_json(video_path)
    if meta_json_path and not find_nfo_for_video(video_path):
        nfo_path = write_nfo_from_meta(
            str(meta_json_path), video_path,
            episode_plot=episode_plot,
            movie_plot=movie_plot,
        )
        if nfo_path:
            result["nfo_written"] = True
            logging.info("[ENRICH] Wrote NFO: %s", nfo_path)

        # For TV episodes, also ensure tvshow.nfo in the series directory
        if kind == "episode":
            series_dir = p.parent.parent  # typically Season X -> Series root
            write_tvshow_nfo_if_missing(
                str(series_dir),
                title=meta.get("series_title"),
                imdb_id=meta.get("series_imdb_id"),
                tvdb_id=int(meta.get("series_tvdb_id")) if meta.get("series_tvdb_id") else None,
            )

    # --- 5. Download poster ---
    poster_kind = "tv" if kind == "episode" else "movie"
    poster_dir = str(p.parent.parent) if kind == "episode" else str(p.parent)
    poster_path = ensure_poster(poster_dir, kind=poster_kind, meta=meta)
    if poster_path:
        result["poster_downloaded"] = True

    return result
