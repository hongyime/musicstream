# musicstream — Operations Runbook

Recovery and operations guide for the musicstream daemon. This is a **deterministic
ETL music-acquisition pipeline** — there is no LLM/inference/agent layer. Its value
is being debuggable and predictable.

> Host: Windows + Docker Desktop (WSL2). Operate from **Git Bash / MSYS** (POSIX
> shell), not PowerShell. `source scripts/dev_helpers.sh` for `dexec`/`dlog`/`dreload`/`dhealth`.

---

## 1. System shape

| Piece | Detail |
|---|---|
| Daemon | FastAPI + APScheduler, single process, `uvicorn --workers 1` (LOAD-BEARING) |
| Container | `musicstream-daemon` — API on **port 9079** |
| Database | PostgreSQL 16 — `musicstream-postgres` (db/user/name all `musicstream`, port 5432) |
| Media server | `musicstream-plex` (32400) · Scrobbler | `musicstream-scrobbler` (9078) |
| Compose | `docker-compose.yml` (prod) + `docker-compose.override.yml` (dev bind-mounts, gitignored) |

**`--workers 1` is required** — scheduler, WebSocket manager, circuit breaker, and the
librespot session/semaphores are in-process singletons. Multiple workers double-register
cron jobs and break librespot single-flight (which locks the Spotify account 1–2h).

### Where state lives
- **Postgres volume** `postgres_data` — tracks, sources, download_attempts, daemon_runs, lb_recommendations (the source of truth).
- **`./data`** — librespot credential blob, throttle state. **`./logs`** — `musicstream.log`, `errors.log`. **`./backups`** — `pg_dump` snapshots (14 retained).
- **Media drive** (`EXTERNAL_MEDIA_DRIVE` → `/media`) — the FLAC/MP3 files.
- Credentials: `.env`, `cookies.txt`, `spotify_token.json`, `data/librespot_credentials.json` (all gitignored; entrypoint enforces `0600`).

---

## 2. Deploying code changes

```bash
# CODE-ONLY change under src/ or migrations/ (bind-mounted in dev) — ~5s, NO rebuild:
docker compose up -d --force-recreate daemon      # or: dreload

# requirements.txt / Dockerfile.daemon / docker-entrypoint.sh change — REBUILD (~5 min):
docker compose build daemon && docker compose up -d daemon
```
Migrations run automatically on boot (`run_migrations()` → `alembic upgrade head`).

In-container Alembic CLI (status/downgrade/autogenerate):
```bash
docker exec musicstream-daemon sh -c 'cd /app && alembic current'   # -> 0003 (head)
```

---

## 3. Health & status

```bash
curl http://localhost:9079/health                       # shallow: DB only (Docker healthcheck) -> {"status":"ok"}
curl http://localhost:9079/health/deep                  # DB + scheduler.running + last-run age; 503 if degraded
curl http://localhost:9079/api/musicstream/stats        # totals: pending/downloaded/failed/active
curl http://localhost:9079/api/musicstream/burn-rate    # downloads/hr + projected ETA
curl http://localhost:9079/api/musicstream/metrics      # per-tier success/fail rates
```
> `/health/deep` reports `degraded` (scheduler_running=false) during the initial boot
> download pipeline — the scheduler only starts at startup step 9, after the boot
> pipeline. That is expected during boot; tune the run-age window via `DEEP_HEALTH_MAX_RUN_AGE_S`.

Mutating endpoints require `Authorization: Bearer $DAEMON_API_TOKEN` (from `.env`):
```bash
TOKEN=$(grep -E '^DAEMON_API_TOKEN=' .env | cut -d= -f2-)
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:9079/api/musicstream/sync
# also: /integrity, /full-backfill, /saved-albums-sync, /followed-artists-sync,
#       /liked-artists-expand?batch=N, /tracks/reset-failed
```

---

## 4. Download tier ladder

T0 librespot (Spotify Premium, single-flight, serial pre-sweep) → T1 SpotiFLAC (lossless)
→ T2 yt-dlp+ytmusicapi → T3 spotdl → T4 yt-dlp YouTube → T5 yt-dlp SoundCloud.
T0/T3 are serial sweeps; T1/T2/T4/T5 run in a `MAX_CONCURRENT_WORKERS` (default 12) ThreadPool.
A track is marked `failed` after `_GIVE_UP_THRESHOLD` (20) failed attempts (tracked in `tracks.attempt_count`).

