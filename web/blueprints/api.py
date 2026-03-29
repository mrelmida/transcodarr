# web/blueprints/api.py
from flask import Blueprint, current_app, jsonify, request, send_file, abort
from urllib.parse import quote
from threading import Thread
from pathlib import Path
from transcodarr_core import core_walk_and_process, core_transcode_file
from dotenv import dotenv_values
import os, re, json, time, math, subprocess, logging, collections
import psutil
from contextlib import suppress

from transcodarr_core import start_watchdog, get_duration_seconds  # <- reuse your ffprobe helper
from transcodarr_core.posters import ensure_poster
from transcodarr_core.database import (
    init_database, get_transcode_history, get_all_transcode_history,
    upsert_movie, get_movie, get_all_movies,
    upsert_tv_episode, get_tv_episode, get_all_tv_episodes,
    get_media_metadata,
    set_ignored, remove_ignored, is_ignored, get_all_ignored, get_ignored_paths,
    get_setting, set_setting, get_all_settings, bulk_set_settings,
    insert_storage_snapshot, get_storage_history, prune_storage_history,
)
from transcodarr_core.metadata import fetch_movie_metadata, fetch_series_metadata
from transcodarr_core.enrich import enrich_media

api_bp = Blueprint("api", __name__)  # <-- name it api_bp to avoid confusion

_state = {"running": False, "thread": None}

# ----------------------- system stats collector -----------------------
import threading as _threading

_stats_lock = _threading.Lock()
_cpu_history = collections.deque(maxlen=2880)       # 24h at 30s intervals
_ram_history = collections.deque(maxlen=2880)
_stats_timestamps = collections.deque(maxlen=2880)
_collector_started = False


def _stats_collector():
    """Background daemon: sample CPU/RAM every ~30s, disk every ~5 min."""
    tick = 0
    while True:
        now = time.time()
        cpu = psutil.cpu_percent(interval=1)   # 1s blocking sample for accuracy
        mem = psutil.virtual_memory()

        with _stats_lock:
            _stats_timestamps.append(now)
            _cpu_history.append(cpu)
            _ram_history.append(mem.percent)

        # Every 10 ticks (~5 min) record disk snapshot to DB
        if tick % 10 == 0:
            try:
                from transcodarr_core.config import Settings
                output = Settings().OUTPUT_FOLDER
                if output:
                    disk = psutil.disk_usage(output)
                    insert_storage_snapshot(disk.total, disk.used, disk.free)
                    prune_storage_history(keep_days=90)
            except Exception:
                pass

        tick += 1
        time.sleep(29)  # ~30s total with the 1s cpu sample


def start_stats_collector():
    """Start the stats collector daemon thread (idempotent)."""
    global _collector_started
    if _collector_started:
        return
    _collector_started = True
    t = Thread(target=_stats_collector, daemon=True)
    t.start()
    logging.info("[STATS] System stats collector started")


# Auto-start collector on module import
start_stats_collector()

# ----------------------- run lock helpers -----------------------
def acquire_run_lock(path: str):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))

def release_run_lock(path: str):
    with suppress(FileNotFoundError):
        os.remove(path)

def is_running_lock(path: str) -> bool:
    return os.path.exists(path)

# ----------------------- watchdog bg thread -----------------------
def _bg(settings, stop_flag_fn, set_stop_flag_fn, lock_path, debounce_sec: float):
    try:
        _state["running"] = True
        set_stop_flag_fn(False)

        # Check AUTO_WORKERS — if 0, don't run watchdog, just idle
        from transcodarr_core.config import get_setting
        try:
            auto_workers = int(get_setting("AUTO_WORKERS", settings.AUTO_WORKERS))
        except (ValueError, TypeError):
            auto_workers = settings.AUTO_WORKERS

        if auto_workers > 0:
            start_watchdog(
                settings=settings,
                stop_flag_fn=stop_flag_fn,
                debounce_sec=debounce_sec,
            )
        else:
            logging.info("[BG] AUTO_WORKERS=0, watchdog disabled. Idling...")
            while not stop_flag_fn():
                import time
                time.sleep(1)
    finally:
        _state["running"] = False
        release_run_lock(lock_path)

# ----------------------- control routes -----------------------
@api_bp.post("/start")
def start():
    if _state.get("thread") and _state["thread"].is_alive():
        return jsonify({"status":"already running"}), 400

    lock_path = current_app.config["RUN_LOCK_PATH"]
    try:
        acquire_run_lock(lock_path)
    except FileExistsError:
        return jsonify({"status":"already running"}), 400

    settings          = current_app.config["SETTINGS"]
    stop_flag_fn      = current_app.config["STOP_FLAG_FN"]
    set_stop_flag_fn  = current_app.config["SET_STOP_FLAG_FN"]
    from transcodarr_core.config import get_setting
    debounce_sec      = float(get_setting("WATCH_DEBOUNCE_SEC", 20.0))

    # Ensure auto executor is alive (may have been shut down by stop)
    worker_pool = current_app.config.get("WORKER_POOL")
    if worker_pool:
        worker_pool.start_auto()

    t = Thread(
        target=_bg,
        args=(settings, stop_flag_fn, set_stop_flag_fn, lock_path, debounce_sec),
        daemon=True,
    )
    _state["thread"] = t
    t.start()
    return jsonify({"status": "started", "debounce_sec": debounce_sec})

@api_bp.get("/status")
def api_status():
    s = current_app.config["SETTINGS"]
    running = is_running_lock(current_app.config["RUN_LOCK_PATH"])
    return jsonify({
        "status": "running" if running else "stopped",
        "watch_folder": s.WATCH_FOLDER,
        "output_folder": s.OUTPUT_FOLDER,
    })

@api_bp.post("/stop")
def stop():
    current_app.config["SET_STOP_FLAG_FN"](True)
    # Cancel queued auto jobs so they don't keep running after stop
    worker_pool = current_app.config.get("WORKER_POOL")
    if worker_pool:
        worker_pool.stop_auto()
    return jsonify({"status": "stopping"})


# ----------------------- settings endpoints -----------------------
# Define all settings with metadata for the UI
SETTINGS_SCHEMA = {
    "encoding": {
        "label": "Encoding",
        "fields": {
            "TARGET_VIDEO_CODEC": {"label": "Video Codec", "type": "select", "options": [
                {"value": "h264", "label": "H.264 (x264)"},
                {"value": "h265", "label": "H.265 (x265)"},
                {"value": "vp9", "label": "VP9"},
                {"value": "av1", "label": "AV1"},
            ]},
            "TARGET_AUDIO_CODEC": {"label": "Audio Codec", "type": "select", "options": [
                {"value": "aac", "label": "AAC"},
                {"value": "ac3", "label": "AC3 (Dolby Digital)"},
                {"value": "eac3", "label": "EAC3 (Dolby Digital Plus)"},
                {"value": "flac", "label": "FLAC"},
                {"value": "opus", "label": "Opus"},
            ]},
            "TARGET_CONTAINER": {"label": "Container", "type": "select", "options": [
                {"value": ".mp4", "label": "MP4 (.mp4)"},
                {"value": ".mkv", "label": "Matroska (.mkv)"},
                {"value": ".webm", "label": "WebM (.webm)"},
            ]},
            "TARGET_RESOLUTION": {"label": "Resolution", "type": "select", "options": [
                {"value": "source", "label": "Match Source"},
                {"value": "1280x720", "label": "720p (1280x720)"},
                {"value": "1920x1080", "label": "1080p (1920x1080)"},
                {"value": "1080p_max", "label": "1080p Max (no upscale)"},
                {"value": "2560x1440", "label": "1440p (2560x1440)"},
                {"value": "3840x2160", "label": "4K (3840x2160)"},
            ]},
            "TARGET_PRESET": {"label": "Preset", "type": "select", "options": [
                {"value": "ultrafast", "label": "Ultrafast"},
                {"value": "superfast", "label": "Superfast"},
                {"value": "veryfast", "label": "Veryfast"},
                {"value": "faster", "label": "Faster"},
                {"value": "fast", "label": "Fast"},
                {"value": "medium", "label": "Medium"},
                {"value": "slow", "label": "Slow"},
                {"value": "slower", "label": "Slower"},
                {"value": "veryslow", "label": "Veryslow"},
            ]},
            "TARGET_PROFILE": {"label": "Profile", "type": "select", "options": [
                {"value": "baseline", "label": "Baseline"},
                {"value": "main", "label": "Main"},
                {"value": "high", "label": "High"},
            ]},
            "TARGET_AUDIO_BITRATE": {"label": "Audio Bitrate", "type": "select", "options": [
                {"value": "128k", "label": "128k"},
                {"value": "192k", "label": "192k"},
                {"value": "256k", "label": "256k"},
                {"value": "320k", "label": "320k"},
                {"value": "448k", "label": "448k"},
            ]},
            "TARGET_AUDIO_CHANNELS": {"label": "Audio Channels", "type": "select", "options": [
                {"value": "2", "label": "2 (Stereo)"},
                {"value": "6", "label": "6 (5.1)"},
                {"value": "8", "label": "8 (7.1)"},
            ]},
            "TARGET_CRF": {"label": "CRF", "type": "select", "options": [
                {"value": "", "label": "Default (codec decides)"},
                {"value": "18", "label": "18 (Visually Lossless)"},
                {"value": "20", "label": "20"},
                {"value": "23", "label": "23 (x264 Default)"},
                {"value": "26", "label": "26"},
                {"value": "28", "label": "28"},
                {"value": "30", "label": "30"},
            ]},
            "TARGET_AUDIO_NORMALIZE": {"label": "Audio Normalization", "type": "select", "options": [
                {"value": "true", "label": "Enabled"},
                {"value": "false", "label": "Disabled"},
            ]},
            "FFMPEG_THREADS": {"label": "FFmpeg Threads", "type": "select", "options": [
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "4", "label": "4"},
                {"value": "8", "label": "8"},
                {"value": "0", "label": "Auto (all cores)"},
            ]},
            "X264_THREADS": {"label": "x264 Threads", "type": "select", "options": [
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "4", "label": "4"},
                {"value": "8", "label": "8"},
                {"value": "0", "label": "Auto (all cores)"},
            ]},
            "COMPRESSION_TIERS_ENABLED": {"label": "Compression Tiers", "type": "select", "options": [
                {"value": "false", "label": "Disabled"},
                {"value": "true", "label": "Enabled"},
            ]},
        }
    },
    "radarr": {
        "label": "Radarr",
        "fields": {
            "RADARR_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:7878"},
            "RADARR_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "RADARR_PATH_FROM": {"label": "Path From", "type": "text", "placeholder": "/downloads/movies"},
            "RADARR_PATH_TO": {"label": "Path To", "type": "text", "placeholder": "/movies"},
        }
    },
    "sonarr": {
        "label": "Sonarr",
        "fields": {
            "SONARR_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:8989"},
            "SONARR_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "SONARR_PATH_FROM": {"label": "Path From", "type": "text", "placeholder": "/downloads/tv"},
            "SONARR_PATH_TO": {"label": "Path To", "type": "text", "placeholder": "/tv"},
        }
    },
    "jellyfin": {
        "label": "Jellyfin",
        "fields": {
            "JELLYFIN_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:8096"},
            "JELLYFIN_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
        }
    },
    "subtitles": {
        "label": "Subtitles",
        "type": "subtitle_providers",  # Special type for provider accounts UI
        "fields": {
            "FFSUBSYNC_MAX_OFFSET": {"label": "Max Sync Offset", "type": "text", "placeholder": "0.5"},
        }
    },
    "api_keys": {
        "label": "API Keys",
        "fields": {
            "TVDB_API_KEY": {"label": "TVDB API Key", "type": "password", "placeholder": ""},
            "TMDB_API_KEY": {"label": "TMDB API Key", "type": "password", "placeholder": ""},
            "OMDB_API_KEY": {"label": "OMDB API Key", "type": "password", "placeholder": ""},
        }
    },
    "database": {
        "label": "Database",
        "fields": {
            "POSTGRES_HOST": {"label": "PostgreSQL Host", "type": "text", "placeholder": "localhost"},
            "POSTGRES_PORT": {"label": "PostgreSQL Port", "type": "text", "placeholder": "5432"},
            "POSTGRES_DB": {"label": "Database Name", "type": "text", "placeholder": "transcodarr"},
            "POSTGRES_USER": {"label": "Username", "type": "text", "placeholder": "transcodarr"},
            "POSTGRES_PASSWORD": {"label": "Password", "type": "password", "placeholder": ""},
        }
    },
    "advanced": {
        "label": "Advanced",
        "fields": {
            "MANUAL_WORKERS": {"label": "Manual Workers", "type": "select", "hint": "Workers for UI-triggered transcodes", "options": [
                {"value": "0", "label": "0 (Disabled)"},
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3", "label": "3"},
                {"value": "4", "label": "4"},
            ]},
            "AUTO_WORKERS": {"label": "Auto Workers", "type": "select", "hint": "Workers for automatic watchdog transcodes", "options": [
                {"value": "0", "label": "0 (Disabled)"},
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3", "label": "3"},
                {"value": "4", "label": "4"},
            ]},
            "TRANSCODARR_URL": {"label": "Transcodarr URL", "type": "text", "placeholder": "http://localhost:5025", "hint": "External URL for webhooks (auto-detected if empty)"},
            "WATCH_DEBOUNCE_SEC": {"label": "Watch Debounce (sec)", "type": "text", "placeholder": "20"},
        }
    },
    "connections": {
        "label": "Connections",
        "type": "connections",  # Special type handled by UI
        "fields": {}
    },
}

