from __future__ import annotations
import json, time, random, logging, contextlib, os, threading, re
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from babelfish import Language
from subliminal.cache import region
from subliminal import scan_video, list_subtitles, download_subtitles
from dogpile.cache.exception import RegionNotConfigured
from subliminal.providers.opensubtitlescom import (
    OpenSubtitlesComProvider,
    OpenSubtitlesComError,
    ServiceUnavailable,
    DownloadLimitReached,
)
# If you kept these, we’ll retain them (TV enrichment remains optional)
from subliminal.refiners.omdb import refine as omdb_refine
from subliminal.refiners.tvdb import refine as tvdb_refine
from subliminal.providers.opensubtitlescom import NoSession
from subliminal.subtitle import Subtitle

import requests
from requests import Response

from ..meta import find_meta_json, _load_imdb_from_meta, load_unified_meta
from ..config import Settings, get_setting

_settings = Settings()  # reads env or .env once
SUBLIMINAL_OSCOM_USER = _settings.SUBLIMINAL_OSCOM_USER
SUBLIMINAL_OSCOM_PASS = _settings.SUBLIMINAL_OSCOM_PASS


# ---------------------------------------------------------------------------
# Monkey-patch: fix podnapisi provider crashing on malformed API entries.
# Upstream bug: PodnapisiProvider.query() uses bare data['id'],
# data['movie']['type'], etc. — a single entry missing any key kills
# the entire provider (returns 0 results).  This replaces the inner
# loop with per-entry try/except so bad entries are skipped.
# ---------------------------------------------------------------------------
def _patch_podnapisi_provider():
    try:
        from subliminal.providers.podnapisi import PodnapisiProvider, PodnapisiSubtitle
        from subliminal.exceptions import NotInitializedProviderError
    except ImportError:
        return  # podnapisi not installed

    # Configurable limits
    _MAX_PAGES = int(os.getenv("PODNAPISI_MAX_PAGES", "8"))
    _MAX_RESULTS = int(os.getenv("PODNAPISI_MAX_RESULTS", "25"))
    _EARLY_STOP = int(os.getenv("PODNAPISI_EARLY_STOP", "5"))

    def _ep_from_slug(slug):
        """Extract (season, episode) from slug like 'en-breaking-bad-2008-S01E02-...'"""
        m = re.search(r'S(\d+)E(\d+)', slug or "", re.IGNORECASE)
        return (int(m.group(1)), int(m.group(2))) if m else (None, None)

    def _ep_from_releases(releases):
        """Extract (season, episode) from release names like 'Breaking.Bad.S01E01.720p...'"""
        for rel in (releases or []):
            m = re.search(r'S(\d+)E(\d+)', rel or "", re.IGNORECASE)
            if m:
                return (int(m.group(1)), int(m.group(2)))
        return (None, None)

    def _episode_matches(s_val, e_val, season, episode, slug, releases):
        """Check if entry matches target episode using metadata, slug, release names."""
        if s_val is not None and e_val is not None:
            return s_val == season and e_val == episode
        s_slug, e_slug = _ep_from_slug(slug)
        if s_slug is not None and e_slug is not None:
            return s_slug == season and e_slug == episode
        s_rel, e_rel = _ep_from_releases(releases)
        if s_rel is not None and e_rel is not None:
            return s_rel == season and e_rel == episode
        # Can't determine episode — reject (season packs, untagged entries)
        return False

    def _entry_matches(data, language, is_episode, season, episode, year):
        """Check if a raw API entry matches our target. Returns True if it passes all filters."""
        movie = data.get("movie") or {}
        if is_episode and movie.get("type") == "movie":
            return False
        # Language filter — API ignores the language param, returns all languages
        try:
            lang_parsed = Language.fromietf(data.get("language", ""))
        except Exception:
            return False
        if lang_parsed != language:
            return False
        # Episode filter
        if is_episode:
            ep_info = movie.get("episode_info") or {}
            slug = data.get("slug") or ""
            releases = (data.get("releases") or []) + (data.get("custom_releases") or [])
            try:
                s_val = int(ep_info["season"]) if "season" in ep_info else None
                e_val = int(ep_info["episode"]) if "episode" in ep_info else None
            except (ValueError, TypeError):
                s_val, e_val = None, None
            if not _episode_matches(s_val, e_val, season, episode, slug, releases):
                return False
        # Year filter for movies
        if not is_episode and year:
            try:
                yr = int(movie["year"]) if "year" in movie else None
            except (ValueError, TypeError):
                yr = None
            if yr and yr != year:
                return False
        return True

    def _patched_query(self, language, keyword, *, season=None, episode=None, year=None):
        if self.session is None:
            raise NotInitializedProviderError

        is_episode = season is not None and episode is not None

        # Single clean query — just the title.
        # Podnapisi API ignores S01E01, seasons, episodes, and language params,
        # so we search broadly and do all filtering client-side.
        if is_episode:
            params = {
                "keywords": keyword,
                "language": str(language),
                "movie_type": ["tv-series", "mini-series"],
            }
        else:
            params = {"keywords": keyword, "language": str(language), "movie_type": "movie"}
            if year:
                params["year"] = year

        logging.info("[PODNAPISI] Searching: %s%s (lang=%s, max_pages=%d)",
                     keyword,
                     f" S{int(season):02d}E{int(episode):02d}" if is_episode else
                     f" ({year})" if year else "",
                     language, _MAX_PAGES)

        # Paginate with early-stop: stop fetching pages once we have enough matches
        pids = set()
        all_entries = []
        matched = []
        page_count = 0

        while True:
            _throttle_podnapisi()
            for _attempt in range(4):
                r = self.session.get(
                    self.server_url + "/search/advanced",
                    params=params,
                    timeout=self.timeout,
                )
                if r.status_code == 429:
                    wait = 2.0 * (2 ** _attempt) * random.uniform(0.8, 1.2)
                    logging.warning("[PODNAPISI] 429 rate-limited, backing off %.1fs (%d/4)",
                                    wait, _attempt + 1)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                break
            else:
                logging.warning("[PODNAPISI] Still 429 after 4 retries — marking exhausted")
                _mark_provider_exhausted("podnapisi")
                break

            result = json.loads(r.text)
            page_count += 1
            page_new = 0

            for data in result.get("data", []):
                pid = data.get("id") or data.get("publish_id") or data.get("slug")
                if not pid or pid in pids:
                    continue
                pids.add(pid)
                all_entries.append(data)
                page_new += 1
                if _entry_matches(data, language, is_episode, season, episode, year):
                    matched.append(data)

            logging.info("[PODNAPISI] Page %s/%s: +%d entries, %d matched so far",
                         result.get("page", page_count), result.get("all_pages", "?"),
                         page_new, len(matched))

            # Early stop: enough good matches found
            if len(matched) >= _EARLY_STOP:
                logging.info("[PODNAPISI] Early stop: %d matches >= %d threshold",
                             len(matched), _EARLY_STOP)
                break

            try:
                if int(result["page"]) >= int(result["all_pages"]):
                    break
                if page_count >= _MAX_PAGES:
                    break
                params["page"] = int(result["page"]) + 1
            except (KeyError, ValueError):
                break

        # Build subtitle objects from matched entries
        subtitles = []
        for data in matched:
            movie = data.get("movie") or {}
            ep_info = movie.get("episode_info") or {}
            slug = data.get("slug") or ""
            releases = (data.get("releases") or []) + (data.get("custom_releases") or [])

            try:
                lang_parsed = Language.fromietf(data["language"])
            except Exception:
                continue

            try:
                s_val = int(ep_info["season"]) if "season" in ep_info else None
                e_val = int(ep_info["episode"]) if "episode" in ep_info else None
            except (ValueError, TypeError):
                s_val, e_val = None, None

            try:
                yr = int(movie["year"]) if "year" in movie else None
            except (ValueError, TypeError):
                yr = None

            pid = data.get("id") or data.get("publish_id") or data.get("slug")
            hearing_impaired = "hearing_impaired" in (data.get("flags") or [])

            # Backfill season/episode from slug/releases if metadata missing
            if is_episode and s_val is None:
                s_slug, e_slug = _ep_from_slug(slug)
                if s_slug is None:
                    s_slug, e_slug = _ep_from_releases(releases)
                s_val = s_slug or season
                e_val = e_slug or episode

            subtitle = self.subtitle_class(
                language=lang_parsed,
                subtitle_id=pid,
                hearing_impaired=hearing_impaired,
                page_link=data.get("url"),
                releases=releases,
                title=movie.get("title"),
                season=s_val,
                episode=e_val,
                year=yr,
            )
            subtitles.append(subtitle)
            if len(subtitles) >= _MAX_RESULTS:
                break

        logging.info("[PODNAPISI] Returning %d subtitles (%d raw, %d pages, %d matched)",
                     len(subtitles), len(all_entries), page_count, len(matched))
        return subtitles

    PodnapisiProvider.query = _patched_query
    logging.info("[PODNAPISI] Monkey-patched query() with single-query deep pagination")

_patch_podnapisi_provider()


# ---------------------------------------------------------------------------
# Provider availability logging (runs at module load)
# ---------------------------------------------------------------------------
def _log_available_providers():
    """Log which Subliminal providers are available at startup."""
    try:
        from subliminal.extensions import provider_manager
        available = [p.name for p in provider_manager]
        logging.info("[PROVIDERS] Subliminal available: %s", available)
    except Exception as e:
        logging.warning("[PROVIDERS] Could not list Subliminal providers: %s", e)


_log_available_providers()  # Run at import time


