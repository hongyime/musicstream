# MUSICSTREAM — Full Codebase Audit
**Audit Date:** 2026-05-02  
**Last updated:** 2026-05-02 (added circuit-breaker cascade bug, marked R4 as fixed)  
**Auditor:** Multi-agent board (Pathologist → Librarian → Archaeologist → Archivist → Adversary → Urbanist → Synthesizer)  
**Branch audited:** `main`

---

## ══ 0. FILESYSTEM HEALTH REPORT ══

### Corrupted files
None. All source `.py` and config files parse correctly. Zero-byte `.gitkeep` files are intentional placeholders, not corruption.

### Orphaned / leftover files

| File Path | Reason Flagged | Recommended Action |
|---|---|---|
| `__pycache__/daemon.cpython-312.pyc` (root) | Stale bytecode from pre-`src/` migration — no `daemon.py` at root | Delete |
| `__pycache__/db.cpython-312.pyc` (root) | Same — no `db.py` at root | Delete |
| `__pycache__/exceptions.cpython-312.pyc` (root) | Same | Delete |
| `__pycache__/models.cpython-312.pyc` (root) | Same | Delete |
| `__pycache__/rate_limiter.cpython-312.pyc` (root) | Same | Delete |
| `discovery/__pycache__/` (root-level dir, no .py files) | Root-level `discovery/` contains only `__pycache__` — source moved to `src/discovery/` | Delete directory |
| `ingestion/__pycache__/` (root-level dir, no .py files) | Same — source is in `src/ingestion/` | Delete directory |
| `integrity/__pycache__/` (root-level dir, no .py files) | Same — source is in `src/integrity/` | Delete directory |
| `ingestion/__pycache__/test_organiser.cpython-312-pytest-9.0.3.pyc` | Test bytecode at root-level ingestion (not src) | Delete with parent |
| `src/legacy_downloader.py` | `AudioExtractor` class, not imported anywhere in current codebase | Archive or delete after review |

### Sync artifacts
None detected.

---

## ══ 1. MASTER FEATURE MAP (SOURCE OF TRUTH) ══

### `src/models.py`
- **Purpose:** SQLAlchemy 2.0 ORM models for PostgreSQL.
- **Classes:** `Track`, `Source`, `LbRecommendation`, `DownloadAttempt`, `DaemonRun`; enums `TrackStatus`, `SourceType`; association table `track_sources`
- **Track fields of note:** `spotify_uri` (UNIQUE, serves as PK for LB tracks via `mb:{mbid}` prefix), `status` (plain VARCHAR, not SQLAlchemy Enum), `file_path` / `file_sha256` / `file_size_bytes` (all NULL until organiser runs), `plex_verified` (Boolean, default False — never set to True anywhere in code)
- **Relationships:** Track ↔ Source (M:M via `track_sources`), Track → DownloadAttempt (1:M, cascade delete), Track → LbRecommendation (1:M)
- **External deps:** SQLAlchemy 2.0

### `src/db.py`
- **Purpose:** PostgreSQL engine and session factory
- **Functions:** `get_engine()` (creates Engine from `DATABASE_URL`), `get_session_factory()`, `init_db()` (lazy singleton init), `get_session()` (context manager: yield→commit / except→rollback / always→close), `run_migrations()` (alembic upgrade head), `wait_for_db()` (5 retries, 5s backoff)
- **Pool config:** `pool_size=10`, `max_overflow=20`, `pool_pre_ping=True`
- **Alembic config path:** `Path(__file__).parent.parent / "alembic.ini"` → resolves to repo root
- **External deps:** SQLAlchemy, alembic, psycopg2-binary; reads `DATABASE_URL` env var

### `src/exceptions.py`
- **Purpose:** Exception hierarchy — all inherit from `MusicStreamError`
- **Classes:** `RateLimitError`, `SpotifyRateLimitError`, `YouTubeMusicRateLimitError`, `DownloadError`, `TaggingError`, `OrganiserError`, `IntegrityError`, `ListenBrainzError`, `MusicBrainzError`, `SpotiFLACError`, `DatabaseError`

### `src/rate_limiter.py`
- **Purpose:** Thread-safe per-service rate limiter with exponential backoff, jitter, and circuit breaker
- **Class `ServiceRateLimiter`:** `wait(service, attempt, retry_after)` sleeps; `record_success/failure()`; `is_healthy()` (auto-recovers after 30 min cooldown); circuit trips at 5 consecutive failures
- **Service configs:** spotify(3s/3600s/10), spotiflac(5s/300s/2), youtube(4s/600s/3), ytmusicapi(2.5s/300s/5), spotdl(3s/180s/3), musicbrainz(1s/60s/1), acoustid(0.5s/30s/3), listenbrainz(1s/60s/5), coverart(0.5s/30s/5)
- **Note:** `concurrent` field in `ServiceRateConfig` is stored but **not enforced** — no semaphore/counter logic; concurrency control solely via `ThreadPoolExecutor.max_workers=4` in downloader
- **Class `ExpiringResolutionCache`:** TTL-based cache used nowhere in the current codebase
- **Class `MusicDownloadChaosMonkey`:** Test-only failure injection; used in `legacy_downloader.py` and referenced in `src/legacy_downloader.py` only — not in production code path