def _get_env_path() -> Path:
    """Get the .env file path."""
    # Check for custom path (useful for Docker volume mounts)
    custom_path = os.environ.get("ENV_FILE_PATH")
    if custom_path:
        return Path(custom_path)
    return Path(__file__).parent.parent.parent / ".env"


def _set_env_key(env_path: Path, key: str, value: str) -> None:
    """
    Set a key in .env file without using temp files.
    Writes directly to the file to avoid permission issues with Docker bind mounts.
    """
    # Read current content
    lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    # Find and update or append the key
    key_found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            # Replace this line
            if value:
                # Quote value if it contains spaces or special chars
                if any(c in value for c in [' ', '"', "'", '\n', '\t', '#']):
                    new_lines.append(f'{key}="{value}"\n')
                else:
                    new_lines.append(f"{key}={value}\n")
            else:
                new_lines.append(f"{key}=\n")
            key_found = True
        else:
            new_lines.append(line)

    # Append if not found
    if not key_found:
        if value:
            if any(c in value for c in [' ', '"', "'", '\n', '\t', '#']):
                new_lines.append(f'{key}="{value}"\n')
            else:
                new_lines.append(f"{key}={value}\n")
        else:
            new_lines.append(f"{key}=\n")

    # Write directly (no temp file)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

@api_bp.get("/settings")
def api_get_settings():
    """Return all settings with schema for UI rendering."""
    try:
        # Load current values from database
        db_values = get_all_settings()

        # Also check current Settings object for defaults (and env fallback)
        s = current_app.config["SETTINGS"]

        # Build response with current values
        result = {"schema": SETTINGS_SCHEMA, "values": {}}

        for section_key, section in SETTINGS_SCHEMA.items():
            for field_key in section["fields"]:
                # Priority: database -> Settings object (which reads env) -> empty
                value = db_values.get(field_key)
                if value is None:
                    value = getattr(s, field_key, None)
                # Don't send None, send empty string
                result["values"][field_key] = value if value is not None else ""

        return jsonify(result)
    except Exception as e:
        logging.exception("[SETTINGS] Failed to get settings")
        return jsonify({"error": str(e), "type": type(e).__name__}), 500

@api_bp.post("/settings")
def api_save_settings():
    """Save settings to database."""
    from transcodarr_core.config import DB_BACKED_SETTINGS

    data = request.get_json() or {}

    updated = []
    errors = []

    # Get list of valid keys from schema
    valid_keys = set()
    for section in SETTINGS_SCHEMA.values():
        valid_keys.update(section["fields"].keys())

    for key, value in data.items():
        if key not in valid_keys:
            continue  # Skip unknown keys
        if key not in DB_BACKED_SETTINGS:
            continue  # Skip infrastructure keys (container paths, etc.)

        try:
            # Save to database
            if set_setting(key, str(value) if value is not None else ""):
                updated.append(key)
            else:
                errors.append({"key": key, "error": "Database write failed"})
        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    # Live-reconfigure worker pool if worker counts changed
    worker_keys = {"MANUAL_WORKERS", "AUTO_WORKERS"}
    if worker_keys & set(updated):
        worker_pool = current_app.config.get("WORKER_POOL")
        if worker_pool:
            try:
                from transcodarr_core.config import get_setting
                mw = int(get_setting("MANUAL_WORKERS", 0))
                aw = int(get_setting("AUTO_WORKERS", 2))
                worker_pool.reconfigure(mw, aw)
            except Exception as e:
                logging.warning("[SETTINGS] Failed to reconfigure worker pool: %s", e)

    return jsonify({
        "status": "ok" if not errors else "partial",
        "updated": updated,
        "errors": errors,
        "message": "Settings saved." if updated else "No changes made."
    })


@api_bp.post("/settings/migrate-from-env")
def api_migrate_settings_from_env():
    """
    One-time migration: Copy runtime settings from .env to database.
    Only copies keys that are designated as DB_BACKED_SETTINGS.
    Existing database values are NOT overwritten.
    """
    from transcodarr_core.config import DB_BACKED_SETTINGS

    env_path = _get_env_path()
    if not env_path.exists():
        return jsonify({"error": "No .env file found", "migrated": [], "skipped": []}), 404

    env_values = dotenv_values(env_path)
    migrated = []
    skipped = []
    errors = []

    # Get existing DB settings to avoid overwriting
    existing_db = get_all_settings()

    for key in DB_BACKED_SETTINGS:
        env_val = env_values.get(key)
        if env_val is None:
            continue  # Not in .env, skip

        # Skip if already in database
        if key in existing_db and existing_db[key]:
            skipped.append({"key": key, "reason": "Already in database"})
            continue

        try:
            if set_setting(key, env_val):
                migrated.append(key)
                logging.info(f"[MIGRATE] Migrated {key} to database")
            else:
                errors.append({"key": key, "error": "Database write failed"})
        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    return jsonify({
        "status": "ok" if not errors else "partial",
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "message": f"Migrated {len(migrated)} settings from .env to database"
    })


# ----------------------- subtitle providers -----------------------
# Supported providers with their configuration requirements
SUBTITLE_PROVIDERS = {
    "opensubtitlescom": {
        "name": "OpenSubtitles.com",
        "requires_auth": True,
        "supports_multiple_accounts": True,
        "config_key": "SUBLIMINAL_OSCOM_ACCOUNTS",  # JSON list of accounts
        "enabled_key": "SUBLIMINAL_OSCOM_ENABLED",   # independent on/off toggle
        "legacy_user_key": "SUBLIMINAL_OSCOM_USER",
        "legacy_pass_key": "SUBLIMINAL_OSCOM_PASS",
    },
    "podnapisi": {
        "name": "Podnapisi",
        "requires_auth": False,
        "supports_multiple_accounts": False,
        "config_key": "SUBLIMINAL_PODNAPISI_ENABLED",
        "enabled_key": "SUBLIMINAL_PODNAPISI_ENABLED",
    },
    "addic7ed": {
        "name": "Addic7ed",
        "requires_auth": True,
        "supports_multiple_accounts": True,
        "config_key": "SUBLIMINAL_ADDIC7ED_ACCOUNTS",
        "enabled_key": "SUBLIMINAL_ADDIC7ED_ENABLED",
    },
    "tvsubtitles": {
        "name": "TVsubtitles",
        "requires_auth": False,
        "supports_multiple_accounts": False,
        "config_key": "SUBLIMINAL_TVSUBTITLES_ENABLED",
        "enabled_key": "SUBLIMINAL_TVSUBTITLES_ENABLED",
    },
}


@api_bp.get("/subtitle-providers")
def api_get_subtitle_providers():
    """Get all subtitle provider configurations."""
    # Load from database with env fallback
    db_values = get_all_settings()
    s = current_app.config["SETTINGS"]

    def get_value(key):
        """Get value from DB first, then Settings object (env fallback)."""
        val = db_values.get(key)
        if val is None:
            val = getattr(s, key, None)
        return val or ""

    result = {"providers": {}}

    for provider_id, provider_info in SUBTITLE_PROVIDERS.items():
        provider_data = {
            "name": provider_info["name"],
            "requires_auth": provider_info["requires_auth"],
            "supports_multiple_accounts": provider_info["supports_multiple_accounts"],
            "enabled": False,
            "accounts": [],
        }

        if provider_info["requires_auth"] and provider_info["supports_multiple_accounts"]:
            # Load accounts from JSON config
            accounts_json = get_value(provider_info["config_key"])
            if accounts_json:
                try:
                    accounts = json.loads(accounts_json)
                    if isinstance(accounts, list):
                        # Don't send passwords to frontend, just usernames
                        provider_data["accounts"] = [
                            {"user": acc.get("user", ""), "has_pass": bool(acc.get("pass"))}
                            for acc in accounts if isinstance(acc, dict)
                        ]
                except json.JSONDecodeError:
                    pass

            # Check legacy single account config
            if not provider_data["accounts"] and "legacy_user_key" in provider_info:
                legacy_user = get_value(provider_info["legacy_user_key"])
                legacy_pass = get_value(provider_info.get("legacy_pass_key", ""))
                if legacy_user:
                    provider_data["accounts"] = [{"user": legacy_user, "has_pass": bool(legacy_pass)}]

        # Load enabled state from the dedicated enabled_key
        enabled_key = provider_info.get("enabled_key")
        if enabled_key:
            enabled_val = get_value(enabled_key)
            if enabled_val:
                provider_data["enabled"] = enabled_val.lower() in ("true", "1", "yes")
            elif provider_info["requires_auth"] and provider_data["accounts"]:
                # Migration: accounts exist but toggle never set → default enabled
                provider_data["enabled"] = True

        result["providers"][provider_id] = provider_data

    return jsonify(result)


@api_bp.post("/subtitle-providers/<provider_id>/accounts")
def api_add_subtitle_account(provider_id: str):
    """Add a new account for a subtitle provider."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 404

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    if not provider_info["requires_auth"] or not provider_info["supports_multiple_accounts"]:
        return jsonify({"error": "Provider does not support accounts"}), 400

    data = request.get_json() or {}
    username = (data.get("user") or "").strip()
    password = (data.get("pass") or "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    config_key = provider_info["config_key"]
    logging.info(f"[SUBTITLE-API] Adding account for {provider_id}")

    # Load existing accounts from database
    accounts_json = get_setting(config_key, "")
    accounts = []
    if accounts_json:
        try:
            accounts = json.loads(accounts_json)
            if not isinstance(accounts, list):
                accounts = []
        except json.JSONDecodeError:
            accounts = []

    # Check for duplicate username
    for acc in accounts:
        if acc.get("user") == username:
            return jsonify({"error": f"Account '{username}' already exists"}), 400

    # Add new account
    accounts.append({"user": username, "pass": password})

    # Save to database
    try:
        if set_setting(config_key, json.dumps(accounts)):
            logging.info(f"[SUBTITLE-API] Saved {len(accounts)} accounts to {config_key}")
        else:
            return jsonify({"error": "Database write failed"}), 500
    except Exception as e:
        logging.error(f"[SUBTITLE-API] Failed to save accounts: {e}")
        return jsonify({"error": f"Failed to save: {e}"}), 500

    return jsonify({"status": "ok", "message": f"Account '{username}' added"})


@api_bp.delete("/subtitle-providers/<provider_id>/accounts/<username>")
def api_delete_subtitle_account(provider_id: str, username: str):
    """Delete an account from a subtitle provider."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 404

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    if not provider_info["requires_auth"] or not provider_info["supports_multiple_accounts"]:
        return jsonify({"error": "Provider does not support accounts"}), 400

    config_key = provider_info["config_key"]

    # Load existing accounts from database
    accounts_json = get_setting(config_key, "")
    accounts = []
    if accounts_json:
        try:
            accounts = json.loads(accounts_json)
            if not isinstance(accounts, list):
                accounts = []
        except json.JSONDecodeError:
            accounts = []

    # Find and remove account
    original_count = len(accounts)
    accounts = [acc for acc in accounts if acc.get("user") != username]

    if len(accounts) == original_count:
        return jsonify({"error": f"Account '{username}' not found"}), 404

    # Save to database
    if accounts:
        set_setting(config_key, json.dumps(accounts))
    else:
        set_setting(config_key, "")

    return jsonify({"status": "ok", "message": f"Account '{username}' removed"})


