# web/routers/settings.py
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import json
import logging

from web.shared_state import (
    SETTINGS_SCHEMA, VALID_PRESETS, VALID_CRFS,
    get_env_path,
)
from transcodarr_core.database import (
    get_all_settings, get_setting, set_setting,
    get_encoding_presets, create_encoding_preset,
    update_encoding_preset, delete_encoding_preset,
    restore_default_presets,
)
from dotenv import dotenv_values

router = APIRouter()


@router.get("/settings")
def api_get_settings(request: Request):
    """Return all settings with schema for UI rendering."""
    try:
        db_values = get_all_settings()
        s = request.app.state.settings

        result = {"schema": SETTINGS_SCHEMA, "values": {}, "encoding_presets": []}

        for section_key, section in SETTINGS_SCHEMA.items():
            for field_key in section["fields"]:
                value = db_values.get(field_key)
                if value is None:
                    value = getattr(s, field_key, None)
                result["values"][field_key] = value if value is not None else ""

        try:
            result["encoding_presets"] = get_encoding_presets()
        except Exception:
            pass

        return result
    except Exception as e:
        logging.exception("[SETTINGS] Failed to get settings")
        return JSONResponse({"error": str(e), "type": type(e).__name__}, status_code=500)


@router.post("/settings")
def api_save_settings(request: Request, data: dict = Body(default={})):
    """Save settings to database."""
    from transcodarr_core.config import DB_BACKED_SETTINGS

    updated = []
    errors = []

    valid_keys = set()
    for section in SETTINGS_SCHEMA.values():
        valid_keys.update(section["fields"].keys())

    for key, value in data.items():
        if key not in valid_keys:
            continue
        if key not in DB_BACKED_SETTINGS:
            continue

        try:
            if set_setting(key, str(value) if value is not None else ""):
                updated.append(key)
            else:
                errors.append({"key": key, "error": "Database write failed"})
        except Exception as e:
            errors.append({"key": key, "error": str(e)})

    # Live-reconfigure worker pool if worker counts changed
    worker_keys = {"MANUAL_WORKERS", "AUTO_WORKERS"}
    if worker_keys & set(updated):
        worker_pool = request.app.state.worker_pool
        if worker_pool:
            try:
                from transcodarr_core.config import get_setting as config_get_setting
                mw = int(config_get_setting("MANUAL_WORKERS", 0))
                aw = int(config_get_setting("AUTO_WORKERS", 2))
                worker_pool.reconfigure(mw, aw)
            except Exception as e:
                logging.warning("[SETTINGS] Failed to reconfigure worker pool: %s", e)

    return {
        "status": "ok" if not errors else "partial",
        "updated": updated,
        "errors": errors,
        "message": "Settings saved." if updated else "No changes made."
    }


@router.post("/settings/migrate-from-env")
def api_migrate_settings_from_env():
    """One-time migration: Copy runtime settings from .env to database."""
    from transcodarr_core.config import DB_BACKED_SETTINGS

    env_path = get_env_path()
    if not env_path.exists():
        return JSONResponse({"error": "No .env file found", "migrated": [], "skipped": []}, status_code=404)

    env_values = dotenv_values(env_path)
    migrated = []
    skipped = []
    errors = []

    existing_db = get_all_settings()

    for key in DB_BACKED_SETTINGS:
        env_val = env_values.get(key)
        if env_val is None:
            continue

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

    return {
        "status": "ok" if not errors else "partial",
        "migrated": migrated,
        "skipped": skipped,
        "errors": errors,
        "message": f"Migrated {len(migrated)} settings from .env to database"
    }


@router.get("/compression-tiers")
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
    return {
        "enabled": str(enabled).lower() == "true",
        "tiers": tiers,
        "preset_options": preset_options,
        "crf_options": crf_options,
    }