### `src/ingestion/scraper.py`
- **Purpose:** Spotify PKCE ingestion — full backfill and incremental sync
- **Key method `full_backfill(session)`:** Fetches all playlists (`/me/playlists`), all playlist tracks, liked songs, saved albums, followed artists. Upserts into `tracks` (status=pending) and `sources`. Returns count of new tracks.
- **Key method `incremental_sync(session)`:** For each source, compares stored `snapshot_id` vs current Spotify API snapshot_id; fetches and upserts only changed playlists.
- **Auth:** PKCE via `SpotifyPKCE` with `open_browser=False`; requires pre-cached token at `SPOTIFY_TOKEN_CACHE` (default `/app/spotify_token.json`). Raises `RuntimeError` if no valid token found — this is a non-blocking exception caught by daemon startup.
- **OAuth scopes:** `playlist-read-private playlist-read-collaborative user-library-read user-follow-read user-read-recently-played`
- **External deps:** spotipy, `SPOTIFY_CLIENT_ID`, `SPOTIFY_TOKEN_CACHE`

### `src/ingestion/downloader.py`
- **Purpose:** 5-tier download orchestrator
- **Class `DownloadOrchestrator`:** `download_pending(session)` fetches all `status='pending'` tracks, runs them through `ThreadPoolExecutor(max_workers=4)`, each thread gets its own session
- **`download_track(track, session)`:** Runs tiers 1–5; records attempt in `download_attempts`; on success sets `status='downloaded'` and `download_method`; on exhaustion marks `status='failed'` if `≥25` failed attempts (`_GIVE_UP_THRESHOLD=25`)
- **Tier 1 (SpotiFLAC):** Downloads to temp subdir, then **transcodes to MP3 320 via FFmpeg subprocess**. Returns MP3 path. ⚠️ FLAC is discarded.
- **Tier 2 (yt-dlp + ytmusicapi):** Searches YTM (songs→videos→no filter), downloads via yt-dlp bestaudio→MP3 320, validates duration ±5s.
- **Tier 3 (spotdl):** Requires `SPOTIFY_CLIENT_SECRET`. Changes working directory with `os.chdir()` — **not thread-safe**.
- **Tier 4 (yt-dlp YouTube ytsearch12):** Two query variants.
- **Tier 5 (yt-dlp SoundCloud scsearch8):** Last resort.
- **Critical gap:** After successful tier download, `download_track` returns `True` WITHOUT calling `MetadataTagger` or `FileOrganiser`. `file_path`, `file_size_bytes`, `format` remain NULL. Files accumulate in `temp/`.
- **External deps:** yt-dlp, SpotiFLAC (optional), ytmusicapi (optional), spotdl (optional), `TEMP_DIR` env var, `cookies.txt`

### `src/ingestion/tagger.py`
- **Purpose:** MusicBrainz + AcoustID metadata tagging pipeline (ORPHANED — never called by downloader)
- **Class `MetadataTagger`:** `tag_file(file_path, track, session)` runs full tag pipeline
- **Tag priority per field:** Spotify → MusicBrainz → yt-dlp embed
- **MusicBrainz lookup:** ISRC → already-known acoustid_id (does NOT generate fingerprint) → title+artist text search
- **`_fingerprint(file_path, track, session)`:** Generates AcoustID fingerprint via pyacoustid. Method exists but is **never called** in `tag_file()` — fingerprinting is unreachable.
- **Tag writing:** mutagen for MP3 (ID3), FLAC (Vorbis), M4A (MP4 atoms)
- **External deps:** mutagen, pyacoustid (optional), requests, `ACOUSTID_API_KEY`

### `src/ingestion/organiser.py`
- **Purpose:** Move tagged files from temp/ to Plex directory structure (ORPHANED — never called by downloader)
- **Class `FileOrganiser`:** `organise(temp_path, track, session)` moves file, computes SHA-256, updates DB, triggers Plex refresh
- **Path format:** `{media_drive}/{album_artist}/{album} ({year})/{NN} - {title}.{ext}`
- **Collision resolution:** Appends ` (2)`, ` (3)`, etc. if path already in DB
- **Plex refresh:** `POST {plex_url}/library/sections/{id}/refresh?X-Plex-Token={token}` (non-fatal on failure)
- **External deps:** requests, `PLEX_URL`, `PLEX_TOKEN`, `PLEX_LIBRARY_SECTION_ID`, `EXTERNAL_MEDIA_DRIVE`

### `src/integrity/checker.py`
- **Purpose:** SHA-256 file integrity checker
- **`IntegrityChecker.run(session)`:** Loads ALL `status='downloaded' AND file_path IS NOT NULL` tracks. Checks file exists; if not: reset to pending, clear path/hash. Checks SHA-256 match; if corrupt: log, reset to pending. Updates `last_checked_at` on all checked tracks.
- **Returns:** `IntegrityResult(missing, corrupt, ok, total_checked)`
- **Note:** Because `FileOrganiser` is never called, `file_path` is always NULL for newly downloaded tracks — the checker finds nothing to check in practice.

### `src/discovery/listenbrainz.py`
- **Purpose:** ListenBrainz CF recommendation ingestion
- **`ListenBrainzDiscovery.run(session)`:** If `lb_recommendations` empty → backfill 200; else → poll 100. For each new MBID: fetch MusicBrainz metadata, create `LbRecommendation` + `Track` (spotify_uri=`mb:{mbid}`), status=pending.
- **API endpoint:** `GET https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording?count={n}&artist_type=top`
- **Handles both response shapes:** `payload.mbids` and `payload.recordings`
- **External deps:** requests, `LISTENBRAINZ_TOKEN`, `LISTENBRAINZ_USERNAME`

### `src/discovery/plex_playlists.py`
- **Purpose:** Create/update monthly Plex discovery playlists from LB recommendations
- **`PlexPlaylistSync.sync_discovery_playlist(session, month, year)`:** Queries downloaded LB tracks for given month, resolves Plex rating keys (one API call per file path), creates or appends to playlist `Discovered: {Month} {Year}`
- **External deps:** requests, `PLEX_TOKEN`, `PLEX_URL`, `PLEX_LIBRARY_SECTION_ID`