@api_bp.post("/subtitle-providers/<provider_id>/toggle")
def api_toggle_subtitle_provider(provider_id: str):
    """Toggle a subtitle provider on/off."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 404

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    enabled_key = provider_info.get("enabled_key")
    if not enabled_key:
        return jsonify({"error": "Provider does not support toggling"}), 400

    data = request.get_json() or {}
    enabled = data.get("enabled", False)

    logging.info(f"[SUBTITLE-API] Toggling {provider_id} to {enabled}")

    try:
        if set_setting(enabled_key, "true" if enabled else "false"):
            logging.info(f"[SUBTITLE-API] Set {enabled_key}={'true' if enabled else 'false'}")
        else:
            return jsonify({"error": "Database write failed"}), 500
    except Exception as e:
        logging.error(f"[SUBTITLE-API] Failed to set key: {e}")
        return jsonify({"error": f"Failed to save: {e}"}), 500

    return jsonify({"status": "ok", "enabled": enabled})


# ----------------------- system stats endpoints -----------------------
@api_bp.get("/system/stats")
def api_system_stats():
    """Return current + historical CPU/RAM/disk stats."""
    with _stats_lock:
        timestamps = list(_stats_timestamps)
        cpu = list(_cpu_history)
        ram = list(_ram_history)

    cur_cpu = psutil.cpu_percent(interval=0)
    cur_mem = psutil.virtual_memory()
    output_folder = current_app.config["SETTINGS"].OUTPUT_FOLDER
    try:
        cur_disk = psutil.disk_usage(output_folder)
        disk_info = {
            "total": cur_disk.total,
            "used": cur_disk.used,
            "free": cur_disk.free,
            "percent": cur_disk.percent,
        }
    except Exception:
        disk_info = None

    return jsonify({
        "current": {
            "cpu_percent": cur_cpu,
            "ram_percent": cur_mem.percent,
            "ram_used": cur_mem.used,
            "ram_total": cur_mem.total,
            "disk": disk_info,
        },
        "history": {
            "timestamps": timestamps,
            "cpu": cpu,
            "ram": ram,
        },
    })


@api_bp.get("/system/stats/storage")
def api_storage_history():
    """Return DB-persisted storage history for graphing."""
    rows = get_storage_history()
    return jsonify({"history": rows})


# ----------------------- compression tiers -----------------------
_VALID_PRESETS = {"ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"}
_VALID_CRFS = {"","18","19","20","21","22","23","24","25","26","28","30"}

@api_bp.get("/compression-tiers")
def api_get_compression_tiers():
    """Return compression tiers config and options for the UI."""
    enabled = get_setting("COMPRESSION_TIERS_ENABLED", "false")
    tiers_json = get_setting("COMPRESSION_TIERS", "")
    tiers = []
    if tiers_json:
        try:
            tiers = json.loads(tiers_json)
        except (json.JSONDecodeError, TypeError):
            pass

    preset_options = [
        {"value": "ultrafast", "label": "Ultrafast"},
        {"value": "superfast", "label": "Superfast"},
        {"value": "veryfast", "label": "Veryfast"},
        {"value": "faster", "label": "Faster"},
        {"value": "fast", "label": "Fast"},
        {"value": "medium", "label": "Medium"},
        {"value": "slow", "label": "Slow"},
        {"value": "slower", "label": "Slower"},
        {"value": "veryslow", "label": "Veryslow"},
    ]
    crf_options = [
        {"value": "", "label": "Default"},
        {"value": "18", "label": "18"},
        {"value": "19", "label": "19"},
        {"value": "20", "label": "20"},
        {"value": "21", "label": "21"},
        {"value": "22", "label": "22"},
        {"value": "23", "label": "23"},
        {"value": "24", "label": "24"},
        {"value": "25", "label": "25"},
        {"value": "26", "label": "26"},
        {"value": "28", "label": "28"},
        {"value": "30", "label": "30"},
    ]
    return jsonify({
        "enabled": str(enabled).lower() == "true",
        "tiers": tiers,
        "preset_options": preset_options,
        "crf_options": crf_options,
    })

@api_bp.post("/compression-tiers")
def api_save_compression_tiers():
    """Validate and save compression tiers."""
    data = request.get_json() or {}
    tiers = data.get("tiers", [])

    if not isinstance(tiers, list):
        return jsonify({"error": "tiers must be a list"}), 400

    # Validate each tier
    for i, tier in enumerate(tiers):
        try:
            min_gb = float(tier.get("min_gb", 0))
            max_gb = float(tier.get("max_gb", 0))
        except (ValueError, TypeError):
            return jsonify({"error": f"Tier {i+1}: invalid size values"}), 400

        if min_gb < 0:
            return jsonify({"error": f"Tier {i+1}: min_gb cannot be negative"}), 400
        if max_gb < 0:
            return jsonify({"error": f"Tier {i+1}: max_gb cannot be negative"}), 400
        if max_gb != 0 and max_gb <= min_gb:
            return jsonify({"error": f"Tier {i+1}: max_gb must be greater than min_gb (or 0 for unlimited)"}), 400

        preset = tier.get("preset", "")
        if preset not in _VALID_PRESETS:
            return jsonify({"error": f"Tier {i+1}: invalid preset '{preset}'"}), 400

        crf = str(tier.get("crf", ""))
        if crf and crf not in _VALID_CRFS:
            return jsonify({"error": f"Tier {i+1}: invalid CRF '{crf}'"}), 400

    # Sort by min_gb
    tiers.sort(key=lambda t: float(t.get("min_gb", 0)))

    # Check for overlapping ranges
    for i in range(len(tiers) - 1):
        curr_max = float(tiers[i].get("max_gb", 0))
        next_min = float(tiers[i+1].get("min_gb", 0))
        if curr_max == 0:
            return jsonify({"error": f"Tier {i+1}: unlimited max_gb must be the last tier"}), 400
        if curr_max > next_min:
            return jsonify({"error": f"Tiers {i+1} and {i+2} overlap"}), 400

    # Save to DB
    try:
        set_setting("COMPRESSION_TIERS", json.dumps(tiers))
    except Exception as e:
        return jsonify({"error": f"Failed to save: {e}"}), 500

    return jsonify({"status": "ok", "tiers": tiers})


# ----------------------- logs tail -----------------------
@api_bp.get("/logs/tail")
def api_logs_tail():
    """
    Offset-based tail with rotation detection.
    Client sends ?pos=<byte_offset>&inode=<token>.
    Returns JSON: { text, pos, inode, reset }
    """
    log_path = current_app.config.get("LOG_PATH", "logs/transcode.log")
    p = Path(log_path)

    if not p.exists():
        return jsonify({"text": "", "pos": 0, "inode": None, "reset": True})

    st = p.stat()
    inode_token = f"{st.st_dev}:{st.st_ino}"

    try:
        pos = int(request.args.get("pos", "0"))
    except Exception:
        pos = 0
    client_inode = request.args.get("inode")

    reset = False
    if client_inode and client_inode != inode_token:
        reset = True
        pos = 0
    elif pos > st.st_size:
        reset = True
        pos = 0

    with open(p, "rb") as f:
        f.seek(pos)
        data = f.read()
        new_pos = pos + len(data)

    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return jsonify({"text": text, "pos": new_pos, "inode": inode_token, "reset": reset})

# ===================================================================
#                       MEDIA LIST ENDPOINTS (with caching)
# ===================================================================

_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi"}

# Sentinel file name - same as used in pipeline.py for "no subtitles" case
_SENTINEL_NAME = ".transcodarr-nosub"


def _has_sentinel(file_path: str) -> bool:
    """Check if a sentinel file exists in the media file's folder."""
    sentinel_path = os.path.join(os.path.dirname(file_path), _SENTINEL_NAME)
    return os.path.exists(sentinel_path)


def _remove_sentinel(file_path: str) -> bool:
    """Remove sentinel file from the media file's folder if it exists."""
    sentinel_path = os.path.join(os.path.dirname(file_path), _SENTINEL_NAME)
    if os.path.exists(sentinel_path):
        os.remove(sentinel_path)
        return True
    return False

# In-memory cache + background scan state
_media_cache = {
    "movies": {"items": [], "last_scan": 0, "scanning": False},
    "tv": {"items": [], "last_scan": 0, "scanning": False},
}
_CACHE_DIR = Path(__file__).parent.parent / "cache"

def _get_cache_path(media_type: str) -> Path:
    _CACHE_DIR.mkdir(exist_ok=True)
    return _CACHE_DIR / f"{media_type}_cache.json"

def _load_cache(media_type: str) -> list[dict]:
    """Load from disk cache into memory."""
    cache_path = _get_cache_path(media_type)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _media_cache[media_type]["items"] = data.get("items", [])
                _media_cache[media_type]["last_scan"] = data.get("last_scan", 0)
                return _media_cache[media_type]["items"]
        except Exception:
            pass
    return []

def _save_cache(media_type: str, items: list[dict]):
    """Save to disk and memory."""
    _media_cache[media_type]["items"] = items
    _media_cache[media_type]["last_scan"] = int(time.time())
    cache_path = _get_cache_path(media_type)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"items": items, "last_scan": _media_cache[media_type]["last_scan"]}, f)
    except Exception:
        pass

def _bytes_to_gb(n: int) -> float:
    try:
        return round(n / (1024**3), 2)
    except Exception:
        return 0.0

def _ffprobe_metadata(path: str) -> dict:
    """Return detailed metadata dict using ffprobe."""
    result_dict = {
        "vcodec": None, "acodec": None, "resolution": None, "runtime_min": None,
        "video_bitrate": None, "audio_bitrate": None, "total_bitrate": None,
        "frame_rate": None, "audio_channels": None, "audio_sample_rate": None,
    }
    try:
        # Single ffprobe call for all info
        cmd = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height,bit_rate,r_frame_rate,channels,sample_rate",
             "-show_entries", "format=duration,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, check=False, timeout=15
        )
        data = json.loads(cmd.stdout or "{}")

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and not result_dict["vcodec"]:
                result_dict["vcodec"] = stream.get("codec_name")
                w, h = stream.get("width"), stream.get("height")
                if w and h:
                    result_dict["resolution"] = f"{w}x{h}"
                # Video bitrate
                vbr = stream.get("bit_rate")
                if vbr:
                    try:
                        result_dict["video_bitrate"] = int(vbr)
                    except Exception:
                        pass
                # Frame rate (e.g., "24000/1001" -> 23.976)
                fps_str = stream.get("r_frame_rate")
                if fps_str and "/" in fps_str:
                    try:
                        num, den = fps_str.split("/")
                        result_dict["frame_rate"] = round(int(num) / int(den), 3)
                    except Exception:
                        pass
                elif fps_str:
                    try:
                        result_dict["frame_rate"] = float(fps_str)
                    except Exception:
                        pass

            elif stream.get("codec_type") == "audio" and not result_dict["acodec"]:
                result_dict["acodec"] = stream.get("codec_name")
                # Audio bitrate
                abr = stream.get("bit_rate")
                if abr:
                    try:
                        result_dict["audio_bitrate"] = int(abr)
                    except Exception:
                        pass
                # Channels
                ch = stream.get("channels")
                if ch:
                    result_dict["audio_channels"] = ch
                # Sample rate
                sr = stream.get("sample_rate")
                if sr:
                    try:
                        result_dict["audio_sample_rate"] = int(sr)
                    except Exception:
                        pass

        fmt = data.get("format", {})
        duration = fmt.get("duration")
        if duration:
            result_dict["runtime_min"] = int(round(float(duration) / 60.0))
        # Total bitrate from format
        total_br = fmt.get("bit_rate")
        if total_br:
            try:
                result_dict["total_bitrate"] = int(total_br)
            except Exception:
                pass

    except Exception:
        pass
    return result_dict


def _format_bitrate(bps: int | None) -> str | None:
    """Format bitrate in human-readable form."""
    if not bps:
        return None
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps // 1000} kbps"


def _format_audio_channels(ch: int | None) -> str | None:
    """Format audio channels nicely."""
    if not ch:
        return None
    mapping = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}
    return mapping.get(ch, f"{ch}ch")


