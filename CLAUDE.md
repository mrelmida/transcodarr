# Transcodarr

Automated video transcoding orchestrator for the *arr ecosystem. Watches a download folder, fetches and syncs subtitles (OpenSubtitles.com / Podnapisi / Addic7ed), transcodes via FFmpeg to a universal direct-play format, updates Radarr/Sonarr paths, and refreshes Jellyfin — fully hands-off.

## Deploy

- **Primary host:** `home-media01`
- **Compose files:**
  - `docker-compose.yml` — main service (uses shared Postgres at `192.168.20.15:5432`)
  - `docker-compose.postgres.yml` — variant with bundled Postgres sidecar (rarely used)
  - `docker-compose.reencode.yml` — variant for batch re-encoding old libraries
- **Run (default):** `docker compose up -d --build`
- **Port:** `5025` (web UI + API)
- **Container:** `transcodarr`

## Architecture

- Python + FastAPI in `web/`, transcoder workers in `srt/` and `core/`, config in `config/`
- Postgres: **shared instance** at `192.168.20.15:5432`, DB `transcodarr` (set `POSTGRES_PASSWORD` in `.env`)
- FFmpeg with progress-stream parsing; dual worker pools (auto from watchdog + manual from UI), resizable live
- Integrations: Radarr, Sonarr, Jellyfin, TMDB, OMDB

## Test / validation

- `pytest` from project root (suite in `tests/`)
- UI smoke: open `http://192.168.20.34:5025/` and check the "System" tab for live CPU/RAM/disk charts

## Gotchas

- Volume mounts (`MOVIES_WATCH_PATH`, `TV_WATCH_PATH`, `MOVIES_OUTPUT_PATH`, `TV_OUTPUT_PATH`) must exist on the host BEFORE the container starts — compose refuses to start otherwise (`:?Set ...` enforcement on env vars).
- `MEDIA_TEMP_FOLDER` defaults to `/tmp/transcodarr` — large transcodes can fill `tmpfs`. Override `TEMP_PATH` in `.env` to a disk-backed location for big batches.
- The Radarr/Sonarr path-remap settings are how transcodarr translates `/output/movies` inside the container back to the host paths Radarr expects — wrong remap = Radarr can't find the files after transcode.

## Project-specific git conventions

None beyond global.