### `src/daemon.py`
- **Purpose:** APScheduler + Flask HTTP control plane
- **Entry point:** `__main__` block — DB init/migrations sync, then Flask starts immediately with startup_sequence in background thread
- **Scheduler jobs:** `*/15 * * * *` spotify_incremental_sync; `0 3 * * *` full_download_pipeline; `0 4 * * *` listenbrainz_discovery; `0 5 * * sun` full_integrity_check + db_backup
- **Flask endpoints:**
  - `GET /health` → `{status, uptime_s, db_tracks}`
  - `GET /status` → last 5 `daemon_runs` rows
  - `POST /sync` → queues `_run_full_pipeline` on scheduler
  - `POST /integrity` → queues `integrity_check`
  - `POST /discover` → queues `listenbrainz_discovery`
  - `GET /metrics` → per-tier download stats from `download_attempts`
  - `POST /backup` → runs `pg_dump` immediately, returns path + size
- **Backup:** `pg_dump {DATABASE_URL} --file {path}` → prune to 14 most recent `.sql` files
- **Logging:** 3 rotating handlers: `musicstream.log` (INFO+), `errors.log` (WARNING+, `musicstream.errors` + `errors` loggers), `daemon.log` (INFO+, `musicstream.daemon`)

### `src/ui.py`
- **Purpose:** Rich CLI output helpers
- **Exports:** `print_header`, `print_success`, `print_warning`, `print_error`, `print_summary`, `print_sources_table`, `create_progress`, `confirm_resume`, `print_interrupted`, `print_fresh_start`, `print_daemon_banner`, `print_integrity_result`
- **Note:** `print_daemon_banner` exported in `__all__` but never called — daemon has its own inline panel in `_print_startup_banner()`

### `src/legacy_downloader.py`
- **Purpose:** Original YouTube-only downloader (`AudioExtractor` class) — predates the 5-tier system
- **Status:** Not imported anywhere in the codebase. Dead code.

### `main.py`
- **Purpose:** CLI entry point
- **Commands:** `scrape` (full_backfill), `download` (download_pending), `status` (DB stats + sources), `integrity`, `daemon` (starts startup_sequence + Flask), `validate` (ruff + mypy)
- **Note:** `cmd_daemon` calls `daemon_module.startup_sequence()` then `app.run()` — this runs startup synchronously BEFORE Flask starts. Contradicts the `__main__` block design which uses a background thread. Only one of these entry points is production-used (Docker runs `python -m src.daemon`).

### `migrations/versions/0001_initial_schema.py`
- **Creates:** All 6 tables (tracks, sources, track_sources, lb_recommendations, download_attempts, daemon_runs), 4 indexes, `update_updated_at` trigger
- **Reversible:** Yes — `downgrade()` drops all objects in reverse dependency order
- **Missing from migration:** `ON DELETE CASCADE` on `track_sources` FK (present in ORM model but not in migration `ForeignKeyConstraint` definitions)

### `setup.bat` / `startup.bat`
- **setup.bat:** 10-step one-time init: prereq checks, .env generation, directories, Spotify OAuth (calls `python -m src.ingestion.spotify_auth`), scrobbler config, firewall rules, docker pull, postgres start + migrations, gitignore validation
- **startup.bat:** 9-option operations menu: start/stop stack, health view, force sync/integrity/backup/discover, live logs, reset failed tracks to pending

---

## ══ 2. RECONCILIATION SUMMARY ══

**Truth Gap:**
- Fully implemented (code matches docs): DB schema, rate limiter, ListenBrainz discovery, Plex playlist sync, integrity checker, scraper (PKCE), Flask control plane, APScheduler, backups, setup.bat, startup.bat, Docker stack
- Partially implemented: Download pipeline (tiers 1–5 exist but tagging and organizing are never wired in)
- Absent in code (documented but missing): AcoustID fingerprinting trigger, `plex_verified` field ever being set to True
- Present in code but absent from docs: `SPOTIFY_CLIENT_SECRET` env var, additional OAuth scopes, `SPOTIFY_TOKEN_CACHE` env var, `src/legacy_downloader.py`

**State of system:** The codebase is architecturally complete — every documented module exists and is individually well-implemented. The fatal gap is at the integration seam: `DownloadOrchestrator.download_track()` marks a track as `downloaded` after the tier download succeeds but never calls `MetadataTagger.tag_file()` or `FileOrganiser.organise()`. The result is that all "downloaded" tracks have `file_path=NULL`, files accumulate untagged in `temp/`, Plex is never notified, and the integrity checker finds nothing to check. The system can ingest Spotify tracks into the DB and download audio files, but cannot deliver them to Plex.

**Production Readiness Score: 8/15** (see §9)

---

## ══ 3. CRITICAL GAPS (UNIMPLEMENTED FEATURES) ══

| Feature | Source Doc | Severity | Why it matters |
|---|---|---|---|
| Tagger + Organiser never called after download | PRD §7.3, §7.4 | P0 | Files stay in `temp/`, `file_path` always NULL, Plex never receives music |
| SpotiFLAC preserves FLAC (primary value prop) | PRD §3.2, §7.2 | P0 | Code transcodes all Tier 1 downloads to MP3 320 — entire lossless proposition is false |
| AcoustID fingerprinting never triggered | PRD §7.3.2 step 2 | P1 | `_fingerprint()` exists but is never called in `tag_file()` — MusicBrainz lookup via AcoustID is unreachable |
| `plex_verified` never set to True | PRD §6.1 (`plex_verified BOOLEAN DEFAULT FALSE`) | P2 | Column defined, never updated — library verification state is always stale/wrong |

---

## ══ 4. UNDOCUMENTED LOGIC (GHOST FEATURES) ══