def _format_timestamp(ts: int | float | None) -> str | None:
    """Format timestamp as relative or absolute date."""
    if not ts:
        return None
    now = time.time()
    diff = now - ts
    if diff < 60:
        return "Just now"
    elif diff < 3600:
        mins = int(diff // 60)
        return f"{mins}m ago"
    elif diff < 86400:
        hrs = int(diff // 3600)
        return f"{hrs}h ago"
    elif diff < 86400 * 7:
        days = int(diff // 86400)
        return f"{days}d ago"
    else:
        from datetime import datetime
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%b %d, %Y")

_year_re = re.compile(r"\((\d{4})\)")
_sxe_re = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")

def _year_from_name(name: str):
    m = _year_re.search(name)
    return int(m.group(1)) if m else None

def _strip_year_from_title(name: str) -> str:
    """Remove trailing (YYYY) from title, e.g. 'Movie Name (2006)' -> 'Movie Name'"""
    return re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip()

def _parse_sxe(name: str):
    m = _sxe_re.search(name)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _parse_multi_episode(name: str) -> list[int] | None:
    """
    Parse multi-episode codes from filename.
    Examples:
      S03E18E19E20E21 -> [18, 19, 20, 21]
      S03E18-E21 -> [18, 19, 20, 21]  (range)
      S01E05 -> None (single episode, use None)
    Returns list of episode numbers if multi-ep, None if single.
    """
    # Pattern 1: S##E##E##E## (concatenated)
    concat_match = re.search(r'[sS](\d{1,2})([eE]\d{1,3})+', name)
    if concat_match:
        # Extract all episode numbers
        episodes = [int(m.group(1)) for m in re.finditer(r'[eE](\d{1,3})', name)]
        if len(episodes) > 1:
            return episodes

    # Pattern 2: S##E##-E## (range)
    range_match = re.search(r'[sS]\d{1,2}[eE](\d{1,3})-[eE]?(\d{1,3})', name)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if end > start:
            return list(range(start, end + 1))

    return None


def _find_video_for_meta(meta_file: Path, video_exts: set) -> Path | None:
    """
    Given a .meta.json file, find its matching video file in the same folder.
    Matching rules (same as meta.py but reversed):
      1. Exact stem match: video.meta.json -> video.mkv
      2. Episode code match: S01E05.meta.json matches S01E05 - Title.mkv
      3. For non-TV (no episode code in meta): return first video file
    Returns None if no matching video found.
    """
    import logging
    logger = logging.getLogger("transcodarr.api")

    folder = meta_file.parent
    meta_stem = meta_file.stem  # e.g., "S01E05" from "S01E05.meta.json" or ".meta" from ".meta.json"
    if meta_stem.endswith(".meta"):
        meta_stem = meta_stem[:-5]  # Remove .meta suffix if present

    logger.debug(f"[_find_video_for_meta] Looking for video matching meta_stem='{meta_stem}'")

    video_files = [f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in video_exts]
    if not video_files:
        logger.debug(f"[_find_video_for_meta] No video files found in {folder}")
        return None

    logger.debug(f"[_find_video_for_meta] Video files in folder: {[v.name for v in video_files]}")

    # 1. Try exact stem match
    for vf in video_files:
        if vf.stem == meta_stem:
            logger.debug(f"[_find_video_for_meta] Exact stem match found: {vf.name}")
            return vf

    logger.debug(f"[_find_video_for_meta] No exact stem match. Video stems: {[v.stem for v in video_files]}")

    # 2. Try episode code match (for TV)
    meta_ep = _parse_sxe(meta_file.name)
    logger.debug(f"[_find_video_for_meta] meta_ep parsed from '{meta_file.name}': {meta_ep}")
    if meta_ep[0] is not None:  # meta has episode code
        for vf in video_files:
            video_ep = _parse_sxe(vf.name)
            logger.debug(f"[_find_video_for_meta] Comparing meta_ep={meta_ep} with video_ep={video_ep} from '{vf.name}'")
            if video_ep == meta_ep:
                logger.debug(f"[_find_video_for_meta] Episode code match found: {vf.name}")
                return vf
        # No matching episode found - don't return wrong episode's video
        logger.debug(f"[_find_video_for_meta] No episode code match found for {meta_ep}")
        return None

    # 3. Non-TV content: fall back to first video file
    return video_files[0] if video_files else None


def _scan_pending_movies(watch_root: Path, temp_root: Path | None) -> list[dict]:
    """Scan watch folder for movies waiting to be processed (have .meta.json and video file)."""
    items: list[dict] = []
    # Check direct path first (watch folder may already be the processing folder)
    movies_root = watch_root / "movies"
    if not movies_root.exists():
        movies_root = watch_root / "_processing" / "movies"
    if not movies_root.exists():
        return items

    # Get worker pool processing info to mark items as processing
    worker_pool_jobs = _get_worker_pool_processing_paths()

    # Build set of movies currently being processed via temp files (main loop)
    # These will be shown by _scan_processing_movies, so skip them here
    processing_stems = set()
    if temp_root and temp_root.exists():
        # Check both paths for processing files (direct path first)
        for search_path in [temp_root / "movies", temp_root / "_processing" / "movies"]:
            if search_path.exists():
                for p in search_path.rglob("*.tmp.mp4"):
                    processing_stems.add(p.stem.replace(".tmp", ""))

    # Get ignored paths for efficient lookup
    try:
        ignored_paths = get_ignored_paths()
    except Exception:
        ignored_paths = set()

    for meta_file in movies_root.rglob("*.meta.json"):
        try:
            folder = meta_file.parent

            # Find matching video file for this meta.json
            video = _find_video_for_meta(meta_file, _VIDEO_EXTS)
            if not video:
                continue  # No matching video - still downloading or already processed

            # Skip if being processed by main loop (has temp file) - those show in processing scan
            if video.stem in processing_stems:
                continue

            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            stat = video.stat()
            raw_title = data.get("title") or folder.name
            title = _strip_year_from_title(raw_title)
            year = data.get("year") or _year_from_name(raw_title)

            # Check for poster in source folder (alongside meta.json and video)
            poster_url = None
            poster_file = folder / "poster.jpg"
            if not poster_file.exists():
                # Try to generate poster from metadata
                try:
                    poster_meta = {
                        "imdb_id": data.get("imdb_id"),
                        "radarr_movie_id": data.get("radarr_movie_id"),
                    }
                    ensure_poster(str(folder), kind="movie", meta=poster_meta)
                except Exception:
                    pass  # Silently fail - poster is optional

            if poster_file.exists():
                # Build relative path from watch_root for API
                try:
                    rel_path = poster_file.relative_to(watch_root)
                    poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                except ValueError:
                    pass

            mtime = int(stat.st_mtime)
            video_path_str = str(video)
            # Check both database ignore AND sentinel file
            is_file_ignored = (video_path_str in ignored_paths) or _has_sentinel(video_path_str)

            # Check if this file is being processed by worker pool
            pool_job = worker_pool_jobs.get(video_path_str)
            if pool_job:
                status = pool_job["status"]
                progress = pool_job["progress"]
                elapsed = pool_job.get("elapsed")
                elapsed_fmt = pool_job.get("elapsed_fmt")
            else:
                status = "pending"
                progress = None
                elapsed = None
                elapsed_fmt = None

            item = {
                "title": title,
                "year": year,
                "path": video_path_str,
                "size_gb": _bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": video.suffix.lstrip(".").lower(),
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": mtime,
                "mtime_fmt": _format_timestamp(mtime),
                "status": status,
                "progress": progress,
                "elapsed": elapsed,
                "elapsed_fmt": elapsed_fmt,
                "poster": poster_url,
                "ignored": is_file_ignored,
            }
            items.append(item)
        except Exception:
            continue

    return items


def _scan_pending_tv(watch_root: Path, temp_root: Path | None) -> list[dict]:
    """Scan watch folder for TV episodes waiting to be processed."""
    import logging
    logger = logging.getLogger("transcodarr.api")

    items: list[dict] = []
    # Check direct path first (watch folder may already be the processing folder)
    tv_root = watch_root / "tv"
    if not tv_root.exists():
        tv_root = watch_root / "_processing" / "tv"
    if not tv_root.exists():
        logger.debug(f"[_scan_pending_tv] Neither {watch_root / 'tv'} nor {watch_root / '_processing' / 'tv'} exists")
        return items

    logger.debug(f"[_scan_pending_tv] Scanning tv_root: {tv_root}")

    # Get worker pool processing info to mark items as processing
    worker_pool_jobs = _get_worker_pool_processing_paths()

    # Build set of episodes currently being processed via temp files (main loop)
    # These will be shown by _scan_processing_tv, so skip them here
    processing_stems = set()
    if temp_root and temp_root.exists():
        # Check both paths for processing files (direct path first)
        for search_path in [temp_root / "tv", temp_root / "_processing" / "tv"]:
            if search_path.exists():
                for p in search_path.rglob("*.tmp.mp4"):
                    processing_stems.add(p.stem.replace(".tmp", ""))

    # Get ignored paths for efficient lookup
    try:
        ignored_paths = get_ignored_paths()
    except Exception:
        ignored_paths = set()

    meta_files_found = list(tv_root.rglob("*.meta.json"))
    logger.debug(f"[_scan_pending_tv] Found {len(meta_files_found)} .meta.json files")

    for meta_file in meta_files_found:
        try:
            folder = meta_file.parent
            logger.debug(f"[_scan_pending_tv] Processing meta: {meta_file}")

            # Find matching video file for this meta.json (uses episode code matching)
            video = _find_video_for_meta(meta_file, _VIDEO_EXTS)
            if not video:
                # Log why no video was found
                video_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in _VIDEO_EXTS]
                logger.debug(f"[_scan_pending_tv] No matching video for {meta_file.name}. Videos in folder: {[v.name for v in video_files]}")
                continue  # No matching video - still downloading or already processed

            # Skip if being processed by main loop (has temp file) - those show in processing scan
            if video.stem in processing_stems:
                logger.debug(f"[_scan_pending_tv] Skipping {video.name} - has temp file (main loop processing)")
                continue

            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            stat = video.stat()

            # Extract show name and episode info
            series = data.get("series") or {}
            show = series.get("title") or folder.parent.name
            episode_data = data.get("episode") or {}
            season = episode_data.get("season")
            episodes = episode_data.get("episodes") or []
            episode = episodes[0] if episodes else None
            # Only include episodes array if multi-episode file
            episodes_list = episodes if len(episodes) > 1 else None

            # Check for poster in source folder (show folder, alongside episodes)
            poster_url = None
            # For TV, poster is typically in the show folder (parent of season folder)
            show_folder = folder.parent if "season" in folder.name.lower() else folder
            poster_file = show_folder / "poster.jpg"
            if not poster_file.exists():
                # Try to generate poster from metadata
                try:
                    poster_meta = {
                        "imdb_id": series.get("imdb_id"),
                        "sonarr_series_id": series.get("sonarr_series_id"),
                    }
                    ensure_poster(str(show_folder), kind="tv", meta=poster_meta)
                except Exception:
                    pass  # Silently fail - poster is optional

            if poster_file.exists():
                try:
                    rel_path = poster_file.relative_to(watch_root)
                    poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                except ValueError:
                    pass

            mtime = int(stat.st_mtime)
            # For multi-episode files, join all episode titles from meta
            episode_titles = episode_data.get("titles") or []
            if episodes_list and len(episode_titles) > 1:
                # Join all episode titles with " + "
                title = " + ".join(episode_titles)
            else:
                # Strip episode code prefix from title (handles S01E05, S01E05E06, S01E05-E06, etc.)
                title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", video.stem)
            video_path_str = str(video)
            # Check both database ignore AND sentinel file
            is_file_ignored = (video_path_str in ignored_paths) or _has_sentinel(video_path_str)

            # Check if this file is being processed by worker pool
            pool_job = worker_pool_jobs.get(video_path_str)
            if pool_job:
                status = pool_job["status"]
                progress = pool_job["progress"]
                elapsed = pool_job.get("elapsed")
                elapsed_fmt = pool_job.get("elapsed_fmt")
            else:
                status = "pending"
                progress = None
                elapsed = None
                elapsed_fmt = None

            item = {
                "show": show,
                "season": season,
                "episode": episode,
                "episodes": episodes_list,  # [18, 19, 20, 21] or None for single-ep
                "title": title or video.stem,
                "path": video_path_str,
                "size_gb": _bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": video.suffix.lstrip(".").lower(),
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": mtime,
                "mtime_fmt": _format_timestamp(mtime),
                "status": status,
                "progress": progress,
                "elapsed": elapsed,
                "elapsed_fmt": elapsed_fmt,
                "poster": poster_url,
                "ignored": is_file_ignored,
            }
            items.append(item)
            logger.debug(f"[_scan_pending_tv] Added item: {video.name} (status={status})")
        except Exception as e:
            logger.warning(f"[_scan_pending_tv] Error processing {meta_file}: {e}")
            continue

    logger.debug(f"[_scan_pending_tv] Total pending TV items found: {len(items)}")
    return items


def _format_duration(seconds: float | int | None) -> str | None:
    """Format duration in human-readable form."""
    if not seconds:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    else:
        hrs = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hrs}h {mins}m" if mins else f"{hrs}h"


def _scan_processing_movies(temp_root: Path, watch_root: Path | None = None) -> list[dict]:
    """Scan temp folder for in-progress movie transcodes via .progress.json files."""
    items: list[dict] = []
    # Check direct path first (temp folder may already be the processing folder)
    movies_root = temp_root / "movies"
    if not movies_root.exists():
        movies_root = temp_root / "_processing" / "movies"
    if not movies_root.exists():
        return items

    for progress_file in movies_root.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Find the corresponding .tmp.mp4 file
            tmp_video = progress_file.with_suffix("").with_suffix(".tmp.mp4")
            if not tmp_video.exists():
                continue

            stat = tmp_video.stat()
            title = data.get("title") or progress_file.stem.replace(".progress", "")

            # Check for poster in source folder (from source_file path)
            poster_url = None
            source_file = data.get("source_file")
            if source_file and watch_root:
                source_folder = Path(source_file).parent
                poster_file = source_folder / "poster.jpg"
                if poster_file.exists():
                    try:
                        rel_path = poster_file.relative_to(watch_root)
                        poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                    except ValueError:
                        pass

            started_at = data.get("started_at") or stat.st_mtime
            elapsed = time.time() - started_at if started_at else None

            item = {
                "title": title,
                "year": data.get("year"),
                "path": str(tmp_video),
                "source_path": source_file or "",
                "size_gb": _bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": "mp4",
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": int(started_at),
                "mtime_fmt": _format_timestamp(started_at),
                "status": "processing",
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": _format_duration(elapsed),
                "poster": poster_url,
            }
            items.append(item)
        except Exception:
            continue

    return items


def _scan_processing_tv(temp_root: Path, watch_root: Path | None = None) -> list[dict]:
    """Scan temp folder for in-progress TV transcodes via .progress.json files."""
    items: list[dict] = []
    # Check direct path first (temp folder may already be the processing folder)
    tv_root = temp_root / "tv"
    if not tv_root.exists():
        tv_root = temp_root / "_processing" / "tv"
    if not tv_root.exists():
        return items

    for progress_file in tv_root.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Find the corresponding .tmp.mp4 file
            tmp_video = progress_file.with_suffix("").with_suffix(".tmp.mp4")
            if not tmp_video.exists():
                continue

            stat = tmp_video.stat()
            show = data.get("show") or ""

            # Check for poster in source folder (from source_file path)
            poster_url = None
            source_file = data.get("source_file")
            if source_file and watch_root:
                source_folder = Path(source_file).parent
                # For TV, poster is in show folder (parent of season folder)
                show_folder = source_folder.parent if "season" in source_folder.name.lower() else source_folder
                poster_file = show_folder / "poster.jpg"
                if poster_file.exists():
                    try:
                        rel_path = poster_file.relative_to(watch_root)
                        poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                    except ValueError:
                        pass

            started_at = data.get("started_at") or stat.st_mtime
            elapsed = time.time() - started_at if started_at else None

            # Strip episode code prefix from title (handles S01E05, S01E05E06, S01E05-E06, etc.)
            raw_title = data.get("title") or progress_file.stem.replace(".progress", "")
            title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", raw_title)

            # Get episodes list for multi-ep files
            episodes = data.get("episodes") or []
            episodes_list = episodes if len(episodes) > 1 else None

            item = {
                "show": show,
                "season": data.get("season"),
                "episode": data.get("episode"),
                "episodes": episodes_list,  # [18, 19, 20, 21] or None for single-ep
                "title": title or raw_title,
                "path": str(tmp_video),
                "source_path": source_file or "",
                "size_gb": _bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": "mp4",
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": int(started_at),
                "mtime_fmt": _format_timestamp(started_at),
                "status": "processing",
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": _format_duration(elapsed),
                "poster": poster_url,
            }
            items.append(item)
        except Exception:
            continue

    return items


def _scan_reencode_progress(temp_root: Path) -> dict[str, dict]:
    """
    Scan temp_root/_reencode/ for *.progress.json files.
    Returns dict mapping source_file_path -> {progress, elapsed, elapsed_fmt}.
    This lets us overlay re-encode progress on "ready" items in the media table.
    """
    result = {}
    reencode_dir = temp_root / "_reencode"
    if not reencode_dir.exists():
        return result

    for progress_file in reencode_dir.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            source_file = data.get("source_file")
            if not source_file:
                continue
            started_at = data.get("started_at")
            elapsed = time.time() - started_at if started_at else None
            result[source_file] = {
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": _format_duration(elapsed) if elapsed else None,
            }
        except Exception:
            continue

    return result


def _load_transcode_meta(video_path: Path) -> dict:
    """Load transcode metadata from database."""
    try:
        # Normalize path for lookup
        path_str = str(video_path)
        history = get_transcode_history(path_str)
        if history:
            return {
                "processed_at": history.get("processed_at"),
                "processing_duration": history.get("processing_duration"),
                "source_file": history.get("source_path"),
                "source_size": history.get("source_size"),
                "copied": history.get("copied", False),
            }
    except Exception as e:
        logging.debug("[_load_transcode_meta] Error loading history for %s: %s", video_path, e)
    return {}


def _read_title_from_nfo(video_path: Path) -> str | None:
    """Read episode title from NFO file if it exists."""
    try:
        nfo_path = video_path.with_suffix(".nfo")
        if not nfo_path.exists():
            return None
        import xml.etree.ElementTree as ET
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        title_el = root.find("title")
        if title_el is not None and title_el.text:
            return title_el.text.strip()
    except Exception:
        pass
    return None


def _get_worker_pool_processing_paths() -> dict:
    """Get paths currently being processed by worker pool with their job info.

    Returns dict mapping file_path -> {status, progress, elapsed, job_id}
    """
    from transcodarr_core.worker_pool import get_worker_pool, JobStatus

    result = {}
    worker_pool = get_worker_pool()
    if not worker_pool:
        return result

    for job in worker_pool.get_all_jobs(include_completed=False):
        elapsed = time.time() - job.started_at if job.started_at else None
        result[job.file_path] = {
            "status": "processing" if job.status == JobStatus.RUNNING else "queued",
            "progress": job.progress,
            "elapsed": elapsed,
            "elapsed_fmt": _format_duration(elapsed) if elapsed else None,
            "job_id": job.job_id,
        }

    return result


def _scan_movies_incremental(root: Path, existing_cache: dict[str, dict], reencode_map: dict[str, dict] | None = None) -> list[dict]:
    """Scan movies, reusing cached metadata when file hasn't changed."""
    items: list[dict] = []
    if not root.exists():
        return items

    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTS:
            try:
                stat = p.stat()
                path_str = str(p)
                mtime = int(stat.st_mtime)
                size_gb = _bytes_to_gb(stat.st_size)

                # Check if we have valid cached data for this file
                cached = existing_cache.get(path_str)
                if cached and cached.get("mtime") == mtime:
                    # Reuse cached metadata, just update poster (might have been added)
                    item = cached.copy()
                else:
                    # Need to probe this file
                    raw_title = p.stem
                    try:
                        folder = p.parent.name
                        if folder and len(folder) > 3:
                            raw_title = folder
                    except Exception:
                        pass

                    # Extract year before stripping it from title
                    year = _year_from_name(raw_title)
                    title = _strip_year_from_title(raw_title)

                    meta = _ffprobe_metadata(path_str)
                    transcode_meta = _load_transcode_meta(p)

                    # Calculate compression ratio if we have source size
                    source_size = transcode_meta.get("source_size")
                    compression_ratio = None
                    if source_size and stat.st_size:
                        compression_ratio = round(source_size / stat.st_size, 2)

                    item = {
                        "title": title,
                        "year": year,
                        "path": path_str,
                        "size_gb": size_gb,
                        "runtime_min": meta["runtime_min"],
                        "container": p.suffix.lstrip(".").lower(),
                        "vcodec": meta["vcodec"],
                        "acodec": meta["acodec"],
                        "resolution": meta["resolution"],
                        "mtime": mtime,
                        "status": "ready",
                        # Extended metadata for popup
                        "video_bitrate": meta["video_bitrate"],
                        "audio_bitrate": meta["audio_bitrate"],
                        "total_bitrate": meta["total_bitrate"],
                        "frame_rate": meta["frame_rate"],
                        "audio_channels": meta["audio_channels"],
                        "audio_sample_rate": meta["audio_sample_rate"],
                        # Formatted versions for display
                        "video_bitrate_fmt": _format_bitrate(meta["video_bitrate"]),
                        "audio_bitrate_fmt": _format_bitrate(meta["audio_bitrate"]),
                        "total_bitrate_fmt": _format_bitrate(meta["total_bitrate"]),
                        "audio_channels_fmt": _format_audio_channels(meta["audio_channels"]),
                        # Transcode metadata
                        "processed_at": transcode_meta.get("processed_at"),
                        "processing_duration": transcode_meta.get("processing_duration"),
                        "processing_duration_fmt": _format_duration(transcode_meta.get("processing_duration")),
                        "source_size_gb": _bytes_to_gb(source_size) if source_size else None,
                        "compression_ratio": compression_ratio,
                    }

                # Always refresh timestamp format and poster URL (cheap)
                item["mtime_fmt"] = _format_timestamp(mtime)
                if item.get("processed_at"):
                    item["processed_at_fmt"] = _format_timestamp(item["processed_at"])
                poster_file = p.parent / "poster.jpg"
                item["poster"] = f"/api/media/poster/movies/{quote(p.parent.name, safe='')}/poster.jpg" if poster_file.exists() else None

                # Merge re-encode progress if active
                if reencode_map and path_str in reencode_map:
                    re_info = reencode_map[path_str]
                    item["reencode_progress"] = re_info["progress"]
                    item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                    item["status"] = "re-encoding"

                items.append(item)
            except Exception:
                continue

    items.sort(key=lambda d: d["mtime"], reverse=True)
    return items

def _scan_tv_incremental(root: Path, existing_cache: dict[str, dict], reencode_map: dict[str, dict] | None = None) -> list[dict]:
    """Scan TV, reusing cached metadata when file hasn't changed."""
    items: list[dict] = []
    if not root.exists():
        return items

    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTS:
            try:
                stat = p.stat()
                path_str = str(p)
                mtime = int(stat.st_mtime)
                size_gb = _bytes_to_gb(stat.st_size)

                rel = p.relative_to(root)
                parts = rel.parts
                show = parts[0] if parts else p.parent.name

                cached = existing_cache.get(path_str)
                if cached and cached.get("mtime") == mtime:
                    item = cached.copy()
                    # Ensure episodes field exists for cached items (may be missing from old cache)
                    if "episodes" not in item:
                        item["episodes"] = _parse_multi_episode(p.name)
                else:
                    season, episode = _parse_sxe(p.name)
                    # Check for multi-episode file
                    episodes_list = _parse_multi_episode(p.name)
                    meta = _ffprobe_metadata(path_str)
                    transcode_meta = _load_transcode_meta(p)

                    # Calculate compression ratio if we have source size
                    source_size = transcode_meta.get("source_size")
                    compression_ratio = None
                    if source_size and stat.st_size:
                        compression_ratio = round(source_size / stat.st_size, 2)

                    # For multi-episode files, try to read combined title from NFO
                    if episodes_list:
                        nfo_title = _read_title_from_nfo(p)
                        if nfo_title:
                            title = nfo_title
                        else:
                            # Fallback: strip episode code from filename
                            title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", p.stem)
                    else:
                        # Single episode: strip episode code prefix from title
                        title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", p.stem)

                    item = {
                        "show": show,
                        "season": season,
                        "episode": episode,
                        "episodes": episodes_list,  # [18, 19, 20, 21] or None for single-ep
                        "title": title or p.stem,
                        "path": path_str,
                        "size_gb": size_gb,
                        "runtime_min": meta["runtime_min"],
                        "container": p.suffix.lstrip(".").lower(),
                        "vcodec": meta["vcodec"],
                        "acodec": meta["acodec"],
                        "resolution": meta["resolution"],
                        "mtime": mtime,
                        "status": "ready",
                        # Extended metadata for popup
                        "video_bitrate": meta["video_bitrate"],
                        "audio_bitrate": meta["audio_bitrate"],
                        "total_bitrate": meta["total_bitrate"],
                        "frame_rate": meta["frame_rate"],
                        "audio_channels": meta["audio_channels"],
                        "audio_sample_rate": meta["audio_sample_rate"],
                        # Formatted versions for display
                        "video_bitrate_fmt": _format_bitrate(meta["video_bitrate"]),
                        "audio_bitrate_fmt": _format_bitrate(meta["audio_bitrate"]),
                        "total_bitrate_fmt": _format_bitrate(meta["total_bitrate"]),
                        "audio_channels_fmt": _format_audio_channels(meta["audio_channels"]),
                        # Transcode metadata
                        "processed_at": transcode_meta.get("processed_at"),
                        "processing_duration": transcode_meta.get("processing_duration"),
                        "processing_duration_fmt": _format_duration(transcode_meta.get("processing_duration")),
                        "source_size_gb": _bytes_to_gb(source_size) if source_size else None,
                        "compression_ratio": compression_ratio,
                    }

                # Always refresh timestamp format and poster URL (cheap)
                item["mtime_fmt"] = _format_timestamp(mtime)
                if item.get("processed_at"):
                    item["processed_at_fmt"] = _format_timestamp(item["processed_at"])
                show_folder = root / show
                poster_file = show_folder / "poster.jpg"
                item["poster"] = f"/api/media/poster/tv/{quote(show, safe='')}/poster.jpg" if poster_file.exists() else None

                # Merge re-encode progress if active
                if reencode_map and path_str in reencode_map:
                    re_info = reencode_map[path_str]
                    item["reencode_progress"] = re_info["progress"]
                    item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                    item["status"] = "re-encoding"

                items.append(item)
            except Exception:
                continue

    items.sort(key=lambda d: d["mtime"], reverse=True)
    return items

def _background_scan(media_type: str, root: Path):
    """Background thread to scan and update cache."""
    if _media_cache[media_type]["scanning"]:
        return  # Already scanning

    _media_cache[media_type]["scanning"] = True
    try:
        # Build lookup from existing cache
        existing = {item["path"]: item for item in _media_cache[media_type]["items"]}

        if media_type == "movies":
            items = _scan_movies_incremental(root, existing)
        else:
            items = _scan_tv_incremental(root, existing)

        _save_cache(media_type, items)
    finally:
        _media_cache[media_type]["scanning"] = False

def _apply_filters(items: list[dict]):
    """Optional ?q= fuzzy filter and ?limit= N limiter from UI."""
    q = (request.args.get("q") or "").strip().lower()
    if q:
        def _match(d: dict):
            blob = " ".join(str(v) for v in d.values() if isinstance(v, (str, int)))
            return q in blob.lower()
        items = [d for d in items if _match(d)]

    try:
        limit = int(request.args.get("limit", "0"))
    except Exception:
        limit = 0
    if limit and limit > 0:
        items = items[:limit]
    return items

@api_bp.get("/media/movies")
def api_media_movies():
    """Return cached movies instantly, trigger background refresh if needed."""
    s = current_app.config["SETTINGS"]
    root = Path(s.OUTPUT_FOLDER) / "movies"
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    # Load from disk cache if memory is empty
    if not _media_cache["movies"]["items"]:
        _load_cache("movies")

    items = list(_media_cache["movies"]["items"])  # Copy to avoid mutation

    # Merge re-encode progress into ready items
    reencode_map = _scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    # Add pending items from watch folder (always fresh, not cached)
    if watch_root:
        pending_items = _scan_pending_movies(watch_root, temp_root)
        items = pending_items + items

    # Add in-progress items from temp folder (always fresh, not cached)
    if temp_root:
        processing_items = _scan_processing_movies(temp_root, watch_root)
        items = processing_items + items  # Processing items first

    # Trigger background refresh if cache is old (>60s) or empty, or if ?refresh=1
    refresh = request.args.get("refresh") == "1"
    cache_age = int(time.time()) - _media_cache["movies"]["last_scan"]
    if refresh or not _media_cache["movies"]["items"] or cache_age > 60:
        if not _media_cache["movies"]["scanning"]:
            t = Thread(target=_background_scan, args=("movies", root), daemon=True)
            t.start()

    items = _apply_filters(items)
    scanning = _media_cache["movies"]["scanning"]
    return jsonify({"items": items, "count": len(items), "scanning": scanning})

@api_bp.get("/media/tv")
def api_media_tv():
    """Return cached TV instantly, trigger background refresh if needed."""
    s = current_app.config["SETTINGS"]
    root = Path(s.OUTPUT_FOLDER) / "tv"
    watch_root = Path(s.WATCH_FOLDER) if s.WATCH_FOLDER else None
    temp_root = Path(s.MEDIA_TEMP_FOLDER) if s.MEDIA_TEMP_FOLDER else None

    if not _media_cache["tv"]["items"]:
        _load_cache("tv")

    items = list(_media_cache["tv"]["items"])  # Copy to avoid mutation

    # Merge re-encode progress into ready items
    reencode_map = _scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    # Add pending items from watch folder (always fresh, not cached)
    if watch_root:
        pending_items = _scan_pending_tv(watch_root, temp_root)
        items = pending_items + items

    # Add in-progress items from temp folder (always fresh, not cached)
    if temp_root:
        processing_items = _scan_processing_tv(temp_root, watch_root)
        items = processing_items + items  # Processing items first

    refresh = request.args.get("refresh") == "1"
    cache_age = int(time.time()) - _media_cache["tv"]["last_scan"]
    if refresh or not _media_cache["tv"]["items"] or cache_age > 60:
        if not _media_cache["tv"]["scanning"]:
            t = Thread(target=_background_scan, args=("tv", root), daemon=True)
            t.start()

    items = _apply_filters(items)
    scanning = _media_cache["tv"]["scanning"]
    return jsonify({"items": items, "count": len(items), "scanning": scanning})


@api_bp.get("/media/tv/debug")
def api_media_tv_debug():
    """Debug endpoint to diagnose pending TV detection issues."""
    s = current_app.config["SETTINGS"]
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
        return jsonify(debug_info)

    # Check which tv_root exists (direct path first)
    tv_root_direct = watch_root / "tv"
    tv_root_processing = watch_root / "_processing" / "tv"

    debug_info["paths_checked"] = [
        {"path": str(tv_root_direct), "exists": tv_root_direct.exists()},
        {"path": str(tv_root_processing), "exists": tv_root_processing.exists()},
    ]

    tv_root = tv_root_direct if tv_root_direct.exists() else tv_root_processing
    if not tv_root.exists():
        debug_info["error"] = f"Neither {tv_root_direct} nor {tv_root_processing} exists"
        return jsonify(debug_info)

    debug_info["tv_root_used"] = str(tv_root)

    # Find all meta files
    meta_files = list(tv_root.rglob("*.meta.json"))
    debug_info["meta_files_found"] = [str(m) for m in meta_files]

    # Check each meta file for matching
    for meta_file in meta_files:
        folder = meta_file.parent
        meta_stem = meta_file.stem
        if meta_stem.endswith(".meta"):
            meta_stem = meta_stem[:-5]

        video_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in _VIDEO_EXTS]

        result = {
            "meta_file": str(meta_file),
            "meta_stem": meta_stem,
            "folder": str(folder),
            "video_files_in_folder": [{"name": v.name, "stem": v.stem} for v in video_files],
            "exact_stem_match": None,
            "episode_code_match": None,
        }

        # Check exact stem match
        for vf in video_files:
            if vf.stem == meta_stem:
                result["exact_stem_match"] = vf.name
                break

        # Check episode code match
        meta_ep = _parse_sxe(meta_file.name)
        result["meta_episode_code"] = meta_ep
        if meta_ep[0] is not None:
            for vf in video_files:
                video_ep = _parse_sxe(vf.name)
                if video_ep == meta_ep:
                    result["episode_code_match"] = {"video": vf.name, "episode_code": video_ep}
                    break

        result["would_match"] = result["exact_stem_match"] is not None or result["episode_code_match"] is not None
        debug_info["matching_results"].append(result)

    return jsonify(debug_info)


