# design.md — Implementation Design
**Generated:** 2026-05-02

---

## 1. B01 — Wire Tagger + Organiser into download_track()

### Architecture change
`DownloadOrchestrator.__init__` gains lazy-init fields for `MetadataTagger` and `FileOrganiser`, constructed from env vars on first use.

```
download_track() flow (new):
  tier_fn(track) → path
    ↓
  tagger.tag_file(path, track, session)   [warn on fail, continue]
    ↓
  organiser.organise(path, track, session) [sets file_path, sha256, format, status='downloaded']
    ↓
  return True
```

### Env vars consumed
| Var | Used by | Default |
|---|---|---|
| `ACOUSTID_API_KEY` | MetadataTagger | `""` (fingerprinting skipped) |
| `EXTERNAL_MEDIA_DRIVE` | FileOrganiser | `"/media"` |
| `PLEX_URL` | FileOrganiser | `"http://localhost:32400"` |
| `PLEX_TOKEN` | FileOrganiser | `""` |
| `PLEX_LIBRARY_SECTION_ID` | FileOrganiser | `""` |

### Session contract
- Tagger receives the same `session` as download; flushes internally, caller commits
- Organiser receives the same `session`; sets `track.status = 'downloaded'` — so `download_track()` must NOT set it itself anymore
- If organiser raises `OrganiserError`: set `status='failed_validation'`, return False
- If tagger raises `TaggingError`: log WARNING, continue to organiser (partial tags acceptable)

### Status state machine (updated)
```
pending → downloading → [tagger] → [organiser] → downloaded
                                                → failed_validation  (organiser error)
         → pending  (all tiers fail, < threshold)
         → failed   (all tiers fail, ≥ threshold)
```

---

## 2. B04 — os.chdir() Thread Safety (Tier 3 spotdl)

Replace `os.chdir()` + `spotdl_client.download_songs()` with a subprocess call using `spotdl`'s `--output` flag. This keeps the download entirely out-of-process, eliminating the CWD race.

```python
subprocess.run(
    ["spotdl", "--output", abs_temp, spotify_uri],
    capture_output=True, timeout=120,
)
```

If `spotdl` CLI is not on PATH, fall back to the current Python API approach but wrapped in a `threading.Lock`.

---

## 3. B05 — pg_dump Password Exposure

Parse `DATABASE_URL` to extract host/port/user/dbname and pass `PGPASSWORD` via env, never via CLI argument.

```python
import urllib.parse
u = urllib.parse.urlparse(database_url)
env = {**os.environ, "PGPASSWORD": u.password or ""}
subprocess.run(
    ["pg_dump", "-h", u.hostname, "-p", str(u.port or 5432),
     "-U", u.username, "-d", u.path.lstrip("/"),
     "--no-password", "--file", str(backup_path)],
    env=env, ...
)
```

---

## 4. B06 — AcoustID Fingerprinting

In `tag_file()`, after reading embedded tags (step 1) but before MusicBrainz lookup (step 2), call `self._fingerprint(file_path, track, session)`. This populates `track.acoustid_id` and `track.mb_recording_id`, making them available for the subsequent `_fetch_musicbrainz()` call.

Only run if `self._acoustid_key` is set and `ACOUSTID_AVAILABLE`.

---

## 5. B08 — Scheduler/sync race

In `/sync`, `/integrity`, `/discover` routes: check `scheduler.running`. If False, dispatch in a raw `threading.Thread(daemon=True)` instead. If True, use `scheduler.add_job()` as now.

---

## 6. B09 — track_sources CASCADE migration

New migration `0002_track_sources_cascade.py`:
- Drop existing FK on `track_sources.track_id`
- Re-add with `ON DELETE CASCADE`
- Same for `source_id` for symmetry

---

## 7. B10 — SIGTERM handler

Register in `__main__` after Flask thread starts:
```python
import signal
def _handle_sigterm(signum, frame):
    scheduler.shutdown(wait=False)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, _handle_sigterm)
```

---

## 8. B11 — Dual engine

`init_db()` gains an optional `engine` param. `wait_for_db()` return value is passed in:
```python
engine = wait_for_db(...)
init_db(engine=engine)
```

---

## 9. B12 — Duplicate startup steps

Remove Steps 1–2 (wait_for_db, run_migrations) from `startup_sequence()`. They already ran in `__main__` before the background thread started.

---

## 10. B13 — Flask auth

Shared-secret middleware: if env var `DAEMON_API_TOKEN` is set, all POST routes and GET /metrics require `Authorization: Bearer <token>` or `X-Daemon-Token: <token>` header. If var not set, auth is skipped (backwards-compatible default-open).

---

## 11. B14 — IntegrityChecker pagination

Replace `.all()` with `.yield_per(500)`:
```python
session.query(Track).filter(...).yield_per(500)
```

---

## 12. B15 — Plex token header

Move `X-Plex-Token` from `params` to `headers` in `_refresh_plex()` and `PlexPlaylistSync`.

---

## 13. B16 — cmd_daemon fix

Mirror `__main__` pattern: start background thread for `startup_sequence()`, then call `app.run()`.

---

## 14. B19 — De-duplicate _compute_sha256

Move to `src/utils.py`. Import in both `checker.py` and `organiser.py`.

---

## 15. B21 — OGG Vorbis tagger

Add `_tag_ogg()` to `MetadataTagger` using `mutagen.oggvorbis.OggVorbis`. Add `.ogg` and `.opus` dispatch in `_write_tags()`.

```python
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
```

OGG Vorbis uses same Vorbis comment format as FLAC — nearly identical tag writing logic.

---

## Dependency compatibility

| Package | Version | Verified |
|---|---|---|
| mutagen | 1.47.0 | `OggVorbis`, `OggOpus` available since 1.40 ✓ |
| SQLAlchemy | 2.0.49 | `yield_per()` available ✓ |
| APScheduler | 3.11.0 | `scheduler.running` property available ✓ |
| spotdl | unpinned | CLI `--output` flag available in all recent versions ✓ |