@router.post("/compression-tiers")
def api_save_compression_tiers(data: dict = Body(default={})):
    """Validate and save compression tiers."""
    tiers = data.get("tiers", [])

    if not isinstance(tiers, list):
        return JSONResponse({"error": "tiers must be a list"}, status_code=400)

    for i, tier in enumerate(tiers):
        try:
            min_gb = float(tier.get("min_gb", 0))
            max_gb = float(tier.get("max_gb", 0))
        except (ValueError, TypeError):
            return JSONResponse({"error": f"Tier {i+1}: invalid size values"}, status_code=400)

        if min_gb < 0:
            return JSONResponse({"error": f"Tier {i+1}: min_gb cannot be negative"}, status_code=400)
        if max_gb < 0:
            return JSONResponse({"error": f"Tier {i+1}: max_gb cannot be negative"}, status_code=400)
        if max_gb != 0 and max_gb <= min_gb:
            return JSONResponse({"error": f"Tier {i+1}: max_gb must be greater than min_gb (or 0 for unlimited)"}, status_code=400)

        preset = tier.get("preset", "")
        if preset not in VALID_PRESETS:
            return JSONResponse({"error": f"Tier {i+1}: invalid preset '{preset}'"}, status_code=400)

        crf = str(tier.get("crf", ""))
        if crf and crf not in VALID_CRFS:
            return JSONResponse({"error": f"Tier {i+1}: invalid CRF '{crf}'"}, status_code=400)

    tiers.sort(key=lambda t: float(t.get("min_gb", 0)))

    for i in range(len(tiers) - 1):
        curr_max = float(tiers[i].get("max_gb", 0))
        next_min = float(tiers[i+1].get("min_gb", 0))
        if curr_max == 0:
            return JSONResponse({"error": f"Tier {i+1}: unlimited max_gb must be the last tier"}, status_code=400)
        if curr_max > next_min:
            return JSONResponse({"error": f"Tiers {i+1} and {i+2} overlap"}, status_code=400)

    try:
        set_setting("COMPRESSION_TIERS", json.dumps(tiers))
    except Exception as e:
        return JSONResponse({"error": f"Failed to save: {e}"}, status_code=500)

    return {"status": "ok", "tiers": tiers}


# ── Encoding Presets ────────────────────────────────────────────────────────

@router.get("/encoding-presets")
def api_get_presets():
    """List all encoding presets."""
    return {"presets": get_encoding_presets()}


@router.post("/encoding-presets")
def api_create_preset(data: dict = Body(default={})):
    """Create a custom encoding preset."""
    name = data.get("name", "").strip()
    settings = data.get("settings", {})
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not settings or not isinstance(settings, dict):
        return JSONResponse({"error": "settings dict is required"}, status_code=400)

    result = create_encoding_preset(name, settings)
    if result is None:
        return JSONResponse({"error": "Preset name already exists"}, status_code=409)
    return {"status": "created", "preset": result}


@router.put("/encoding-presets/{preset_id}")
def api_update_preset(preset_id: int, data: dict = Body(default={})):
    """Update a custom encoding preset. Cannot update built-in presets."""
    name = data.get("name", "").strip()
    settings = data.get("settings", {})
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if not settings or not isinstance(settings, dict):
        return JSONResponse({"error": "settings dict is required"}, status_code=400)

    result = update_encoding_preset(preset_id, name, settings)
    if result is None:
        return JSONResponse({"error": "Preset not found or is a built-in preset"}, status_code=400)
    return {"status": "updated", "preset": result}


@router.delete("/encoding-presets/{preset_id}")
def api_delete_preset(preset_id: int):
    """Delete a custom encoding preset. Cannot delete built-in presets."""
    if delete_encoding_preset(preset_id):
        return {"status": "deleted"}
    return JSONResponse({"error": "Preset not found or is a built-in preset"}, status_code=400)


@router.post("/encoding-presets/restore")
def api_restore_presets():
    """Re-insert any missing built-in presets."""
    count = restore_default_presets()
    return {"status": "ok", "restored": count}