# ----------------------- delete output endpoint -----------------------
@api_bp.delete("/media/output")
def api_delete_output():
    """Delete output files (and companions) by path. Source files are never touched."""
    data = request.get_json(silent=True) or {}
    paths = data.get("paths", [])
    if not paths or not isinstance(paths, list):
        return jsonify({"error": "paths array required"}), 400

    s = current_app.config["SETTINGS"]
    output_folder = os.path.realpath(s.OUTPUT_FOLDER)

    deleted = []
    errors = []
    companion_exts = (".nfo", ".srt", ".sub", ".idx", ".ass", ".ssa", ".meta.json",
                      ".jpg", ".png", "-thumb.jpg", "-poster.jpg")

    for p in paths:
        real = os.path.realpath(p)
        # Security: ensure path is under OUTPUT_FOLDER
        if not real.startswith(output_folder + os.sep) and real != output_folder:
            errors.append({"path": p, "error": "path outside output folder"})
            continue

        if not os.path.isfile(real):
            errors.append({"path": p, "error": "file not found"})
            continue

        try:
            # Delete main file
            os.remove(real)
            deleted.append(p)

            # Delete companion files (same stem, various extensions)
            stem = os.path.splitext(real)[0]
            parent = os.path.dirname(real)
            for ext in companion_exts:
                companion = stem + ext
                if os.path.isfile(companion):
                    os.remove(companion)

            # Remove empty parent directories up to output_folder
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

    # Invalidate media cache
    if deleted:
        _media_cache["movies"]["items"] = []
        _media_cache["movies"]["last_scan"] = 0
        _media_cache["tv"]["items"] = []
        _media_cache["tv"]["last_scan"] = 0

    return jsonify({"deleted": deleted, "errors": errors, "count": len(deleted)})


