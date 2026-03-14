import os
import logging
import requests
from pathlib import Path
from typing import Optional, List, Dict
from transcodarr_core.config import Settings


settings = Settings()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _check_env():
    if not settings.RADARR_URL or not settings.RADARR_API_KEY:
        raise RuntimeError("settings.RADARR_URL and settings.RADARR_API_KEY must be set.")

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Api-Key": settings.RADARR_API_KEY})
    return s

def _normalize_path(p: str) -> str:
    # Make comparison robust across OS/mount styles
    # Use POSIX-like, trim trailing slashes, lower-case
    return Path(p).as_posix().rstrip("/").lower()

def _dir_of(path_like: str) -> str:
    p = Path(path_like)
    return str(p if p.is_dir() else p.parent)

def _remap_for_radarr(local_dir: str) -> str:
    """Translate local path to Radarr's view if settings.RADARR_PATH_FROM/TO are set."""
    if settings.RADARR_PATH_FROM and settings.RADARR_PATH_TO:
        try:
            local_posix = Path(local_dir).as_posix()
            if local_posix.startswith(settings.RADARR_PATH_FROM):
                return local_posix.replace(settings.RADARR_PATH_FROM, settings.RADARR_PATH_TO, 1)
        except Exception:
            pass
    return local_dir

def _get_all_movies() -> List[Dict]:
    _check_env()
    with _session() as s:
        r = s.get(f"{settings.RADARR_URL}/api/v3/movie", timeout=settings.RADARR_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

def _delete_movie(movie_id: int, *, delete_files: bool, add_import_exclusion: bool) -> None:
    _check_env()
    params = {
        "deleteFiles": str(bool(delete_files)).lower(),
        "addImportExclusion": str(bool(add_import_exclusion)).lower(),
    }
    with _session() as s:
        r = s.delete(f"{settings.RADARR_URL}/api/v3/movie/{movie_id}", params=params, timeout=settings.RADARR_TIMEOUT_S)
        # Radarr may return 200/202/204
        if r.status_code not in (200, 202, 204):
            r.raise_for_status()


# -------------------------------------------------------------------
# Public: update movie path
# -------------------------------------------------------------------
def update_movie_path(
    source_path: str,
    new_path: str,
    *,
    dry_run: bool = False,
) -> bool:
    """
    Update a movie's path in Radarr to point to a new location.

    - source_path: Current path to the movie file or folder (used to find the movie in Radarr)
    - new_path: New path where the movie now lives (output folder)
    - Returns True if successful
    """
    try:
        # Find the movie by its current path
        movie_dir_local = _dir_of(source_path)
        movie_dir_for_radarr = _remap_for_radarr(movie_dir_local)
        want = _normalize_path(movie_dir_for_radarr)

        logging.info(f"[RADARR] Looking for movie at: {movie_dir_for_radarr}")

        movies = _get_all_movies()
        target: Optional[Dict] = None
        for m in movies:
            radarr_path = m.get("path") or ""
            if _normalize_path(radarr_path) == want:
                target = m
                break

        if not target:
            logging.warning(f"[RADARR] No movie matched path '{movie_dir_for_radarr}'.")
            return False

        movie_id = target.get("id")
        title = target.get("title")
        old_path = target.get("path")

        # Calculate new path for Radarr (remap if needed, but for output)
        new_dir = _dir_of(new_path)
        # If there's path remapping, we need to apply it to output too
        # But typically output paths should be direct
        new_path_for_radarr = new_dir

        logging.info(f"[RADARR] Updating '{title}' (id={movie_id}) path: {old_path} -> {new_path_for_radarr}")

        if dry_run:
            logging.info(f"[RADARR] DRY RUN: would update path to {new_path_for_radarr}")
            return True

        # Update the movie object with new path
        target["path"] = new_path_for_radarr

        # PUT the updated movie back (moveFiles=false since we already moved it)
        _check_env()
        with _session() as s:
            r = s.put(
                f"{settings.RADARR_URL}/api/v3/movie/{movie_id}",
                json=target,
                params={"moveFiles": "false"},
                timeout=settings.RADARR_TIMEOUT_S
            )
            r.raise_for_status()

        logging.info(f"[RADARR] Successfully updated path for '{title}' to {new_path_for_radarr}")
        return True

    except Exception as e:
        logging.error(f"[RADARR] Update path failed: {e}")
        return False


# -------------------------------------------------------------------
# Public: delete by PATH
# -------------------------------------------------------------------
def delete_movie_by_path(
    path_to_movie_file_or_dir: str,
    *,
    delete_files: bool = True,
    add_import_exclusion: bool = False,
    dry_run: bool = False,
) -> bool:
    """
    Delete a Radarr movie by matching its movie folder path to the given path.

    - Provide either a file path (we'll use its parent) or a folder path.
    - If settings.RADARR_PATH_FROM/TO are set, the supplied path is remapped before matching.
    - Returns True if a matching movie was found (and deleted or would be deleted in dry_run).
    """
    try:
        movie_dir_local = _dir_of(path_to_movie_file_or_dir)
        movie_dir_for_radarr = _remap_for_radarr(movie_dir_local)

        want = _normalize_path(movie_dir_for_radarr)
        logging.info(f"[RADARR] Resolving by path: {movie_dir_local} -> {movie_dir_for_radarr}")

        movies = _get_all_movies()
        target: Optional[Dict] = None
        for m in movies:
            radarr_path = m.get("path") or ""
            if _normalize_path(radarr_path) == want:
                target = m
                break

        if not target:
            logging.warning(f"[RADARR] No movie matched path '{movie_dir_for_radarr}'.")
            return False

        movie_id = target.get("id")
        title = target.get("title")
        logging.info(f"[RADARR] Matched '{title}' (id={movie_id}) at '{target.get('path')}'.")

        if dry_run:
            logging.info(f"[RADARR] DRY RUN: would delete id={movie_id} deleteFiles={delete_files} importExcl={add_import_exclusion}")
            return True

        _delete_movie(movie_id, delete_files=delete_files, add_import_exclusion=add_import_exclusion)
        logging.info(f"[RADARR] Delete requested for '{title}' (id={movie_id}).")
        return True

    except Exception as e:
        logging.error(f"[RADARR] Delete by path failed for '{path_to_movie_file_or_dir}': {e}")
        return False

if __name__ == "__main__":
    #test: python3 -m transcodarr_core.radarr "/downloads/movies/I Know What You Did Last Summer (2025)/I Know What You Did Last Summer (2025).mp4" --dry-run

    import argparse, sys
    parser = argparse.ArgumentParser(description="Delete a Radarr movie by path.")
    parser.add_argument("path", help="Path to the movie file or its folder")
    parser.add_argument("--keep-files", action="store_true", help="Do NOT delete files from disk")
    parser.add_argument("--exclude", action="store_true", help="Add import exclusion in Radarr")
    parser.add_argument("--dry-run", action="store_true", help="Do not actually delete")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    ok = delete_movie_by_path(
        args.path,
        delete_files=not args.keep_files,
        add_import_exclusion=args.exclude,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)