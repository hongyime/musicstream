# STATE

## 2026-09-02 - robustness sweep implemented and verified
- Implemented async manual Spotify jobs with `/api/musicstream/jobs/{job_id}` status, stale DOWNLOADING requeue, DB-backed download liveness in `/health/deep`, periodic JSONL/latest health snapshots, one-shot Spotify refresh-token failure alerts, and guarded Plex port fallback.
- Live config: local `.env` has `PLEX_HOST_PORT_AUTO_FALLBACK=true`; Plex is currently published on host port 32402. The sample env defaults this flag to false for conservative installs.
- Verification: PowerShell self-heal syntax parse passed; `docker compose config --quiet` passed; full pytest passed 341 tests, 1 skipped, with 3 existing return-not-None warnings in `tests/test_artwork_report.py`.
- Live proof: daemon/plex/postgres/scrobbler are healthy; `/health/deep` is OK with scheduler running, stale_downloading=0, progress_fresh=true, Spotify token_degraded=false. DB snapshot at 2026-09-02 21:53 SGT: 132642 downloaded, 64076 pending, 3 successful downloads in the last hour.
- Operational note: Docker Desktop briefly left the recreated daemon under a temporary name after `removal ... already in progress`; it was renamed back to `musicstream-daemon`. Existing untracked `docker_ports.txt` was preserved.

## 2026-08-24 — Wave 3 spec drafted
- SPEC.md §W3 added: Discovery Parity (LB weekly playlists -> MBID resolve -> auto-download), QUALITY_CUTOFF (default mp3_320, FLAC opt-in), blocklist, m3u export, webhook alerts + token early-warning, library browse/search tab.
- Constraints: $0 spend; Plex demoted to optional push; download-chain focus.
- Next: build W3a tasks T12-T18.

<!-- MOLT_AUTO_START -->
## Auto State

- Updated: 2026-08-25 12:05:19 +08:00
- Machine: PRAWN-L390
- Harness: claude
- Event: stop
- Branch: main
- HEAD: b6d031c
- Dirty files: 1
- Resume hint: Read .agents/STATE.md, then the latest file in .agents/handoffs/ if present.
<!-- MOLT_AUTO_END -->

---
## 2026-08-25 - W3a IMPLEMENTED + VERIFIED (T12-T18)
- SPEC.md W3-T rows marked x. 313 tests pass incl. 27 new (blocklist/m3u/notify-token).
- Live QA: 2224 playlists exported to Y:/playlists w/ container-to-host path translation; block/unblock+reset-failed V7 verified via TestClient; token refresher FIXED live (secret-aware refresh; cached token was expired -1h, now fresh).
- Gotchas fixed along the way: env.py cp1252 decode, main.py cmd-after-main ordering, Path('/media') win32 backslash normalization, PKCE-vs-secret refresh semantics.
- NEXT: W3b = T19 cutoff+transcode -> T20 upgrade pass; T21-T23 discover-weekly; T24-T26 library tab + FE badges.

## 2026-08-25 - daemon redeployed on W3a code
- docker compose up -d --force-recreate daemon (dev override bind-mounts src -> no rebuild needed).
- Token refresher hardened: in-place cache write (os.replace breaks single-file bind mounts); secret-aware refresh verified LIVE both host+container. auth/status degraded=False.

## 2026-08-25 - W3b IMPLEMENTED (T19-T26) + pushed
- Transcode-on-import live (cutoff mp3_320, KEEP_FLAC_MASTER honored). Upgrade-pass = single bulk UPDATE w/ 500/run cap after mass-requeue incident (restored 114k rows).
- Discover-weekly engine done+tested; LIVE run empty because LB troi-bot feed needs opt-in -> user must follow listenbrainz.org/user/troi-bot.
- FE: Library tab + block buttons + token banner built into image? NO - docker build timed out on npm; dist docker-cp'd into running container. REBUILD DEBT: next docker compose build daemon will bake it properly.
- All containers Up. 326 tests green.

## 2026-08-25 - download push + LB diagnosis
- Root cause of slow downloads: MAX_CONCURRENT_WORKERS was 2 -> 8; yt-dlp was stale (2026.06.09) killing tier2/tier4 at 100% fail -> upgraded to 2026.08.19 in container + Dockerfile now installs latest at build.
- Full backlog drain launched detached (main.py download). pending 72.7k draining.
- LB weekly playlists empty: troi-bot delivers via createdfor feed (now scanned); Spotify plays never reach LB natively - user should connect listenbrainz.org/settings/import/spotify.

## 2026-09-02 - live stall repaired and verified
- Other-agent catchup: latest main is b6d031c (W3 + daily yt-dlp self-heal); .agents files were already dirty and docker_ports.txt was untracked.
- Live status before repair: containers shallow-healthy, but /health/deep degraded because APScheduler is not running; daemon_run 2509 started 2026-08-29 16:45 UTC and is still incomplete.
- Download status before repair: 132626/196725 downloaded (67.42%), 64079 pending, no successful attempts in the last 24h; last attempt was 2026-08-29 17:16 UTC.
- ListenBrainz status before repair: lb_recommendations has 459 rows, 455 ingested, latest fetch 2026-08-25 11:54 UTC; Spotify token is expired, so Spotify-backed expansion/sync needs reauth.
- Fixed local Codex hooks parse issue by removing UTF-8 BOM from the local hooks config; JSON parse verified after edit.
- Code repair: daemon now records a fresh startup run and starts APScheduler before long startup maintenance/downloads; self-heal now reads /health/deep and restarts daemon on scheduler-not-running or stale last-run.
- Runtime repair: self-heal restarted musicstream-daemon once; /health/deep now OK with scheduler_running=true. Watchdog loop restarted as pwsh process 19992 so it has the patched script loaded.
- Download proof after repair: 132632/196725 downloaded, 64073 pending, 0 active at last check; 11 attempts and 6 successes in the last hour. Plex host port remains 32402.
- LB proof after repair: startup ListenBrainz CF poll fetched 100 recs and added 0 new tracks; count-only API comparison showed 100/100 MBIDs already known. Weekly playlist scan found 0 playlists.
- Test proof: full pytest passed 326 tests, 1 skipped, 3 existing warnings. Integration endpoint fixture now sets SKIP_BACKGROUND_STARTUP=true to avoid writing live daemon_runs during tests; test-created row 2511 was marked completed with note local pytest startup probe.

## 2026-09-02 - Spotify token self-heal implemented
- Code hardening: startup refreshes an expired Spotify cache before sync; Spotify sync/saved-albums/followed-artists/liked-artists/LB artist expansion refresh stale cache before creating Spotify clients; auth/status refreshes expired cache and rebuilds a stale in-memory auth manager.
- Concurrency hardening: Spotify task entry points share a non-blocking lock, so startup/scheduler/manual sync cannot stack multiple long Spotify runs. Startup now runs Spotify sync/backfill in parallel and starts the download drain without waiting for Spotify sync to finish.
- Verification: py_compile passed for daemon/tasks/token tests; self-heal PowerShell syntax parses; targeted pytest passed 18 tests; full pytest passed 330 tests, 1 skipped, 3 existing warnings.
- Live proof after final daemon recreate: /health/deep OK with scheduler_running=true; auth/status authenticated with token_degraded=false and about 0.99h left; downloads moved to 132635 downloaded, 1 downloading, 64082 pending; ListenBrainz poll still returned 0 new tracks from 100 recommendations.