| Module/Function | File | What it does | Why to document |
|---|---|---|---|
| `SpotifyScraper` — saved albums ingestion | `src/ingestion/scraper.py` | Fetches and upserts saved albums via `get_saved_albums()` — not in PRD §7.1 | Users may expect this or be confused by unexpected source types |
| `SpotifyScraper` — followed artists ingestion | `src/ingestion/scraper.py` | Fetches followed artists' full discographies — not in PRD §7.1 | Major scope expansion: followed artists = enormous track counts |
| `SPOTIFY_CLIENT_SECRET` env var | `src/ingestion/downloader.py:424`, `setup.bat:130`, `.env.example:12` | Required for Tier 3 spotdl; silently skipped if absent | PRD §16 only documents `SPOTIFY_CLIENT_ID`; secret needed for spotdl but not mentioned |
| `SPOTIFY_TOKEN_CACHE` env var | `src/ingestion/scraper.py:74` | Controls path to Spotify token JSON; default `/app/spotify_token.json` | Not in PRD §16 — critical for Docker deployment |
| `startup.bat` option [8] — Reset Failed Tracks | `startup.bat:278-299` | Resets all `status='failed'` tracks to pending AND deletes ALL download_attempts | Deletes history; more destructive than documented |
| `ExpiringResolutionCache` class | `src/rate_limiter.py:222` | Full TTL-based cache implementation | Not used anywhere — either document intended use or remove |
| `MusicDownloadChaosMonkey` class | `src/rate_limiter.py:306` | Random failure injection for testing | Only used in `legacy_downloader.py`; should either be in tests/ or removed |
| `ServiceRateConfig.concurrent` field | `src/rate_limiter.py:28` | Stores concurrency limit per service | Field is defined and set but never enforced — concurrency is controlled only by `MAX_CONCURRENT=4` |

---

## ══ 5. DOCUMENTATION DRIFT ══

| Documented Behavior | Actual Behavior | File | Correction Needed |
|---|---|---|---|
| PRD §5: repo structure flat (no `src/` prefix) | All source code is under `src/`; flat structure is for legacy dirs with only `__pycache__` | `PRD.md:130-157` | Update §5 repo tree to show `src/` prefix |
| PRD §3.2/§7.2: Tier 1 outputs lossless FLAC | Tier 1 transcodes to MP3 320 via FFmpeg immediately after SpotiFLAC download | `src/ingestion/downloader.py:290-312` | Either remove transcode step to restore FLAC output, or update PRD to state Tier 1 also outputs MP3 |
| PRD §3.3: SpotiFLAC priority `["qobuz", "tidal", …]` (Qobuz first) | Code iterates `["tidal", "qobuz", "amazon", "deezer", "youtube"]` (Tidal first) | `src/ingestion/downloader.py:272` | Align code or PRD |
| PRD §7.1 Spotify scopes: 3 scopes only | Scraper and auth helper use 5 scopes (`+user-follow-read +user-read-recently-played`) | `src/ingestion/scraper.py:35-41` | Add new scopes to PRD §7.1 |
| PRD §12: give-up after 9 failures | `_GIVE_UP_THRESHOLD = 25` | `src/ingestion/downloader.py:67` | Correct PRD §12 threshold |
| PRD §9.2 Dockerfile CMD: `["python", "daemon.py"]` | Actual: `["python", "-m", "src.daemon"]` | `Dockerfile.daemon:16` | Update PRD §9.2 |
| PRD §9.1 plex uses `network_mode: host`, `start_period: 60s` | Actual: no `network_mode`, `start_period: 120s` | `docker-compose.yml:21-39` | Update PRD §9.1 |
| PRD §16: env vars reference — missing `SPOTIFY_CLIENT_SECRET`, `DATABASE_URL`, `PLEX_TOKEN`, `PLEX_URL`, `PLEX_LIBRARY_SECTION_ID`, `PLEX_USERNAME`, `SPOTIFY_TOKEN_CACHE` | All present in `.env.example` and required for operation | `PRD.md:909-934` | Add all missing vars to PRD §16 |

---

## ══ 6. DATA INTEGRITY REPORT ══

PostgreSQL database is remote/containerized — direct schema introspection is not available in this environment. Analysis is based on migration code and ORM definitions.

| Table | Schema Match | Notes |
|---|---|---|
| `tracks` | PASS (migration matches ORM) | `file_path`, `file_size_bytes`, `format` always NULL for newly downloaded tracks — organiser never called |
| `sources` | PASS | `SourceType` enum has `HISTORY` (in code) not defined in PRD |
| `track_sources` | PARTIAL — FK cascade missing | Migration FKs lack `ON DELETE CASCADE`; ORM model has correct cascade. Orphaned rows possible if tracks deleted outside ORM |
| `lb_recommendations` | PASS | |
| `download_attempts` | PASS | `ON DELETE CASCADE` present in migration |
| `daemon_runs` | PASS | `tracks_scraped` field always 0 — `_record_run_complete` call sites don't pass `scraped` count |

**Incomplete write risk:** Every download marks `status='downloaded'` but leaves `file_path=NULL`. A query `SELECT COUNT(*) FROM tracks WHERE status='downloaded' AND file_path IS NULL` would return the full "downloaded" count — these records appear complete but reference no file.

---

## ══ 7. CODE QUALITY FINDINGS ══

### [SECURITY]

| # | Description | File | Function | Severity | Fix |
|---|---|---|---|---|---|
| S1 | `pg_dump` invoked with full `DATABASE_URL` (containing password) as CLI argument — visible in process list and shell history | `src/daemon.py:339` | `db_backup()` | P1 | Use `PGPASSWORD` env var or `--no-password` with `.pgpass` file instead |
| S2 | Flask control plane (POST /sync, /integrity, /discover, /backup, GET /metrics, /status) has no authentication — any process that can reach port 9079 can trigger pipeline actions or read operational data | `src/daemon.py:652-783` | Flask routes | P2 | Add shared-secret header check (e.g. `X-Daemon-Token`) to mutating endpoints |
| S3 | Plex token passed as URL query parameter — logged by HTTP servers and proxy systems | `src/ingestion/organiser.py:269` | `_refresh_plex()` | P2 | Use `X-Plex-Token` request header instead of params |