---

## 5. Restart / recovery behaviour (P0-1 / P0-2)

- **On boot**, `reset_orphaned_downloads(all_rows=True)` resets ALL `status='downloading'`
  rows to `pending` before any sweep (safe under `--workers 1`). No queue slot leaks on restart.
- **On shutdown**, lifespan signals the sweeps to stop and resets in-flight `downloading`
  rows to `pending`. `stop_grace_period: 30s` gives uvicorn time to run this before SIGKILL.
- Net: a restart never permanently strands tracks in `downloading`.

```bash
# Manual one-off reset of stuck rows (boot reset does this automatically):
docker exec musicstream-postgres psql -U musicstream -d musicstream \
  -c "UPDATE tracks SET status='pending' WHERE status='downloading';"
# Requeue failed tracks:
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:9079/api/musicstream/tracks/reset-failed
```

---

## 6. Backups

```bash
ls -lt backups/*.sql                                    # newest first; 14 retained, pg_dump'd weekly (Sun) + on boot
bash scripts/verify_backup_restore.sh                   # restore newest into a scratch DB and verify (run monthly)
```
> The daemon's `pg_dump` is v17 while the server is v16, so dumps carry cosmetic v17
> directives (`\restrict`, `SET transaction_timeout`); restore with `psql` WITHOUT
> `ON_ERROR_STOP` (they warn but skip) — `verify_backup_restore.sh` does this and asserts
> on restored row counts.

Manual restore to a fresh server:
```bash
psql -U musicstream -d musicstream < backups/musicstream_<TS>.sql   # ignore the 2 cosmetic warnings
```

---

## 7. Scheduled jobs (APScheduler, SGT)

`spotify_sync` */15min · `saved_albums_sync` */6h · `followed_artists_sync` Sun 06:00 ·
`liked_artists_expand` 02:00 · `download_pipeline` 03:00 · `lb_discovery` 04:00 ·
`integrity_check` Wed/Sun 05:00 · `db_backup` Sun 05:00. (misfire_grace_time=3600.)

### Host-scheduled watchdogs (Windows Startup folder — `shell:startup`)
This environment cannot register Scheduled Tasks (`schtasks` Access denied); use the
Startup folder with a `.bat` that loops Git Bash:
```bat
@echo off
"C:\Program Files\Git\bin\bash.exe" -lc "while true; do /c/musicstream/scripts/watchdog_stuck_downloads.sh; sleep 600; done"
```
- `scripts/watchdog_stuck_downloads.sh` — alerts if tracks sit in `downloading` >30min (silent otherwise).
- `scripts/verify_backup_restore.sh` — monthly restore check.

---

## 8. Troubleshooting

| Symptom | Action |
|---|---|
| Tracks stuck `downloading` | Restart daemon (boot reset requeues). Check `watchdog_stuck_downloads.sh` log. |
| No downloads happening | `/health/deep` → is `scheduler_running`? Check `docker logs musicstream-daemon`; circuit breakers (`/api/musicstream/metrics`). |
| Spotify account locked (1–2h) | librespot single-flight violated — confirm `--workers 1`, never run parallel librespot. Wait it out. |
| yt-dlp / spotdl broke "overnight" | A pinned backend needs a deliberate bump — see `requirements.txt` pins + Dependabot PRs. |
| `ModuleNotFoundError: src.models` from alembic | `migrations/env.py` sys.path bootstrap missing — should be present (P1-1). |
| OAuth "needs_auth" loop | Token file not writable — entrypoint chmods it; re-auth via `/auth/spotify/login`. |
| Backlog ETA | `curl /api/musicstream/burn-rate`. If too slow, the serial T0 librespot pre-sweep is the likely bottleneck (tier-ordering, not more workers — see SCOPING_P2-9_P2-3.md). |

Logs: `docker logs musicstream-daemon` (console) and `./logs/musicstream.log` (+ `errors.log`).
The file logger self-heals via a 60s watchdog after uvicorn's dictConfig wipes handlers.

---

## 9. Boot autostart
The whole stack autostarts via a Startup-folder `.bat` running `docker compose up -d`
(see `scripts/Musicstream_Startup.bat`). Plex/Tailscale mobile access details are in `README.md`.
