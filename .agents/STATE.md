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

## 2026-09-10 - Portfolio persistence review

Portfolio upkeep reviewed storage/session settings, provider backoff, backup rotation, frontend polling and fixture boundaries. All 77 tracked Python files parsed. PostgreSQL/media/OAuth/backup storage is separate from the Prawn Home widget and is not automatically Vercel/Supabase usage. Five/ten-second UI refreshes and the ten-plus-twenty database pool are review candidates, not measured cloud savings. No daemon, downloads, token refresh, scrobbling, notifications, media or private database records were accessed; prior live-state/test claims were not reverified.

## 2026-09-18 — daemon stall fix + memory hardening (A/B/C/D applied)

- Symptom before repair: daemon Up 16h, RSS 502/512 MiB (98%), FastAPI socket hung. Docker healthcheck still reported healthy because interval=60s × retries=3 was too loose to detect the hung endpoint. APScheduler jobs (Spotify sync, requeue-stale, token probe) kept firing in-process so logs looked alive. `curl` inside the container to `127.0.0.1:9079/health` timed out; `curl` from host got HTTP 000.
- Immediate repair: `docker restart musicstream-daemon` → RSS dropped 502 → 126 MiB (75%), endpoint returned HTTP 200 in 0.5s. Confirms it was memory bloat over long uptime, not a permanent break.
- A. Nightly `docker restart musicstream-daemon` registered as Windows scheduled task `MusicstreamDaemonNightlyRestart` (schtasks user-scope task, daily 04:00 SGT, RunLevel LIMITED). Backstop against the 16h bloat pattern.
- B. `docker-compose.yml` daemon healthcheck tightened: `interval 60→20s`, `retries 3→2`, `timeout 10→5s`, `start_period 30→60s`, added `-m 5` to curl so a hung socket fails the probe fast. Unhealthy detection now ~40s instead of >3 min. Host self-heal (per 2026-09-02 STATE) can react much sooner.
- C. `.env` `MAX_CONCURRENT_WORKERS 8→4`. Halves peak yt-dlp/ffmpeg subprocess RAM. STATE 2026-08-25 pushed it 2→8 to drain backlog but throughput dropped to 17 successes/24h anyway, so the aggressive setting wasn't paying off. Gotcha: my PowerShell shell had a stale `$env:MAX_CONCURRENT_WORKERS=8` that shadowed `.env` via compose variable substitution on first recreate — had to `Remove-Item Env:MAX_CONCURRENT_WORKERS` before the second `docker compose up`. Nightly scheduled task runs in a fresh env so this trap doesn't apply there.
- D. tracemalloc instrumentation, **opt-in** via `TRACEMALLOC_ENABLED=1` (default off after we learned the cost). When enabled, `lifespan` calls `tracemalloc.start(TRACEMALLOC_FRAMES default 3)` post-import so the FastAPI/SQLAlchemy/spotipy import graph isn't traced. New APScheduler job `tracemalloc_dump` runs hourly + on shutdown, appending `{ts, rss_mib, top[15]{size_mib, count, loc}}` to `/app/logs/tracemalloc.jsonl`. First attempt (frames=10 at module-import) hung the daemon for 6+ min on Python 3.14 before I reverted.
- Verification: daemon Up 7 min (healthy), shallow /health 200/0.9s, /health/deep 200/28s (slow because pending=256878 requires a large count query — separate issue), scheduler_running=true, downloading=1, spotify_token 0.8h left, token_degraded=false. Downloader log: `Worker concurrency set to: MAX_CONCURRENT=4`. `tracemalloc.is_tracing()` returns False (opt-in default).
- Files changed: `docker-compose.yml` (healthcheck block), `.env` (MAX_CONCURRENT_WORKERS), `src/daemon.py` (lifespan tracemalloc init, `_tracemalloc_dump` helper, scheduler job registration, shutdown dump). No tests were re-run this session; `python -c "import ast; ast.parse(...)"` confirmed daemon.py syntax; `docker compose config --quiet` confirmed compose validity.
- Not addressed (deliberately): the actual leak (D is instrumentation, not a fix), the 257k pending backlog vs 17 success/24h throughput collapse, `nebula-sync` unhealthy 8h and `unifiedanalyzer_scheduler` unhealthy 3h (out-of-scope neighbour containers on the same host). To hunt the leak next: `docker compose exec daemon sh -c 'TRACEMALLOC_ENABLED=1 exit'` won't work — need to set it in `.env` or `docker-compose.yml`, then `docker compose up -d --force-recreate daemon`, wait 2-3h, then read `logs/tracemalloc.jsonl` and look for `size_mib` growing at the same call site across snapshots.