### [LOGIC]

| # | Description | File | Function | Severity | Fix |
|---|---|---|---|---|---|
| L1 | **Tagger and Organiser never called** — after a successful tier download, `download_track()` sets `status='downloaded'` but returns without calling `MetadataTagger.tag_file()` or `FileOrganiser.organise()`. `file_path` is never set. | `src/ingestion/downloader.py:185-200` | `download_track()` | P0 | After successful tier: (1) call `tagger.tag_file(path, track, session)`, (2) call `organiser.organise(path, track, session)`, (3) handle tagging/organiser errors |
| L2 | **SpotiFLAC (Tier 1) converts FLAC to MP3** — entire primary value prop is that Tier 1 produces lossless FLAC, but code immediately transcodes with `ffmpeg -codec:a libmp3lame -b:a 320k` | `src/ingestion/downloader.py:290-312` | `_tier1_spotiflac()` | P0 | Remove FFmpeg transcode step; return original FLAC/lossless file; update `format` detection accordingly |
| L3 | **AcoustID fingerprinting never triggered** — `_fingerprint(file_path)` method exists but is never called inside `tag_file()`; the `_fetch_musicbrainz()` method only uses `track.acoustid_id` if already set, which it never is for new downloads | `src/ingestion/tagger.py:268-270` | `_fetch_musicbrainz()` | P1 | In `tag_file()`, call `self._fingerprint(file_path, track, session)` before `_fetch_musicbrainz()`, or call it as a post-move async step |
| L4 | **`os.chdir()` in Tier 3 spotdl is not thread-safe** — `os.chdir()` changes the working directory for the entire process; with `MAX_CONCURRENT=4` workers, concurrent Tier 3 attempts race to change the CWD, corrupting relative paths for all threads | `src/ingestion/downloader.py:435-446` | `_tier3_spotdl()` | P1 | Use `subprocess`-based spotdl with explicit `--output` path instead of relying on CWD; or use a per-thread lock |
| L5 | **`wait_for_db()` creates a discarded engine** — `wait_for_db()` creates an Engine via `get_engine()` and returns it; caller (`__main__`, `startup_sequence`) ignores the return value; then `init_db()` creates a second Engine from scratch. Two engines = two connection pools (20 connections each = 40 total) | `src/db.py:155`, `src/daemon.py:483,807` | `wait_for_db()` | P2 | Pass the engine returned by `wait_for_db()` into `init_db()` rather than re-creating |
| L6 | **`startup_sequence()` re-runs DB init and migrations** — `__main__` runs DB init + migrations before starting Flask; `startup_sequence()` runs them again in the background thread. Migrations are idempotent but wastes time on every startup | `src/daemon.py:479-496` | `startup_sequence()` | P2 | Remove Steps 1–2 from `startup_sequence()` since they already ran in `__main__` |
| L7 | **`plex_verified` field is never set to True** — defined in schema and ORM with `default=False`, but no code path ever sets it to `True` | `src/models.py:127`, entire codebase | — | P2 | Either set it after successful Plex refresh in `FileOrganiser._refresh_plex()`, or remove the column |
| L8 | **YTMusic client instantiated 3 times per Tier 2 attempt** — `YTMusic()` is called inside the `search_filters` loop, creating a new HTTP session each time | `src/ingestion/downloader.py:342` | `_tier2_ytdlp_ytm()` | P3 | Instantiate `YTMusic()` once per `DownloadOrchestrator` instance |
| L9 | **`download_pending()` holds outer session for entire download batch** — outer session fetches pending tracks, then is kept open (via `with get_session() as session:`) while `ThreadPoolExecutor` runs all downloads; for a large library this holds a DB connection for hours | `src/ingestion/downloader.py:90-157` | `download_pending()` | P2 | Fetch track IDs in a short-lived session, close it, then spawn threads |
| L10 | **`cmd_daemon` in main.py runs startup_sequence synchronously before Flask** — calling `main.py daemon` runs startup_sequence before `app.run()`, meaning health endpoint is unavailable during startup; contradicts the background-thread design in `__main__` | `main.py:302-311` | `cmd_daemon()` | P2 | Mirror the `__main__` pattern: start Flask first, run startup in thread |

### [PERFORMANCE]

| # | Description | File | Function | Severity | Fix |
|---|---|---|---|---|---|
| P1 | **`IntegrityChecker` loads all downloaded tracks at once** — `session.query(Track).filter(...).all()` for a 10k+ track library loads hundreds of MB into memory | `src/integrity/checker.py:107-113` | `IntegrityChecker.run()` | P2 | Use `yield_per(500)` or paginated batches |
| P2 | **`_resolve_plex_keys` makes one serial HTTP call per track** — for a 100-track discovery batch: 100 sequential Plex API calls | `src/discovery/plex_playlists.py:204-230` | `_resolve_plex_keys()` | P2 | Use concurrent.futures or batch Plex search if API supports it |
| P3 | **`ServiceRateConfig.concurrent` never enforced** — rate limiter tracks concurrency limits in config but has no semaphore; `begin_operation`/`end_operation` are no-ops | `src/rate_limiter.py:194-196` | legacy shims | P2 | Add semaphore per service in `ServiceRateLimiter.__init__`; acquire/release in `wait()` and shim methods |

### [RELIABILITY]