# ---------------------------------------------------------------------------
# Round-robin account rotation for OpenSubtitles.com
# ---------------------------------------------------------------------------
def _load_oscom_accounts() -> List[Dict[str, str]]:
    """
    Load OpenSubtitles.com accounts from config.
    Supports both legacy single account and new multi-account JSON list.
    Returns list of {"user": ..., "pass": ...} dicts.
    """
    accounts = []

    # Try parsing SUBLIMINAL_OSCOM_ACCOUNTS as JSON list first
    accounts_json = _settings.SUBLIMINAL_OSCOM_ACCOUNTS
    if accounts_json:
        try:
            parsed = json.loads(accounts_json)
            if isinstance(parsed, list):
                for acc in parsed:
                    if isinstance(acc, dict) and acc.get("user") and acc.get("pass"):
                        accounts.append({"user": acc["user"], "pass": acc["pass"]})
                if accounts:
                    logging.info("[OSCOM] Loaded %d accounts for round-robin rotation", len(accounts))
        except json.JSONDecodeError as e:
            logging.warning("[OSCOM] Failed to parse SUBLIMINAL_OSCOM_ACCOUNTS JSON: %s", e)

    # Fallback to legacy single account if no accounts loaded
    if not accounts and SUBLIMINAL_OSCOM_USER and SUBLIMINAL_OSCOM_PASS:
        accounts.append({"user": SUBLIMINAL_OSCOM_USER, "pass": SUBLIMINAL_OSCOM_PASS})
        logging.info("[OSCOM] Using single legacy account")

    return accounts

_OSCOM_ACCOUNTS = _load_oscom_accounts()
_OSCOM_ACCOUNT_INDEX = 0
_OSCOM_ACCOUNT_LOCK = threading.Lock()


def _get_next_oscom_account() -> Optional[Dict[str, str]]:
    """
    Get the next account in round-robin rotation.
    Thread-safe; rotates index on each call.
    """
    global _OSCOM_ACCOUNT_INDEX

    if not _OSCOM_ACCOUNTS:
        return None

    with _OSCOM_ACCOUNT_LOCK:
        account = _OSCOM_ACCOUNTS[_OSCOM_ACCOUNT_INDEX]
        _OSCOM_ACCOUNT_INDEX = (_OSCOM_ACCOUNT_INDEX + 1) % len(_OSCOM_ACCOUNTS)
        logging.info("[OSCOM] Using account %s (index %d/%d)",
                     account["user"], _OSCOM_ACCOUNT_INDEX, len(_OSCOM_ACCOUNTS))
        return account


def _create_oscom_provider(max_result_pages: int = 0) -> Optional[OpenSubtitlesComProvider]:
    """
    Create an OpenSubtitlesComProvider with the next account in rotation.
    Returns None if no accounts are configured.

    :param max_result_pages: Limit pagination (0 = unlimited, 1 = first page only)
    """
    account = _get_next_oscom_account()
    if not account:
        logging.warning("[OSCOM] No OpenSubtitles.com accounts configured")
        return None

    provider = OpenSubtitlesComProvider(
        username=account["user"],
        password=account["pass"],
        max_result_pages=max_result_pages,
    )
    provider.initialize()
    return provider


# ---------------------------------------------------------------------------
# Multi-provider support with fallback
# ---------------------------------------------------------------------------
def _load_addic7ed_accounts() -> List[Dict[str, str]]:
    """Load Addic7ed accounts from config (same format as OSCOM)."""
    accounts = []
    accounts_json = _settings.SUBLIMINAL_ADDIC7ED_ACCOUNTS
    # Only parse if we have actual content (not empty string)
    if accounts_json and accounts_json.strip():
        try:
            parsed = json.loads(accounts_json)
            if isinstance(parsed, list):
                for acc in parsed:
                    if isinstance(acc, dict) and acc.get("user") and acc.get("pass"):
                        accounts.append({"user": acc["user"], "pass": acc["pass"]})
                if accounts:
                    logging.info("[ADDIC7ED] Loaded %d accounts", len(accounts))
        except json.JSONDecodeError as e:
            logging.warning("[ADDIC7ED] Failed to parse accounts JSON: %s", e)
    return accounts

_ADDIC7ED_ACCOUNTS = _load_addic7ed_accounts()
_ADDIC7ED_ACCOUNT_INDEX = 0
_ADDIC7ED_ACCOUNT_LOCK = threading.Lock()


def _get_next_addic7ed_account() -> Optional[Dict[str, str]]:
    """Get the next Addic7ed account in round-robin rotation."""
    global _ADDIC7ED_ACCOUNT_INDEX

    if not _ADDIC7ED_ACCOUNTS:
        return None

    with _ADDIC7ED_ACCOUNT_LOCK:
        account = _ADDIC7ED_ACCOUNTS[_ADDIC7ED_ACCOUNT_INDEX]
        _ADDIC7ED_ACCOUNT_INDEX = (_ADDIC7ED_ACCOUNT_INDEX + 1) % len(_ADDIC7ED_ACCOUNTS)
        logging.info("[ADDIC7ED] Using account %s (index %d/%d)",
                     account["user"], _ADDIC7ED_ACCOUNT_INDEX, len(_ADDIC7ED_ACCOUNTS))
        return account


def _build_provider_configs() -> dict:
    """Build provider_configs dict for subliminal's list_subtitles/ProviderPool."""
    configs = {}
    # Addic7ed: rotate to next account
    acc = _get_next_addic7ed_account()
    if acc:
        configs["addic7ed"] = {"username": acc["user"], "password": acc["pass"]}
    return configs


def _is_provider_enabled(setting_name: str) -> bool:
    """Check if a provider is enabled via its DB-backed enabled flag."""
    val = (getattr(_settings, setting_name, None) or "").strip().lower()
    return val in ("true", "1", "yes")


def _is_oscom_enabled() -> bool:
    val = (getattr(_settings, "SUBLIMINAL_OSCOM_ENABLED", None) or "").strip().lower()
    if not val:
        # Migration: if toggle was never set but accounts exist, default enabled
        return bool(_OSCOM_ACCOUNTS)
    return val in ("true", "1", "yes")


def _is_podnapisi_enabled() -> bool:
    return _is_provider_enabled("SUBLIMINAL_PODNAPISI_ENABLED")


def _is_addic7ed_enabled() -> bool:
    val = (getattr(_settings, "SUBLIMINAL_ADDIC7ED_ENABLED", None) or "").strip().lower()
    if not val:
        # Migration: if toggle was never set but accounts exist, default enabled
        return bool(_ADDIC7ED_ACCOUNTS)
    return val in ("true", "1", "yes")


def _is_tvsubtitles_enabled() -> bool:
    return _is_provider_enabled("SUBLIMINAL_TVSUBTITLES_ENABLED")


def _get_provider_order() -> List[str]:
    """
    Get the order of subtitle providers to try.
    Each provider must be both configured (accounts/etc.) AND enabled (toggle on).
    """
    order_str = _settings.SUBLIMINAL_PROVIDER_ORDER
    if order_str:
        # Manual order — still filter by enabled
        enabled = set(_get_enabled_providers())
        return [p.strip().lower() for p in order_str.split(",")
                if p.strip() and p.strip().lower() in enabled]

    return _get_enabled_providers()


def _get_enabled_providers() -> List[str]:
    """Get list of provider names that are both configured AND enabled."""
    providers = []
    if _OSCOM_ACCOUNTS and _is_oscom_enabled():
        providers.append("opensubtitlescom")
    if _is_podnapisi_enabled():
        providers.append("podnapisi")
    if _ADDIC7ED_ACCOUNTS and _is_addic7ed_enabled():
        providers.append("addic7ed")
    if _is_tvsubtitles_enabled():
        providers.append("tvsubtitles")
    return providers


# Track which providers have been exhausted — with timed cooldowns.
# Maps provider_name → expiry timestamp (time.time() + cooldown_seconds).
# A provider is "exhausted" until its cooldown expires.
_EXHAUSTED_PROVIDERS: Dict[str, float] = {}
_EXHAUSTED_LOCK = threading.Lock()

# Default cooldown per provider (seconds).  OS.com resets daily; podnapisi
# rate-limits are shorter but aggressive — 15 min is a safe default.
_PROVIDER_COOLDOWNS: Dict[str, float] = {
    "opensubtitlescom": float(os.getenv("OSCOM_COOLDOWN_SEC", "3600")),      # 1 hour
    "podnapisi":        float(os.getenv("PODNAPISI_COOLDOWN_SEC", "900")),    # 15 min
    "addic7ed":         float(os.getenv("ADDIC7ED_COOLDOWN_SEC", "1800")),    # 30 min
    "tvsubtitles":      float(os.getenv("TVSUBTITLES_COOLDOWN_SEC", "900")),   # 15 min
}
_DEFAULT_COOLDOWN = 900.0  # fallback for unknown providers


def _mark_provider_exhausted(provider_name: str):
    """Mark a provider as exhausted with a timed cooldown."""
    cooldown = _PROVIDER_COOLDOWNS.get(provider_name, _DEFAULT_COOLDOWN)
    with _EXHAUSTED_LOCK:
        _EXHAUSTED_PROVIDERS[provider_name] = time.time() + cooldown
        logging.warning("[PROVIDERS] Marked %s as exhausted — cooldown %.0fs", provider_name, cooldown)


def _is_provider_exhausted(provider_name: str) -> bool:
    """Check if a provider is still in its cooldown window."""
    with _EXHAUSTED_LOCK:
        expiry = _EXHAUSTED_PROVIDERS.get(provider_name)
        if expiry is None:
            return False
        if time.time() >= expiry:
            # Cooldown expired — remove and allow
            del _EXHAUSTED_PROVIDERS[provider_name]
            logging.info("[PROVIDERS] %s cooldown expired, re-enabling", provider_name)
            return False
        remaining = expiry - time.time()
        logging.debug("[PROVIDERS] %s still cooling down (%.0fs left)", provider_name, remaining)
        return True


def _reset_exhausted_providers():
    """Remove only expired cooldowns (called at start of each fetch session)."""
    now = time.time()
    with _EXHAUSTED_LOCK:
        expired = [p for p, exp in _EXHAUSTED_PROVIDERS.items() if now >= exp]
        for p in expired:
            del _EXHAUSTED_PROVIDERS[p]
            logging.info("[PROVIDERS] %s cooldown expired during reset, re-enabling", p)
        still_cooling = {p: f"{exp - now:.0f}s left" for p, exp in _EXHAUSTED_PROVIDERS.items()}
        if still_cooling:
            logging.info("[PROVIDERS] Still cooling down: %s", still_cooling)