# ----------------------- metadata endpoints -----------------------
@api_bp.get("/media/metadata/movie")
def api_movie_metadata():
    """
    Fetch movie metadata (description, genres, rating) from Radarr.
    Query params: imdb_id, tmdb_id, title, year
    """
    imdb_id = request.args.get("imdb_id")
    tmdb_id = request.args.get("tmdb_id")
    title = request.args.get("title")
    year = request.args.get("year")
    if year:
        try:
            year = int(year)
        except ValueError:
            year = None

    if not any([imdb_id, tmdb_id, title]):
        return jsonify({"error": "Must provide imdb_id, tmdb_id, or title"}), 400

    metadata = fetch_movie_metadata(imdb_id=imdb_id, tmdb_id=tmdb_id, title=title, year=year)
    if metadata:
        return jsonify(metadata)
    return jsonify({"error": "Metadata not found"}), 404


@api_bp.get("/media/metadata/series")
def api_series_metadata():
    """
    Fetch TV series metadata (description, genres, rating) from Sonarr.
    Query params: imdb_id, tvdb_id, tmdb_id, title
    """
    imdb_id = request.args.get("imdb_id")
    tvdb_id = request.args.get("tvdb_id")
    tmdb_id = request.args.get("tmdb_id")
    title = request.args.get("title")

    if tvdb_id:
        try:
            tvdb_id = int(tvdb_id)
        except ValueError:
            tvdb_id = None
    if tmdb_id:
        try:
            tmdb_id = int(tmdb_id)
        except ValueError:
            tmdb_id = None

    if not any([imdb_id, tvdb_id, tmdb_id, title]):
        return jsonify({"error": "Must provide imdb_id, tvdb_id, tmdb_id, or title"}), 400

    metadata = fetch_series_metadata(imdb_id=imdb_id, tvdb_id=tvdb_id, tmdb_id=tmdb_id, title=title)
    if metadata:
        return jsonify(metadata)
    return jsonify({"error": "Metadata not found"}), 404


# ----------------------- poster serving -----------------------
@api_bp.get("/media/poster/<path:subpath>")
def api_media_poster(subpath: str):
    """
    Serve poster.jpg from media folders.
    Usage: /api/media/poster/movies/Movie (2024)/poster.jpg
           /api/media/poster/tv/Show Name/poster.jpg
           /api/media/poster/temp/...
           /api/media/poster/watch/...  (source folder)
    """
    s = current_app.config["SETTINGS"]
    # Sanitize: prevent directory traversal
    if ".." in subpath:
        abort(400)

    # Check which folder to serve from
    if subpath.startswith("watch/"):
        if not s.WATCH_FOLDER:
            abort(404)
        subpath = subpath[6:]  # Remove "watch/"
        base_folder = Path(s.WATCH_FOLDER)
    elif subpath.startswith("temp/"):
        if not s.MEDIA_TEMP_FOLDER:
            abort(404)
        subpath = subpath[5:]  # Remove "temp/"
        base_folder = Path(s.MEDIA_TEMP_FOLDER)
    else:
        base_folder = Path(s.OUTPUT_FOLDER)

    poster_path = base_folder / subpath

    if not poster_path.exists() or not poster_path.is_file():
        abort(404)

    # Ensure we're still within the base folder
    try:
        poster_path.resolve().relative_to(base_folder.resolve())
    except ValueError:
        abort(403)

    return send_file(poster_path, mimetype="image/jpeg")


# ===================================================================
#                       WEBHOOK ENDPOINTS
# ===================================================================
import logging
import requests
from datetime import datetime, timezone

def _remap_path(path: str, path_from: str, path_to: str) -> str:
    """Remap paths from container paths to host paths if configured."""
    if path and path_from and path_to and path.startswith(path_from):
        path = path_to + path[len(path_from):]
    # Strip /_processing from paths (downloads folder is already the processing folder)
    if path:
        path = path.replace("/_processing/", "/")
    return path