| # | Description | File | Function | Severity | Fix |
|---|---|---|---|---|---|
| R1 | **POST /sync queues job on scheduler before scheduler.start()** — if `/sync` is called during the background startup window before step 9 (`scheduler.start()`), the job is queued but APScheduler does not run it until started | `src/daemon.py:652-669` | `sync()` Flask route | P1 | Run `_run_full_pipeline()` in a raw thread if `not scheduler.running`, or ensure scheduler starts before Flask |
| R2 | **`track_sources` missing ON DELETE CASCADE in migration** — if a track is deleted outside the ORM (e.g. via startup.bat option [8] which runs raw SQL), orphaned `track_sources` rows remain | `migrations/versions/0001_initial_schema.py:112-115` | migration `upgrade()` | P2 | Add migration 0002 with `ALTER TABLE track_sources ADD CONSTRAINT fk_ts_track FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE` |
| R3 | **No timeout on requests.Session calls for cover art** — `_fetch_cover_art` uses `requests.get(url, timeout=15)` but the `_mb_session` used for MusicBrainz lookups has no default timeout — only individual calls set timeout=10 | `src/ingestion/tagger.py:135` | `MetadataTagger.__init__()` | P2 | Set `requests.Session` adapter with default timeout |
| R4 | **Circuit breaker cascade kills entire download batch** ✅ FIXED — `ServiceRateLimiter` threshold=5 with 4 concurrent workers: as few as 2 tracks each failing Tier 2 once trips the YouTube breaker; remaining tracks skip Tiers 2/4/5 for 30-minute cooldown. Duration mismatches (wrong video = expected) were also counted as service failures, accelerating the trip. **Fix applied:** threshold raised to 20, cooldown lowered to 300s, duration mismatch removed from `record_failure` call. | `src/ingestion/downloader.py:84-86`, `src/ingestion/downloader.py:399`, `src/rate_limiter.py:62-63` | `DownloadOrchestrator.__init__()`, `_tier2_ytdlp_ytm()` | P1 | **Done** — see commit |

### [DEAD]

| # | Description | File | Severity |
|---|---|---|---|
| D1 | `src/legacy_downloader.py` — `AudioExtractor` class, complete old downloader, imported nowhere | `src/legacy_downloader.py` | P3 |
| D2 | `ui.py:print_daemon_banner` — exported in `__all__`, never called; daemon uses its own inline `_print_startup_banner()` | `src/ui.py:170` | P3 |
| D3 | `ExpiringResolutionCache` class — full TTL cache implementation, used nowhere in production code | `src/rate_limiter.py:222` | P3 |
| D4 | `ServiceRateConfig.concurrent` field — stored but never enforced; `begin_operation`/`end_operation` are no-ops | `src/rate_limiter.py:28` | P3 |
| D5 | `Track.plex_verified` column — default False, never set to True | `src/models.py:127` | P3 |
| D6 | Root-level `__pycache__` (5 stale pyc files) and root-level `discovery/`, `ingestion/`, `integrity/` dirs (only `__pycache__`) | root directory | P3 |
| D7 | Duplicate `_compute_sha256` — identical 10-line function in both `checker.py:44` and `organiser.py:221` | `src/integrity/checker.py`, `src/ingestion/organiser.py` | P3 |

---

## ══ 8. STRUCTURAL REORGANIZATION PLAN ══

### 8a. Current File Tree (abbreviated — full)
```
musicstream/
├── __pycache__/             ← STALE: 5 root-level pyc files
├── discovery/               ← STALE: only __pycache__, no .py files
│   └── __pycache__/
├── ingestion/               ← STALE: only __pycache__, no .py files
│   └── __pycache__/
├── integrity/               ← STALE: only __pycache__, no .py files
│   └── __pycache__/
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial_schema.py
├── src/
│   ├── __init__.py
│   ├── daemon.py
│   ├── db.py
│   ├── exceptions.py
│   ├── legacy_downloader.py  ← DEAD CODE
│   ├── models.py
│   ├── rate_limiter.py
│   ├── ui.py
│   ├── discovery/
│   │   ├── __init__.py
│   │   ├── listenbrainz.py
│   │   └── plex_playlists.py
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── downloader.py
│   │   ├── organiser.py
│   │   ├── scraper.py
│   │   ├── spotify_auth.py
│   │   └── tagger.py
│   └── integrity/
│       ├── __init__.py
│       └── checker.py
├── tests/
│   ├── test_daemon_flask.py
│   ├── test_db.py
│   ├── test_downloader.py
│   ├── test_exceptions.py
│   ├── test_integrity.py
│   ├── test_listenbrainz.py
│   ├── test_models.py
│   ├── test_organiser.py
│   ├── test_organiser_legacy.py   ← tests legacy_downloader organiser
│   ├── test_rate_limiter.py
│   ├── test_scraper.py
│   └── test_tagger.py
├── .env                      ← PROTECTED
├── .env.example
├── .gitignore
├── alembic.ini
├── AGENTS.md
├── backups/.gitkeep          ← PROTECTED (directory)
├── cookies.txt               ← PROTECTED
├── docker-compose.yml
├── Dockerfile.daemon
├── logs/.gitkeep             ← PROTECTED (directory)
├── main.py
├── PRD.md
├── README.md
├── requirements.txt
├── setup.bat
├── startup.bat
└── CONTRIBUTING.md / LICENSE / SECURITY.md
```

### 8b. Target File Tree
```
musicstream/
├── src/                      (no change — already correct)
│   ├── daemon.py
│   ├── db.py
│   ├── exceptions.py
│   ├── models.py
│   ├── rate_limiter.py
│   ├── ui.py
│   ├── discovery/
│   ├── ingestion/
│   └── integrity/
├── migrations/               (no change)
├── tests/                    (remove test_organiser_legacy.py after legacy_downloader.py removed)
├── main.py                   (keep at root)
├── docker-compose.yml
├── Dockerfile.daemon
├── alembic.ini
├── requirements.txt
├── setup.bat / startup.bat
├── docs/                     ← NEW: move PRD.md, README.md, CONTRIBUTING.md, SECURITY.md here
├── .env                      ← PROTECTED
├── .env.example
├── .gitignore
└── backups/.gitkeep / logs/.gitkeep / cookies.txt  ← PROTECTED
```

