2026-08-24: Drafted SPEC.md §W3 (discovery parity, quality cutoff mp3_320 default, portability). Decision: -only stack, Plex optional, m3u as portable playlist artifact.
- 2026-08-24 22:13:57 +08:00 [PRAWN-L390/claude/stop] branch=main head=91a0d60 dirty=3
2026-08-25: Implemented SPEC W3a end-to-end. Key decision: m3u exports carry HOST paths (translated from /media) since players run on Windows; token refresher sends client_secret when configured because cached token is confidential-client origin.
- 2026-08-25 00:48:41 +08:00 [PRAWN-L390/claude/stop] branch=main head=91a0d60 dirty=20
- 2026-08-25 07:32:36 +08:00 [PRAWN-L390/claude/stop] branch=main head=91a0d60 dirty=20
- 2026-08-25 09:19:10 +08:00 [PRAWN-L390/claude/stop] branch=main head=4ab24f3 dirty=1
- 2026-08-25 11:11:15 +08:00 [PRAWN-L390/claude/stop] branch=main head=453d4f3 dirty=1
- 2026-08-25 12:05:19 +08:00 [PRAWN-L390/claude/stop] branch=main head=b6d031c dirty=1
2026-09-02: Live stall diagnosis found shallow Docker health was insufficient: daemon was up but APScheduler was not running and startup daemon_run 2509 was incomplete since 2026-08-29. Decision: self-heal must treat /health/deep degraded scheduler/run freshness as daemon repair signal.
2026-09-02: Implemented and live-verified scheduler-first daemon startup plus deep-health watchdog repair. Decision: tests that spawn uvicorn on port 9089 set SKIP_BACKGROUND_STARTUP=true so local test runs do not create live startup/download maintenance rows.
2026-09-02: Added Spotify token refresh at startup/auth status and stale-token refresh before Spotify sync/artist expansion tasks. Decision: scheduled/manual Spotify entry points must repair refreshable cache before constructing Spotipy clients instead of relying only on hourly early-warning probes.
2026-09-02: Added non-blocking Spotify task lock and moved startup Spotify maintenance parallel to download drain. Decision: download progress must not wait behind long Spotify sync/backfill work, and overlapping Spotify triggers should skip instead of piling onto the daemon.
