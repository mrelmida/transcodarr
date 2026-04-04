# srt/transcodarr_core/auto_preset.py
"""
Auto preset rule evaluation engine.
Probes source file properties and matches against Auto rules to select the right preset.
"""
import logging
from typing import Optional

from .database import get_setting, get_encoding_preset, get_auto_preset
from .ffmpeg.probe import ffprobe_json


def resolve_auto_preset(file_path: str, meta: dict | None) -> dict | None:
    """
    Evaluate Auto rules against source file properties.
    Returns the matched preset's settings dict, or None if Auto is not active.
    """
    active_id = get_setting("ACTIVE_PRESET_ID")
    if not active_id:
        return None

    try:
        active_id = int(active_id)
    except (ValueError, TypeError):
        return None

    auto = get_auto_preset()
    if not auto or auto["id"] != active_id:
        # Active preset is not Auto — load it directly and return its settings
        preset = get_encoding_preset(active_id)
        if preset and preset.get("settings"):
            return preset["settings"]
        return None

    rules_data = auto.get("auto_rules")
    if not rules_data:
        return None

    rules = rules_data.get("rules", [])
    fallback_id = rules_data.get("fallback_preset_id")

    # Probe source file
    source = _probe_source(file_path)
    media_type = _get_media_type(meta)

    logging.info("[AUTO] Evaluating rules for: %s (height=%s codec=%s type=%s)",
                 file_path, source["height"], source["codec"], media_type)

    # Evaluate rules top-to-bottom, first match wins
    for rule in rules:
        conditions = rule.get("conditions", {})
        if _matches(conditions, source, media_type):
            target_id = rule.get("target_preset_id")
            preset = get_encoding_preset(target_id) if target_id else None
            if preset and preset.get("settings"):
                logging.info("[AUTO] Rule matched: '%s' -> preset '%s' (id=%s)",
                             rule.get("name", "unnamed"), preset["name"], target_id)
                return preset["settings"]
            logging.warning("[AUTO] Rule '%s' matched but target preset %s not found",
                            rule.get("name"), target_id)

    # Fallback
    if fallback_id:
        preset = get_encoding_preset(fallback_id)
        if preset and preset.get("settings"):
            logging.info("[AUTO] No rule matched, using fallback preset '%s'", preset["name"])
            return preset["settings"]

    logging.warning("[AUTO] No rule matched and no fallback configured")
    return None


def _probe_source(file_path: str) -> dict:
    """Extract video properties from source file."""
    info = ffprobe_json(file_path)
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            return {
                "height": int(st.get("height", 0)),
                "width": int(st.get("width", 0)),
                "codec": st.get("codec_name", ""),
            }
    return {"height": 0, "width": 0, "codec": ""}


def _get_media_type(meta: dict | None) -> str | None:
    """Normalize media type from metadata."""
    if not meta:
        return None
    kind = (meta.get("kind") or "").lower()
    if kind == "episode":
        return "tv"
    if kind == "movie":
        return "movie"
    return None


_RESOLUTION_RANGES = {
    "sd_below":     (0, 480),
    "720p_below":   (0, 720),
    "1080p_below":  (0, 1080),
    "above_1080p":  (1081, 99999),
    "4k_above":     (2160, 99999),
    "sd":           (1, 480),
    "720p":         (481, 720),
    "1080p":        (721, 1080),
    "1440p":        (1081, 1440),
    "4k":           (1441, 2160),
}


def _matches(conditions: dict, source: dict, media_type: str | None) -> bool:
    """Check if all non-null conditions match the source properties."""
    res = conditions.get("resolution")
    if res is not None:
        bounds = _RESOLUTION_RANGES.get(res)
        if bounds:
            lo, hi = bounds
            if source["height"] < lo or source["height"] > hi:
                return False

    codecs = conditions.get("video_codec")
    if codecs is not None and source["codec"] not in codecs:
        return False

    mt = conditions.get("media_type")
    if mt is not None and media_type != mt:
        return False

    return True
