from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List, Union


# ---------------------------------------------------------------------------
# Episode code extraction for matching
# ---------------------------------------------------------------------------
_EP_CODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")

def _extract_ep_code(filename: str) -> Optional[Tuple[int, int]]:
    """Extract (season, episode) from a filename like S03E04."""
    m = _EP_CODE_RE.search(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# ---------------------------------------------------------------------------
# Sidecar discovery
# ---------------------------------------------------------------------------
def find_meta_json(video_path: str) -> Optional[Path]:
    """
    Prefer <stem>.meta.json next to the file.
    If not found, try to match by episode code (SxxExx) for TV episodes.
    Falls back to first *.meta.json only for non-episode files.
    """
    p = Path(video_path)
    exact = p.with_suffix(".meta.json")
    if exact.exists():
        return exact

    alts = sorted(p.parent.glob("*.meta.json"))
    if not alts:
        return None

    # Try to match by episode code for TV episodes
    video_ep = _extract_ep_code(p.name)
    if video_ep:
        for alt in alts:
            alt_ep = _extract_ep_code(alt.name)
            if alt_ep == video_ep:
                logging.debug("[META] Matched %s to %s by episode code %s", p.name, alt.name, video_ep)
                return alt
        # No episode match found - don't return wrong episode's meta
        logging.warning("[META] No meta.json found matching episode %s for %s", f"S{video_ep[0]:02d}E{video_ep[1]:02d}", p.name)
        return None

    # Non-TV content: fall back to first meta.json
    return alts[0]


# ---------------------------------------------------------------------------
# IMDb loader (movie OR TV). Returns a single best IMDb id if available.
# ---------------------------------------------------------------------------
def _coerce_imdb(tt: Optional[Union[str, int]]) -> Optional[str]:
    if tt is None:
        return None
    s = str(tt).strip()
    if not s:
        return None
    if not s.startswith("tt"):
        # make a best-effort 'ttNNNNNNN'
        try:
            s = "tt" + str(int(s)).rjust(7, "0")
        except Exception:
            return None
    return s


def _load_imdb_from_meta(meta_path: Path) -> Optional[str]:
    """
    Back-compat helper used by fetch.py.
    Order of preference:
      1) episode.first_imdb_id
      2) series.imdb_id
      3) imdb_id (movie)
    """
    try:
        data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    except Exception as e:
        logging.warning("[META] Failed to read meta json %s: %s", meta_path, e)
        return None

    # 1) episode.first_imdb_id
    ep_first = None
    try:
        ep_first = (((data.get("episode") or {}).get("first_imdb_id")) or None)
    except Exception:
        pass
    imdb = _coerce_imdb(ep_first)

    # 2) series.imdb_id
    if not imdb:
        imdb = _coerce_imdb((data.get("series") or {}).get("imdb_id"))

    # 3) top-level imdb_id (movies)
    if not imdb:
        imdb = _coerce_imdb(data.get("imdb_id"))

    # Optional friendly log
    if imdb:
        t = data.get("title") or (data.get("series") or {}).get("title")
        y = data.get("year")
        tmdb = data.get("tmdb_id") or (data.get("series") or {}).get("tmdb_id")
        logging.info("[META] title=%r year=%r imdb_id=%r tmdb_id=%r path=%s", t, y, imdb, tmdb, meta_path)

    return imdb


# ---------------------------------------------------------------------------
# Unified meta (used by pipeline & helpers)
# ---------------------------------------------------------------------------
UNIFIED_META_SUFFIX = ".meta.json"


def find_unified_meta(video_path: str) -> Optional[str]:
    """
    Look for a sidecar *.meta.json in the same folder as the media.
    Prefer a filename sharing the base stem.
    If not found, try to match by episode code (SxxExx) for TV episodes.
    Falls back to first *.meta.json only for non-episode files.
    """
    p = Path(video_path)
    folder = p.parent
    exact = folder / (p.stem + UNIFIED_META_SUFFIX)
    if exact.exists():
        return str(exact)

    alts = sorted(folder.glob(f"*{UNIFIED_META_SUFFIX}"))
    if not alts:
        return None

    # Try to match by episode code for TV episodes
    video_ep = _extract_ep_code(p.name)
    if video_ep:
        for alt in alts:
            alt_ep = _extract_ep_code(alt.name)
            if alt_ep == video_ep:
                logging.debug("[META] Matched %s to %s by episode code %s", p.name, alt.name, video_ep)
                return str(alt)
        # No episode match found - don't return wrong episode's meta
        logging.warning("[META] No meta.json found matching episode %s for %s", f"S{video_ep[0]:02d}E{video_ep[1]:02d}", p.name)
        return None

    # Non-TV content: fall back to first meta.json
    return str(alts[0])


def _normalize_episode_ids(ids: Dict[str, Any] | None) -> Tuple[List[str], List[int], List[Union[int, str]]]:
    """
    Returns (imdb_ids, tvdb_ids, tmdb_ids) with sane types.
    - imdb_ids: list[str] like ["tt1234567"]
    - tvdb_ids: list[int]
    - tmdb_ids: list[int|str] (Sonarr may return int or empty)
    """
    if not isinstance(ids, dict):
        return ([], [], [])

    imdb_raw = ids.get("imdb") or []
    tvdb_raw = ids.get("tvdb") or []
    tmdb_raw = ids.get("tmdb") or []

    imdb_ids: List[str] = []
    for v in imdb_raw:
        tt = _coerce_imdb(v)
        if tt:
            imdb_ids.append(tt)

    tvdb_ids: List[int] = []
    for v in tvdb_raw:
        try:
            n = int(v)
            if n > 0:
                tvdb_ids.append(n)
        except Exception:
            continue

    tmdb_ids: List[Union[int, str]] = []
    for v in tmdb_raw:
        if v in (None, ""):
            continue
        try:
            tmdb_ids.append(int(v))
        except Exception:
            tmdb_ids.append(str(v))

    return (imdb_ids, tvdb_ids, tmdb_ids)


def load_unified_meta(video_path: str) -> Dict[str, Any]:
    """
    Returns a normalized dict. Handles both movie + TV shapes created by your hooks.

    Keys (present when known):
      kind: "movie" | "episode"

      # Movie-style:
      imdb_id, tmdb_id, radarr_movie_id, title, year

      # TV-style:
      series_title, series_tvdb_id, series_imdb_id, sonarr_series_id
      season, episodes (list[int]), titles (list[str]), first_title
      ep_imdb_ids (list[str]), ep_tvdb_ids (list[int]), ep_tmdb_ids (list[int|str])
      file_path, file_relative
    """
    meta_path = find_unified_meta(video_path)
    out: Dict[str, Any] = {}
    if not meta_path:
        return out

    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except Exception as e:
        logging.debug("[META] Could not parse meta at %s: %s", meta_path, e)
        return out

    # Kind inference
    kind = (raw.get("kind") or ("episode" if "episode" in raw or "series" in raw else "movie")).lower()
    out["kind"] = kind

    # Common / movie fields
    out["title"] = raw.get("title")  # movie hook
    out["year"] = raw.get("year")
    out["imdb_id"] = _coerce_imdb(raw.get("imdb_id"))
    out["tmdb_id"] = raw.get("tmdb_id")
    out["radarr_movie_id"] = raw.get("radarr_movie_id")

    # TV fields
    series = raw.get("series") or {}
    episode = raw.get("episode") or {}
    file_block = raw.get("file") or {}

    if series:
        out["series_title"] = series.get("title")
        out["series_tvdb_id"] = series.get("tvdb_id")
        out["series_imdb_id"] = _coerce_imdb(series.get("imdb_id"))
        out["sonarr_series_id"] = series.get("sonarr_series_id")

    if episode:
        # season & episode numbers
        out["season"] = episode.get("season")
        eps = episode.get("episodes")
        if isinstance(eps, list):
            out["episodes"] = [int(e) for e in eps if isinstance(e, int) or (isinstance(e, str) and e.isdigit())]
        else:
            out["episodes"] = None

        # titles array (new hook) + first title
        titles = episode.get("titles")
        if isinstance(titles, list):
            out["titles"] = [t for t in titles if isinstance(t, str)]
            out["first_title"] = out["titles"][0] if out["titles"] else None
        else:
            # back-compat: some older hooks wrote a single "title"
            single = episode.get("title")
            out["titles"] = [single] if isinstance(single, str) and single else []
            out["first_title"] = single if isinstance(single, str) else None

        # episode ids block → normalized lists
        ep_imdb_ids, ep_tvdb_ids, ep_tmdb_ids = _normalize_episode_ids(episode.get("ids"))
        out["ep_imdb_ids"] = ep_imdb_ids
        out["ep_tvdb_ids"] = ep_tvdb_ids
        out["ep_tmdb_ids"] = ep_tmdb_ids

        # convenience: first_imdb_id if present
        out["first_imdb_id"] = _coerce_imdb(episode.get("first_imdb_id")) or (ep_imdb_ids[0] if ep_imdb_ids else None)

    # file info written by hook
    out["file_path"] = file_block.get("path")
    out["file_relative"] = file_block.get("relative")

    # Best overall IMDb for this asset (movie or TV)
    # Prefer the episode-first IMDb, else series-level, else movie-level already set above
    best_imdb = (
        out.get("first_imdb_id")
        or out.get("series_imdb_id")
        or out.get("imdb_id")
    )
    out["best_imdb_id"] = best_imdb

    return out