def _write_meta_json(out_dir: Path, stem: str, data: dict) -> Path:
    """Write .meta.json file atomically."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{stem}.meta.json"
    tmp_file = out_dir / f".meta.{int(time.time())}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(out_file)
        logging.info("[WEBHOOK] Wrote metadata: %s", out_file)
        return out_file
    except Exception as e:
        logging.error("[WEBHOOK] Failed to write %s: %s", out_file, e)
        if tmp_file.exists():
            tmp_file.unlink()
        raise


@api_bp.post("/webhook/radarr")
def webhook_radarr():
    """
    Receive Radarr webhook (On Import / On Upgrade).
    Writes .meta.json sidecar for the imported movie.
    """
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "MovieAdded", "Grab", "Test"):
        logging.debug("[WEBHOOK/RADARR] Ignoring event: %s", event_type)
        return jsonify({"status": "ignored", "event": event_type})

    # Handle test event
    if event_type == "Test":
        logging.info("[WEBHOOK/RADARR] Test event received successfully")
        return jsonify({"status": "ok", "message": "Test successful"})

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

    # Remap paths if configured
    s = current_app.config["SETTINGS"]
    path_from = getattr(s, "RADARR_PATH_FROM", "") or ""
    path_to = getattr(s, "RADARR_PATH_TO", "") or ""
    movie_path = _remap_path(movie_path, path_from, path_to)
    file_path = _remap_path(file_path, path_from, path_to)

    if not file_path and not movie_path:
        return jsonify({"error": "No file path in payload"}), 400

    # Build .meta.json (matching shell script format)
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

    # Determine output directory
    if file_path:
        out_dir = Path(file_path).parent
        stem = Path(file_path).stem
    else:
        out_dir = Path(movie_path)
        stem = f"{title} ({year})" if year else title

    try:
        out_file = _write_meta_json(out_dir, stem, meta)
        return jsonify({"status": "ok", "file": str(out_file)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.post("/webhook/sonarr")
def webhook_sonarr():
    """
    Receive Sonarr webhook (On Import / On Upgrade).
    Writes .meta.json sidecar for the imported episode.
    """
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = payload.get("eventType", "")
    if event_type not in ("Download", "EpisodeFileDelete", "Grab", "Test"):
        logging.debug("[WEBHOOK/SONARR] Ignoring event: %s", event_type)
        return jsonify({"status": "ignored", "event": event_type})

    # Handle test event
    if event_type == "Test":
        logging.info("[WEBHOOK/SONARR] Test event received successfully")
        return jsonify({"status": "ok", "message": "Test successful"})

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

    # Remap paths if configured
    s = current_app.config["SETTINGS"]
    path_from = getattr(s, "SONARR_PATH_FROM", "") or ""
    path_to = getattr(s, "SONARR_PATH_TO", "") or ""
    series_path = _remap_path(series_path, path_from, path_to)
    file_path = _remap_path(file_path, path_from, path_to)

    if not file_path:
        return jsonify({"error": "No file path in payload"}), 400

    # Extract episode info
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
        # Episode-level IDs (if available in webhook)
        if ep.get("imdbId"):
            ep_imdb_ids.append(ep.get("imdbId"))
        if ep.get("tvdbId"):
            ep_tvdb_ids.append(ep.get("tvdbId"))
        if ep.get("tmdbId"):
            ep_tmdb_ids.append(ep.get("tmdbId"))

    first_title = ep_titles[0] if ep_titles else ""
    first_imdb = ep_imdb_ids[0] if ep_imdb_ids else None

    # Build .meta.json (matching shell script format)
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

    # Build filename like "S01E05 - Episode Title"
    out_dir = Path(file_path).parent
    ep_str = f"S{season_num:02d}" if season_num is not None else "S00"
    for n in ep_numbers:
        if n is not None:
            ep_str += f"E{n:02d}"
    stem = f"{ep_str} - {first_title}" if first_title else ep_str

    try:
        out_file = _write_meta_json(out_dir, stem, meta)
        return jsonify({"status": "ok", "file": str(out_file)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===================================================================
#                    CONNECTION MANAGEMENT
# ===================================================================

def _get_webhook_url():
    """Get the webhook URL that Radarr/Sonarr should call back to."""
    # Try to determine our external URL
    # This can be overridden via TRANSCODARR_URL env var
    base_url = os.environ.get("TRANSCODARR_URL", "").rstrip("/")
    if not base_url:
        # Fallback: use request host (works when accessed directly)
        base_url = request.host_url.rstrip("/")
    return base_url


def _find_existing_webhook(notifications: list, name_prefix: str) -> dict | None:
    """Find an existing Transcodarr webhook in the notifications list."""
    for n in notifications:
        if n.get("name", "").startswith(name_prefix):
            return n
    return None


@api_bp.get("/connections")
def api_connections_status():
    """Get status of Radarr/Sonarr webhook connections."""
    s = current_app.config["SETTINGS"]
    result = {"radarr": None, "sonarr": None}

    # Check Radarr
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""
    if radarr_url and radarr_key:
        try:
            resp = requests.get(
                f"{radarr_url.rstrip('/')}/api/v3/notification",
                params={"apikey": radarr_key},
                timeout=5
            )
            if resp.ok:
                notifications = resp.json()
                existing = _find_existing_webhook(notifications, "Transcodarr")
                result["radarr"] = {
                    "configured": True,
                    "connected": existing is not None,
                    "webhook_id": existing.get("id") if existing else None,
                    "webhook_url": existing.get("fields", [{}])[0].get("value") if existing else None,
                }
            else:
                result["radarr"] = {"configured": True, "connected": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            result["radarr"] = {"configured": True, "connected": False, "error": str(e)}
    else:
        result["radarr"] = {"configured": False}

    # Check Sonarr
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""
    if sonarr_url and sonarr_key:
        try:
            resp = requests.get(
                f"{sonarr_url.rstrip('/')}/api/v3/notification",
                params={"apikey": sonarr_key},
                timeout=5
            )
            if resp.ok:
                notifications = resp.json()
                existing = _find_existing_webhook(notifications, "Transcodarr")
                result["sonarr"] = {
                    "configured": True,
                    "connected": existing is not None,
                    "webhook_id": existing.get("id") if existing else None,
                    "webhook_url": existing.get("fields", [{}])[0].get("value") if existing else None,
                }
            else:
                result["sonarr"] = {"configured": True, "connected": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            result["sonarr"] = {"configured": True, "connected": False, "error": str(e)}
    else:
        result["sonarr"] = {"configured": False}

    return jsonify(result)


@api_bp.post("/connections/radarr")
def api_connect_radarr():
    """Register Transcodarr webhook in Radarr."""
    s = current_app.config["SETTINGS"]
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return jsonify({"error": "Radarr URL and API key not configured"}), 400

    webhook_url = _get_webhook_url() + "/api/webhook/radarr"

    # Check for existing webhook
    try:
        resp = requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if existing:
            # Update existing webhook
            existing_id = existing["id"]
            # Update the URL field
            for field in existing.get("fields", []):
                if field.get("name") == "url":
                    field["value"] = webhook_url
            resp = requests.put(
                f"{radarr_url.rstrip('/')}/api/v3/notification/{existing_id}",
                params={"apikey": radarr_key},
                json=existing,
                timeout=10
            )
            resp.raise_for_status()
            return jsonify({"status": "updated", "webhook_id": existing_id, "url": webhook_url})

        # Create new webhook
        webhook_config = {
            "name": "Transcodarr",
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "onGrab": False,
            "onDownload": True,
            "onUpgrade": True,
            "onRename": False,
            "onMovieAdded": False,
            "onMovieDelete": False,
            "onMovieFileDelete": False,
            "onMovieFileDeleteForUpgrade": False,
            "onHealthIssue": False,
            "onHealthRestored": False,
            "onApplicationUpdate": False,
            "onManualInteractionRequired": False,
            "supportsOnGrab": True,
            "supportsOnDownload": True,
            "supportsOnUpgrade": True,
            "supportsOnRename": True,
            "supportsOnMovieAdded": True,
            "supportsOnMovieDelete": True,
            "supportsOnMovieFileDelete": True,
            "supportsOnMovieFileDeleteForUpgrade": True,
            "supportsOnHealthIssue": True,
            "supportsOnHealthRestored": True,
            "supportsOnApplicationUpdate": True,
            "supportsOnManualInteractionRequired": True,
            "includeHealthWarnings": False,
            "tags": [],
            "fields": [
                {"name": "url", "value": webhook_url},
                {"name": "method", "value": 1},  # POST
            ]
        }

        resp = requests.post(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            json=webhook_config,
            timeout=10
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        return jsonify({"status": "created", "webhook_id": new_id, "url": webhook_url})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


@api_bp.delete("/connections/radarr")
def api_disconnect_radarr():
    """Remove Transcodarr webhook from Radarr."""
    s = current_app.config["SETTINGS"]
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return jsonify({"error": "Radarr URL and API key not configured"}), 400

    try:
        resp = requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return jsonify({"status": "not_found"})

        resp = requests.delete(
            f"{radarr_url.rstrip('/')}/api/v3/notification/{existing['id']}",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        return jsonify({"status": "deleted", "webhook_id": existing["id"]})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


@api_bp.post("/connections/sonarr")
def api_connect_sonarr():
    """Register Transcodarr webhook in Sonarr."""
    s = current_app.config["SETTINGS"]
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return jsonify({"error": "Sonarr URL and API key not configured"}), 400

    webhook_url = _get_webhook_url() + "/api/webhook/sonarr"

    try:
        resp = requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if existing:
            # Update existing webhook
            existing_id = existing["id"]
            for field in existing.get("fields", []):
                if field.get("name") == "url":
                    field["value"] = webhook_url
            resp = requests.put(
                f"{sonarr_url.rstrip('/')}/api/v3/notification/{existing_id}",
                params={"apikey": sonarr_key},
                json=existing,
                timeout=10
            )
            resp.raise_for_status()
            return jsonify({"status": "updated", "webhook_id": existing_id, "url": webhook_url})

        # Create new webhook
        webhook_config = {
            "name": "Transcodarr",
            "implementation": "Webhook",
            "configContract": "WebhookSettings",
            "onGrab": False,
            "onDownload": True,
            "onUpgrade": True,
            "onRename": False,
            "onSeriesAdd": False,
            "onSeriesDelete": False,
            "onEpisodeFileDelete": False,
            "onEpisodeFileDeleteForUpgrade": False,
            "onHealthIssue": False,
            "onHealthRestored": False,
            "onApplicationUpdate": False,
            "onManualInteractionRequired": False,
            "supportsOnGrab": True,
            "supportsOnDownload": True,
            "supportsOnUpgrade": True,
            "supportsOnRename": True,
            "supportsOnSeriesAdd": True,
            "supportsOnSeriesDelete": True,
            "supportsOnEpisodeFileDelete": True,
            "supportsOnEpisodeFileDeleteForUpgrade": True,
            "supportsOnHealthIssue": True,
            "supportsOnHealthRestored": True,
            "supportsOnApplicationUpdate": True,
            "supportsOnManualInteractionRequired": True,
            "includeHealthWarnings": False,
            "tags": [],
            "fields": [
                {"name": "url", "value": webhook_url},
                {"name": "method", "value": 1},  # POST
            ]
        }

        resp = requests.post(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            json=webhook_config,
            timeout=10
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        return jsonify({"status": "created", "webhook_id": new_id, "url": webhook_url})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


@api_bp.delete("/connections/sonarr")
def api_disconnect_sonarr():
    """Remove Transcodarr webhook from Sonarr."""
    s = current_app.config["SETTINGS"]
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return jsonify({"error": "Sonarr URL and API key not configured"}), 400

    try:
        resp = requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return jsonify({"status": "not_found"})

        resp = requests.delete(
            f"{sonarr_url.rstrip('/')}/api/v3/notification/{existing['id']}",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        return jsonify({"status": "deleted", "webhook_id": existing["id"]})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


@api_bp.post("/connections/radarr/test")
def api_test_radarr():
    """Test the Radarr webhook by triggering a test notification."""
    s = current_app.config["SETTINGS"]
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return jsonify({"error": "Radarr URL and API key not configured"}), 400

    try:
        # Find our webhook
        resp = requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return jsonify({"error": "Webhook not registered"}), 400

        # Trigger test
        resp = requests.post(
            f"{radarr_url.rstrip('/')}/api/v3/notification/test",
            params={"apikey": radarr_key},
            json=existing,
            timeout=10
        )
        if resp.ok:
            return jsonify({"status": "ok", "message": "Test notification sent"})
        else:
            return jsonify({"error": f"Test failed: {resp.text}"}), 500

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


@api_bp.post("/connections/sonarr/test")
def api_test_sonarr():
    """Test the Sonarr webhook by triggering a test notification."""
    s = current_app.config["SETTINGS"]
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return jsonify({"error": "Sonarr URL and API key not configured"}), 400

    try:
        resp = requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = _find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return jsonify({"error": "Webhook not registered"}), 400

        resp = requests.post(
            f"{sonarr_url.rstrip('/')}/api/v3/notification/test",
            params={"apikey": sonarr_key},
            json=existing,
            timeout=10
        )
        if resp.ok:
            return jsonify({"status": "ok", "message": "Test notification sent"})
        else:
            return jsonify({"error": f"Test failed: {resp.text}"}), 500

    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500


# ===================================================================
#                    WORKER POOL & MANUAL TRANSCODE
# ===================================================================

@api_bp.get("/workers/status")
def api_workers_status():
    """Get worker pool status."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    status = worker_pool.get_status()
    return jsonify(status)


@api_bp.post("/transcode/manual")
def api_transcode_manual():
    """Queue a manual transcode job."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    data = request.get_json() or {}
    file_path = data.get("file_path")
    media_type = data.get("media_type", "movie")

    if not file_path:
        return jsonify({"error": "file_path is required"}), 400

    # Check if file exists
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    # Check if already on ignore list - if so, we need to remove it first
    try:
        if is_ignored(file_path):
            return jsonify({"error": "File is on ignore list. Remove from ignore first."}), 400
    except Exception:
        pass

    # Check if manual workers are disabled
    if worker_pool.manual_workers <= 0:
        return jsonify({"error": "Manual transcoding is disabled (MANUAL_WORKERS=0)"}), 503

    # Check if pool can accept job
    if not worker_pool.can_accept_job():
        status = worker_pool.get_status()
        return jsonify({
            "error": "All manual workers busy",
            "active_manual_jobs": status["active_manual_jobs"],
            "manual_workers": status["manual_workers"],
        }), 503

    # Submit job
    job = worker_pool.submit_manual_job(
        file_path=file_path,
        media_type=media_type,
        title=data.get("title"),
        year=data.get("year"),
        show=data.get("show"),
        season=data.get("season"),
        episode=data.get("episode"),
    )

    if job:
        return jsonify({"status": "queued", "job": job.to_dict()})
    else:
        return jsonify({"error": "Failed to queue job"}), 500


@api_bp.post("/transcode/batch")
def api_transcode_batch():
    """Queue a batch of files for sequential transcoding on one worker."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    data = request.get_json() or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items list is required"}), 400

    if worker_pool.manual_workers <= 0:
        return jsonify({"error": "Manual transcoding is disabled (MANUAL_WORKERS=0)"}), 503

    if not worker_pool.can_accept_job():
        status = worker_pool.get_status()
        return jsonify({
            "error": "All manual workers busy",
            "active_manual_jobs": status["active_manual_jobs"],
            "manual_workers": status["manual_workers"],
        }), 503

    # Validate items
    valid = []
    for it in items:
        fp = it.get("file_path")
        if not fp or not os.path.exists(fp):
            continue
        if is_ignored(fp):
            continue
        valid.append(it)

    if not valid:
        return jsonify({"error": "No valid files in batch"}), 400

    job = worker_pool.submit_batch_job(valid)
    if job:
        return jsonify({"status": "queued", "job": job.to_dict(), "batch_size": len(valid)})
    else:
        return jsonify({"error": "Failed to queue batch"}), 500


@api_bp.get("/transcode/jobs")
def api_transcode_jobs():
    """List all transcode jobs."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    include_completed = request.args.get("include_completed", "true").lower() == "true"
    limit = int(request.args.get("limit", "100"))

    jobs = worker_pool.get_all_jobs(include_completed=include_completed, limit=limit)
    return jsonify({
        "jobs": [j.to_dict() for j in jobs],
        "count": len(jobs),
    })


@api_bp.get("/transcode/jobs/<job_id>")
def api_transcode_job(job_id: str):
    """Get a specific transcode job."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    job = worker_pool.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job.to_dict())


@api_bp.delete("/transcode/jobs/<job_id>")
def api_cancel_job(job_id: str):
    """Cancel a queued transcode job."""
    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    cancelled = worker_pool.cancel_job(job_id)
    if cancelled:
        return jsonify({"status": "cancelled"})
    else:
        return jsonify({"error": "Cannot cancel (job may be running or completed)"}), 400


@api_bp.post("/transcode/stop")
def api_stop_transcode():
    """Stop a running or queued transcode for a specific file."""
    from transcodarr_core.worker_pool import terminate_proc_for_file

    data = request.get_json() or {}
    file_path = data.get("file_path")
    if not file_path:
        return jsonify({"error": "file_path required"}), 400

    worker_pool = current_app.config.get("WORKER_POOL")
    if not worker_pool:
        return jsonify({"error": "Worker pool not initialized"}), 500

    # Try to kill the FFmpeg process
    killed = terminate_proc_for_file(file_path)
    # Also remove from processing set so it can be re-queued
    worker_pool._remove_processing_file(file_path)

    # Try to cancel queued manual job for this file
    cancelled = False
    for job in worker_pool.get_jobs_for_file(file_path):
        if job.status.value in ("queued", "running"):
            if worker_pool.cancel_job(job.job_id):
                cancelled = True

    if killed or cancelled:
        return jsonify({"status": "stopped", "killed": killed, "cancelled": cancelled})
    else:
        return jsonify({"error": "No active transcode found for this file"}), 404


# ===================================================================
#                    MANUAL SUBTITLE SEARCH
# ===================================================================

