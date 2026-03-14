# srt/transcodarr_core/jellyfin.py
import os, logging, requests
from typing import Optional

def refresh_library(library_id: Optional[str] = None, *, url: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 8) -> bool:
    """
    Refresh Jellyfin libraries.
    - If library_id is None → refresh all libraries.
    - Else refresh a specific virtual folder by its ID.
    Env fallback:
      JELLYFIN_URL, JELLYFIN_API_KEY
    """
    logging.info(f"[JELLYFIN] Jellyfin refresh requested.")
    if not url or not api_key:
        try:
            from .config import get_setting
            url = url or get_setting("JELLYFIN_URL")
            token = api_key or get_setting("JELLYFIN_API_KEY")
        except Exception:
            url = url or os.getenv("JELLYFIN_URL")
            token = api_key or os.getenv("JELLYFIN_API_KEY")
    else:
        token = api_key
    if not url or not token:
        logging.warning("[JELLYFIN] Missing JELLYFIN_URL/JELLYFIN_API_KEY; skip refresh.")
        return False

    headers = {"X-Emby-Token": token}
    try:
        if library_id:
            # POST /Library/Refresh?LibraryId=<id>
            r = requests.post(f"{url.rstrip('/')}/Library/Refresh", params={"LibraryId": library_id}, headers=headers, timeout=timeout)
        else:
            # POST /Library/Refresh (all)
            r = requests.post(f"{url.rstrip('/')}/Library/Refresh", headers=headers, timeout=timeout)
        r.raise_for_status()
        logging.info("[JELLYFIN] Library refresh triggered %s.", f"(LibraryId={library_id})" if library_id else "(all)")
        return True
    except Exception as e:
        logging.warning(f"[JELLYFIN] Refresh failed: {e}")
        return False

if __name__ == "__main__":
    #test: python3 -m transcodarr_core.jellyfin

    import argparse, sys

    ap = argparse.ArgumentParser(description="Trigger a Jellyfin library refresh.")
    ap.add_argument("--library-id", help="Refresh only this library (VirtualFolder Id). Omit to refresh all.", default=None)
    ap.add_argument("--url", help="Jellyfin base URL (overrides JELLYFIN_URL env). Example: http://192.168.1.10:8096", default=None)
    ap.add_argument("--api-key", help="Jellyfin API key (overrides JELLYFIN_API_KEY env).", default=None)
    ap.add_argument("--timeout", type=int, default=8, help="HTTP timeout in seconds (default: 8)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    ok = refresh_library(
        library_id=args.library_id,
        url=args.url,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    if ok:
        logging.info(f"[JELLYFIN] Jellyfin refresh successful.")
    sys.exit(0 if ok else 1)