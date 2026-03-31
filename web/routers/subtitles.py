# web/routers/subtitles.py
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pathlib import Path
import os, json, logging

from web.shared_state import SUBTITLE_PROVIDERS
from transcodarr_core.database import get_all_settings, get_setting, set_setting

router = APIRouter()


@router.get("/subtitle-providers")
def api_get_subtitle_providers(request: Request):
    """Get all subtitle provider configurations."""
    db_values = get_all_settings()
    s = request.app.state.settings

    def get_value(key):
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
            accounts_json = get_value(provider_info["config_key"])
            if accounts_json:
                try:
                    accounts = json.loads(accounts_json)
                    if isinstance(accounts, list):
                        provider_data["accounts"] = [
                            {"user": acc.get("user", ""), "has_pass": bool(acc.get("pass"))}
                            for acc in accounts if isinstance(acc, dict)
                        ]
                except json.JSONDecodeError:
                    pass

            if not provider_data["accounts"] and "legacy_user_key" in provider_info:
                legacy_user = get_value(provider_info["legacy_user_key"])
                legacy_pass = get_value(provider_info.get("legacy_pass_key", ""))
                if legacy_user:
                    provider_data["accounts"] = [{"user": legacy_user, "has_pass": bool(legacy_pass)}]

        enabled_key = provider_info.get("enabled_key")
        if enabled_key:
            enabled_val = get_value(enabled_key)
            if enabled_val:
                provider_data["enabled"] = enabled_val.lower() in ("true", "1", "yes")
            elif provider_info["requires_auth"] and provider_data["accounts"]:
                provider_data["enabled"] = True

        result["providers"][provider_id] = provider_data

    return result


@router.post("/subtitle-providers/{provider_id}/accounts")
def api_add_subtitle_account(provider_id: str, data: dict = Body(default={})):
    """Add a new account for a subtitle provider."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return JSONResponse({"error": "Unknown provider"}, status_code=404)

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    if not provider_info["requires_auth"] or not provider_info["supports_multiple_accounts"]:
        return JSONResponse({"error": "Provider does not support accounts"}, status_code=400)

    username = (data.get("user") or "").strip()
    password = (data.get("pass") or "").strip()

    if not username or not password:
        return JSONResponse({"error": "Username and password are required"}, status_code=400)

    config_key = provider_info["config_key"]
    logging.info(f"[SUBTITLE-API] Adding account for {provider_id}")

    accounts_json = get_setting(config_key, "")
    accounts = []
    if accounts_json:
        try:
            accounts = json.loads(accounts_json)
            if not isinstance(accounts, list):
                accounts = []
        except json.JSONDecodeError:
            accounts = []

    for acc in accounts:
        if acc.get("user") == username:
            return JSONResponse({"error": f"Account '{username}' already exists"}, status_code=400)

    accounts.append({"user": username, "pass": password})

    try:
        if set_setting(config_key, json.dumps(accounts)):
            logging.info(f"[SUBTITLE-API] Saved {len(accounts)} accounts to {config_key}")
        else:
            return JSONResponse({"error": "Database write failed"}, status_code=500)
    except Exception as e:
        logging.error(f"[SUBTITLE-API] Failed to save accounts: {e}")
        return JSONResponse({"error": f"Failed to save: {e}"}, status_code=500)

    return {"status": "ok", "message": f"Account '{username}' added"}


@router.delete("/subtitle-providers/{provider_id}/accounts/{username}")
def api_delete_subtitle_account(provider_id: str, username: str):
    """Delete an account from a subtitle provider."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return JSONResponse({"error": "Unknown provider"}, status_code=404)

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    if not provider_info["requires_auth"] or not provider_info["supports_multiple_accounts"]:
        return JSONResponse({"error": "Provider does not support accounts"}, status_code=400)

    config_key = provider_info["config_key"]

    accounts_json = get_setting(config_key, "")
    accounts = []
    if accounts_json:
        try:
            accounts = json.loads(accounts_json)
            if not isinstance(accounts, list):
                accounts = []
        except json.JSONDecodeError:
            accounts = []

    original_count = len(accounts)
    accounts = [acc for acc in accounts if acc.get("user") != username]

    if len(accounts) == original_count:
        return JSONResponse({"error": f"Account '{username}' not found"}, status_code=404)

    if accounts:
        set_setting(config_key, json.dumps(accounts))
    else:
        set_setting(config_key, "")

    return {"status": "ok", "message": f"Account '{username}' removed"}


