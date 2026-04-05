# src/transcodarr_core/sonarr.py
from __future__ import annotations
import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Iterable
import requests
import re
from .config import Settings, get_media_paths

settings = Settings()

# Detect SxxEyy-like tokens and typical video files
_EP_CODE_RE = re.compile(r"[Ss]\d{1,2}[ ._-]?[Ee]\d{1,3}")
_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov"}

# Cached remap prefixes: (sonarr_prefix, local_prefix) or None
_remap_cache: Optional[tuple[str, str]] = None
_remap_detected: bool = False

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _check_env():
    if not settings.SONARR_URL or not settings.SONARR_API_KEY:
        raise RuntimeError("settings.SONARR_URL and settings.SONARR_API_KEY must be set.")

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-Api-Key": settings.SONARR_API_KEY})
    return s

def _normalize_path(p: str) -> str:
    return Path(p).as_posix().rstrip("/").lower()

def _dir_of(path_like: str) -> str:
    p = Path(path_like)
    return str(p if p.is_dir() else p.parent)

def _detect_remap() -> Optional[tuple[str, str]]:
    """Auto-detect path remap by comparing a Sonarr series path to our local watch path.
    Returns (sonarr_prefix, local_prefix) or None if no remap needed."""
    global _remap_cache, _remap_detected
    if _remap_detected:
        return _remap_cache
    _remap_detected = True

    # Manual override still works if set
    if settings.SONARR_PATH_FROM and settings.SONARR_PATH_TO:
        _remap_cache = (settings.SONARR_PATH_FROM, settings.SONARR_PATH_TO)
        logging.info("[SONARR] Using manual path remap: %s <-> %s", settings.SONARR_PATH_FROM, settings.SONARR_PATH_TO)
        return _remap_cache

    try:
        _check_env()
        mp = get_media_paths()
        local_prefix = mp["tv_watch"].rstrip("/")
        if not local_prefix:
            return None

        # Get series from Sonarr to detect its path prefix
        series_list = _get_all_series()
        if not series_list:
            logging.debug("[SONARR] No series in Sonarr, cannot auto-detect remap")
            return None

        # Find a series whose folder name matches one in our watch path
        local_folders = set()
        if os.path.isdir(local_prefix):
            local_folders = {d.lower() for d in os.listdir(local_prefix) if os.path.isdir(os.path.join(local_prefix, d))}

        for s in series_list:
            sonarr_path = (s.get("path") or "").rstrip("/")
            if not sonarr_path:
                continue
            sonarr_folder = Path(sonarr_path).name.lower()
            if sonarr_folder in local_folders:
                sonarr_prefix = str(Path(sonarr_path).parent)
                if _normalize_path(sonarr_prefix) == _normalize_path(local_prefix):
                    logging.info("[SONARR] Paths match, no remap needed")
                    return None
                _remap_cache = (sonarr_prefix, local_prefix)
                logging.info("[SONARR] Auto-detected path remap: Sonarr=%s <-> Local=%s", sonarr_prefix, local_prefix)
                return _remap_cache

        logging.debug("[SONARR] Could not auto-detect remap (no matching folders)")
    except Exception as e:
        logging.debug("[SONARR] Auto-detect remap failed: %s", e)

    return None

def _remap_for_sonarr(local_path: str) -> str:
    """Translate local (container) path to Sonarr's view using auto-detected remap."""
    remap = _detect_remap()
    if remap:
        sonarr_prefix, local_prefix = remap
        posix = Path(local_path).as_posix()
        if posix.startswith(local_prefix):
            return posix.replace(local_prefix, sonarr_prefix, 1)
    return local_path

def remap_from_sonarr(sonarr_path: str) -> str:
    """Translate Sonarr path to local (container) path using auto-detected remap."""
    remap = _detect_remap()
    if remap:
        sonarr_prefix, local_prefix = remap
        if sonarr_path.startswith(sonarr_prefix):
            return sonarr_path.replace(sonarr_prefix, local_prefix, 1)
    return sonarr_path