def _get_next_provider() -> Optional[str]:
    """
    Get the next non-exhausted provider in priority order.
    Returns None if all providers are exhausted.
    """
    order = _get_provider_order()
    for provider in order:
        if not _is_provider_exhausted(provider):
            return provider
    logging.warning("[PROVIDERS] All providers exhausted")
    return None


_OS_RATE_LOCK = threading.Lock()
_LAST_OS_CALL  = 0.0
MIN_OS_GAP_SEC = float(os.getenv("OSCOM_MIN_GAP_SEC", "0.8"))   # 0.8–1.2s is reasonable
MAX_BACKOFF    = float(os.getenv("OSCOM_MAX_BACKOFF", "8.0"))   # cap backoff

def _throttle_opensubtitles():
    global _LAST_OS_CALL
    with _OS_RATE_LOCK:
        now = time.time()
        wait = _LAST_OS_CALL + MIN_OS_GAP_SEC - now
        if wait > 0:
            time.sleep(wait)
        _LAST_OS_CALL = time.time()


_POD_RATE_LOCK = threading.Lock()
_LAST_POD_CALL = 0.0
MIN_POD_GAP_SEC = float(os.getenv("PODNAPISI_MIN_GAP_SEC", "2.0"))  # podnapisi rate-limits aggressively

def _throttle_podnapisi():
    global _LAST_POD_CALL
    with _POD_RATE_LOCK:
        now = time.time()
        wait = _LAST_POD_CALL + MIN_POD_GAP_SEC - now
        if wait > 0:
            time.sleep(wait)
        _LAST_POD_CALL = time.time()

def _jitter(base: float) -> float:
    # +/- 20% jitter to avoid thundering herd
    return base * random.uniform(0.8, 1.2)


# ---------------------------------------------------------------------------
# Cache + provider helpers
# ---------------------------------------------------------------------------
def enrich_episode_ids(video_path: str) -> Optional[str]:
    """
    Ensure episode-level IDs are present in the sidecar .meta.json for TV episodes.
    Writes back episode IMDb (and keeps series IDs) if discovered. Returns episode imdb_id or None.
    """
    meta_path = find_meta_json(video_path)
    if not meta_path:
        return None

    # Load TV context from meta
    series_title, season, episodes, series_imdb = _load_tv_from_meta(str(meta_path))
    if not (series_title and season and episodes):
        return None

    ep_no = int(episodes[0])

    # Existing IDs (if any)
    ep_imdb_from_meta, ep_tvdb_from_meta, series_tvdb_id, _first_title = _load_episode_ids_and_title(str(meta_path))
    if ep_imdb_from_meta:
        # already good; still normalize/write series IDs if missing
        _write_back_episode_ids(meta_path, ep_imdb=ep_imdb_from_meta,
                                series_imdb=series_imdb, series_tvdb=series_tvdb_id)
        return ep_imdb_from_meta

    # Try TVDB v4 first
    episode_imdb_id = None
    token = _tvdb_login()
    if token:
        if ep_tvdb_from_meta:
            episode_imdb_id = _tvdb_episode_imdb_by_episode_id(token, ep_tvdb_from_meta)
        if not episode_imdb_id and series_tvdb_id and season and ep_no:
            episode_imdb_id = _tvdb_episode_imdb_by_series_season_ep(token, series_tvdb_id, int(season), ep_no)

    # OMDb fallback
    if not episode_imdb_id:
        episode_imdb_id = _omdb_episode_imdb(series_imdb, season, ep_no)

    if episode_imdb_id:
        _write_back_episode_ids(meta_path,
                                ep_imdb=episode_imdb_id,
                                series_imdb=series_imdb,
                                series_tvdb=series_tvdb_id)
        logging.info("[ENRICH] Saved episode imdb_id=%s into %s", episode_imdb_id, os.path.basename(meta_path))
        return episode_imdb_id

    logging.info("[ENRICH] No episode imdb_id found for %s S%02dE%02d", series_title, int(season), ep_no)
    return None

def _write_back_episode_ids(meta_path: str,
                            *,
                            ep_imdb: Optional[str] = None,
                            ep_tvdb: Optional[int] = None,
                            series_imdb: Optional[str] = None,
                            series_tvdb: Optional[int] = None) -> None:
    """
    Merge newly discovered IDs into the sidecar meta JSON.
    Preserves existing fields; keeps ids as lists in episode.ids.
    """
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except Exception:
        raw = {}

    series = raw.get("series") or {}
    episode = raw.get("episode") or {}
    ids = episode.get("ids") or {}

    # --- normalize & merge episode IDs ---
    if ep_imdb:
        # normalize to 'tt########' form
        tt = _coerce_tt(ep_imdb)
        if tt:
            imdb_list = ids.get("imdb")
            if not isinstance(imdb_list, list):
                imdb_list = []
            if tt not in imdb_list:
                imdb_list.insert(0, tt)
            ids["imdb"] = imdb_list

    if isinstance(ep_tvdb, int) and ep_tvdb > 0:
        tvdb_list = ids.get("tvdb")
        if not isinstance(tvdb_list, list):
            tvdb_list = []
        if ep_tvdb not in tvdb_list:
            tvdb_list.insert(0, ep_tvdb)
        ids["tvdb"] = tvdb_list

    # write back ids block
    if ids:
        episode["ids"] = ids
        raw["episode"] = episode

    # --- optionally update series IDs (single values) ---
    if series_imdb:
        stt = _coerce_tt(series_imdb)
        if stt:
            series["imdb_id"] = stt
    if isinstance(series_tvdb, int) and series_tvdb > 0:
        series["tvdb_id"] = series_tvdb
    if series:
        raw["series"] = series

    # atomic-ish write
    try:
        tmp = f"{meta_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_path)
        logging.info("[META] Updated %s with episode/series IDs.", os.path.basename(meta_path))
    except Exception as e:
        logging.warning("[META] Failed to update %s: %s", meta_path, e)

def _rotate_to_next_account() -> Optional[OpenSubtitlesComProvider]:
    """
    Create a fresh provider with the next account in rotation.
    Used when current account hits NoSession/rate limits.
    Returns None if no more accounts available or rotation fails.
    """
    if len(_OSCOM_ACCOUNTS) <= 1:
        logging.warning("[OSCOM] Only one account configured, cannot rotate")
        return None

    logging.info("[OSCOM] Rotating to next account due to session/rate limit issues")
    new_provider = _create_oscom_provider()
    if new_provider and _login_with_retry(new_provider):
        return new_provider
    return None


def _download_with_session(provider, sub, max_relogins: int = 2, allow_rotation: bool = True):
    """
    Download subtitle with session handling and optional account rotation.
    Returns tuple (success: bool, provider: OpenSubtitlesComProvider) - provider may be
    rotated to a new account if NoSession persists.
    """
    current_provider = provider
    just_relogged = False  # Track if we just did a successful re-login

    for i in range(max_relogins + 1):
        try:
            _throttle_opensubtitles()
            current_provider.download_subtitle(sub)
            return True, current_provider
        except NoSession:
            logging.info("[OS] session expired; re-login and retry id=%s (attempt %d/%d)",
                        getattr(sub, "id", "?"), i + 1, max_relogins + 1)

            # If we just re-logged successfully but still got NoSession, this account
            # is likely rate-limited/quota exhausted - rotate instead of retrying login
            if just_relogged and allow_rotation:
                logging.info("[OSCOM] NoSession after successful re-login - account likely quota exhausted")
                new_provider = _rotate_to_next_account()
                if new_provider:
                    logging.info("[OSCOM] Switched to new account, retrying download")
                    current_provider = new_provider
                    just_relogged = False
                    continue
                else:
                    logging.warning("[OSCOM] No more accounts to rotate to for id=%s", getattr(sub, "id", "?"))
                    return False, current_provider

            if _login_with_retry(current_provider):
                just_relogged = True
                continue  # Retry with same provider after re-login

            # Re-login failed - try rotating to next account
            if allow_rotation:
                new_provider = _rotate_to_next_account()
                if new_provider:
                    logging.info("[OSCOM] Switched to new account, retrying download")
                    current_provider = new_provider
                    just_relogged = False
                    continue

            logging.warning("[OSCOM] All re-login attempts failed for id=%s", getattr(sub, "id", "?"))
            return False, current_provider
        except DownloadLimitReached as e:
            just_relogged = False
            logging.warning("[OSCOM] Download limit reached for current account: %s", e)
            # Try rotating to next account
            if allow_rotation:
                new_provider = _rotate_to_next_account()
                if new_provider:
                    logging.info("[OSCOM] Switched to new account after quota hit, retrying download")
                    current_provider = new_provider
                    continue
            # No rotation possible or failed - re-raise so caller can handle
            raise
        except OpenSubtitlesComError as e:
            just_relogged = False
            # If rate-limited here, back off briefly and retry once
            if "Too Many Requests" in str(e):
                time.sleep(_jitter(2.0))
                continue
            logging.warning("[SUBPICK] OS.com download error id=%s: %s", getattr(sub, "id", "?"), e)
            return False, current_provider
        except ServiceUnavailable as e:
            just_relogged = False
            time.sleep(_jitter(2.0))
    return False, current_provider


def _ensure_cache_region():
    try:
        _ = region.backend
    except RegionNotConfigured:
        region.configure("dogpile.cache.memory", expiration_time=24 * 60 * 60)


def _login_with_retry(provider, max_tries: int = 4) -> bool:
    delay = 1.5
    for attempt in range(1, max_tries + 1):
        try:
            _throttle_opensubtitles()
            provider.login()
            return True
        except OpenSubtitlesComError as e:
            # Too Many Requests -> backoff
            logging.warning("[SUBPICK] OS.com login error (%d/%d): %s", attempt, max_tries, e)
            time.sleep(_jitter(min(delay, MAX_BACKOFF)))
            delay *= 2
        except Exception as e:
            logging.warning("[SUBPICK] OS.com unexpected login error (%d/%d): %s", attempt, max_tries, e)
            time.sleep(_jitter(min(delay, MAX_BACKOFF)))
            delay *= 2
    return False