### 8c. Move Plan

| Step | Action | Source Path | Destination | Protected? | Backup Required? |
|---|---|---|---|---|---|
| 1 | Delete stale root `__pycache__` | `__pycache__/` (root) | — | No | No |
| 2 | Delete stale root `discovery/` | `discovery/` (root, __pycache__ only) | — | No | No |
| 3 | Delete stale root `ingestion/` | `ingestion/` (root, __pycache__ only) | — | No | No |
| 4 | Delete stale root `integrity/` | `integrity/` (root, __pycache__ only) | — | No | No |
| 5 | Archive or delete dead code | `src/legacy_downloader.py` | (review first) | No | No |
| 6 | Move PRD.md to docs/ | `PRD.md` | `docs/PRD.md` | No | No |

### 8d. New Directories
| Directory | Purpose |
|---|---|
| `docs/` | Project documentation (PRD, architecture notes) — keeps root clean |

### 8e. .gitignore Additions

| Pattern | Reason |
|---|---|
| `spotify_token.json` | Auth token — already present ✓ |
| `docs/__pycache__/` | If any Python tools process docs |
| `alembic.ini` notes | Currently committed — acceptable, contains no secrets if DATABASE_URL uses env substitution |
| `AUDIT.md` | This file itself should not be committed to production branches |

**Current `.gitignore` gap:** The rule `.*` negates itself with the long exception list — consider replacing with an explicit allowlist. The rule `skills/` and `skills-lock.json` reference non-existent directories in this repo.

---

## ══ 9. PRODUCTION READINESS CHECKLIST ══

| # | Item | Status | Justification |
|---|---|---|---|
| 1 | All secrets externalized to environment variables | PASS | No hardcoded secrets found in source. `pg_dump` receives DATABASE_URL (which contains password) via env var, but passes it on CLI — see S1 |
| 2 | All dependencies pinned to explicit versions | PARTIAL | `yt-dlp[default]`, `spotdl`, `rich` are unpinned in `requirements.txt`. Intentional for `spotdl`/`yt-dlp` (compatibility constraints) but introduces reproducibility risk |
| 3 | All database migrations versioned and reversible | PASS | Single migration `0001` with correct `downgrade()`. All tables, indexes, and trigger removed in reverse order |
| 4 | All external API calls have timeout and retry configurations | PARTIAL | `requests.get()` calls have `timeout=10-30s`. Rate limiter retries up to 3×. `pg_dump` has 300s timeout. `yt-dlp` has `retries=3`. MusicBrainz `requests.Session` has no default timeout (per-call only) |
| 5 | Logging is structured (JSON or key-value) | PARTIAL | Log messages use key=value format consistently. Not JSON-structured. RotatingFileHandler with consistent formatter. Acceptable for this scale |
| 6 | No debug routes, test endpoints, or dev-only flags active in production paths | PASS | No debug routes. `MusicDownloadChaosMonkey` defaults to `enabled=False` |
| 7 | Graceful shutdown handling for long-running processes | FAIL | No `signal.signal(SIGTERM, ...)` handler. APScheduler `BackgroundScheduler` does not shut down cleanly on SIGTERM — in-progress downloads will be interrupted mid-tier |
| 8 | Error responses do not leak stack traces or internal paths | PASS | Flask routes return `{"error": str(exc)}` — `str(exc)` may contain file paths but no full tracebacks |
| 9 | Input validation at all external-facing interfaces | PARTIAL | `/status`, `/health`, `/metrics` have no input. `/sync`, `/integrity`, `/discover`, `/backup` are POST with no body — no injection surface. No auth check is a separate concern |
| 10 | Health check endpoint present | PASS | `GET /health` returns `{status, uptime_s, db_tracks}` — matches docker-compose healthcheck config |
| 11 | All file writes are atomic or guarded against partial-write corruption | PARTIAL | `shutil.move()` is atomic on same filesystem. `pg_dump` deletes partial file on failure. yt-dlp uses `.part` files. `_tag_opus()` uses atomic `os.replace()`. Direct mutagen saves (MP3/FLAC) are not atomic |
| 12 | Rate limiting or abuse prevention on public-facing endpoints | FAIL | No authentication, no rate limiting on Flask endpoints. Anyone on the container network can spam `/sync` |
| 13 | All authentication tokens/sessions have expiry logic | PASS | Spotify PKCE tokens are refreshed automatically by spotipy on each request. `ExpiringResolutionCache` has TTL (though unused). Circuit breaker auto-recovers after 30 min |
| 14 | Test coverage for all critical paths | PARTIAL | Unit tests exist for all modules individually (`tests/test_downloader.py`, `test_tagger.py`, etc.). No integration test covering the full pipeline (download→tag→organise→DB). The core bug (tagger/organiser never called) would be caught by an end-to-end test |
| 15 | Build/start process is documented and reproducible | PASS | `setup.bat` + `startup.bat` documented in PRD §15. `Dockerfile.daemon` is minimal and deterministic. `requirements.txt` pins most deps |

**Score: 8/15 PASS, 2 FAIL, 5 PARTIAL**

---

## ══ 10. PRIORITIZED REMEDIATION ROADMAP ══

