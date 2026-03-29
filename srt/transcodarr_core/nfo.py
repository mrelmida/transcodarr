# src/transcodarr_core/nfo.py
from __future__ import annotations
import os, json, logging, contextlib
from pathlib import Path
from typing import Optional, Any
from xml.etree import ElementTree as ET
from xml.dom import minidom

def _get_first(v, default=None):
    return (v[0] if isinstance(v, list) and v else default)

def _text(parent, tag, value):
    if value is None:
        return None
    el = ET.SubElement(parent, tag)
    el.text = str(value)
    return el

def _uniq(parent: ET.Element, type_: str, value: str, default: bool=False):
    if not value: return None
    el = ET.SubElement(parent, "uniqueid")
    el.set("type", type_)
    if default: el.set("default", "true")
    el.text = value
    return el

def _pretty(elem: ET.Element) -> bytes:
    rough = ET.tostring(elem, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")


def _xml_esc(s: Optional[str]) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

def write_tvshow_nfo(dest_dir: str, series_meta: dict) -> str | None:
    """
    Write /<series_root>/tvshow.nfo once with strong identifiers.
    series_meta expects keys like: title, imdb_id, tvdb_id
    """
    out = Path(dest_dir) / "tvshow.nfo"

    title    = (series_meta or {}).get("title")
    imdb_id  = (series_meta or {}).get("imdb_id")
    tvdb_id  = (series_meta or {}).get("tvdb_id")

    logging.info("[NFO] write_tvshow_nfo dir=%s title=%r imdb=%r tvdb=%r",
                 dest_dir, title, imdb_id, tvdb_id)

    # If we have absolutely nothing, don’t write a blank file
    if not any([title, imdb_id, tvdb_id]):
        logging.warning("[NFO] No series fields available; skipping tvshow.nfo")
        return None

    root = ET.Element("tvshow")
    _text(root, "title", title)

    ids = ET.SubElement(root, "ids")
    if imdb_id:
        ET.SubElement(ids, "imdb").text = imdb_id
    if tvdb_id:
        ET.SubElement(ids, "tvdb").text = str(tvdb_id)

    out.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    with contextlib.suppress(Exception):
        tree.write(out, encoding="utf-8", xml_declaration=True)
        logging.info("[NFO] Wrote %s", out)
        return str(out)
    logging.warning("[NFO] Failed to write %s", out)
    return None

def write_tvshow_nfo_if_missing(series_dir: str, *, title: str | None, imdb_id: str | None = None,
                                tvdb_id: int | None = None, tmdb_id: int | None = None) -> None:
    """
    Create tvshow.nfo exactly once (no overwrite). Minimal, precise IDs for Jellyfin.
    """
    try:
        sd = Path(series_dir)
        sd.mkdir(parents=True, exist_ok=True)
        nfo = sd / "tvshow.nfo"
        if nfo.exists():
            return

        lines = ["<?xml version='1.0' encoding='utf-8'?>", "<tvshow>"]
        if title:
            lines.append(f"  <title>{_xml_esc(title)}</title>")
        if imdb_id:
            lines.append(f"  <imdbid>{_xml_esc(imdb_id)}</imdbid>")
        if tvdb_id:
            lines.append(f"  <tvdbid>{int(tvdb_id)}</tvdbid>")
        if tmdb_id:
            lines.append(f"  <tmdbid>{int(tmdb_id)}</tmdbid>")
        lines.append("</tvshow>\n")

        nfo.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        # deliberately silent — never block the pipeline if NFO write fails
        pass

def _el_text(parent: ET.Element, tag: str) -> Optional[str]:
    """Get text content of a child element, or None."""
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _el_int(parent: ET.Element, tag: str) -> Optional[int]:
    """Get integer content of a child element, or None."""
    t = _el_text(parent, tag)
    if t is None:
        return None
    try:
        return int(t)
    except (ValueError, TypeError):
        return None


def find_nfo_for_video(video_path: str) -> Optional[str]:
    """Find <stem>.nfo next to the video file."""
    nfo = Path(video_path).with_suffix(".nfo")
    return str(nfo) if nfo.exists() else None


def find_tvshow_nfo(video_path: str) -> Optional[str]:
    """Find tvshow.nfo in parent or grandparent directory of video."""
    p = Path(video_path).parent
    for d in (p, p.parent):
        candidate = d / "tvshow.nfo"
        if candidate.exists():
            return str(candidate)
    return None


def read_nfo_as_meta(nfo_path: str) -> dict:
    """
    Parse an NFO XML file into a dict compatible with load_unified_meta() output.
    Handles <movie>, <episodedetails>, and <tvshow> root elements.
    Returns empty dict on failure.
    """
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
    except Exception as e:
        logging.warning("[NFO-READ] Failed to parse %s: %s", nfo_path, e)
        return {}

    tag = root.tag.lower()

    # Collect uniqueid elements into a dict keyed by type
    uids = {}
    for uid in root.findall("uniqueid"):
        uid_type = (uid.get("type") or "").strip().lower()
        uid_val = (uid.text or "").strip()
        if uid_type and uid_val:
            uids[uid_type] = uid_val

    if tag == "movie":
        title = _el_text(root, "title")
        year = _el_int(root, "year")
        imdb_id = uids.get("imdb")
        tmdb_id = uids.get("tmdb")
        tvdb_id = uids.get("tvdb")
        return {
            "kind": "movie",
            "title": title,
            "year": year,
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "best_imdb_id": imdb_id,
        }

    if tag == "episodedetails":
        showtitle = _el_text(root, "showtitle")
        season = _el_int(root, "season")
        episode = _el_int(root, "episode")
        ep_imdb = uids.get("imdb")
        series_imdb = uids.get("imdb:series")
        series_tvdb = uids.get("tvdb:series")
        return {
            "kind": "episode",
            "series_title": showtitle,
            "season": season,
            "episodes": [episode] if episode is not None else [],
            "imdb_id": ep_imdb,
            "series_imdb_id": series_imdb,
            "best_imdb_id": series_imdb or ep_imdb,
            "series_tvdb_id": series_tvdb,
        }

    if tag == "tvshow":
        title = _el_text(root, "title")
        # IDs from <ids> block
        ids_el = root.find("ids")
        imdb_id = None
        tvdb_id = None
        if ids_el is not None:
            imdb_id = _el_text(ids_el, "imdb")
            tvdb_id = _el_text(ids_el, "tvdb")
        # Also check <imdbid>/<tvdbid> (write_tvshow_nfo_if_missing format)
        if not imdb_id:
            imdb_id = _el_text(root, "imdbid")
        if not tvdb_id:
            tvdb_id = _el_text(root, "tvdbid")
        # Also check uniqueid elements
        if not imdb_id:
            imdb_id = uids.get("imdb")
        if not tvdb_id:
            tvdb_id = uids.get("tvdb")
        return {
            "kind": "tvshow",
            "series_title": title,
            "series_imdb_id": imdb_id,
            "series_tvdb_id": tvdb_id,
            "best_imdb_id": imdb_id,
        }

    logging.info("[NFO-READ] Unrecognized root element <%s> in %s", tag, nfo_path)
    return {}


def write_nfo_from_meta(meta_path: str, out_video_path: str, *, episode_plot: str = None, movie_plot: str = None) -> Optional[str]:
    """
    Read sidecar meta from *processing* tree and write an .nfo next to *out_video_path*.
    Optionally accepts episode_plot or movie_plot to embed a <plot> element.
    Returns the NFO path, or None if skipped.
    """
    try:
        if not (meta_path and os.path.exists(meta_path)):
            logging.info("[NFO] No meta at %s", meta_path); return None
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            meta = json.load(f)

        series = meta.get("series")  or {}
        ep     = meta.get("episode") or {}
        movie  = meta.get("movie")   or {}

        # Infer kind if missing: if has series/episode data, it's TV; otherwise movie
        kind = (meta.get("kind") or "").lower().strip()
        if not kind:
            kind = "episode" if (series or ep) else "movie"

        nfo_path = str(Path(out_video_path).with_suffix(".nfo"))

        if kind == "episode":
            root = ET.Element("episodedetails")
            _text(root, "showtitle", series.get("title"))
            _text(root, "season", ep.get("season"))

            # Handle multi-episode files
            ep_numbers = ep.get("episodes") or []
            ep_titles = ep.get("titles") or []

            if len(ep_numbers) > 1:
                # Multi-episode: write first episode and episodenumberend for range
                _text(root, "episode", ep_numbers[0])
                _text(root, "episodenumberend", ep_numbers[-1])
                # Also write all episode numbers individually (some media servers prefer this)
                for ep_num in ep_numbers:
                    _text(root, "episodenum", ep_num)
                # Combined title
                _text(root, "title", " + ".join(ep_titles) if ep_titles else None)
                # Store individual titles for reference
                if ep_titles:
                    titles_el = ET.SubElement(root, "titles")
                    for t in ep_titles:
                        _text(titles_el, "title", t)
            else:
                # Single episode
                _text(root, "episode", _get_first(ep_numbers))
                _text(root, "title", _get_first(ep_titles))

            if episode_plot:
                _text(root, "plot", episode_plot)

            ids = ep.get("ids") or {}
            ep_imdb = _get_first(ids.get("imdb"))
            ep_tvdb = _get_first(ids.get("tvdb"))
            ep_tmdb = _get_first(ids.get("tmdb"))

            if ep_imdb: _uniq(root, "imdb", ep_imdb, default=True)
            if ep_tvdb: _uniq(root, "tvdb", str(ep_tvdb))
            if ep_tmdb: _uniq(root, "tmdb", str(ep_tmdb))

            # series-level hints (won’t override episode ids)
            s_imdb = series.get("imdb_id")
            s_tvdb = series.get("tvdb_id")
            s_tmdb = series.get("tmdb_id")
            if s_imdb and s_imdb != ep_imdb: _uniq(root, "imdb:series", s_imdb)
            if s_tvdb and s_tvdb != ep_tvdb: _uniq(root, "tvdb:series", str(s_tvdb))
            if s_tmdb and s_tmdb != ep_tmdb: _uniq(root, "tmdb:series", str(s_tmdb))

        elif kind == "movie":
            root = ET.Element("movie")
            title = movie.get("title") or meta.get("title")
            year  = movie.get("year")  or meta.get("year")
            imdb  = movie.get("imdb_id") or meta.get("imdb_id")
            tmdb  = movie.get("tmdb_id") or meta.get("tmdb_id")
            tvdb  = movie.get("tvdb_id") or meta.get("tvdb_id")

            _text(root, "title", title)
            if year: _text(root, "year", year)
            if movie_plot:
                _text(root, "plot", movie_plot)
            if imdb: _uniq(root, "imdb", imdb, default=True)
            if tmdb: _uniq(root, "tmdb", str(tmdb))
            if tvdb: _uniq(root, "tvdb", str(tvdb))
        else:
            logging.info("[NFO] Unknown kind in %s; skipping.", meta_path); return None

        with open(nfo_path, "wb") as f:
            f.write(_pretty(root))
        logging.info("[NFO] Wrote %s", nfo_path)
        return nfo_path
    except Exception as e:
        logging.warning("[NFO] Failed for %s -> %s: %s", meta_path, out_video_path, e)
        return None
