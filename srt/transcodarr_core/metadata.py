# srt/transcodarr_core/metadata.py
"""
Fetch and cache media metadata (descriptions, etc.) from Radarr/Sonarr APIs.
"""
import logging
import requests
from typing import Optional, Dict
from .config import Settings
from .database import upsert_media_metadata, get_media_metadata

settings = Settings()


def _radarr_session() -> requests.Session:
    """Get a Radarr API session."""
    s = requests.Session()
    s.headers.update({"X-Api-Key": settings.RADARR_API_KEY})
    return s


def _sonarr_session() -> requests.Session:
    """Get a Sonarr API session."""
    s = requests.Session()
    s.headers.update({"X-Api-Key": settings.SONARR_API_KEY})
    return s


def fetch_movie_metadata(imdb_id: str = None, tmdb_id: str = None, title: str = None, year: int = None) -> Optional[Dict]:
    """
    Fetch movie metadata from Radarr API and cache it.
    Returns cached data if available, otherwise fetches fresh.
    """
    # Check cache first
    cached = get_media_metadata("movie", imdb_id=imdb_id, tmdb_id=str(tmdb_id) if tmdb_id else None)
    if cached and cached.get("description"):
        logging.debug("[METADATA] Cache hit for movie: %s", imdb_id or tmdb_id or title)
        return cached

    if not settings.RADARR_URL or not settings.RADARR_API_KEY:
        logging.debug("[METADATA] Radarr not configured, skipping movie metadata fetch")
        return cached

    try:
        with _radarr_session() as session:
            # Get all movies from Radarr
            r = session.get(f"{settings.RADARR_URL}/api/v3/movie", timeout=settings.RADARR_TIMEOUT_S)
            r.raise_for_status()
            movies = r.json()

            # Find matching movie
            target = None
            for movie in movies:
                if imdb_id and movie.get("imdbId") == imdb_id:
                    target = movie
                    break
                if tmdb_id and movie.get("tmdbId") == tmdb_id:
                    target = movie
                    break
                if title and movie.get("title", "").lower() == title.lower():
                    if year is None or movie.get("year") == year:
                        target = movie
                        break

            if not target:
                logging.debug("[METADATA] Movie not found in Radarr: %s", imdb_id or tmdb_id or title)
                return cached

            # Extract metadata
            metadata = {
                "imdb_id": target.get("imdbId"),
                "tmdb_id": str(target.get("tmdbId")) if target.get("tmdbId") else None,
                "title": target.get("title"),
                "year": target.get("year"),
                "description": target.get("overview"),
                "genres": ", ".join(target.get("genres", [])),
                "rating": target.get("ratings", {}).get("imdb", {}).get("value") or
                         target.get("ratings", {}).get("tmdb", {}).get("value"),
                "runtime": target.get("runtime"),
                "status": target.get("status"),
                "source": "radarr",
            }

            # Cache it
            if metadata.get("imdb_id") or metadata.get("tmdb_id"):
                upsert_media_metadata("movie", metadata)
                logging.info("[METADATA] Cached movie metadata: %s (%s)", metadata["title"], metadata.get("year"))

            return metadata

    except Exception as e:
        logging.warning("[METADATA] Failed to fetch movie metadata: %s", e)
        return cached


def fetch_series_metadata(imdb_id: str = None, tvdb_id: int = None, tmdb_id: int = None,
                          title: str = None) -> Optional[Dict]:
    """
    Fetch TV series metadata from Sonarr API and cache it.
    Returns cached data if available, otherwise fetches fresh.
    """
    # Check cache first
    cached = get_media_metadata("series", imdb_id=imdb_id, tmdb_id=str(tmdb_id) if tmdb_id else None)
    if cached and cached.get("description"):
        logging.debug("[METADATA] Cache hit for series: %s", imdb_id or tvdb_id or title)
        return cached

    if not settings.SONARR_URL or not settings.SONARR_API_KEY:
        logging.debug("[METADATA] Sonarr not configured, skipping series metadata fetch")
        return cached

    try:
        with _sonarr_session() as session:
            # Get all series from Sonarr
            r = session.get(f"{settings.SONARR_URL}/api/v3/series", timeout=settings.SONARR_TIMEOUT_S)
            r.raise_for_status()
            series_list = r.json()

            # Find matching series
            target = None
            for series in series_list:
                if imdb_id and series.get("imdbId") == imdb_id:
                    target = series
                    break
                if tvdb_id and series.get("tvdbId") == tvdb_id:
                    target = series
                    break
                if tmdb_id and series.get("tmdbId") == tmdb_id:
                    target = series
                    break
                if title and series.get("title", "").lower() == title.lower():
                    target = series
                    break

            if not target:
                logging.debug("[METADATA] Series not found in Sonarr: %s", imdb_id or tvdb_id or title)
                return cached

            # Extract metadata
            metadata = {
                "imdb_id": target.get("imdbId"),
                "tmdb_id": str(target.get("tmdbId")) if target.get("tmdbId") else None,
                "tvdb_id": str(target.get("tvdbId")) if target.get("tvdbId") else None,
                "title": target.get("title"),
                "year": target.get("year"),
                "description": target.get("overview"),
                "genres": ", ".join(target.get("genres", [])),
                "rating": target.get("ratings", {}).get("value"),
                "runtime": target.get("runtime"),
                "status": target.get("status"),
                "network": target.get("network"),
                "source": "sonarr",
            }

            # Cache it
            if metadata.get("imdb_id") or metadata.get("tmdb_id") or metadata.get("tvdb_id"):
                upsert_media_metadata("series", metadata)
                logging.info("[METADATA] Cached series metadata: %s (%s)", metadata["title"], metadata.get("year"))

            return metadata

    except Exception as e:
        logging.warning("[METADATA] Failed to fetch series metadata: %s", e)
        return cached


def get_movie_description(imdb_id: str = None, tmdb_id: str = None,
                          title: str = None, year: int = None) -> Optional[str]:
    """Convenience function to get just the movie description."""
    metadata = fetch_movie_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, title=title, year=year)
    return metadata.get("description") if metadata else None


def get_series_description(imdb_id: str = None, tvdb_id: int = None, tmdb_id: int = None,
                           title: str = None) -> Optional[str]:
    """Convenience function to get just the series description."""
    metadata = fetch_series_metadata(imdb_id=imdb_id, tvdb_id=tvdb_id, tmdb_id=tmdb_id, title=title)
    return metadata.get("description") if metadata else None
