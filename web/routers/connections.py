# web/routers/connections.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os, logging
import requests as http_requests

from web.shared_state import find_existing_webhook

router = APIRouter()


def _get_webhook_url(request: Request):
    """Get the webhook URL that Radarr/Sonarr should call back to."""
    base_url = os.environ.get("TRANSCODARR_URL", "").rstrip("/")
    if not base_url:
        base_url = str(request.base_url).rstrip("/")
    return base_url


@router.get("/connections")
def api_connections_status(request: Request):
    """Get status of Radarr/Sonarr webhook connections."""
    s = request.app.state.settings
    result = {"radarr": None, "sonarr": None}

    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""
    if radarr_url and radarr_key:
        try:
            resp = http_requests.get(
                f"{radarr_url.rstrip('/')}/api/v3/notification",
                params={"apikey": radarr_key},
                timeout=5
            )
            if resp.ok:
                notifications = resp.json()
                existing = find_existing_webhook(notifications, "Transcodarr")
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

    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""
    if sonarr_url and sonarr_key:
        try:
            resp = http_requests.get(
                f"{sonarr_url.rstrip('/')}/api/v3/notification",
                params={"apikey": sonarr_key},
                timeout=5
            )
            if resp.ok:
                notifications = resp.json()
                existing = find_existing_webhook(notifications, "Transcodarr")
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

    return result


@router.post("/connections/radarr")
def api_connect_radarr(request: Request):
    """Register Transcodarr webhook in Radarr."""
    s = request.app.state.settings
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return JSONResponse({"error": "Radarr URL and API key not configured"}, status_code=400)

    webhook_url = _get_webhook_url(request) + "/api/webhook/radarr"

    try:
        resp = http_requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if existing:
            existing_id = existing["id"]
            for field in existing.get("fields", []):
                if field.get("name") == "url":
                    field["value"] = webhook_url
            resp = http_requests.put(
                f"{radarr_url.rstrip('/')}/api/v3/notification/{existing_id}",
                params={"apikey": radarr_key},
                json=existing,
                timeout=10
            )
            resp.raise_for_status()
            return {"status": "updated", "webhook_id": existing_id, "url": webhook_url}

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
                {"name": "method", "value": 1},
            ]
        }

        resp = http_requests.post(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            json=webhook_config,
            timeout=10
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        return {"status": "created", "webhook_id": new_id, "url": webhook_url}

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/connections/radarr")
def api_disconnect_radarr(request: Request):
    """Remove Transcodarr webhook from Radarr."""
    s = request.app.state.settings
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return JSONResponse({"error": "Radarr URL and API key not configured"}, status_code=400)

    try:
        resp = http_requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return {"status": "not_found"}

        resp = http_requests.delete(
            f"{radarr_url.rstrip('/')}/api/v3/notification/{existing['id']}",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        return {"status": "deleted", "webhook_id": existing["id"]}

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/connections/sonarr")
def api_connect_sonarr(request: Request):
    """Register Transcodarr webhook in Sonarr."""
    s = request.app.state.settings
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return JSONResponse({"error": "Sonarr URL and API key not configured"}, status_code=400)

    webhook_url = _get_webhook_url(request) + "/api/webhook/sonarr"

    try:
        resp = http_requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if existing:
            existing_id = existing["id"]
            for field in existing.get("fields", []):
                if field.get("name") == "url":
                    field["value"] = webhook_url
            resp = http_requests.put(
                f"{sonarr_url.rstrip('/')}/api/v3/notification/{existing_id}",
                params={"apikey": sonarr_key},
                json=existing,
                timeout=10
            )
            resp.raise_for_status()
            return {"status": "updated", "webhook_id": existing_id, "url": webhook_url}

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
                {"name": "method", "value": 1},
            ]
        }

        resp = http_requests.post(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            json=webhook_config,
            timeout=10
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        return {"status": "created", "webhook_id": new_id, "url": webhook_url}

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/connections/sonarr")
def api_disconnect_sonarr(request: Request):
    """Remove Transcodarr webhook from Sonarr."""
    s = request.app.state.settings
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return JSONResponse({"error": "Sonarr URL and API key not configured"}, status_code=400)

    try:
        resp = http_requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return {"status": "not_found"}

        resp = http_requests.delete(
            f"{sonarr_url.rstrip('/')}/api/v3/notification/{existing['id']}",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        return {"status": "deleted", "webhook_id": existing["id"]}

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/connections/radarr/test")
def api_test_radarr(request: Request):
    """Test the Radarr webhook by triggering a test notification."""
    s = request.app.state.settings
    radarr_url = getattr(s, "RADARR_URL", "") or ""
    radarr_key = getattr(s, "RADARR_API_KEY", "") or ""

    if not radarr_url or not radarr_key:
        return JSONResponse({"error": "Radarr URL and API key not configured"}, status_code=400)

    try:
        resp = http_requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": radarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return JSONResponse({"error": "Webhook not registered"}, status_code=400)

        resp = http_requests.post(
            f"{radarr_url.rstrip('/')}/api/v3/notification/test",
            params={"apikey": radarr_key},
            json=existing,
            timeout=10
        )
        if resp.ok:
            return {"status": "ok", "message": "Test notification sent"}
        else:
            return JSONResponse({"error": f"Test failed: {resp.text}"}, status_code=500)

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/connections/sonarr/test")
def api_test_sonarr(request: Request):
    """Test the Sonarr webhook by triggering a test notification."""
    s = request.app.state.settings
    sonarr_url = getattr(s, "SONARR_URL", "") or ""
    sonarr_key = getattr(s, "SONARR_API_KEY", "") or ""

    if not sonarr_url or not sonarr_key:
        return JSONResponse({"error": "Sonarr URL and API key not configured"}, status_code=400)

    try:
        resp = http_requests.get(
            f"{sonarr_url.rstrip('/')}/api/v3/notification",
            params={"apikey": sonarr_key},
            timeout=10
        )
        resp.raise_for_status()
        notifications = resp.json()
        existing = find_existing_webhook(notifications, "Transcodarr")

        if not existing:
            return JSONResponse({"error": "Webhook not registered"}, status_code=400)

        resp = http_requests.post(
            f"{sonarr_url.rstrip('/')}/api/v3/notification/test",
            params={"apikey": sonarr_key},
            json=existing,
            timeout=10
        )
        if resp.ok:
            return {"status": "ok", "message": "Test notification sent"}
        else:
            return JSONResponse({"error": f"Test failed: {resp.text}"}, status_code=500)

    except http_requests.exceptions.RequestException as e:
        return JSONResponse({"error": str(e)}, status_code=500)
