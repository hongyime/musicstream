# musicstream SPEC

## §S Status

P1 ✓ complete. P2 started. W3a (T12–T18) ✓ complete 2026-08-25 — see §W3. W3b pending.

## §D Done (P1)

- `POST /admin/validate-invalid-tracks` ✓
- `POST /admin/cleanup-invalid-tracks` ✓
- `GET /api/artwork-report` ✓
- `src/ingestion/artwork_checker.py` — embedded art check helper ✓
- `tests/conftest.py` — centralized fixtures ✓
- baseline test fixes: `MAX_CONCURRENT`, fixture isolation, SpotiFLAC cond. tests ✓
- `POST /api/refresh-artwork` ✓ (P2 started)

## §I Interfaces

```
api: POST /admin/validate-invalid-tracks → {checked, updated, marked, errors}
api: POST /admin/cleanup-invalid-tracks → {deleted}   ! dry_run?=1
api: GET  /api/artwork-report           → {tracks_with_cover_art_url, tracks_without_cover_art_url, tracks_without_embedded_art, missing_by_album[], missing_by_artist[]}
api: POST /api/refresh-artwork          → mode=missing|all, limit=n, dry_run?=1
```

## §V Invariants

```
V1: validate-invalid → cleanup; never cleanup without prior validate pass
V2: cleanup predicate ! only rows with explicit invalid marker
V3: spotdl HTTP mode behind flag; CLI fallback always available
V4: ∀ artwork refresh → dry_run first option; no silent overwrites
V5: ∀ batch spotdl → track↔file mapping by subdir/manifest; ⊥ fuzzy filename match
V6: admin endpoints ? token gate if token configured
```

## §T Tasks

| id  | status | task                                    | cites     |
|-----|--------|-----------------------------------------|-----------|
| T1  | x      | invalid-data validate endpoint          | V1,V2     |
| T2  | x      | invalid-data cleanup endpoint           | V1,V2,V6  |
| T3  | x      | artwork-report endpoint                 | §I        |
| T4  | x      | embedded artwork check helper           | V4        |
| T5  | x      | baseline test fixes                     | §F        |
| T6  | x      | refresh-artwork endpoint                | V4        |
| T7  | .      | integration tests (daemon live)         |           |
| T8  | .      | dashboard artwork card                  | §I        |
| T9  | x      | spotdl batch PoC (behind flag)          | V3,V5     |
| T10 | x      | folder.jpg generator                    | V4        |
| T11 | x      | spotdl HTTP service PoC (behind flag)   | V3        |

## §P Plan

### P2 (next)
- T7: integration tests — start daemon, hit all 4 endpoints ✓
- T8: dashboard `/` → artwork card; link report; button → refresh (manual only; ⊥ auto) ✓
- T9: spotdl batch PoC (CLI array mode) ✓
- T10: folder.jpg generator ✓
- T11: spotdl HTTP service PoC (behind flag) ✓

### P3 → superseded by §W3 (see EOF)

## §F Test Failures → Fixes

| id  | file                        | cause                                      | fix                                                   |
|-----|-----------------------------|--------------------------------------------|-------------------------------------------------------|
| F1  | `tests/test_downloader.py`  | `MAX_CONCURRENT` hardcoded old value       | `int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))`  |
| F2  | fixture isolation           | session rollback missing; uri collisions   | centralize `tests/conftest.py`; rollback before close |
| F3  | SpotiFLAC tests             | stale vs `ENABLE_TIER1=false`              | `xfail`/`skip` when tier1 disabled                    |
| F4  | scraper mocks               | `spotipy.cache_handler` patch path wrong   | patch import path used by module under test            |

## §A Acceptance

```
A1: invalid rows
  checked_count = actual invalid count in DB
  Spotify exists → metadata patched
  Spotify ⊥ → explicit marker set; optional delete

A2: artwork
  GET /api/artwork-report → stable JSON schema
  POST /api/refresh-artwork updates subset; tags intact
  ⊥ crash on missing/readonly files

A3: tests
  targeted suite green
  `python main.py validate` green

A4: spotdl batch PoC
  ⊥ event-loop regression
  Tier3 throughput > current CLI baseline
  rollback via env flag
```

## §R Risks