# ---------------------------------------------------------------------------
# Optional TV refiners (we leave them in; they won’t break movies)
# ---------------------------------------------------------------------------
def _coerce_tt(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()

    if not s:
        return None

    if s.startswith("tt"):
        return s

    # Sometimes the API returns numeric imdb IDs; normalize to tt########
    try:
        n = int(s)
        return f"tt{n:07d}"
    except Exception:
        return None

def _load_episode_ids_and_title(meta_path: str) -> tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    """Returns (episode_imdb_id, episode_tvdb_id, series_tvdb_id, first_episode_title) from meta."""
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)

    except Exception:
        return (None, None, None, None)

    series = (raw.get("series") or {})
    ep = (raw.get("episode") or {})
    ids = (ep.get("ids") or {})
    imdbs = ids.get("imdb") or []
    tvdbs = ids.get("tvdb") or []
    titles = ep.get("titles") or []

    ep_imdb = None
    for v in imdbs:
        tt = _coerce_tt(v)
        if tt:
            ep_imdb = tt
            break

    ep_tvdb = None
    for v in tvdbs:
        try:
            n = int(v)
            if n > 0:
                ep_tvdb = n
                break
        except Exception:
            continue

    series_tvdb = None
    try:
        sv = series.get("tvdb_id")

        if sv is not None:
            series_tvdb = int(sv)

    except Exception:
        series_tvdb = None

    first_title = titles[0].strip() if titles and isinstance(titles[0], str) else None
    return (ep_imdb, ep_tvdb, series_tvdb, first_title)

# ---------------------------------------------------------------------------
# TVDB v4 helpers
# ---------------------------------------------------------------------------

_TVDB_BASE = "https://api4.thetvdb.com/v4"

def _tvdb_login() -> Optional[str]:
    apikey = get_setting("TVDB_API_KEY")
    pin = os.getenv("TVDB_PIN", "") or None
    if not apikey:
        logging.info("[TVDBv4] TVDB_API_KEY not set; skipping TVDB enrichment.")
        return None
    payload = {"apikey": apikey}
    if pin:
        payload["pin"] = pin
    try:
        r: Response = requests.post(f"{_TVDB_BASE}/login", json=payload, timeout=10)
        r.raise_for_status()
        token = (r.json().get("data") or {}).get("token")
        if token:
            logging.info("[TVDBv4] Auth OK; token acquired.")
            return token
        logging.warning("[TVDBv4] Login response missing token.")
    except Exception as e:
        logging.warning("[TVDBv4] Login failed: %s", e)
    return None

def _tvdb_get(token: str, path: str, params: dict | None = None) -> Optional[dict]:
    try:
        r: Response = requests.get(
            f"{_TVDB_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=12,
        )
        r.raise_for_status()
        return r.json()

    except Exception as e:
        logging.warning("[TVDBv4] GET %s failed: %s", path, e)
        return None

# ---------------- TVDB v4: IMPROVED PARSERS + LOGGING ----------------
def _tvdb__extract_imdb_from_episode_obj(obj: dict) -> Optional[str]:
    # 1) direct field
    tt = _coerce_tt(obj.get("imdbId"))
    if tt:
        return tt
    # 2) externalIds list (v4)
    for lst_key in ("externalIds", "remoteIds"):
        for rid in obj.get(lst_key) or []:
            # TVDB v4 typically uses 'sourceName'='imdb' or 'type'=='imdb'
            if str(rid.get("sourceName", "")).lower() == "imdb" or str(rid.get("type", "")).lower() == "imdb":
                tt = _coerce_tt(rid.get("id"))
                if tt:
                    return tt
    return None

def _tvdb_episode_imdb_by_episode_id(token: str, episode_id: int) -> Optional[str]:
    # Try plain record
    js = _tvdb_get(token, f"/episodes/{episode_id}")
    if js and isinstance(js.get("data"), dict):
        tt = _tvdb__extract_imdb_from_episode_obj(js["data"])
        if tt:
            logging.info("[TVDBv4] episodes/%s -> imdbId=%s", episode_id, tt)
            return tt

    # Fallback to extended (exposes externalIds reliably)
    jsx = _tvdb_get(token, f"/episodes/{episode_id}/extended", params={"meta": "externalIds"})
    if jsx and isinstance(jsx.get("data"), dict):
        tt = _tvdb__extract_imdb_from_episode_obj(jsx["data"])
        if tt:
            logging.info("[TVDBv4] episodes/%s/extended -> imdbId=%s", episode_id, tt)
            return tt

    logging.info("[TVDBv4] episodes/%s -> no imdb id found", episode_id)
    return None

def _tvdb_episode_imdb_by_series_season_ep(token: str, series_id: int, season: int, episode: int) -> Optional[str]:
    page = 0
    while True:
        js = _tvdb_get(
            token,
            f"/series/{series_id}/episodes/official",
            params={"seasonNumber": season, "page": page},
        )

        if not js:
            logging.info("[TVDBv4] series/%s/episodes/official page=%s -> no data", series_id, page)
            return None
        data = js.get("data") or {}
        eps = data.get("episodes") or []
        # scan current page
        for row in eps:
            try:
                num = int(row.get("number") if "number" in row else row.get("episodeNumber"))
            except Exception:
                num = None
            if num == int(episode):
                # Try inline imdb on row
                tt = _tvdb__extract_imdb_from_episode_obj(row)
                if tt:
                    logging.info("[TVDBv4] series=%s S%02dE%02d -> imdbId=%s (from listing)", series_id, season, episode, tt)
                    return tt
                # Else fetch detail (and extended) by episode id
                eid = row.get("id")
                if isinstance(eid, int) and eid > 0:
                    tt = _tvdb_episode_imdb_by_episode_id(token, eid)
                    if tt:
                        logging.info("[TVDBv4] series=%s S%02dE%02d -> imdbId=%s (from detail)", series_id, season, episode, tt)
                        return tt
                logging.info("[TVDBv4] series=%s S%02dE%02d -> no imdb on that row", series_id, season, episode)
                return None  # we found the row; no point paging further
        # go to next page if available
        links = data.get("links") or {}
        nxt = links.get("next")
        if not nxt or int(nxt) == 0:
            break
        page = int(nxt)
    logging.info("[TVDBv4] series=%s S%02dE%02d -> no matching episode row", series_id, season, episode)
    return None

# ---------------------------------------------------------------------------
# OMDb fallback
# ---------------------------------------------------------------------------

def _omdb_episode_imdb(series_imdb: Optional[str], season: Optional[int], episode: Optional[int]) -> Optional[str]:
    api_key = get_setting("OMDB_API_KEY")
    if not api_key or not series_imdb or not season or not episode:
        return None
    try:
        r: Response = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": api_key, "i": series_imdb, "Season": int(season)},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        for row in data.get("Episodes", []):
            if int(row.get("Episode")) == int(episode):
                tt = (row.get("imdbID") or "").strip()
                if tt.startswith("tt"):
                    logging.info("[OMDb] mapped S%02dE%02d -> %s", int(season), int(episode), tt)
                    return tt
    except Exception as e:
        logging.warning("[OMDb] lookup failed: %s", e)
    return None

# ---------------------------------------------------------------------------
# Small TV meta reader (compatible with your write_meta_tv.sh)
# ---------------------------------------------------------------------------
def _load_tv_from_meta(meta_path: str) -> Tuple[Optional[str], Optional[int], Optional[List[int]], Optional[str]]:
    """
    Returns (series_title, season, episodes_list, series_imdb)
    """
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except Exception:
        return (None, None, None, None)

    series = raw.get("series") or {}
    episode = raw.get("episode") or {}

    series_title = series.get("title") or raw.get("series_title")
    season = episode.get("season") or raw.get("season")
    eps = episode.get("episodes") or raw.get("episodes")
    series_imdb = (series.get("imdb_id") or raw.get("imdb_id") or None)

    try:
        season = int(season) if season is not None else None
    except Exception:
        season = None

    if isinstance(eps, list):
        eps = [e for e in eps if isinstance(e, int)]
    else:
        eps = None

    return (series_title, season, eps, series_imdb)


def _load_episode_titles(meta_path: str) -> List[str]:
    """
    Load all episode titles from meta.json for multi-episode files.
    Returns list like ["The Serpent's Pass", "The Drill"]
    """
    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except Exception:
        return []

    ep = raw.get("episode") or {}
    titles = ep.get("titles") or []
    return [t.strip() for t in titles if isinstance(t, str) and t.strip()]


def _build_multi_episode_code(season: int, episodes: List[int]) -> str:
    """
    Build episode code string for multi-episode files.
    Examples: S02E12E13, S02E12-E13
    """
    if not episodes:
        return ""
    eps_str = "".join(f"E{e:02d}" for e in sorted(episodes))
    return f"S{season:02d}{eps_str}"