## 2026-09-18 — second pass: throughput unblock + memory + leak-hunt setup

- DB analysis showed the real throughput ceiling: only ~60 download_attempts in 24h. 45 of 50 were `tier0_librespot rate_limited`. Phase 1 (librespot serial + 10s inter-track pace + 2h max budget) was consuming the daily pipeline cycle while rate-limited; Phase 2 (yt-dlp batch with MAX_CONCURRENT workers) never got real airtime. `download_attempts` on 17-Sep shows tier2_ytdlp_ytm worked fine at 87-100% success — the batch path is healthy when it runs.
- Throughput fixes (all in `.env` + one code change):
  - `LIBRESPOT_SWEEP_CONCURRENT=true` — Phase 1 in a separate thread so Phase 2 doesn't wait. The env flag already existed as an escape hatch; flipping it on.
  - `LIBRESPOT_SWEEP_MAX_SECONDS=1800` — cap librespot phase to 30 min per run (down from 7200s default). Required a small code change in `src/ingestion/downloader.py`: `download_pending_librespot(max_seconds=0.0)` now reads the env var when `max_seconds<=0`, keeping the function signature backward-compatible.
  - Scheduler `download_pipeline` cron changed `hour=3` → `hour="*/4"` — 4-hourly instead of daily. Six chances per day to drain the backlog instead of one.
- Memory: bumped `docker-compose.yml` daemon limit `512M → 1024M`. Under load with 4 yt-dlp workers + ffmpeg subprocesses + LIBRESPOT_SWEEP_CONCURRENT running Phase 1 in parallel with Phase 2 + startup Spotify sync + LB discovery, the 512 MiB cap was reached in 13 min from cold boot and starved the FastAPI /health endpoint. Post-bump: 213 MiB / 1 GiB (20%) after 5 min warm, healthy. The bump is a workaround; the actual leak is what tracemalloc is instrumented to find.
- Tracemalloc leak-hunt now armed: `TRACEMALLOC_ENABLED=1` in `.env`, baseline dump lands in `/app/logs/tracemalloc.jsonl` ~3s after boot (offloaded to a thread via `asyncio.to_thread` — the first attempt ran inline in the asyncio event loop and blocked the /health endpoint for seconds; that's what looked like a leak but was really loop starvation). Hourly dumps continue via the APScheduler job. To hunt: `jq '.top[0]' logs/tracemalloc.jsonl` across snapshots — a growing `size_mib` at the same `loc` string is the leak.
- Neighbour-container check: `nebula-sync` is actually healthy right now (FailingStreak=0). Recent errors are Tailscale reachability timeouts to two pihole replicas (`100.87.34.38`, `100.92.164.125`) — transient network, not a broken container. `pihole` itself is healthy but its log is warning about **load average 47-60 on an 8-vCPU WSL2 VM**. `unifiedcollector_postgres` alone consumes ~180% CPU sustained. Musicstream's CPU-starved boots (72s+ for DB connection, 90s+ for alembic migrations) are downstream of this host contention — not fixable in musicstream config.
- pytest sweep run in an ephemeral container (`docker compose run --rm --no-deps daemon pytest tests/ -q`): **329 passed, 3 failed, 4 errors, 6 skipped, 71 warnings, 341s total**. All 3 failures + 4 errors are `PytestUnknownMarkWarning: Unknown pytest.mark.asyncio` — pytest-asyncio plugin missing from the image (STATE 2026-09-02 shows 341 passing → this is a regression in the image, not caused by my changes). None of the failing tests touch code I modified.
- Files changed this pass: `.env`, `.env.example`, `docker-compose.yml`, `src/daemon.py`, `src/ingestion/downloader.py`, `.agents/STATE.md`, `.agents/JOURNAL.md`. Committed to `main` with `git add` per-file (no `git add .` — `docker_ports.txt` is legitimately untracked local state; STATE 2026-09-02 flagged preservation).
- Not yet done: (1) actual leak location — needs 2-3h of production traffic through tracemalloc-enabled daemon then a `jq` diff across `logs/tracemalloc.jsonl` entries; (2) pytest-asyncio reinstate — needs a Dockerfile.daemon change to pin the plugin; (3) the WSL2 host load average is another day's problem.