def _get_all_series() -> List[Dict]:
    _check_env()
    with _session() as s:
        r = s.get(f"{settings.SONARR_URL}/api/v3/series", timeout=settings.SONARR_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

def _get_episodefiles(series_id: int) -> List[Dict]:
    _check_env()
    with _session() as s:
        r = s.get(f"{settings.SONARR_URL}/api/v3/episodefile?seriesId={series_id}", timeout=settings.SONARR_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

def _delete_episodefile(file_id: int, *, delete_files: bool) -> None:
    """
    Sonarr: DELETE /episodefile/{id}?deleteFiles=true|false
    Removes the EpisodeFile record, and (optionally) deletes the file from disk.
    """
    _check_env()
    params = {"deleteFiles": str(bool(delete_files)).lower()}
    with _session() as s:
        r = s.delete(f"{settings.SONARR_URL}/api/v3/episodefile/{file_id}", params=params, timeout=settings.SONARR_TIMEOUT_S)
        if r.status_code not in (200, 202, 204):
            r.raise_for_status()

# -------------------------------------------------------------------
# Public: get series status for path update decision
# -------------------------------------------------------------------
def get_series_status(source_path: str) -> dict | None:
    """
    Get series status for deciding whether to update path.

    Returns dict with:
      - series_id: int
      - title: str
      - ended: bool (True if series status is "ended")
      - episode_file_count: int (number of tracked episode files)

    Returns None if series not found.
    """
    try:
        path_for_sonarr = _remap_for_sonarr(source_path)
        source_normalized = _normalize_path(path_for_sonarr)

        series_list = _get_all_series()

        # Find series by source path
        target = None
        for s in series_list:
            series_path = s.get("path") or ""
            if not series_path:
                continue
            if source_normalized.startswith(_normalize_path(series_path)):
                target = s
                break

        if not target:
            return None

        series_id = target.get("id")
        episode_files = _get_episodefiles(series_id)

        return {
            "series_id": series_id,
            "title": target.get("title"),
            "ended": target.get("status", "").lower() == "ended",
            "episode_file_count": len(episode_files),
        }
    except Exception as e:
        logging.error(f"[SONARR] get_series_status failed: {e}")
        return None


# -------------------------------------------------------------------
# Public: update series path
# -------------------------------------------------------------------
def update_series_path(
    source_path: str,
    new_path: str,
    *,
    dry_run: bool = False,
) -> bool:
    """
    Update a series' root path in Sonarr to point to a new location.

    - source_path: Current path to an episode file or series folder (used to find the series)
    - new_path: New path where the series now lives (output folder)
    - Returns True if successful (or if path already updated)
    """
    try:
        # Calculate expected new series root path from new_path
        # Output structure: /output/tv/Show Name/Season X/episode.mp4
        new_episode_path = Path(new_path)
        if "season" in new_episode_path.parent.name.lower():
            new_series_path = str(new_episode_path.parent.parent)
        else:
            # Fallback: assume structure is /output/tv/Show/episode
            new_series_path = str(new_episode_path.parent)

        # Remap source path to Sonarr's view
        path_for_sonarr = _remap_for_sonarr(source_path)
        source_normalized = _normalize_path(path_for_sonarr)
        new_normalized = _normalize_path(new_series_path)

        logging.info(f"[SONARR] Looking for series (source: {path_for_sonarr}, target: {new_series_path})")

        series_list = _get_all_series()

        # Find the series by checking both source path AND output path
        # (series may already have been updated by a previous episode)
        target = None
        found_by = None
        for s in series_list:
            series_path = s.get("path") or ""
            if not series_path:
                continue
            series_normalized = _normalize_path(series_path)

            # Check if source path is under this series' folder
            if source_normalized.startswith(series_normalized):
                target = s
                found_by = "source"
                break

            # Check if output path matches this series' folder (already updated)
            if series_normalized == new_normalized or new_normalized.startswith(series_normalized):
                target = s
                found_by = "output"
                break

        if not target:
            logging.warning(f"[SONARR] No series matched source '{path_for_sonarr}' or output '{new_series_path}'.")
            return False

        series_id = target.get("id")
        title = target.get("title")
        current_path = target.get("path")

        # Check if already updated to the output path
        if _normalize_path(current_path) == new_normalized:
            logging.info(f"[SONARR] Series '{title}' path already set to output: {current_path}")
            return True

        logging.info(f"[SONARR] Updating '{title}' (id={series_id}) path: {current_path} -> {new_series_path}")

        if dry_run:
            logging.info(f"[SONARR] DRY RUN: would update path to {new_series_path}")
            return True

        # Update the series object with new path
        target["path"] = new_series_path

        # PUT the updated series back (moveFiles=false since we already moved it)
        _check_env()
        with _session() as s:
            r = s.put(
                f"{settings.SONARR_URL}/api/v3/series/{series_id}",
                json=target,
                params={"moveFiles": "false"},
                timeout=settings.SONARR_TIMEOUT_S
            )
            r.raise_for_status()

        logging.info(f"[SONARR] Successfully updated path for '{title}' to {new_series_path}")
        return True

    except Exception as e:
        logging.error(f"[SONARR] Update series path failed: {e}")
        return False


# -------------------------------------------------------------------
# Public: delete by FILE PATH
# -------------------------------------------------------------------
def delete_episode_by_path(
    path_to_episode_file_or_dir: str,
    *,
    delete_files: bool = True,
    dry_run: bool = False,
    allow_dir: bool = False,  # NEW: mass-delete only if you say so
) -> bool:
    """
    Delete Sonarr episode-file record(s) by matching their path(s).

    SAFETY:
      - By default, this function deletes a SINGLE episode matching the exact file path,
        even if the given path happens to be a directory on this container or remapped host.
      - To enable directory-wide cleanup, set allow_dir=True explicitly.

    Matching rules:
      - We first remap the given path using SONARR_PATH_FROM/TO.
      - If the *string* of the given path looks like a single episode file
        (has SxxEyy OR a typical video extension), we use exact-file matching.
      - Else, if allow_dir=True, delete all episodefiles whose paths lie under the directory.
      - Else, we refuse to do a directory delete.
    """
    try:
        # Normalize original and remap to Sonarr’s perspective
        local = Path(path_to_episode_file_or_dir)
        path_for_sonarr = _remap_for_sonarr(str(local))
        want = _normalize_path(path_for_sonarr)

        # ---- Decide "file" vs "dir" mode SAFE-LY, independent of container FS ----
        # Heuristic: treat as SINGLE FILE if the string looks like an episode file
        # (contains SxxEyy or ends with a known video extension).
        looks_like_file = bool(_EP_CODE_RE.search(Path(path_for_sonarr).name)) or \
                          (Path(path_for_sonarr).suffix.lower() in _VIDEO_EXTS)

        # Fall back to filesystem checks if heuristic didn’t trigger (e.g., movies or odd names)
        if not looks_like_file:
            try:
                # Only trust the container FS type if path actually exists
                if local.exists():
                    looks_like_file = local.is_file()
                    is_dir_like = local.is_dir()
                else:
                    is_dir_like = False
            except Exception:
                is_dir_like = False
        else:
            is_dir_like = False

        # If the caller wants a directory cleanup, they must opt in.
        if is_dir_like and not allow_dir:
            logging.warning("[SONARR] Refusing directory-wide delete without allow_dir=True: %s", path_for_sonarr)
            return False

        logging.info("[SONARR] Resolving by path: %s -> %s (mode=%s)",
                     local, path_for_sonarr, "file" if looks_like_file else ("dir" if allow_dir else "file"))

        series_list = _get_all_series()

        # Narrow to series whose path prefixes the target (best-effort)
        candidates: List[Dict] = []
        for s in series_list:
            sp = (s.get("path") or "")
            if not sp:
                continue
            if want.startswith(_normalize_path(sp)):
                candidates.append(s)
        if not candidates:
            candidates = series_list  # fallback

        matched_any = False
        for s in candidates:
            series_id = s.get("id")
            series_title = s.get("title")
            files = _get_episodefiles(series_id)

            for f in files:
                fpath = f.get("path") or ""
                if not fpath:
                    continue
                n_f = _normalize_path(fpath)

                if looks_like_file:
                    # Exact file match only
                    if n_f != want:
                        continue
                else:
                    # Directory cleanup only if explicitly allowed
                    if not allow_dir:
                        continue
                    # delete if the episode file is under the directory
                    if not n_f.startswith(want + "/"):
                        continue

                matched_any = True
                file_id = f.get("id")
                tag = f"{series_title} :: {Path(fpath).name}"
                logging.info("[SONARR] Matched episodefile id=%s path='%s' (%s)", file_id, fpath, tag)

                if dry_run:
                    logging.info("[SONARR] DRY RUN: would delete id=%s deleteFiles=%s", file_id, delete_files)
                    continue

                try:
                    _delete_episodefile(file_id, delete_files=delete_files)
                    logging.info("[SONARR] Delete requested for episodefile id=%s (%s).", file_id, tag)
                except Exception as e:
                    logging.error("[SONARR] Delete failed for episodefile id=%s (%s): %s", file_id, tag, e)

        if not matched_any:
            logging.warning("[SONARR] No episodefiles matched path '%s'.", path_for_sonarr)
            return False

        return True

    except Exception as e:
        logging.error("[SONARR] Delete by path failed for '%s': %s", path_to_episode_file_or_dir, e)
        return False

if __name__ == "__main__":
    # test: python3 -m transcodarr_core.sonarr "/downloads/_processing/tv/Family Guy/Season 1" --dry-run
    import argparse, sys
    parser = argparse.ArgumentParser(description="Delete Sonarr episodefile(s) by path.")
    parser.add_argument("path", help="Episode file path OR a directory to clean up")
    parser.add_argument("--keep-files", action="store_true", help="Do NOT delete files from disk")
    parser.add_argument("--dry-run", action="store_true", help="Do not actually delete")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    ok = delete_episode_by_path(
        args.path,
        delete_files=not args.keep_files,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)