def _extract_season_number(text: str) -> Optional[int]:
    """
    Extract season number from a release name.
    Returns the first season number found, or None.
    """
    # S01, S1, Season 1, etc.
    m = re.search(r'[Ss](\d{1,2})(?:[Ee]|\b)', text)
    if m:
        return int(m.group(1))
    m = re.search(r'[Ss]eason\s*(\d{1,2})', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 3x18 format
    m = re.search(r'(\d{1,2})x\d{1,3}', text)
    if m:
        return int(m.group(1))
    return None


def _extract_episode_numbers_strict(text: str) -> set:
    """
    Extract episode numbers from a string - STRICT version for multi-episode matching.
    Only extracts numbers that are clearly episode indicators, not from resolution/bitrate.

    Matches:
    - E10, E11, e10, e11 (standard episode markers)
    - E10E11, E10-E11, E10.E11 (multi-episode patterns)
    - 3x10 (alternative format)
    - Ep10, Episode 10

    Does NOT match:
    - 1080p, 720p (resolution)
    - x264, x265, H264, H.264 (codec)
    - DD5.1, DD2.0 (audio)
    """
    numbers = set()

    # First, remove common false-positive patterns
    # Replace resolution, codec, and audio patterns with spaces
    cleaned = text
    # Remove resolution patterns (1080p, 720p, 480p, 2160p)
    cleaned = re.sub(r'\b\d{3,4}[pPiI]\b', ' ', cleaned)
    # Remove codec patterns (x264, x265, H264, H.264, HEVC)
    cleaned = re.sub(r'\b[xXhH]\.?26[45]\b', ' ', cleaned)
    # Remove audio patterns (DD5.1, DD2.0, AAC2.0, DTS-HD)
    cleaned = re.sub(r'\b(?:DD|AAC|DTS)[\d.]+\b', ' ', cleaned, flags=re.IGNORECASE)
    # Remove bitrate patterns (like 10bit)
    cleaned = re.sub(r'\b\d+bit\b', ' ', cleaned, flags=re.IGNORECASE)

    # Pattern 1: Standard episode markers E##, e##
    # Match E10, E11, but require word boundary or another E before/after
    for m in re.finditer(r'[Ee](\d{1,3})(?=[Ee\.\-\s]|$)', cleaned):
        num = int(m.group(1))
        if 1 <= num <= 99:  # Valid episode range
            numbers.add(num)

    # Pattern 2: Hyphenated multi-episode (E10-E11, E10-11)
    for m in re.finditer(r'[Ee](\d{1,3})\s*-\s*[Ee]?(\d{1,3})', cleaned):
        start, end = int(m.group(1)), int(m.group(2))
        if 1 <= start <= 99 and 1 <= end <= 99:
            for ep in range(start, end + 1):
                numbers.add(ep)

    # Pattern 3: Alternative format 3x10 (season x episode)
    for m in re.finditer(r'\d{1,2}x(\d{1,3})', cleaned):
        num = int(m.group(1))
        if 1 <= num <= 99:
            numbers.add(num)

    # Pattern 4: ep10, episode10, Episode 10
    for m in re.finditer(r'(?:ep|episode)\s*(\d{1,3})', cleaned, re.IGNORECASE):
        num = int(m.group(1))
        if 1 <= num <= 99:
            numbers.add(num)

    return numbers


def _get_title_keywords(series_title: str) -> set:
    """
    Extract meaningful keywords from a series title for matching.
    Filters out common words and short words.
    """
    # Common words to ignore
    stop_words = {'the', 'a', 'an', 'of', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'is', 'it'}

    # Split on non-alphanumeric, lowercase
    words = re.split(r'[^a-zA-Z0-9]+', series_title.lower())

    # Keep words that are 3+ chars and not stop words
    keywords = {w for w in words if len(w) >= 3 and w not in stop_words}

    return keywords


def _score_fuzzy_title_match(release_name: str, search_query: str) -> int:
    """
    Score a subtitle release based on fuzzy title matching.
    Used when no season/episode criteria are provided.

    Scoring:
    - +100 base if any keyword matches
    - +50 for each keyword from search query found in release
    - +25 bonus if all keywords match (full title match)
    - +10 for each word order match (consecutive keywords)

    Returns 0 if no keywords match at all.
    """
    query_keywords = _get_title_keywords(search_query)
    if not query_keywords:
        return 0

    release_lower = release_name.lower()

    # Count keyword matches
    matches = 0
    for keyword in query_keywords:
        if keyword in release_lower:
            matches += 1

    if matches == 0:
        return 0

    # Base score for having any match
    score = 100

    # Points per keyword match
    score += matches * 50

    # Bonus for full title match (all keywords present)
    if matches == len(query_keywords):
        score += 25

    # Bonus for consecutive keyword matches (word order)
    query_words = [w for w in re.split(r'[^a-zA-Z0-9]+', search_query.lower()) if len(w) >= 3]
    if len(query_words) >= 2:
        for i in range(len(query_words) - 1):
            # Check if two consecutive query words appear close together in release
            w1, w2 = query_words[i], query_words[i + 1]
            pattern = rf'\b{re.escape(w1)}\b.{{0,20}}\b{re.escape(w2)}\b'
            if re.search(pattern, release_lower):
                score += 10

    return score


def _score_season_only_match(release_name: str, required_season: int, search_query: str) -> int:
    """
    Score a subtitle release when only season is specified (no specific episodes).
    Matches any episode in the correct season.

    Scoring:
    - +100 base if season matches
    - +50 for each title keyword match
    - +25 bonus for full title match
    """
    # Check season
    found_season = _extract_season_number(release_name)
    if found_season is None:
        # No season in release - could still be valid, give lower score
        pass
    elif found_season != required_season:
        return 0  # Wrong season

    # Base score
    score = 100 if found_season == required_season else 50

    # Title keyword matching
    title_keywords = _get_title_keywords(search_query)
    release_lower = release_name.lower()
    matches = sum(1 for kw in title_keywords if kw in release_lower)

    if matches == 0:
        return 0  # Must have some title match

    score += matches * 50

    # Bonus for full title match
    if matches == len(title_keywords) and title_keywords:
        score += 25

    return score


def _score_multi_episode_match(
    release_name: str,
    required_season: int,
    required_episodes: List[int],
    series_title: str
) -> int:
    """
    Score a subtitle release for multi-episode matching.

    Returns:
    - 0 if it doesn't match (wrong season or missing episodes)
    - Higher scores for better matches (title keywords, exact episode format)

    Scoring:
    - Base: 100 points if all episodes found and season matches
    - +50 for each title keyword found
    - +25 for exact episode format (E10E11 vs just having both numbers)
    """
    # Extract season
    found_season = _extract_season_number(release_name)
    if found_season is not None and found_season != required_season:
        return 0  # Wrong season

    # Extract episodes (strict)
    found_episodes = _extract_episode_numbers_strict(release_name)
    required_set = set(required_episodes)

    # Must have ALL required episodes
    if not required_set.issubset(found_episodes):
        return 0

    # Base score for matching
    score = 100

    # Bonus for title keywords
    title_keywords = _get_title_keywords(series_title)
    release_lower = release_name.lower()
    for keyword in title_keywords:
        if keyword in release_lower:
            score += 50

    # Bonus for exact multi-episode format (E10E11 or E10-E11)
    eps_sorted = sorted(required_episodes)
    if len(eps_sorted) == 2:
        # Check for E10E11 or E10-E11 format
        ep1, ep2 = eps_sorted
        patterns = [
            rf'[Ee]{ep1:02d}[Ee]{ep2:02d}',  # E10E11
            rf'[Ee]{ep1}[Ee]{ep2}',  # E10E11 without leading zeros
            rf'[Ee]{ep1:02d}\s*-\s*[Ee]?{ep2:02d}',  # E10-E11 or E10-11
            rf'[Ee]{ep1}\s*-\s*[Ee]?{ep2}',  # E10-E11 without leading zeros
        ]
        for pattern in patterns:
            if re.search(pattern, release_name):
                score += 25
                break

    return score


def _matches_multi_episode(release_name: str, required_episodes: List[int], season: int = None, series_title: str = None) -> bool:
    """
    Check if a subtitle release name matches the required multi-episode criteria.

    Args:
        release_name: The subtitle release name to check
        required_episodes: List of episode numbers that must ALL be present
        season: Required season number (optional but recommended)
        series_title: Series title for keyword matching (optional)

    Returns True if release matches all criteria.
    """
    score = _score_multi_episode_match(
        release_name,
        required_season=season or 0,
        required_episodes=required_episodes,
        series_title=series_title or ""
    )
    return score > 0


def _query_os_generic(
    prov: OpenSubtitlesComProvider,
    lang: Language,
    query: str,
) -> list:
    """
    Generic text-based query to OpenSubtitles without structured season/episode params.
    Used for multi-episode files where we need fuzzy matching.
    """
    logging.info("[SUBPICK] OS.com generic query: %r", query)
    _throttle_opensubtitles()
    try:
        return prov.query(languages={lang}, query=query) or []
    except (OpenSubtitlesComError, ServiceUnavailable) as e:
        logging.warning("[SUBPICK] Generic query failed: %s", e)
        return []


def _query_os_for_episode(
    prov: OpenSubtitlesComProvider,
    lang: Language,
    *,
    series_title: str,
    season: int,
    episode: int,
    series_imdb_id: str | None,
    episode_imdb_id: str | None,
) -> list:
    # Prefer pointed episode imdb if we have it
    if episode_imdb_id:
        try:
            logging.info("[SUBPICK] OS.com query via episode imdb_id + (show_imdb_id) + season/episode")
            return (
                prov.query(
                    languages={lang},
                    imdb_id=episode_imdb_id,
                    show_imdb_id=series_imdb_id,
                    season=season,
                    episode=episode,
                )
                or []
            )
        except TypeError as e:
            logging.debug("[SUBPICK] imdb_id + show_imdb_id path unsupported: %s", e)

    # Fallback: title + S/E (always safe)
    logging.info("[SUBPICK] OS.com query via title + season/episode")
    return (
        prov.query(
            languages={lang},
            query=series_title.replace(":", ""),
            season=season,
            episode=episode,
        )
        or []
    )


# ---------------------------------------------------------------------------
# Utilities to normalize Subliminal results (avoid wrong types)
# ---------------------------------------------------------------------------
def _normalize_subliminal_results(results: Any, video_obj) -> List[Any]:
    from subliminal.subtitle import Subtitle

    if isinstance(results, dict):
        cands = results.get(video_obj)
        if cands is None:
            try:
                _, cands = next(iter(results.items()))
            except StopIteration:
                cands = []
    else:
        cands = results

    # Flatten any nested structures just in case
    flat = []
    for s in (cands or []):
        if isinstance(s, list):
            flat.extend(s)
        else:
            flat.append(s)

    return [s for s in flat if isinstance(s, Subtitle)]


def _rank_key(sub) -> tuple:
    return (
        -(getattr(sub, "download_count", 0) or 0),
        getattr(sub, "hearing_impaired", False),
        getattr(sub, "id", 0),
    )


# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------
def fetch_extra_subs(
    video_path: str,
    lang: str = "eng",
    max_downloads: int = 3,
    max_retries: int = 3,
    meta_override: dict | None = None,
) -> list[str]:
    saved: list[str] = []
    p = Path(video_path)
    out_dir, stem = p.parent, p.stem

    # language normalization
    try:
        lang_obj = Language(lang)
    except Exception:
        try:
            lang_obj = Language.fromalpha2(lang)
        except Exception:
            lang_obj = Language("eng")
    lang2 = lang_obj.alpha2

    _ensure_cache_region()

    if meta_override:
        imdb_series_or_movie = (meta_override.get("best_imdb_id") or
                                meta_override.get("imdb_id") or
                                meta_override.get("series_imdb_id"))
        series_title = meta_override.get("series_title")
        season = meta_override.get("season")
        episodes = meta_override.get("episodes")
        series_imdb = meta_override.get("series_imdb_id")
        meta_path = None
    else:
        meta_path = find_meta_json(video_path)
        if not meta_path:
            logging.warning("[SUBPICK] No meta json next to video: %s", video_path)
            return []
        imdb_series_or_movie = _load_imdb_from_meta(meta_path)
        series_title, season, episodes, series_imdb = _load_tv_from_meta(str(meta_path))

    # Friendly meta logs (you already print elsewhere too)
    logging.info(
        "[META.TV] series=%s season=%s eps=%s series_imdb=%r ep_imdb=%s ep_tvdb=%s series_tvdb=%s first_title=%s",
        series_title,
        season,
        episodes,
        series_imdb,
        None,
        None,
        None,
        None,
    )

    # Reset provider exhaustion tracking for this fetch session
    _reset_exhausted_providers()

    # Get list of enabled providers for fallback
    enabled_providers = _get_enabled_providers()
    logging.info("[PROVIDERS] Enabled providers: %s", enabled_providers)

    if not enabled_providers:
        logging.error("[SUBPICK] No subtitle providers configured")
        return []

    # Initialize OpenSubtitles.com provider if available (for direct downloads)
    provider = None
    if "opensubtitlescom" in enabled_providers:
        provider = _create_oscom_provider()
        if not provider:
            logging.warning("[SUBPICK] OpenSubtitles.com configured but no accounts available")
            _mark_provider_exhausted("opensubtitlescom")

    try:
        # ------------------------------- TV PATH -------------------------------
        if series_title and season and episodes:

            # -------------------- MULTI-EPISODE HANDLING --------------------
            # If single video file contains multiple episodes, use generic search
            if len(episodes) > 1:
                episode_titles = _load_episode_titles(str(meta_path))
                ep_code = _build_multi_episode_code(int(season), episodes)

                logging.info(
                    "[SUBPICK] Multi-episode detected: %s episodes=%s titles=%s",
                    ep_code,
                    episodes,
                    episode_titles,
                )

                # Create a separate provider with MORE pages to find multi-ep matches
                # 10 pages = ~200 results to improve chances of finding multi-episode subs
                multi_ep_provider = None
                if not _is_provider_exhausted("opensubtitlescom"):
                    multi_ep_provider = _create_oscom_provider(max_result_pages=10)
                if not multi_ep_provider:
                    logging.warning("[SUBPICK] No OpenSubtitles.com provider for multi-episode search")
                    # Fall through to subliminal aggregator with all enabled providers
                    video = scan_video(video_path)
                    fallback_providers = set(p for p in enabled_providers if not _is_provider_exhausted(p))
                    if fallback_providers:
                        logging.info("[SUBPICK] Trying Subliminal fallback for multi-episode with providers: %s", fallback_providers)
                        results = list_subtitles([video], {lang_obj}, providers=fallback_providers, provider_configs=_build_provider_configs())
                        cands = _normalize_subliminal_results(results, video)
                        if cands:
                            cands = sorted(cands, key=_rank_key)[:max_downloads]
                            try:
                                download_subtitles(cands)
                                for idx_f, sub in enumerate(cands, 1):
                                    data = getattr(sub, "content", None)
                                    if not data:
                                        continue
                                    prov = getattr(sub, "provider_name", "fallback")
                                    hi = ".hi" if getattr(sub, "hearing_impaired", False) else ""
                                    out = out_dir / f"{stem}.{lang2}.{idx_f}.{prov}{hi}.srt"
                                    with contextlib.suppress(Exception):
                                        out.write_bytes(data)
                                        saved.append(str(out))
                                        logging.info("[SUBPICK] Saved %s (fallback)", out.name)
                                return saved
                            except Exception as e:
                                logging.warning("[SUBPICK] Fallback download failed: %s", e)
                    return []

                try:
                    # Build search queries - focus on queries that return relevant results
                    queries_to_try = []
                    first_ep = episodes[0]
                    last_ep = episodes[-1]

                    # Query 1: Series + first episode (gets results for the show)
                    queries_to_try.append(f"{series_title} S{int(season):02d}E{first_ep:02d}")

                    # Query 2: Series + last episode (often indexed separately)
                    if last_ep != first_ep:
                        queries_to_try.append(f"{series_title} S{int(season):02d}E{last_ep:02d}")

                    # Query 3: Series + hyphenated range (common multi-ep format)
                    if len(episodes) == 2:
                        ep_range = f"S{int(season):02d}E{first_ep:02d}-E{last_ep:02d}"
                        queries_to_try.append(f"{series_title} {ep_range}")

                    # Query 4: Just series name + season (broadest, catches various formats)
                    queries_to_try.append(f"{series_title} S{int(season):02d}")

                    all_results = []
                    seen_ids = set()

                    for query in queries_to_try:
                        try:
                            results = _query_os_generic(multi_ep_provider, lang_obj, query)
                            new_count = 0
                            for sub in results:
                                sub_id = getattr(sub, "id", None)
                                if sub_id and sub_id not in seen_ids:
                                    seen_ids.add(sub_id)
                                    all_results.append(sub)
                                    new_count += 1
                            logging.info("[SUBPICK] Query returned %d new results (total: %d)", new_count, len(all_results))
                        except Exception as e:
                            logging.warning("[SUBPICK] Query failed: %s", e)
                            continue

                    logging.info("[SUBPICK] Multi-episode search: %d unique results", len(all_results))

                    # Score and filter results - require ALL episodes AND correct season
                    scored_subs = []
                    for sub in all_results:
                        release = getattr(sub, "release", "") or ""
                        filename = getattr(sub, "filename", "") or ""
                        check_text = f"{release} {filename}"

                        # Score the match (0 = no match, higher = better)
                        score = _score_multi_episode_match(
                            check_text,
                            required_season=int(season),
                            required_episodes=episodes,
                            series_title=series_title
                        )

                        if score > 0:
                            scored_subs.append((score, sub))
                            logging.info("[MULTI-EP MATCH] score=%d id=%s release=%r",
                                         score, getattr(sub, "id", "?"), release)

                    logging.info("[SUBPICK] Found %d subtitles matching S%02d episodes %s",
                                 len(scored_subs), int(season), episodes)

                    matching_subs = []
                    if not scored_subs:
                        logging.warning("[SUBPICK] No multi-episode subtitle matches found")
                        # Fall through to subliminal fallback below
                    else:
                        # Sort by score (descending), then by download count
                        scored_subs.sort(key=lambda x: (-x[0], _rank_key(x[1])))
                        matching_subs = [sub for score, sub in scored_subs[:5]]

                        if _login_with_retry(multi_ep_provider):
                            for idx, s in enumerate(matching_subs, 1):
                                try:
                                    ok, multi_ep_provider = _download_with_session(multi_ep_provider, s)
                                    if not ok:
                                        continue
                                except DownloadLimitReached:
                                    logging.error("[SUBPICK] Download limit reached")
                                    _mark_provider_exhausted("opensubtitlescom")
                                    break
                                except Exception as e:
                                    logging.warning("[SUBPICK] Download failed: %s", e)
                                    continue

                                data = getattr(s, "content", None)
                                if not data:
                                    continue

                                hi = ".hi" if getattr(s, "hearing_impaired", False) else ""
                                out = out_dir / f"{stem}.{lang2}.{idx}.opensubtitlescom{hi}.srt"
                                with contextlib.suppress(Exception):
                                    out.write_bytes(data)
                                    saved.append(str(out))
                                    logging.info("[SUBPICK] Saved multi-ep sub: %s", out.name)

                        if saved:
                            return saved

                    # Fallback: try subliminal aggregator for multi-episode
                    video = scan_video(video_path)
                    fallback_providers = set(p for p in enabled_providers if not _is_provider_exhausted(p))
                    if fallback_providers:
                        logging.info("[SUBPICK] Trying Subliminal fallback for multi-episode with providers: %s (OSCOM matches=%d, saved=%d)",
                                    fallback_providers, len(matching_subs), len(saved))
                        results = list_subtitles([video], {lang_obj}, providers=fallback_providers, provider_configs=_build_provider_configs())
                        cands = _normalize_subliminal_results(results, video)
                        if cands:
                            cands = sorted(cands, key=_rank_key)[:max_downloads]
                            try:
                                download_subtitles(cands)
                                for idx_f, sub in enumerate(cands, 1):
                                    data = getattr(sub, "content", None)
                                    if not data:
                                        continue
                                    prov = getattr(sub, "provider_name", "fallback")
                                    hi = ".hi" if getattr(sub, "hearing_impaired", False) else ""
                                    out = out_dir / f"{stem}.{lang2}.{idx_f}.{prov}{hi}.srt"
                                    with contextlib.suppress(Exception):
                                        out.write_bytes(data)
                                        saved.append(str(out))
                                        logging.info("[SUBPICK] Saved %s (fallback)", out.name)
                            except Exception as e:
                                logging.warning("[SUBPICK] Fallback download failed: %s", e)
                    return saved

                finally:
                    # Clean up the multi-episode provider
                    with contextlib.suppress(Exception):
                        multi_ep_provider.terminate()

            # -------------------- SINGLE EPISODE (existing logic) --------------------
            ep_no = int(episodes[0])
            logging.info(
                "[SUBPICK] Using OpenSubtitles.com for TV '%s' S%02dE%02d (lang=%s)",
                series_title,
                int(season),
                ep_no,
                lang2,
            )

            video = scan_video(video_path)  # Episode object

            # Seed from meta
            ep_imdb_from_meta, ep_tvdb_from_meta, series_tvdb_id, first_title = _load_episode_ids_and_title(
                str(meta_path))
            if series_imdb:
                setattr(video, "series_imdb_id", series_imdb)
            if ep_imdb_from_meta:
                setattr(video, "imdb_id", ep_imdb_from_meta)

            logging.info("[SEED] imdb_id=%r series_imdb_id=%r ep_tvdb=%r series_tvdb=%r",
                         getattr(video, "imdb_id", None),
                         getattr(video, "series_imdb_id", None),
                         ep_tvdb_from_meta, series_tvdb_id)

            # ------- YOUR DIRECT ENRICHMENT (preferred) -------
            episode_imdb_id = getattr(video, "imdb_id", None)
            if not episode_imdb_id:
                token = _tvdb_login()
                if token:
                    if ep_tvdb_from_meta:
                        episode_imdb_id = _tvdb_episode_imdb_by_episode_id(token, ep_tvdb_from_meta)
                    if not episode_imdb_id and series_tvdb_id and season and ep_no:
                        episode_imdb_id = _tvdb_episode_imdb_by_series_season_ep(token, series_tvdb_id, int(season),
                                                                                 ep_no)
                    if episode_imdb_id:
                        setattr(video, "imdb_id", episode_imdb_id)
                        _write_back_episode_ids(meta_path,
                                                ep_imdb=episode_imdb_id,
                                                series_imdb=series_imdb,
                                                series_tvdb=series_tvdb_id)

            # ------- Optional OMDb fallback -------
            if not episode_imdb_id:
                episode_imdb_id = _omdb_episode_imdb(series_imdb, season, ep_no)
                if episode_imdb_id:
                    setattr(video, "imdb_id", episode_imdb_id)
                    _write_back_episode_ids(meta_path,
                                            ep_imdb=episode_imdb_id,
                                            series_imdb=series_imdb,
                                            series_tvdb=series_tvdb_id)

            # (Optional) Subliminal refiners – OFF by default. Enable with USE_SUBLIMINAL_REFINERS=1
            if not episode_imdb_id and os.getenv("USE_SUBLIMINAL_REFINERS", "0") == "1":
                try:
                    tvdb_key = get_setting("TVDB_API_KEY")
                    if tvdb_key:
                        from subliminal.refiners.tvdb import refine as tvdb_refine
                        tvdb_refine(video, apikey=tvdb_key, force=False)
                    omdb_key = get_setting("OMDB_API_KEY")
                    if omdb_key:
                        from subliminal.refiners.omdb import refine as omdb_refine
                        omdb_refine(video, apikey=omdb_key, force=False)
                    episode_imdb_id = getattr(video, "imdb_id", None)
                except Exception as e:
                    logging.debug("[REFINE] Subliminal refiners skipped: %s", e)

            logging.info("[REFINE] final series_imdb_id=%s episode_imdb_id=%s",
                         getattr(video, "series_imdb_id", None), getattr(video, "imdb_id", None))

            # ---------- Query OpenSubtitles (pointed if we have imdb) ----------
            subs = []
            series_imdb_id = getattr(video, "series_imdb_id", None) or series_imdb or None

            # Only try OpenSubtitles.com direct query if provider is available
            if provider and not _is_provider_exhausted("opensubtitlescom"):
                try:
                    subs = _query_os_for_episode(
                        provider,
                        lang_obj,
                        series_title=series_title,
                        season=int(season),
                        episode=ep_no,
                        series_imdb_id=series_imdb_id,
                        episode_imdb_id=episode_imdb_id,
                    )
                except (OpenSubtitlesComError, ServiceUnavailable) as e:
                    logging.warning("[SUBPICK] Provider error on TV query: %s", e)
                    subs = []
            else:
                logging.info("[SUBPICK] Skipping OpenSubtitles.com direct query (provider unavailable or exhausted)")

            # If we have direct provider results, download them first
            if subs and provider:
                subs = sorted(subs, key=_rank_key)[:max_downloads]
                if _login_with_retry(provider):
                    for idx, s in enumerate(subs, 1):
                        try:
                            ok, provider = _download_with_session(provider, s)
                            if not ok:
                                continue
                        except DownloadLimitReached as e:
                            logging.error("[SUBPICK] OS.com quota reached: %s", e)
                            _mark_provider_exhausted("opensubtitlescom")
                            break  # Fall through to subliminal aggregator fallback
                        except (OpenSubtitlesComError, ServiceUnavailable) as e:
                            logging.warning("[SUBPICK] Download failed id=%s: %s", getattr(s, "id", "?"), e)
                            continue

                        data = getattr(s, "content", None)
                        if not data:
                            continue
                        hi = ".hi" if getattr(s, "hearing_impaired", False) else ""
                        out = out_dir / f"{stem}.{lang2}.{idx}.opensubtitlescom{hi}.srt"
                        with contextlib.suppress(Exception):
                            out.write_bytes(data)
                            saved.append(str(out))
                            logging.info("[SUBPICK] Saved %s (id=%s dl=%s)", out.name, s.id, s.download_count)
                    if saved:
                        return saved

            # 2) Fallback to Subliminal aggregator with all enabled providers
            # This runs when: OSCOM returned 0 results, downloads failed, or OSCOM exhausted
            fallback_providers = set(p for p in enabled_providers if not _is_provider_exhausted(p))
            if not fallback_providers:
                logging.warning("[SUBPICK] No fallback providers available (all exhausted)")
                return []
            logging.info("[SUBPICK] Trying Subliminal fallback with providers: %s (OSCOM results=%d, saved=%d)",
                        fallback_providers, len(subs) if subs else 0, len(saved))
            results = list_subtitles([video], {lang_obj}, providers=fallback_providers, provider_configs=_build_provider_configs())
            cands = _normalize_subliminal_results(results, video)
            if not cands:
                logging.info("[SUBPICK] 0 results via list_subtitles fallback")
                return []

            # Defensive: ensure every item looks like a Subtitle (has provider_name/id)
            bad = [type(s).__name__ for s in cands if not hasattr(s, "provider_name")]
            if bad:
                logging.warning("[SUBPICK] Unexpected candidate types in fallback: %s", set(bad))
                return []

            cands = sorted(cands, key=_rank_key)[:max_downloads]
            try:
                # download_subtitles expects a list of Subtitle objects, NOT a dict
                download_subtitles(cands)
            except Exception as e:
                logging.warning("[SUBPICK] Subliminal download_subtitles error: %s", e)
                return []

            for idx_f, sub in enumerate(cands, 1):
                data = getattr(sub, "content", None)
                if not data:
                    continue
                prov = getattr(sub, "provider_name", "fallback")
                hi = ".hi" if getattr(sub, "hearing_impaired", False) else ""
                out = out_dir / f"{stem}.{lang2}.{idx_f}.{prov}{hi}.srt"
                with contextlib.suppress(Exception):
                    out.write_bytes(data)
                    saved.append(str(out))
                    logging.info("[SUBPICK] Saved %s", out.name)
            return saved

        # ------------------------------ MOVIE PATH -----------------------------
        # If it's NOT TV, try a pointed imdb_id query first (precise + quick)
        subs = []
        if imdb_series_or_movie and provider and not _is_provider_exhausted("opensubtitlescom"):
            logging.info("[OS] pointed movie query via imdb_id=%s", imdb_series_or_movie)
            try:
                _throttle_opensubtitles()
                subs = provider.query(languages={lang_obj}, imdb_id=imdb_series_or_movie) or []
            except (OpenSubtitlesComError, ServiceUnavailable) as e:
                logging.warning("[SUBPICK] OS.com movie query error: %s", e)
                subs = []
        elif imdb_series_or_movie:
            logging.info("[SUBPICK] Skipping OpenSubtitles.com movie query (provider unavailable or exhausted)")

        if subs and provider:
            subs = sorted(subs, key=_rank_key)[:max_downloads]
            if _login_with_retry(provider):
                for idx, s in enumerate(subs, 1):
                    try:
                        ok, provider = _download_with_session(provider, s)
                        if not ok:
                            continue
                    except DownloadLimitReached as e:
                        logging.error("[SUBPICK] OS.com quota reached: %s", e)
                        _mark_provider_exhausted("opensubtitlescom")
                        break  # Fall through to subliminal aggregator fallback
                    except (OpenSubtitlesComError, ServiceUnavailable) as e:
                        logging.warning("[SUBPICK] Download failed id=%s: %s", getattr(s, "id", "?"), e)
                        continue

                    data = getattr(s, "content", None)
                    if not data:
                        continue
                    hi = ".hi" if getattr(s, "hearing_impaired", False) else ""
                    out = out_dir / f"{stem}.{lang2}.{idx}.opensubtitlescom{hi}.srt"
                    with contextlib.suppress(Exception):
                        out.write_bytes(data)
                        saved.append(str(out))
                        logging.info("[SUBPICK] Saved %s (id=%s dl=%s)", out.name, s.id, s.download_count)
            if saved:
                return saved

        # Final movie fallback: generic Subliminal search with all enabled providers
        # This runs when: OSCOM returned 0 results, downloads failed, or OSCOM exhausted
        fallback_providers = set(p for p in enabled_providers if not _is_provider_exhausted(p))
        if not fallback_providers:
            logging.warning("[SUBPICK] No fallback providers available for movie (all exhausted)")
            return []
        logging.info("[SUBPICK] Trying Subliminal fallback for movie with providers: %s (OSCOM results=%d, saved=%d)",
                    fallback_providers, len(subs) if subs else 0, len(saved))
        video = scan_video(video_path)  # Movie object
        results = list_subtitles([video], {lang_obj}, providers=fallback_providers, provider_configs=_build_provider_configs())
        cands = _normalize_subliminal_results(results, video)

        if not cands:
            logging.info("[SUBPICK] 0 results in generic fallback")
            return []

        # HARD FILTER + DIAGNOSTIC LOGGING
        bad_items = [repr(type(s)) for s in cands if not isinstance(s, Subtitle)]
        if bad_items:
            logging.warning("[SUBPICK] Movie fallback returned non-Subtitle items: %s", bad_items)
        cands = [s for s in cands if isinstance(s, Subtitle)]
        if not cands:
            logging.info("[SUBPICK] No valid Subtitle objects after filtering.")
            return []

        cands = sorted(cands, key=_rank_key)[:max_downloads]

        try:
            # download_subtitles expects a list of Subtitle objects, NOT a dict
            download_subtitles(cands)
        except Exception as e:
            logging.warning("[SUBPICK] Subliminal download_subtitles error: %s", e)
            return []

        for idx_f, sub in enumerate(cands, 1):
            data = getattr(sub, "content", None)
            if not data:
                continue
            prov = getattr(sub, "provider_name", "fallback")
            hi = ".hi" if getattr(sub, "hearing_impaired", False) else ""
            out = out_dir / f"{stem}.{lang2}.{idx_f}.{prov}{hi}.srt"
            with contextlib.suppress(Exception):
                out.write_bytes(data)
                saved.append(str(out))
                logging.info("[SUBPICK] Saved %s", out.name)
        return saved

    finally:
        if provider:
            logging.info("[SUBPICK] Terminating OS.com provider")
            with contextlib.suppress(Exception):
                provider.terminate()


# ---------------------------------------------------------------------------
# Manual subtitle search (for edge cases with custom search parameters)
# ---------------------------------------------------------------------------
def fetch_subtitles_manual(
    video_path: str,
    search_query: str,
    season: Optional[int] = None,
    episodes: Optional[List[int]] = None,
    lang: str = "eng",
    max_downloads: int = 5,
) -> dict:
    """
    Manual subtitle search with custom parameters.

    For edge cases like:
    - Movie split into TV episodes (Family Guy movie -> S04E28-E30)
    - Wrong season/episode detection
    - Alternative titles

    Args:
        video_path: Path to the video file (for output naming)
        search_query: Custom search string (e.g., "Family Guy The Griffin Family History")
        season: Season number override (optional)
        episodes: Episode number(s) override (optional, list for multi-ep)
        lang: Language code (default: "eng")
        max_downloads: Maximum subtitles to download

    Returns:
        dict with:
            - saved: list of saved subtitle paths
            - searched: list of queries tried
            - found: number of results found
            - matched: number that matched criteria
            - errors: list of any errors
    """
    result = {
        "saved": [],
        "searched": [],
        "found": 0,
        "matched": 0,
        "errors": [],
    }

    p = Path(video_path)
    out_dir, stem = p.parent, p.stem

    # Language normalization
    try:
        lang_obj = Language(lang)
    except Exception:
        try:
            lang_obj = Language.fromalpha2(lang)
        except Exception:
            lang_obj = Language("eng")
    lang2 = lang_obj.alpha2

    _ensure_cache_region()

    # Get enabled providers
    enabled_providers = _get_enabled_providers()
    logging.info("[MANUAL-SEARCH] Starting manual search for: %s", video_path)
    logging.info("[MANUAL-SEARCH] Query: %r, season=%s, episodes=%s", search_query, season, episodes)
    logging.info("[MANUAL-SEARCH] Enabled providers: %s", enabled_providers)

    if not enabled_providers:
        result["errors"].append("No subtitle providers configured")
        return result

    # Create OSCOM provider if available
    provider = None
    if "opensubtitlescom" in enabled_providers:
        provider = _create_oscom_provider(max_result_pages=10)

    if not provider:
        result["errors"].append("OpenSubtitles.com provider not available")
        return result

    try:
        # Build search queries based on what criteria we have
        queries = []

        if season is not None and episodes:
            # Case 1: Season + episodes - targeted episode queries
            first_ep = episodes[0]
            last_ep = episodes[-1]

            # Most specific first
            queries.append(f"{search_query} S{season:02d}E{first_ep:02d}")
            if len(episodes) > 1:
                queries.append(f"{search_query} S{season:02d}E{last_ep:02d}")
                queries.append(f"{search_query} S{season:02d}E{first_ep:02d}-E{last_ep:02d}")
            queries.append(f"{search_query} S{season:02d}")
            queries.append(search_query)  # Broadest fallback

        elif season is not None:
            # Case 2: Season only - find any episode in that season
            queries.append(f"{search_query} S{season:02d}")
            queries.append(f"{search_query} Season {season}")
            queries.append(search_query)  # Broadest fallback

        else:
            # Case 3: No season/episode - pure title search
            # Try multiple variations to maximize results
            queries.append(search_query)

            # Try without year if query looks like "Title 2024"
            year_match = re.search(r'\s+(19|20)\d{2}\s*$', search_query)
            if year_match:
                query_no_year = search_query[:year_match.start()].strip()
                if query_no_year:
                    queries.append(query_no_year)

            # Try with common subtitle markers
            queries.append(f"{search_query} english")
            queries.append(f"{search_query} srt")

        # Run searches
        all_results = []
        seen_ids = set()

        for query in queries:
            try:
                logging.info("[MANUAL-SEARCH] Searching: %r", query)
                result["searched"].append(query)
                subs = _query_os_generic(provider, lang_obj, query)
                new_count = 0
                for sub in subs:
                    sub_id = getattr(sub, "id", None)
                    if sub_id and sub_id not in seen_ids:
                        seen_ids.add(sub_id)
                        all_results.append(sub)
                        new_count += 1
                logging.info("[MANUAL-SEARCH] Query returned %d new results", new_count)
            except Exception as e:
                logging.warning("[MANUAL-SEARCH] Query failed: %s", e)
                result["errors"].append(f"Query '{query}' failed: {e}")

        result["found"] = len(all_results)
        logging.info("[MANUAL-SEARCH] Total unique results: %d", len(all_results))

        # Filter/score results based on what criteria we have
        matching_subs = []

        if season is not None and episodes:
            # Case 1: Season + specific episodes - use multi-episode matching
            logging.info("[MANUAL-SEARCH] Scoring with season=%d, episodes=%s", season, episodes)
            for sub in all_results:
                release = getattr(sub, "release", "") or ""
                filename = getattr(sub, "filename", "") or ""
                check_text = f"{release} {filename}"

                score = _score_multi_episode_match(
                    check_text,
                    required_season=season,
                    required_episodes=episodes,
                    series_title=search_query
                )

                if score > 0:
                    matching_subs.append((score, sub))
                    logging.info("[MANUAL-SEARCH] Episode match: score=%d release=%r", score, release)

        elif season is not None:
            # Case 2: Season only (no specific episodes) - match any episode in season
            logging.info("[MANUAL-SEARCH] Scoring with season=%d only (any episode)", season)
            for sub in all_results:
                release = getattr(sub, "release", "") or ""
                filename = getattr(sub, "filename", "") or ""
                check_text = f"{release} {filename}"

                score = _score_season_only_match(check_text, season, search_query)

                if score > 0:
                    matching_subs.append((score, sub))
                    logging.info("[MANUAL-SEARCH] Season match: score=%d release=%r", score, release)

        else:
            # Case 3: No season/episode - pure fuzzy title matching
            logging.info("[MANUAL-SEARCH] Fuzzy title matching (no season/episode criteria)")
            for sub in all_results:
                release = getattr(sub, "release", "") or ""
                filename = getattr(sub, "filename", "") or ""
                check_text = f"{release} {filename}"

                score = _score_fuzzy_title_match(check_text, search_query)

                if score > 0:
                    matching_subs.append((score, sub))
                    logging.info("[MANUAL-SEARCH] Fuzzy match: score=%d release=%r", score, release)

        result["matched"] = len(matching_subs)

        if matching_subs:
            # Sort by score descending, then by download count
            matching_subs.sort(key=lambda x: (-x[0], _rank_key(x[1])))
            subs_to_download = [sub for score, sub in matching_subs[:max_downloads]]
            logging.info("[MANUAL-SEARCH] Using %d scored matches", len(subs_to_download))
        else:
            # No matches - fallback to top results by download count
            logging.warning("[MANUAL-SEARCH] No fuzzy matches, falling back to top results by download count")
            subs_to_download = sorted(all_results, key=_rank_key)[:max_downloads]

        # Download subtitles
        if subs_to_download and _login_with_retry(provider):
            for idx, sub in enumerate(subs_to_download, 1):
                try:
                    ok, provider = _download_with_session(provider, sub)
                    if not ok:
                        result["errors"].append(f"Download failed for {getattr(sub, 'id', '?')}")
                        continue
                except DownloadLimitReached:
                    result["errors"].append("Download limit reached")
                    break
                except Exception as e:
                    result["errors"].append(f"Download error: {e}")
                    continue

                data = getattr(sub, "content", None)
                if not data:
                    continue

                hi = ".hi" if getattr(sub, "hearing_impaired", False) else ""
                release = getattr(sub, "release", "manual") or "manual"
                # Clean release name for filename
                safe_release = re.sub(r'[^\w\-.]', '_', release)[:50]
                out_file = out_dir / f"{stem}.{lang2}.{idx}.manual.{safe_release}{hi}.srt"

                with contextlib.suppress(Exception):
                    out_file.write_bytes(data)
                    result["saved"].append(str(out_file))
                    logging.info("[MANUAL-SEARCH] Saved: %s", out_file.name)

        logging.info("[MANUAL-SEARCH] Complete. Saved %d subtitles.", len(result["saved"]))
        return result

    finally:
        if provider:
            with contextlib.suppress(Exception):
                provider.terminate()