# Changelog

## v1.1.0 (2026-03-31)

### Changed
- **Backend migrated from Flask+Gunicorn to FastAPI+Uvicorn** — async-native framework, auto-generated API docs at `/docs`, identical API contract
- Web layer split from single 3600-line file into 9 focused router modules
- Entrypoint switched to `uvicorn` with single-worker model (in-process state preserved)
- UI dark theme aligned with shared media stack CSS variables (`--bg: #0d1117`, `--bg-card: #161b22`, `--accent: #58a6ff`)
- Logs panel now fills available viewport height

### Added
- `/health` endpoint for container health checks
- `/docs` and `/openapi.json` auto-generated API documentation (FastAPI built-in)
- Metadata enrichment system — fetch metadata, NFO files, and posters for individual or all media files
- Per-episode plot descriptions pulled from Sonarr and written into episode NFOs
- Bulk enrichment with progress tracking and cancellation (`/media/enrich-all`)
- "Meta" button on individual items and "Enrich All" bulk action in the web UI
- Optional centralized syslog logging support via `SYSLOG_ADDRESS` env var

### Fixed
- 10-bit SDR content (anime, Blu-ray rips) no longer misdetected as HDR, which caused zscale tonemapping crashes
- Subtitle fallback path crash (`NameError: tmp_no_subs`) when primary transcode failed
- Verify rejecting valid transcodes of large files — duration tolerance relaxed from 2% to 5%

### Removed
- Flask, Flask-CORS, Gunicorn dependencies
- `FLASK_ENV` environment variable (no longer needed)

## v1.0.0 (2026-03-11)

- Initial public release
