# src/transcodarr_core/posters.py
import logging, requests, contextlib
from pathlib import Path
from .config import get_setting

def _get_json(url, headers=None, params=None, to=10):
    logging.debug("[POSTER] GET %s params=%s", url, params)
    r = requests.get(url, headers=headers or {}, params=params or {}, timeout=to)
    r.raise_for_status()
    return r.json()

def _get_img(url, to=12):
    logging.info("[POSTER] Downloading image: %s", url)
    r = requests.get(url, timeout=to)
    r.raise_for_status()
    if "image" not in r.headers.get("Content-Type",""):
        logging.debug("[POSTER] Non-image content-type: %s", r.headers.get("Content-Type"))
        return None
    return r.content

def ensure_poster(dest_dir: str, *, kind: str, meta: dict, filename="poster.jpg") -> str | None:
    out = Path(dest_dir) / filename
    if out.exists() and out.stat().st_size > 10_000:
        logging.info("[POSTER] Exists %s (skip)", out)
        return str(out)

    imdb_id  = (meta or {}).get("imdb_id")
    sonarr_id = (meta or {}).get("sonarr_series_id")
    radarr_id = (meta or {}).get("radarr_movie_id")

    logging.info("[POSTER] ensure kind=%s dir=%s imdb=%r sonarr_id=%r radarr_id=%r",
                 kind, dest_dir, imdb_id, sonarr_id, radarr_id)

    url = None

    # Sonarr / Radarr
    sonarr_url = get_setting("SONARR_URL")
    sonarr_key = get_setting("SONARR_API_KEY")
    if kind == "tv" and sonarr_url and sonarr_key and sonarr_id:
        try:
            js = _get_json(
                f"{sonarr_url.rstrip('/')}/api/v3/series/{sonarr_id}",
                headers={"X-Api-Key": sonarr_key}
            )
            for img in js.get("images") or []:
                if img.get("coverType") == "poster":
                    url = img.get("remoteUrl") or img.get("url")
                    logging.info("[POSTER] Sonarr poster: %s", url)
                    break
        except Exception as e:
            logging.debug("[POSTER] Sonarr failed: %s", e)

    radarr_url = get_setting("RADARR_URL")
    radarr_key = get_setting("RADARR_API_KEY")
    if kind == "movie" and radarr_url and radarr_key and radarr_id:
        try:
            js = _get_json(
                f"{radarr_url.rstrip('/')}/api/v3/movie/{radarr_id}",
                headers={"X-Api-Key": radarr_key}
            )
            for img in js.get("images") or []:
                if img.get("coverType") == "poster":
                    url = img.get("remoteUrl") or img.get("url")
                    logging.info("[POSTER] Radarr poster: %s", url)
                    break
        except Exception as e:
            logging.debug("[POSTER] Radarr failed: %s", e)

    # TMDB via imdb
    tmdb_key = get_setting("TMDB_API_KEY")
    if not url and tmdb_key and imdb_id:
        try:
            find = _get_json(
                f"https://api.themoviedb.org/3/find/{imdb_id}",
                params={"api_key": tmdb_key, "external_source": "imdb_id"}
            )
            key = "tv_results" if kind == "tv" else "movie_results"
            arr = find.get(key) or []
            if arr:
                tid = arr[0]["id"]
                imgs = _get_json(
                    f"https://api.themoviedb.org/3/{'tv' if kind=='tv' else 'movie'}/{tid}/images",
                    params={"api_key": tmdb_key, "include_image_language": "en,null"}
                )
                posters = imgs.get("posters") or []
                if posters:
                    url = f"https://image.tmdb.org/t/p/w342{posters[0]['file_path']}"
                    logging.info("[POSTER] TMDB poster: %s", url)
        except Exception as e:
            logging.debug("[POSTER] TMDB failed: %s", e)

    # OMDb fallback
    omdb_key = get_setting("OMDB_API_KEY")
    if not url and omdb_key and imdb_id:
        try:
            omdb = _get_json("https://www.omdbapi.com/", params={"apikey": omdb_key, "i": imdb_id})
            p = (omdb.get("Poster") or "").strip()
            if p and p.lower() != "n/a":
                url = p
                logging.info("[POSTER] OMDb poster: %s", url)
        except Exception as e:
            logging.debug("[POSTER] OMDb failed: %s", e)

    if not url:
        logging.warning("[POSTER] No poster URL found for dir=%s", dest_dir)
        return None

    data = _get_img(url)
    if not data:
        logging.warning("[POSTER] Failed to download: %s", url)
        return None

    out.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        out.write_bytes(data)
        logging.info("[POSTER] Wrote %s", out)
        return str(out)
    logging.warning("[POSTER] Write failed for %s", out)
    return None