@router.post("/subtitle-providers/{provider_id}/toggle")
def api_toggle_subtitle_provider(provider_id: str, data: dict = Body(default={})):
    """Toggle a subtitle provider on/off."""
    if provider_id not in SUBTITLE_PROVIDERS:
        return JSONResponse({"error": "Unknown provider"}, status_code=404)

    provider_info = SUBTITLE_PROVIDERS[provider_id]
    enabled_key = provider_info.get("enabled_key")
    if not enabled_key:
        return JSONResponse({"error": "Provider does not support toggling"}, status_code=400)

    enabled = data.get("enabled", False)

    logging.info(f"[SUBTITLE-API] Toggling {provider_id} to {enabled}")

    try:
        if set_setting(enabled_key, "true" if enabled else "false"):
            logging.info(f"[SUBTITLE-API] Set {enabled_key}={'true' if enabled else 'false'}")
        else:
            return JSONResponse({"error": "Database write failed"}, status_code=500)
    except Exception as e:
        logging.error(f"[SUBTITLE-API] Failed to set key: {e}")
        return JSONResponse({"error": f"Failed to save: {e}"}, status_code=500)

    return {"status": "ok", "enabled": enabled}


@router.post("/subtitles/search")
def api_subtitles_manual_search(data: dict = Body(default={})):
    """Manual subtitle search with custom parameters."""
    from transcodarr_core.subtitles.fetch import fetch_subtitles_manual

    file_path = data.get("file_path")
    search_query = data.get("search_query")

    if not file_path:
        return JSONResponse({"error": "file_path is required"}, status_code=400)
    if not search_query:
        return JSONResponse({"error": "search_query is required"}, status_code=400)

    if not os.path.exists(file_path):
        return JSONResponse({"error": f"File not found: {file_path}"}, status_code=404)

    season = data.get("season")
    if season is not None:
        try:
            season = int(season)
        except (ValueError, TypeError):
            return JSONResponse({"error": "season must be an integer"}, status_code=400)

    episodes = data.get("episodes")
    if episodes is not None:
        if not isinstance(episodes, list):
            try:
                episodes = [int(episodes)]
            except (ValueError, TypeError):
                return JSONResponse({"error": "episodes must be an integer or array of integers"}, status_code=400)
        else:
            try:
                episodes = [int(e) for e in episodes]
            except (ValueError, TypeError):
                return JSONResponse({"error": "episodes must be integers"}, status_code=400)

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

        return {
            "status": "ok" if result["saved"] else "no_results",
            **result
        }

    except Exception as e:
        logging.exception("[API] Manual subtitle search failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/subtitles")
def api_subtitles_delete(data: dict = Body(default={})):
    """Delete all subtitle files associated with a video file."""
    file_path = data.get("file_path")

    if not file_path:
        return JSONResponse({"error": "file_path is required"}, status_code=400)

    video_path = Path(file_path)
    if not video_path.exists():
        return JSONResponse({"error": f"File not found: {file_path}"}, status_code=404)

    video_stem = video_path.stem
    video_dir = video_path.parent
    subtitle_extensions = {".srt", ".sub", ".ass", ".ssa", ".vtt"}

    deleted = []
    errors = []

    for sub_file in video_dir.iterdir():
        if not sub_file.is_file():
            continue
        if sub_file.suffix.lower() not in subtitle_extensions:
            continue
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
        return {
            "status": "partial",
            "deleted": deleted,
            "count": len(deleted),
            "errors": errors
        }

    return {
        "status": "ok",
        "deleted": deleted,
        "count": len(deleted)
    }
