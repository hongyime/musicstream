# tasks.md — Execution Task List
**Generated:** 2026-05-02

Status: ⬜ Pending | 🔄 In Progress | ✅ Done

---

## BLOCK A — P0 (system broken without these)

### T01 — Add OGG/Opus tagger support [B21]
**File:** `src/ingestion/tagger.py`
**Acceptance:** `_write_tags()` dispatches `.ogg` → `_tag_ogg()` and `.opus` → `_tag_opus()` without raising; mutagen OggVorbis/OggOpus imports present
**Status:** ✅

### T02 — De-duplicate `_compute_sha256` into `src/utils.py` [B19]
**Files:** new `src/utils.py`, `src/integrity/checker.py`, `src/ingestion/organiser.py`
**Acceptance:** single implementation in `utils.py`; both callers import from there; no duplicate function remains
**Status:** ✅

### T03 — Wire tagger + organiser into `download_track()` [B01]
**File:** `src/ingestion/downloader.py`
**Acceptance:** after successful tier: tagger called (TaggingError → warn + continue), organiser called (OrganiserError → status=failed_validation + return False); `track.status` NOT set to downloaded by download_track itself (organiser owns that transition); `file_path` populated on success
**Status:** ✅

---

## BLOCK B — P1 (data loss / crash)

### T04 — Fix os.chdir() race in Tier 3 [B04]
**File:** `src/ingestion/downloader.py`
**Acceptance:** Tier 3 no longer calls `os.chdir()`; uses subprocess with `--output` flag or threading.Lock; concurrent workers cannot interfere
**Status:** ✅

### T05 — Fix pg_dump password exposure [B05]
**File:** `src/daemon.py`
**Acceptance:** `pg_dump` called without `DATABASE_URL` in CLI args; password passed via `PGPASSWORD` env var only; verified by reading final subprocess call
**Status:** ✅

### T06 — Trigger AcoustID fingerprinting in tag pipeline [B06]
**File:** `src/ingestion/tagger.py`
**Acceptance:** `tag_file()` calls `self._fingerprint(file_path, track, session)` before `_fetch_musicbrainz()`; only when acoustid_key set and ACOUSTID_AVAILABLE
**Status:** ✅

### T07 — Fix /sync scheduler race [B08]
**File:** `src/daemon.py`
**Acceptance:** `/sync`, `/integrity`, `/discover` routes check `scheduler.running`; if False, dispatch in daemon thread; if True, use `scheduler.add_job()`
**Status:** ✅

---

## BLOCK C — P2 (reliability)

### T08 — Add migration 0002: track_sources CASCADE [B09]
**File:** `migrations/versions/0002_track_sources_cascade.py`
**Acceptance:** migration file created with correct `upgrade()` and `downgrade()`; FK re-added with `ON DELETE CASCADE`
**Status:** ✅

### T09 — Add SIGTERM handler [B10]
**File:** `src/daemon.py`
**Acceptance:** `signal.signal(SIGTERM, ...)` registered in `__main__`; handler calls `scheduler.shutdown(wait=False)` then raises SystemExit(0)
**Status:** ✅

### T10 — Fix dual engine creation [B11]
**File:** `src/db.py`, `src/daemon.py`
**Acceptance:** `init_db()` accepts optional `engine` param; `wait_for_db()` return value passed to `init_db()`; one engine, one pool
**Status:** ✅

### T11 — Remove duplicate startup steps [B12]
**File:** `src/daemon.py`
**Acceptance:** `startup_sequence()` no longer calls `wait_for_db()` or `run_migrations()`; those steps removed from the docstring steps 1-2 inside that function
**Status:** ✅

### T12 — Add Flask endpoint auth [B13]
**File:** `src/daemon.py`
**Acceptance:** helper `_check_auth()` reads `DAEMON_API_TOKEN` env var; if set, POST /sync /integrity /discover /backup and GET /metrics require matching token; returns 401 on mismatch; no-op if var unset
**Status:** ✅

### T13 — Paginate IntegrityChecker [B14]
**File:** `src/integrity/checker.py`
**Acceptance:** `.all()` replaced with `.yield_per(500)`; result object still populated correctly
**Status:** ✅

### T14 — Move Plex token to request header [B15]
**Files:** `src/ingestion/organiser.py`, `src/discovery/plex_playlists.py`
**Acceptance:** `X-Plex-Token` in `headers`, not `params`, on all Plex HTTP calls
**Status:** ✅

### T15 — Fix cmd_daemon startup order [B16]
**File:** `main.py`
**Acceptance:** `cmd_daemon()` runs `startup_sequence()` in a background daemon thread, then calls `app.run()`; Flask starts before startup completes
**Status:** ✅

---

## BLOCK D — P3 (cleanup)

### T16 — Delete legacy_downloader.py [B18]
**Files:** `src/legacy_downloader.py`, `tests/test_organiser_legacy.py`
**Acceptance:** files removed; no remaining imports anywhere
**Status:** ✅

### T17 — Delete stale root __pycache__ and orphan dirs [B17]
**Acceptance:** `__pycache__/`, `discovery/`, `ingestion/`, `integrity/` at repo root deleted
**Status:** ✅

### T18 — Pin yt-dlp in requirements.txt [B20]
**File:** `requirements.txt`
**Acceptance:** `yt-dlp` pinned to a specific version with comment; spotdl left unpinned per existing rationale
**Status:** ✅

---

## Execution order
A (T01→T02→T03) → B (T04→T05→T06→T07) → C (T08→T09→T10→T11→T12→T13→T14→T15) → D (T16→T17→T18)
