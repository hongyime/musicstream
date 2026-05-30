# musicstream — Deep Scoping: P2-9 (Decomposition) & P2-3 (Multi-Worker)

**Author:** Principal Systems Architect review
**Date:** 2026-05-30
**Status:** SCOPING ONLY — not approved for execution. Both are Batch C, gated on measured throughput.
**Companion to:** REMEDIATION_PLAN.md

> Skeptical-architect framing up front: **neither of these is a bug.** They are investments. P2-9 buys maintainability; P2-3 buys throughput. Both carry real regression risk against a system that currently *works* (25 k tracks delivered, 6 tiers proven in the DB). The default answer to "should we do these now" is **no, not until the data forces it.** This document scopes them so the decision is informed, and defines the trigger conditions that would justify each.

---

# PART A — P2-9: downloader.py / tagger.py Decomposition

## A.1 Why this is even on the list

Current reality (measured from live code):

- `src/ingestion/downloader.py` — **1,678 LOC**, one class `DownloadOrchestrator` with **26 methods** spanning: orchestration (`download_pending`, the 3 sweeps), per-track state machine (`_download_track_inner`), 6 tier implementations (`_tier0`–`_tier5`), YouTube scoring, cookie management, MP3 opts, output-file resolution, attempt recording, give-up logic, and 4 static exception classifiers.
- `src/ingestion/tagger.py` — **1,111 LOC**, class `MetadataTagger` with **25+ methods**: tag read/write for 5 formats (MP3/FLAC/M4A/OGG/Opus), MusicBrainz lookup (4 variants), AcoustID fingerprint, cover-art fetch, SSRF-safe HTTP, Spotify backfill, album-artist resolution, DB update.

The problem is not size per se — it's **mixed responsibility and shared mutable surface**. A change to tier-4 scoring sits 600 lines from the orchestration loop that calls it, and both touch `self._rate_limiter`, the module-level `_librespot_session`, and the same `Session`. That entanglement is what makes refactor risk *and* the cost of NOT refactoring both high.

## A.2 Concrete coupling hotspots (the actual risk)

From the structure scan:

1. **Module-level mutable globals** (`downloader.py:56–79`):
   - `_librespot_session` (rebuilt on timeout, mutated under `_librespot_session_lock`)
   - `_SPOTIFLAC_SEMAPHORE = Semaphore(2)`, `_LIBRESPOT_SEMAPHORE = Semaphore(1)`
   - These are reached via `global` in 3 places (lines 84, 434, 921). Any decomposition that moves tier methods into separate modules MUST decide who owns these. They are concurrency-control state, not tier logic.

2. **`download_track` does the atomic claim AND the tier loop AND attempt recording AND give-up.** Four responsibilities in one method (`_download_track_inner`, lines 541–678).

3. **Tagger's `tag_file` orchestrates**: embedded-read → fingerprint → MB lookup → cover fetch → write → DB update (`tag_file`, line 410). Six external-IO steps in one method, each independently failure-prone.

## A.3 Proposed target structure

```
src/ingestion/
  downloader/
    __init__.py              # re-exports DownloadOrchestrator (API-compatible)
    orchestrator.py          # download_pending + 3 sweeps + batch/ThreadPool logic
    track_runner.py          # _download_track_inner: claim → tier loop → record → give-up
    concurrency.py           # _librespot_session singleton, semaphores, lock (the globals)
    scoring.py               # _score_youtube_candidate + future per-tier scorers
    tiers/
      __init__.py
      tier0_librespot.py
      tier1_spotiflac.py
      tier2_ytmusic.py
      tier3_spotdl.py
      tier4_youtube.py
      tier5_soundcloud.py
      base.py                # Tier protocol: (track) -> Optional[path]; shared helpers
    cookies.py               # _get_or_refresh_cookie_copy, cleanup_temp_cookies
    output.py                # _find_output_file, _build_mp3_opts, _resolve_method_label
    classifiers.py           # the 4 static exception classifiers

  tagger/
    __init__.py              # re-exports MetadataTagger
    tagger.py                # tag_file orchestration only
    formats/
      mp3.py flac.py m4a.py ogg.py opus.py   # one _tag_* each, dispatched by extension
    musicbrainz.py           # 4 MB lookup variants + _parse_recording
    fingerprint.py           # AcoustID
    coverart.py              # _fetch_cover_art + SSRF-safe HTTP (_ssrf_safe*, _ip_is_safe)
    resolve.py               # album-artist + field resolution rules
```

**Key design constraint:** `DownloadOrchestrator` and `MetadataTagger` public APIs stay byte-identical. tasks.py, daemon.py, and tests import the same names. This is a pure internal reorg — zero behaviour change is the success criterion.