| Priority | Action | Rationale | Files Affected | Effort |
|---|---|---|---|---|
| 1 | **[P0] Wire tagger and organiser into download_track()** — after successful tier download, call `MetadataTagger.tag_file(path, track, session)` then `FileOrganiser.organise(path, track, session)`; handle each failure independently (tagging failure = warn + continue; organiser failure = status='failed') | Core pipeline broken — system cannot deliver music to Plex | `src/ingestion/downloader.py` | M |
| 2 | **[P0] Remove FLAC→MP3 transcode in Tier 1** — delete the FFmpeg subprocess block in `_tier1_spotiflac()`; return the original file from SpotiFLAC; adjust `_resolve_method_label` to detect FLAC vs non-FLAC output | Preserves the primary value prop (lossless FLAC) | `src/ingestion/downloader.py:290-312` | S |
| 3 | ✅ **[P1] Fix circuit breaker cascade** — DONE. threshold raised 5→20, cooldown 1800→300s, duration-mismatch removed from `record_failure("youtube")` | With 4 workers, threshold=5 trips in seconds; 30-min cooldown blocks entire batch | `src/ingestion/downloader.py`, `src/rate_limiter.py` | S |
| 4 | **[P1] Fix os.chdir() thread-safety in Tier 3** — replace `os.chdir()` with subprocess-based spotdl invocation using explicit `--output` flag, or add a per-process lock around Tier 3 calls | Race condition corrupts all threads' relative paths under concurrent downloads | `src/ingestion/downloader.py:435-446` | S |
| 4 | **[P1] Fix pg_dump password exposure** — replace `subprocess.run(["pg_dump", database_url, ...])` with `PGPASSWORD=... pg_dump -h host -U user -d dbname` to avoid password in process args | Password visible in process list / shell history | `src/daemon.py:339` | S |
| 5 | **[P1] Trigger AcoustID fingerprinting in tag pipeline** — in `tag_file()`, call `self._fingerprint(file_path, track, session)` before `_fetch_musicbrainz()` so the AcoustID path is reachable | PRD documents this as step 2 of MusicBrainz lookup; currently unreachable | `src/ingestion/tagger.py:158-170` | S |
| 6 | **[P1] Ensure scheduler is running before accepting /sync** — in Flask `/sync`, `/integrity`, `/discover` routes: if `not scheduler.running`, run the job directly in a daemon thread instead of via `scheduler.add_job()` | Jobs queued before scheduler.start() are silently deferred | `src/daemon.py:652-709` | S |
| 7 | **[P2] Add `track_sources` ON DELETE CASCADE migration** — create `migrations/versions/0002_track_sources_cascade.py` adding the missing FK cascade | startup.bat [8] runs raw SQL deletes; orphaned rows accumulate | `migrations/versions/` | S |
| 8 | **[P2] Add SIGTERM handler for graceful shutdown** — register `signal.signal(SIGTERM, ...)` to call `scheduler.shutdown(wait=False)` and flush open sessions | Docker sends SIGTERM on stop; in-progress downloads interrupted silently | `src/daemon.py` | S |
| 9 | **[P2] Fix dual-engine creation** — pass engine from `wait_for_db()` into `init_db(engine)` parameter rather than creating a second engine | Two connection pools active simultaneously (40 connections) | `src/db.py:70-82`, `src/daemon.py:483-484,807-808` | S |
| 10 | **[P2] Remove duplicate startup steps from startup_sequence()** — Steps 1–2 in `startup_sequence()` are already executed in `__main__`; remove them from the background thread path | Wasted startup time; two migration runs per boot | `src/daemon.py:479-496` | S |
| 11 | **[P2] Add authentication to mutating Flask endpoints** — add shared-secret middleware for POST /sync, /integrity, /discover, /backup | No auth = anyone on container network can trigger pipeline | `src/daemon.py:652-783` | S |
| 12 | **[P2] Paginate IntegrityChecker** — replace `.all()` with `yield_per(500)` | Memory spike on large libraries | `src/integrity/checker.py:107-113` | S |
| 13 | **[P2] Move Plex token to request header** in `FileOrganiser._refresh_plex()` | Token visible in HTTP logs | `src/ingestion/organiser.py:268-269` | S |
| 14 | **[P2] Fix cmd_daemon in main.py** to mirror `__main__` background-thread pattern | startup_sequence blocks Flask in CLI path | `main.py:302-311` | S |
| 15 | **[P3] Delete stale root-level __pycache__ and orphaned dirs** | `rm -rf __pycache__/ discovery/ ingestion/ integrity/` (root only) | Root directory | S |
| 16 | **[P3] Archive or delete legacy_downloader.py** | Dead code; confuses readers | `src/legacy_downloader.py`, `tests/test_organiser_legacy.py` | S |
| 17 | **[P3] De-duplicate _compute_sha256** — move to `src/utils.py` or `src/db.py`; import from both `checker.py` and `organiser.py` | Two identical implementations | `src/integrity/checker.py:44`, `src/ingestion/organiser.py:221` | S |
| 18 | **[P3] Update PRD.md** — fix §5 repo structure, §3.2/7.2 FLAC claim, §3.3 service priority, §7.1 scopes, §9.1 docker-compose, §9.2 Dockerfile CMD, §12 threshold, §16 env vars | Documentation misleads anyone using it | `PRD.md` | M |
| 19 | **[P3] Pin yt-dlp and spotdl** in requirements.txt — use `yt-dlp==<latest-stable>` and `spotdl==<latest>` with a comment explaining the constraint | Unpinned = undetected breakage on pip install | `requirements.txt` | S |
| 20 | **[P3] Remove or enforce ServiceRateConfig.concurrent** — add semaphore per service in `ServiceRateLimiter.__init__()` and acquire/release it, or remove the unused field | Documented rate limiting doesn't match actual behavior | `src/rate_limiter.py` | M |

---

*Audit complete. Top two items (L1: wire tagger+organiser, L2: restore FLAC output) are blocking correctness — no music reaches Plex without them.*