| risk                                    | mitigation                              |
|-----------------------------------------|-----------------------------------------|
| Spotify/artwork API rate limits         | flags + dry-run first                   |
| heavy I/O during artwork refresh        | limit=n param; dry_run                  |
| false-positive delete in cleanup        | narrow SQL predicate; V1 gate           |
| batch track→file mapping errors         | subdir/manifest; V5                     |
| spotdl HTTP mode ops burden             | PoC only; CLI always fallback; V3       |

## §E Env

```
env: SPOTDL_SERVICE_URL   ? HTTP mode URL
env: SPOTDL_MODE          ? cli|http  default=cli
env: MAX_CONCURRENT_WORKERS ! int default=4
env: ENABLE_TIER1         ? bool
```

## §N Next

Start T7 (integration tests): start daemon → smoke all 4 new endpoints → then T8 dashboard.

---

# §W3 Wave 3 — Discovery Parity, Quality Cutoff & Portability

Scope locked with owner 2026-08-24:
- **$0 hard constraint** — no paid services or keys anywhere in the wave. Qobuz (paid) tier is moot ⇒ default quality cutoff is mp3_320.
- **Download-chain first; Plex demoted to optional push target.** Playback happens in whatever player the owner uses; playlists ship as portable .m3u files.
- **Player compatibility:** some players ⊥ FLAC + FLAC file size concern ⇒ QUALITY_CUTOFF default mp3_320; FLAC strictly opt-in via env.
- Dashboard remains the ops/download-status surface; a read-only Library tab is added to it (no playback server work).
- Failure alerting is REQUIRED: post-run webhook summaries + immediate failure alerts + Spotify-token early warning.

## §W3.I Interfaces

```
api: POST /api/musicstream/tracks/{id}/block     → {id, blocked: true}    ! token
api: POST /api/musicstream/tracks/{id}/unblock   → {id, blocked: false}   ! token
api: GET  /api/musicstream/library?q&artist&album&format&status&page&page_size
                                                 → {items[], total, page, page_size}
api: POST /api/musicstream/discover-weekly       → {playlists:[{name, entries, resolved_local, queued_missing}], m3u_paths[]}   ! token
env: QUALITY_CUTOFF       ? mp3_320|flac     default=mp3_320
env: KEEP_FLAC_MASTER     ? bool             default=false   # only meaningful when cutoff=mp3_320 and a FLAC was acquired
env: PLAYLISTS_EXPORT_DIR ? path             default=<EXTERNAL_MEDIA_DRIVE>\playlists
env: WEBHOOK_URL          ? URL              Discord-compatible JSON POST (Discord webhook URL or ntfy URL); empty=off
env: NOTIFY_ON            ? failures|all|none  default=failures
env: AUTO_BLOCK_THRESHOLD ! int              default=6   # consecutive full-chain failed passes before auto-block
env: TOKEN_WARN_HOURS     ? int              default=48
```

## §W3.V Invariants

```
V7  blocked tracks are inert: skipped by downloader, reset-failed, integrity auto-requeue,
    and discovery ingest; reanimated only by explicit unblock
V8  ∀ playlist publish → m3u written FIRST; Plex push best-effort (unset/unreachable Plex ⊥ fail run)
V9  discovery rows deterministic+idempotent: spotify_uri=`lb:recording:{mb_recording_id}`;
    never mutate Spotify-sourced rows; dedupe on (artist,title,duration±5s) before insert
V10 stored format respects cutoff: cutoff=mp3_320 ∧ FLAC acquired → transcode mp3@320 (ffmpeg),
    delete intermediate unless KEEP_FLAC_MASTER=1
V11 upgrade-pass candidates = status∈{failed,downloaded} ∧ format='mp3' ∧ method∉premium-tiers ∧ ¬blocked;
    ≤1 requeue per pass per track
V12 webhook delivery: 3 attempts, exponential backoff; final failure logged, never raises the run
V13 token age > TOKEN_WARN_HOURS ∧ refresh-fail → alert webhook + /auth/status degraded=true
```

## §W3.T Tasks