## A.4 Tier protocol (the one genuine design decision)

Define a `Tier` protocol so tier methods become injectable, testable units:

```python
class Tier(Protocol):
    name: str
    def attempt(self, track: Track, ctx: TierContext) -> Optional[str]: ...
```

`TierContext` carries the shared dependencies the tiers currently reach through `self`: rate limiter, concurrency primitives, cookie manager, output helpers. This is what kills the god-object: tiers stop being methods on a 1,678-line class and become small classes that receive what they need. It also makes P2-2 (scorer unit tests) and per-tier testing trivial.

## A.5 Effort, sequencing, risk

- **Prerequisite (HARD GATE):** P2-10 (coverage measured + floor in CI) MUST land first. Refactoring this without a coverage net is reckless — the whole value of the existing audit-hardened code is that it works, and you cannot prove a pure-reorg preserved behaviour without tests.
- **Effort:** 1.5–2 days, done as ~8 mechanical PRs (one module group each), each green-CI before the next. This is a textbook **ralph-loop** candidate — long list of mechanical, independently-verifiable extractions.
- **Order:** extract leaf utilities first (classifiers, output, cookies — no dependencies), then formats (tagger), then tiers (need TierContext), then orchestrator last (the thing everything else plugs into).
- **Regression risk:** MEDIUM. Mitigated entirely by: API-compatibility constraint + coverage gate + one-module-per-PR + behaviour-diff (run a download cycle on a scratch DB before/after, compare `download_method` distribution).
- **Rollback:** each PR is independently revertable; `__init__.py` re-exports mean callers never change.

## A.6 Trigger condition (when to actually do P2-9)

Do it when **either**:
1. You need to add a 7th tier or materially change scoring, and the change requires reading >300 lines of unrelated code to do safely; **or**
2. A bug in one tier requires touching the shared class and you fear collateral damage to the others.

Until one of those is true, the 1,678-line file is ugly but not costing you. **Do not refactor on aesthetics.**

---

# PART B — P2-3: Lift the `--workers 1` Constraint (Horizontal Download Workers)

## B.1 The constraint, precisely

`Dockerfile.daemon:7–10` documents it; the code enforces it implicitly. `--workers 1` is **load-bearing** because four things are process-local singletons:

1. **APScheduler `BackgroundScheduler`** (daemon.py:183) — multi-worker would register all 8 cron jobs N times → N concurrent download pipelines stomping each other.
2. **WebSocket `manager`** (daemon.py, `_broadcast_health`) — health broadcast state is per-process; N workers = N partial views.
3. **Circuit-breaker `_CircuitState`** (rate_limiter.py:56) — **in-memory per process.** Worker A trips librespot's breaker; workers B/C/D keep hammering the already-rate-limited Spotify account. This is the dangerous one — it actively defeats the rate-limiting that protects `bryanseah234` from a 1-2hr account lockout.
4. **`_librespot_session` + semaphores** (downloader.py:56–79) — module-global. `_LIBRESPOT_SEMAPHORE = Semaphore(1)` enforces single-flight librespot **within a process**. Across processes it enforces nothing → N simultaneous librespot streams → instant account rate-limit.

**What is already cross-process safe** (good news): throttle state IS persisted to `/app/data/throttle_state.json` (rate_limiter.py:290, `ServiceThrottle._persist`/`_restore`), and the DB atomic-claim (`UPDATE ... WHERE status=pending`, rowcount guard) already prevents two workers double-claiming a track. So the *claim* layer is multi-worker-ready. The *rate-limiting and session* layers are not.

## B.2 Why naive `--workers N` is actively harmful

If you just bumped `--workers` today:
- 8 cron jobs × N → N download pipelines, N integrity checks, N backups racing.
- librespot single-flight semaphore becomes meaningless → Spotify account locked within minutes (your MEMORY notes the 1-2hr dead-account penalty; upstream-confirmed at <5s inter-stream).
- Circuit breakers fragment → no coordinated backoff → 429 storms.

**This is not "slower than ideal," it's "breaks the Spotify account and gets you rate-limited."** Hard no on the naive path.

## B.3 The real architecture to make it safe

Three things must move out of process into shared state:

### B.3.1 Scheduler — singleton election
Only ONE process may own the scheduler. Options, cheapest first:
- **(A) Dedicated scheduler process** (recommended): split the daemon into `web` (N uvicorn workers, API only) + `scheduler` (1 replica, runs APScheduler + triggers pipelines). Compose gains a second service from the same image with a different command. Clean, no election logic.
- (B) Advisory-lock election: every worker tries `pg_advisory_lock`; the winner runs the scheduler. More fragile, avoids a second container.