@api_bp.post("/subtitles/search")
def api_subtitles_manual_search():
    """
    Manual subtitle search with custom parameters.

    For edge cases like:
    - Movie split into TV episodes (Family Guy movie -> S04E28-E30)
    - Wrong season/episode detection
    - Alternative titles

    Request body:
    {
        "file_path": "/path/to/video.mp4",  // Required: video file path
        "search_query": "Family Guy The Griffin Family History",  // Required: custom search string
        "season": 4,  // Optional: season number override
        "episodes": [28, 29, 30],  // Optional: episode number(s) override (array)
        "lang": "eng"  // Optional: language code (default: eng)
    }

    Returns:
    {
        "status": "ok",
        "saved": ["/path/to/subtitle1.srt", ...],
        "searched": ["query1", "query2", ...],
        "found": 150,  // Total results found
        "matched": 3,  // Results matching criteria
        "errors": []
    }
    """
    from transcodarr_core.subtitles.fetch import fetch_subtitles_manual

    data = request.get_json() or {}

    file_path = data.get("file_path")
    search_query = data.get("search_query")

    if not file_path:
        return jsonify({"error": "file_path is required"}), 400
    if not search_query:
        return jsonify({"error": "search_query is required"}), 400

    # Check if file exists
    if not os.path.exists(file_path):
        return jsonify({"error": f"File not found: {file_path}"}), 404

    # Parse optional parameters
    season = data.get("season")
    if season is not None:
        try:
            season = int(season)
        except (ValueError, TypeError):
            return jsonify({"error": "season must be an integer"}), 400

    episodes = data.get("episodes")
    if episodes is not None:
        if not isinstance(episodes, list):
            # Allow single episode as int
            try:
                episodes = [int(episodes)]
            except (ValueError, TypeError):
                return jsonify({"error": "episodes must be an integer or array of integers"}), 400
        else:
            try:
                episodes = [int(e) for e in episodes]
            except (ValueError, TypeError):
                return jsonify({"error": "episodes must be integers"}), 400

    lang = data.get("lang", "eng")
    max_downloads = data.get("max_downloads", 5)
    try:
        max_downloads = int(max_downloads)
    except (ValueError, TypeError):
        max_downloads = 5

    logging.info("[API] Manual subtitle search: file=%s query=%r season=%s episodes=%s max=%d",
                 file_path, search_query, season, episodes, max_downloads)

    try:
        result = fetch_subtitles_manual(
            video_path=file_path,
            search_query=search_query,
            season=season,
            episodes=episodes,
            lang=lang,
            max_downloads=max_downloads,
        )

        return jsonify({
            "status": "ok" if result["saved"] else "no_results",
            **result
        })

    except Exception as e:
        logging.exception("[API] Manual subtitle search failed")
        return jsonify({"error": str(e)}), 500


@api_bp.delete("/subtitles")
def api_subtitles_delete():
    """
    Delete all subtitle files associated with a video file.

    Request body:
    {
        "file_path": "/path/to/video.mp4"  // Required: video file path
    }

    Returns:
    {
        "status": "ok",
        "deleted": ["/path/to/video.srt", "/path/to/video.en.srt"],
        "count": 2
    }
    """
    data = request.get_json() or {}
    file_path = data.get("file_path")

    if not file_path:
        return jsonify({"error": "file_path is required"}), 400

    video_path = Path(file_path)
    if not video_path.exists():
        return jsonify({"error": f"File not found: {file_path}"}), 404

    # Find all subtitle files matching the video name
    video_stem = video_path.stem
    video_dir = video_path.parent
    subtitle_extensions = {".srt", ".sub", ".ass", ".ssa", ".vtt"}

    deleted = []
    errors = []

    # Match patterns like: video.srt, video.en.srt, video.eng.srt, etc.
    for sub_file in video_dir.iterdir():
        if not sub_file.is_file():
            continue
        if sub_file.suffix.lower() not in subtitle_extensions:
            continue
        # Check if subtitle belongs to this video
        # e.g., "Movie.2024.srt" or "Movie.2024.en.srt" for "Movie.2024.mp4"
        sub_stem = sub_file.stem
        if sub_stem == video_stem or sub_stem.startswith(video_stem + "."):
            try:
                sub_file.unlink()
                deleted.append(str(sub_file))
                logging.info("[API] Deleted subtitle: %s", sub_file)
            except Exception as e:
                errors.append(f"{sub_file.name}: {e}")
                logging.warning("[API] Failed to delete subtitle %s: %s", sub_file, e)

    if errors:
        return jsonify({
            "status": "partial",
            "deleted": deleted,
            "count": len(deleted),
            "errors": errors
        })

    return jsonify({
        "status": "ok",
        "deleted": deleted,
        "count": len(deleted)
    })


# ===================================================================
#                    MEDIA IGNORE LIST
# ===================================================================

@api_bp.post("/media/ignore")
def api_media_ignore():
    """Add or remove a file from the ignore list (toggle).

    Merges database ignore system with sentinel files:
    - Shows as ignored if in database OR has sentinel file
    - Unignoring removes from database AND deletes sentinel if present
    """
    data = request.get_json() or {}
    file_path = data.get("file_path")
    action = data.get("action", "toggle")  # "add", "remove", or "toggle"

    if not file_path:
        return jsonify({"error": "file_path is required"}), 400

    try:
        # Check both database and sentinel file
        in_database = is_ignored(file_path)
        has_sentinel = _has_sentinel(file_path)
        currently_ignored = in_database or has_sentinel

        if action == "toggle":
            if currently_ignored:
                # Remove from both database and sentinel
                if in_database:
                    remove_ignored(file_path)
                sentinel_removed = _remove_sentinel(file_path)
                return jsonify({
                    "status": "removed",
                    "ignored": False,
                    "sentinel_removed": sentinel_removed
                })
            else:
                reason = data.get("reason", "Manual ignore from UI")
                set_ignored(file_path, reason)
                return jsonify({"status": "added", "ignored": True})
        elif action == "add":
            if not in_database:
                reason = data.get("reason", "Manual ignore from UI")
                set_ignored(file_path, reason)
            return jsonify({"status": "added", "ignored": True})
        elif action == "remove":
            # Remove from both database and sentinel
            if in_database:
                remove_ignored(file_path)
            sentinel_removed = _remove_sentinel(file_path)
            return jsonify({
                "status": "removed",
                "ignored": False,
                "sentinel_removed": sentinel_removed
            })
        else:
            return jsonify({"error": "Invalid action"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.get("/media/ignored")
def api_media_ignored():
    """List all ignored files."""
    try:
        ignored = get_all_ignored()
        return jsonify({
            "items": ignored,
            "count": len(ignored),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_bp.get("/media/ignored/check")
def api_check_ignored():
    """Check if a specific file is ignored (database or sentinel)."""
    file_path = request.args.get("file_path")
    if not file_path:
        return jsonify({"error": "file_path is required"}), 400

    try:
        in_database = is_ignored(file_path)
        has_sentinel = _has_sentinel(file_path)
        return jsonify({
            "file_path": file_path,
            "ignored": in_database or has_sentinel,
            "in_database": in_database,
            "has_sentinel": has_sentinel,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ===================================================================
#                    METADATA ENRICHMENT ENDPOINTS
# ===================================================================

# Enrichment progress tracking
_enrich_state = {"running": False, "total": 0, "processed": 0, "nfo_written": 0, "posters_downloaded": 0, "errors": 0}

@api_bp.post("/media/enrich")
def api_enrich_single():
    """Enrich a single media file with metadata, NFO, and poster."""
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path is required"}), 400

    try:
        from transcodarr_core.enrich import enrich_media
        result = enrich_media(path)
        return jsonify({"ok": True, **result})
    except Exception as e:
        logging.error("[ENRICH] Failed to enrich %s: %s", path, e)
        return jsonify({"error": str(e)}), 500


@api_bp.post("/media/enrich-all")
def api_enrich_all():
    """Start bulk enrichment of all movies/episodes missing NFOs."""
    if _enrich_state["running"]:
        return jsonify({"error": "Enrichment already running", "status": _enrich_state}), 409

    def _run_enrichment():
        from transcodarr_core.enrich import enrich_media
        from transcodarr_core.nfo import find_nfo_for_video
        import time as _time

        _enrich_state["running"] = True
        _enrich_state["processed"] = 0
        _enrich_state["nfo_written"] = 0
        _enrich_state["posters_downloaded"] = 0
        _enrich_state["errors"] = 0

        try:
            # Gather all media files from DB
            movies = get_all_movies()
            episodes = get_all_tv_episodes()

            # Filter to those missing NFOs
            to_enrich = []
            for m in movies:
                if m.get("path") and not find_nfo_for_video(m["path"]):
                    to_enrich.append(m["path"])
            for e in episodes:
                if e.get("path") and not find_nfo_for_video(e["path"]):
                    to_enrich.append(e["path"])

            _enrich_state["total"] = len(to_enrich)
            logging.info("[ENRICH] Starting bulk enrichment: %d files", len(to_enrich))

            for path in to_enrich:
                if not _enrich_state["running"]:
                    logging.info("[ENRICH] Bulk enrichment cancelled")
                    break

                try:
                    result = enrich_media(path)
                    if result.get("nfo_written"):
                        _enrich_state["nfo_written"] += 1
                    if result.get("poster_downloaded"):
                        _enrich_state["posters_downloaded"] += 1
                except Exception as e:
                    logging.warning("[ENRICH] Failed to enrich %s: %s", path, e)
                    _enrich_state["errors"] += 1

                _enrich_state["processed"] += 1

                # Rate limit: small delay between API calls
                _time.sleep(0.5)

            logging.info("[ENRICH] Bulk enrichment complete: %d/%d processed, %d NFOs, %d posters",
                         _enrich_state["processed"], _enrich_state["total"],
                         _enrich_state["nfo_written"], _enrich_state["posters_downloaded"])
        finally:
            _enrich_state["running"] = False

    t = Thread(target=_run_enrichment, daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "started"})


@api_bp.get("/media/enrich-status")
def api_enrich_status():
    """Check progress of bulk enrichment."""
    return jsonify(_enrich_state)


@api_bp.post("/media/enrich-stop")
def api_enrich_stop():
    """Stop a running bulk enrichment."""
    if _enrich_state["running"]:
        _enrich_state["running"] = False
        return jsonify({"ok": True, "status": "stopping"})
    return jsonify({"ok": True, "status": "not_running"})


# ===================================================================
#                    DIAGNOSTIC ENDPOINTS
# ===================================================================

@api_bp.get("/debug/logging")
def api_debug_logging():
    """
    Diagnostic endpoint to test logging.
    Writes a test message and verifies it appears in the log file.
    """
    import uuid
    import time as _time

    log_path = current_app.config.get("LOG_PATH", "logs/transcode.log")
    test_id = str(uuid.uuid4())[:8]
    test_msg = f"[DIAG_TEST_{test_id}] Logging test message"

    # Get handler info before logging
    handlers_before = []
    for h in logging.root.handlers:
        info = {"type": type(h).__name__, "level": h.level}
        if hasattr(h, 'baseFilename'):
            info["file"] = h.baseFilename
        if hasattr(h, 'stream') and h.stream:
            info["stream"] = str(h.stream)
        handlers_before.append(info)

    # Get file state before
    file_before = None
    if os.path.exists(log_path):
        file_before = os.path.getsize(log_path)

    # Log the test message
    logging.info(test_msg)

    # Force flush all handlers
    for h in logging.root.handlers:
        try:
            h.flush()
            if hasattr(h, 'stream') and h.stream:
                try:
                    os.fsync(h.stream.fileno())
                except:
                    pass
        except:
            pass

    # Small delay to ensure disk write
    _time.sleep(0.1)

    # Check if message appears in file
    found_in_file = False
    file_after = None
    last_lines = []
    if os.path.exists(log_path):
        file_after = os.path.getsize(log_path)
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
                found_in_file = test_id in content
                # Get last 5 lines
                lines = content.strip().split('\n')
                last_lines = lines[-5:] if len(lines) > 5 else lines
        except Exception as e:
            last_lines = [f"Error reading file: {e}"]

    # Test direct file write (bypass logging entirely)
    direct_write_success = False
    direct_write_error = None
    direct_test_msg = f"[DIRECT_WRITE_{test_id}] Direct file write test\n"
    try:
        with open(log_path, 'a') as f:
            f.write(direct_test_msg)
            f.flush()
            os.fsync(f.fileno())
        direct_write_success = True
    except Exception as e:
        direct_write_error = str(e)

    # Re-check file after direct write
    file_after_direct = None
    found_direct = False
    if os.path.exists(log_path):
        file_after_direct = os.path.getsize(log_path)
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
                found_direct = f"DIRECT_WRITE_{test_id}" in content
                lines = content.strip().split('\n')
                last_lines = lines[-5:] if len(lines) > 5 else lines
        except:
            pass

    # Check file permissions
    file_stat = None
    try:
        import stat
        st = os.stat(log_path)
        file_stat = {
            "mode": oct(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size,
        }
    except Exception as e:
        file_stat = {"error": str(e)}

    return jsonify({
        "test_id": test_id,
        "test_message": test_msg,
        "log_path": log_path,
        "log_path_absolute": os.path.abspath(log_path),
        "file_exists": os.path.exists(log_path),
        "file_size_before": file_before,
        "file_size_after": file_after,
        "file_size_after_direct": file_after_direct,
        "found_in_file": found_in_file,
        "direct_write_success": direct_write_success,
        "direct_write_error": direct_write_error,
        "found_direct_write": found_direct,
        "file_stat": file_stat,
        "handlers": handlers_before,
        "last_lines": last_lines,
        "root_logger_level": logging.root.level,
        "working_directory": os.getcwd(),
    })