# bugfix.md — Bug Registry
**Generated:** 2026-05-02 | **Source:** AUDIT.md

Legend: ✅ Fixed | ✅ Fixed | 🟡 Partial

---

## P0 — System-breaking

| ID | Bug | Status | Root Cause | Impact |
|---|---|---|---|---|
| B01 | Tagger + Organiser never called after download | ✅ Fixed | `download_track()` sets `status='downloaded'` and returns True without calling `MetadataTagger.tag_file()` or `FileOrganiser.organise()` | `file_path` always NULL; files rot in `temp/`; Plex never notified; integrity checker finds nothing |
| B02 | SpotiFLAC FLAC→MP3 transcode destroys quality | ✅ Fixed | `_tier1_spotiflac()` ran ffmpeg transcode before returning; removed, now returns original file | Was: lossy-to-lossy transcode, discarded original format |
| B03 | SpotiFLAC import casing fails on Linux | ✅ Fixed | `from SpotiFLAC import` fails on case-sensitive Linux filesystem; pip installs as `spotiflac` | Tier 1 silently unavailable in all Docker deployments |

## P1 — Data loss / crash path

| ID | Bug | Status | Root Cause | Impact |
|---|---|---|---|---|
| B04 | `os.chdir()` in Tier 3 not thread-safe | ✅ Fixed | `_tier3_spotdl()` calls `os.chdir()` which affects entire process; 4 concurrent workers race | Corrupts relative paths for all concurrent threads mid-download |
| B05 | `pg_dump` exposes DB password in process args | ✅ Fixed | `subprocess.run(["pg_dump", database_url, ...])` — DATABASE_URL contains password | Password visible in `ps aux`, shell history, container logs |
| B06 | AcoustID fingerprinting never triggered | ✅ Fixed | `_fingerprint()` exists but never called in `tag_file()`; `_fetch_musicbrainz()` only uses pre-existing `acoustid_id` | MusicBrainz AcoustID lookup path completely unreachable |
| B07 | Circuit breaker cascade kills download batch | ✅ Fixed | threshold=5 with 4 workers trips in seconds; 30-min cooldown blocks rest of batch; duration mismatch wrongly counted as service failure | All tracks in batch skip tiers 2/4/5 after first few failures |
| B08 | `/sync` queues jobs before scheduler started | ✅ Fixed | `scheduler.add_job()` called while scheduler not yet running (step 9 of startup_sequence); job queued but not executed | Manual sync via HTTP endpoint silently does nothing if called during startup window |

## P2 — Reliability / quality

| ID | Bug | Status | Root Cause | Impact |
|---|---|---|---|---|
| B09 | `track_sources` missing ON DELETE CASCADE in migration | ✅ Fixed | Migration 0001 FK lacks `ON DELETE CASCADE`; ORM has it but DB doesn't | startup.bat [8] runs raw SQL `DELETE FROM tracks`; orphaned `track_sources` rows accumulate |
| B10 | No SIGTERM handler | ✅ Fixed | No `signal.signal(SIGTERM)` registered | Docker `stop` sends SIGTERM; in-progress downloads interrupted mid-file |
| B11 | Dual engine creation wastes connection pool | ✅ Fixed | `wait_for_db()` creates engine 1 (returned, ignored); `init_db()` creates engine 2. Two pools = 40 idle connections | Resource waste; potential connection exhaustion |
| B12 | Startup sequence re-runs DB init and migrations | ✅ Fixed | `__main__` runs wait_for_db+init_db+migrations; `startup_sequence()` repeats steps 1-2 in background thread | Double migration run on every boot; wastes ~5s |
| B13 | No auth on mutating Flask endpoints | ✅ Fixed | POST /sync, /integrity, /discover, /backup have no authentication | Anyone on container network can trigger pipeline or read operational data |
| B14 | IntegrityChecker loads all tracks into memory | ✅ Fixed | `.all()` on full downloaded-track set | Memory spike on large libraries (10k+ tracks = hundreds MB) |
| B15 | Plex token in URL query param | ✅ Fixed | `params={"X-Plex-Token": token}` in `_refresh_plex()` | Token visible in HTTP server logs and proxy access logs |
| B16 | `cmd_daemon` in main.py blocks Flask | ✅ Fixed | Runs `startup_sequence()` synchronously before `app.run()` | Health endpoint unreachable during entire startup when using CLI path |

## P3 — Cleanup / maintenance

| ID | Bug | Status | Root Cause | Impact |
|---|---|---|---|---|
| B17 | Stale root-level `__pycache__` and orphaned dirs | ✅ Fixed | Pre-`src/` migration bytecode left behind | Confusing repo layout |
| B18 | `legacy_downloader.py` is dead code | ✅ Fixed | Old `AudioExtractor` class; no imports anywhere in codebase | ~300 lines of dead code in production package |
| B19 | Duplicate `_compute_sha256` in checker and organiser | ✅ Fixed | Copy-paste duplication | Maintenance risk; fix one, miss the other |
| B20 | `yt-dlp` and `spotdl` unpinned in requirements.txt | ✅ Fixed | Intentional comment in requirements.txt; spotdl constrains yt-dlp | Silent breakage on new installs |
| B21 | OGG Vorbis not handled by tagger | ✅ Fixed | `_write_tags()` handles mp3/flac/m4a only; SpotiFLAC now returns OGG | Tags not written to Tier 1 OGG downloads |