→ **Recommend (A).** It also cleanly separates "serve the dashboard" from "do the work," which is good regardless.

### B.3.2 Circuit breaker — shared state
Move `_CircuitState` from in-memory to a shared store so all workers see one breaker per service:
- **(A) Postgres table** `circuit_state(service PK, state, failure_count, opened_at, updated_at)` with row-level locking. No new infra — you already have Postgres. Slight latency per check (mitigate with a short in-process TTL cache, e.g. 2s).
- (B) Redis. Faster, but adds a dependency the stack doesn't currently have.

→ **Recommend (A)** — no new moving parts. The throttle-state JSON file pattern proves you're comfortable persisting limiter state; this just moves it somewhere multiple processes can safely share (a file is NOT safe for concurrent multi-process writes; the DB is).

### B.3.3 librespot single-flight — cross-process lock
`Semaphore(1)` must become a **Postgres advisory lock** (or dedicate librespot to the single scheduler process — see B.4). librespot is fundamentally single-account, single-flight; it does not parallelize. Trying to run it in N workers is wrong by nature.

## B.4 The pragmatic shape (recommended if P2-3 is ever justified)

Don't make all tiers multi-worker. **Split by parallelizability:**

- **Scheduler process (1 replica):** owns APScheduler, runs librespot pre-sweep (single-flight by nature) and spotdl sweep. These are inherently serial — keep them where they already work.
- **Worker processes (N replicas):** run ONLY the parallelizable batch tiers (SpotiFLAC, YT Music, YouTube, SoundCloud) against the shared claim queue. These hit different backends, tolerate concurrency, and are where throughput actually scales.
- **Shared state:** circuit breaker → Postgres table; claim → existing atomic UPDATE (already safe).

This gives you horizontal scaling on the tiers that benefit, while leaving the account-sensitive single-flight tiers untouched in the one process that already handles them correctly.

## B.5 Effort, risk

- **Effort:** 3–5 days. Scheduler split (~1d), circuit-breaker-to-DB (~1.5d incl. the TTL cache + migration), librespot advisory lock or process-pinning (~0.5d), compose/topology + testing under real concurrency (~1d+).
- **Regression risk:** HIGH. You're changing the concurrency model of a system whose entire reliability story is built on in-process singletons. Every audit finding about races (#11, #12, #15) was solved *assuming one process*. Multi-process reopens all of them for re-validation.
- **Prerequisite:** P0-1/P0-2 (crash recovery) and P2-10 (coverage) both landed. Do not add concurrency to a system that still strands rows on restart.

## B.6 Trigger condition (when to actually do P2-3)

This is the critical gate. **Measure first (P2-7 burn-rate), then decide.** Compute:

```
backlog_remaining / current_tracks_per_hour = ETA
```

- If ETA after Batch A/B fixes is **acceptable** (queue converges in weeks, not years) → **DO NOT do P2-3.** The single-process daemon is fine; you'd be adding HIGH-risk complexity for throughput you don't need.
- If ETA is **unacceptable AND** profiling shows the bottleneck is worker concurrency (not the librespot 5s inter-track sleep, not network, not a single slow tier) → P2-3 is justified.

**Likely finding (my prediction):** the binding constraint is NOT worker count — it's the **serial librespot pre-sweep with mandatory 5s inter-track sleep** running first on every cycle. 61 k pending × tier0-first × 5s floor dominates wall-clock. If so, the fix is **not** multi-worker (B) at all — it's making tier0 best-effort/time-boxed and letting the 12-worker batch carry the volume, OR running librespot concurrently with the batch instead of as a blocking pre-sweep. That's a 1-hour change, not a 5-day rearchitecture.

→ **Strong recommendation:** instrument burn-rate (P2-7), confirm where the wall-clock actually goes, and try the tier0-ordering fix BEFORE committing to P2-3. P2-3 is the right answer only if measurement proves worker concurrency is genuinely the wall.

---

# Summary decision table

| Item | Do it now? | Hard prerequisite | Trigger to proceed | Risk |
|---|---|---|---|---|
| P2-9 decomposition | No | P2-10 coverage gate | Adding/changing a tier requires reading unrelated code, or cross-tier collateral-damage fear | MEDIUM |
| P2-3 multi-worker | No | P0-1, P0-2, P2-10 + P2-7 burn-rate measured | ETA unacceptable AND profiling proves worker count is the bottleneck (not tier0 ordering) | HIGH |

Both remain Batch C. The honest architect's position: **ship Batch A/B, measure burn-rate, and there's a good chance P2-3 dissolves into a 1-hour tier-ordering tweak instead.** P2-9 waits until the code's shape actually obstructs a real change.