| id  | status | task                                                                   | cites      |
|-----|--------|------------------------------------------------------------------------|------------|
| T12 | x      | migration: tracks.blocked / blocked_reason / blocked_at                 | V7         |
| T13 | x      | downloader + reset-failed + integrity honor blocked; FE Blocked badge   | V7         |
| T14 | x      | auto-block at AUTO_BLOCK_THRESHOLD consecutive full-chain fails (attempt_count) | V7 |
| T15 | x      | m3u exporter + publish hook + BACKFILL: 2224 playlists → Y:/playlists w/ host-path translation (container /media ↔ host drive) | V8 |
| T16 | x      | Plex push optional: skip when PLEX_URL unset/unreachable                | V8         |
| T17 | x      | webhook notifier: run summaries + failure alerts (+auto-block & integrity hooks) | V12 |
| T18 | x      | token early-warning: hourly probe + silent refresh (secret-aware) + degraded auth/status | V13 |
| T19 | x      | transcode-on-import hooked post-organise (both finalize paths); real-ffmpeg tests; failure keeps FLAC  | V10 |
| T20 | x      | bulk-UPDATE upgrade pass + Sat 02:00 cron + POST /upgrade-pass; UPGRADE_PASS_LIMIT=500 trickle cap (mass-requeue incident 2026-08-25 → restored) | V11,V7 |
| T21 | x      | DiscoverWeekly fetcher: /user/{u}/playlists + /playlist/{mbid} JSPF; ⚠ LIVE RUN EMPTY — troi-bot feed requires opt-in: follow listenbrainz.org/user/troi-bot | V9 |
| T22 | x      | migration 0006 kind column; resolver MBID → fuzzy±5s → synthetic mb:{mbid} track (repo convention, not lb:recording:) via _ingest_recommendation(kind=…) | V9 |
| T23 | x      | Mon 06:00 cron + POST /discover-weekly + resolved m3u export; fixture-pinned offline tests | V8,V9 |
| T24 | x      | GET /library?q/artist/album/format/status/page — ILIKE search at personal scale (trgm idx deferred, not needed <150k rows) | §W3.I |
| T25 | x      | FE Library tab: debounced search + format/status filters + pager + Unblock on blocked rows | §W3.I,V7 |
| T26 | x      | FE Block button on failed rows + degraded-token banner from auth-status | V12,V7 |

## §W3.P Waves

```
W3a (Do Now):        T12→T13→T14 · T15→T16 · T17 · T18          (independent lanes)
W3b (Do Next):       T19→T20 · T21→T22→T23 · T24→T25→T26        (cutoff BEFORE discovery downloads)
```

## §W3.A Acceptance

```
A-block : seed track attempt_count≥threshold → auto-blocked; reset-failed leaves it; unblock → pending
A-m3u   : after publish → file exists at PLAYLISTS_EXPORT_DIR; starts #EXTM3U; entry count == resolved count; every path exists on disk
A-hook  : local HTTP receiver gets {run_type, tracks_scraped, tracks_downloaded, tracks_failed, ...} at run end;
          receiver forced 500 → exactly 3 attempts then logged, run still succeeds
A-token : mocked expiring token + failing refresh → alert POST fired + GET /auth/status degraded=true
A-dw    : fixture JSPF of 5 entries (2 mbid-hits, 1 fuzzy-hit, 2 missing) → response {resolved_local:3, queued_missing:2};
          queued rows have spotify_uri LIKE 'lb:recording:%'; re-running POST is idempotent (no dup rows)
A-cutoff: stub FLAC-producing source w/ cutoff=mp3_320 → final artifact .mp3 @320k, no .flac left (KEEP_FLAC_MASTER=0)
A-upg   : ytm-sourced mp3 track → upgrade pass requeues exactly once; premium-sourced + blocked tracks untouched
A-lib   : q='bohem' returns matching page; artist/format/status filters compose; out-of-range page → empty items, stable total
```

## §W3.R Risks

| risk                                             | mitigation                                              |
|--------------------------------------------------|---------------------------------------------------------|
| LB playlist API shape drift                      | fixture-pinned JSPF tests; 429 backoff via existing rate_limiter |
| ffmpeg transcode CPU spikes                      | per-pass batch cap; reuse worker pool                    |
| fuzzy resolver false positives                   | duration ±5s gate + min artist/title similarity; log every fuzzy resolution |
| synthetic-uri collides w/ later Spotify ingest   | V9 dedupe check before insert                            |
| Plex removal breaks scrobbling (multi-scrobbler polls Plex) | OUT OF SCOPE here — flagged; future Subsonic layer would replace feed |

## §W3.E Env

Update `.env.example` in the same PR as T15/T17/T19 (new vars listed in §W3.I). No new third-party API keys required — WEBHOOK_URL is a self-created Discord/ntfy URL.
