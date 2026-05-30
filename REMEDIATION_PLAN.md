# musicstream — Remediation Plan

**Author:** Principal Systems Architect review
**Date:** 2026-05-30
**Basis:** Live code + live DB + running containers (not aspirational docs)
**Scope:** Deterministic ETL music-acquisition daemon. **No LLM / inference / agent layer exists or is planned** — this plan deliberately omits any such work.

---

## How to read this

Each item has: **Severity**, **Root cause** (evidence-backed), **Fix**, **Files**, **Effort**, **Verification**, **Risk if skipped**.

Severity legend: **P0** = active bug with observed data impact · **P1** = correctness/ops gap likely to bite · **P2** = hardening / maintainability.

Sequencing is at the end. Nothing here requires a rebuild except where noted; most are code-only and ship via `docker compose up -d --force-recreate daemon` because `docker-compose.override.yml` bind-mounts `./src`.

---

## P0 — Active bugs with observed data impact

### P0-1 · Orphaned `DOWNLOADING` rows strand permanently

**Severity:** P0
**Evidence:** Live DB shows **83 rows in `status='downloading'`**, oldest `updated_at` from **2026-05-27** (3 days stale). These are stranded queue slots — no worker will ever revisit them.

**Root cause (confirmed):**
The stuck-reset logic exists only inside `DownloadOrchestrator.download_pending()` (downloader.py:224–257), which is **Phase 2** of `download_pipeline()` (tasks.py:160–194). But:
- **Phase 1** (`download_pending_librespot`, tasks.py:171) and **Phase 3** (`download_pending_spotdl`, tasks.py:186) each set rows to `DOWNLOADING` via `download_track()`'s atomic claim. If the container is killed (SIGTERM, crash, `docker compose up --force-recreate`) during Phase 1 or Phase 3, those rows strand and the Phase-2-only reset on the *next* cycle has a 30-minute `updated_at` cutoff that can skip them depending on timing.
- The reset queries `Track.updated_at < now-30min`. A row touched late in a cycle, then orphaned, then re-touched by a partial subsequent claim, keeps refreshing `updated_at` and evades the cutoff. The May-27 survivors prove the cutoff is leaking.

**Fix:** Promote the stuck-reset to a single guaranteed startup step that runs **before any sweep**, independent of `download_pending()`. Make it a standalone function in `tasks.py` (e.g. `reset_orphaned_downloads()`) called from `_background_startup()` in daemon.py right after migrations, and ALSO at the top of `download_pipeline()` before Phase 1. Keep the existing in-`download_pending` reset as defense-in-depth, but the authoritative one runs once per process boot with no per-cycle timing dependency.

Decision needed on cutoff: a process-boot reset can safely reset **all** `DOWNLOADING` rows to `PENDING` because `--workers 1` guarantees no other worker is alive at boot (the in-process pool cannot survive the process). This eliminates the leaky 30-min heuristic entirely for the boot path. The 30-min cutoff stays only for the mid-run `download_pending` path where live workers may exist.

**Files:** `src/core/tasks.py` (new fn + call in `download_pipeline`), `src/daemon.py` (`_background_startup` call site ~line 287).

**Effort:** ~30 min code, trivial.

**Verification:**
1. `docker exec musicstream-postgres psql -U musicstream -d musicstream -t -c "SELECT count(*) FROM tracks WHERE status='downloading';"` before/after restart — should drop to 0 at boot, then reflect only genuinely-live downloads.
2. Immediate one-off cleanup of the existing 83: safe to run now — `UPDATE tracks SET status='pending' WHERE status='downloading' AND updated_at < now() - interval '30 minutes';`

**Risk if skipped:** Queue slots silently leak on every restart. Over months this compounds; tracks that should download never do, with no error surfaced.

---

### P0-2 · Drain-on-shutdown missing → root cause of P0-1

**Severity:** P0 (root cause feeder for P0-1)
**Evidence:** `tini` forwards SIGTERM (Dockerfile.daemon:105) and `lifespan` shutdown only calls `scheduler.shutdown()` (daemon.py:254). In-flight download workers are abandoned: temp files orphaned, rows left `DOWNLOADING`.

**Root cause:** Lifespan shutdown does not signal the ThreadPoolExecutor pools to stop accepting work or wait for graceful completion, and does not reset rows it abandons.

**Fix:** Add a shutdown phase that (a) sets a module-level `_shutting_down` flag the sweep loops check between tracks (they already loop track-by-track with time budgets — cheap to add a flag check), and (b) on lifespan exit, best-effort reset any `DOWNLOADING` rows this process owns back to `PENDING`. Combined with P0-1's boot reset, this closes the loop from both ends.

**Files:** `src/daemon.py` (lifespan shutdown), `src/ingestion/downloader.py` (flag check in the three sweep loops).

**Effort:** ~45 min.

**Verification:** `docker compose restart daemon` mid-download; confirm no rows older than the restart are left `DOWNLOADING`, and `/app/temp` has no orphan files accumulating.

**Risk if skipped:** Every controlled restart re-creates P0-1. The two are the same wound from opposite sides.

---

## P1 — Correctness / operational gaps

### P1-1 · In-container Alembic CLI is broken (PYTHONPATH)

**Severity:** P1
**Evidence:** `docker exec musicstream-daemon sh -c 'cd /app && alembic current'` → `ModuleNotFoundError: No module named 'src.models'` at `migrations/env.py:18`.

**Root cause:** `migrations/env.py` does `from src.models import Base` with no `sys.path` bootstrap. It only works inside the daemon process because uvicorn has already put `/app` on `sys.path`. The standalone `alembic` CLI does not, so manual migration ops (status check, downgrade, autogenerate) are impossible from inside the container.

**Fix:** Prepend the repo root to `sys.path` at the top of `migrations/env.py`:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```
before `from src.models import Base`.

**Files:** `migrations/env.py`.

**Effort:** 2 min.

**Verification:** `docker exec musicstream-daemon sh -c 'cd /app && alembic current'` prints `0002 (head)` cleanly.

**Risk if skipped:** No manual migration control. The day you need to inspect/downgrade/generate a migration in prod, you can't — you're flying blind on schema state.

---

### P1-2 · 2,130 downloaded rows have NULL/empty `download_method`

**Severity:** P1
**Evidence:** `SELECT count(*) FROM tracks WHERE status='downloaded' AND (download_method IS NULL OR download_method='')` → **2,130**.

**Root cause:** Provenance not always written on the success path (likely legacy rows or a tier that returns success without setting `download_method`). Cannot audit which source delivered these, nor re-fetch by source if a tier is later found to produce bad files.

**Fix:** Backfill from `download_attempts` where a successful attempt exists:
```sql
UPDATE tracks t SET download_method = da.method
FROM download_attempts da
WHERE da.track_id = t.id AND da.success = true
  AND (t.download_method IS NULL OR t.download_method='');
```
Then audit the success path in `download_track` to ensure `download_method` is always set on every tier's success branch (not just some). Add a NOT-NULL-on-downloaded invariant check to the integrity checker.

**Files:** one-off SQL + `src/ingestion/downloader.py` success branches + `src/integrity/checker.py`.

**Effort:** ~30 min.

**Verification:** Count returns 0 after backfill; integrity check reports no downloaded-without-method rows.

**Risk if skipped:** Unauditable provenance. If a tier is found to ship low-quality files, you can't identify and re-fetch the affected tracks.

---

### P1-3 · Volatile download backends are unpinned

**Severity:** P1
**Evidence:** `requirements.txt` — `yt-dlp[default]`, `spotdl`, `librespot`, `rich` carry **no version pin**. These are the four highest-churn, highest-breakage dependencies in the stack (tiers 0/2/3/4/5 all depend on them).

**Root cause:** Unpinned. A silent upstream release between rebuilds can break extraction, change CLI flags, or alter auth behaviour with zero warning. yt-dlp in particular ships breaking changes frequently.

**Fix:** Pin all four to current working versions (capture from the live container: `docker exec musicstream-daemon pip freeze | grep -iE 'yt-dlp|spotdl|librespot|rich'`). Add them to Dependabot's allow-list so bumps come as reviewable PRs with CI, not silent rebuild surprises. **Note:** pinning yt-dlp trades "auto-fixes when YouTube changes" for "controlled upgrades" — accept that you must bump it deliberately when extraction breaks. That is the correct trade for a production pipeline.

**Files:** `requirements.txt`, `.github/dependabot.yml`.

**Effort:** ~20 min. **Requires rebuild** (`docker compose build daemon`).

**Verification:** `pip freeze` matches pins; a test download through each tier still succeeds.

**Risk if skipped:** "It broke overnight and I changed nothing" — the single most likely future outage.

---

### P1-4 · `downloading`-count watchdog (would auto-catch P0-1)

**Severity:** P1
**Evidence:** P0-1 went unnoticed for 3 days. No alert exists for stuck state.

**Root cause:** No monitoring on queue-state anomalies.

**Fix:** Add a scheduled watchdog (sibling-project pattern — Startup-folder/`schtasks` script or a Hermes cron `no_agent` job) that queries `downloading` count and alerts if `> threshold` for `> 30 min`. Reuse the house watchdog pattern already established in facetracker/unifiedcollector. Keep it script-only (no LLM) — emit a message only on anomaly, silent otherwise.

**Files:** new `scripts/watchdog_stuck_downloads.sh` + task registration (Startup folder, per house pattern — this bash session cannot register Scheduled Tasks).

**Effort:** ~30 min.

**Verification:** Force a stuck row, confirm alert fires.

**Risk if skipped:** Next stuck-state incident is again found by manual inspection, days late.

---

### P1-5 · `asyncio.get_event_loop()` deprecated path will break

**Severity:** P1
**Evidence:** daemon.py:439 `_self_heal_lb_discovery_if_overdue` uses `asyncio.get_event_loop().run_in_executor(...)`. Deprecated in 3.10+, emits DeprecationWarning under 3.12 (the container runtime), removal scheduled.

**Fix:** Replace with `asyncio.get_running_loop().run_in_executor(...)` (this is called from async context) or `asyncio.to_thread(...)` to match the rest of the codebase's pattern.

**Files:** `src/daemon.py`.

**Effort:** 5 min.

**Verification:** No DeprecationWarning in logs on the self-heal path; LB self-heal still fires when overdue.

**Risk if skipped:** Breaks on a future Python bump; LB discovery self-heal silently stops.

---

### P1-6 · Health endpoint reports healthy on a wedged scheduler

**Severity:** P1
**Evidence:** `/health` (daemon.py:443) checks only DB reachability. Scheduler/run-liveness not surfaced. A dead scheduler → no downloads, but Docker healthcheck stays green.

**Fix:** Extend `/health` (or add `/health/deep`) to include `scheduler.running` and age of the most recent `daemon_runs.started_at`. Return `degraded` (503) if scheduler is down or no run has started in > expected interval. Keep the shallow `/health` for Docker's liveness probe (DB only) and add the deep one for monitoring — don't make Docker restart the container on a scheduler hiccup.

**Files:** `src/daemon.py`.

**Effort:** ~20 min.

**Verification:** Kill scheduler in a test; `/health/deep` reports degraded while `/health` stays ok.

**Risk if skipped:** Silent pipeline stall presenting as "healthy."

---

## P2 — Hardening / maintainability

### P2-1 · Secrets in repo-adjacent plaintext
`.env` (9.2 KB), `cookies.txt`, `spotify_token.json`, librespot creds are bind-mounted plaintext. The daemon already *warns* on permissive modes (daemon.py:196 `_audit_credential_permissions`). **Act on the warning:** `chmod 600` all credential files, document the expected modes, and confirm `.gitignore` covers every one. Effort ~15 min. Risk: secrets readable by any host process/user.

### P2-2 · Tier-4 YouTube official-source scorer has no unit tests
The `_score_youtube_candidate` regex reject logic (lyric/cover/8D/slowed/nightcore/etc.) silently drops candidates. High false-negative risk (rejecting legit official audio). Add table-driven unit tests with real-world title samples. Effort ~45 min. Risk: silent over-rejection starves tier 4.

### P2-3 · Lift the `--workers 1` constraint (architectural)
Scheduler, WS manager, circuit-breaker, worker pool are all in-process singletons (Dockerfile.daemon:7–10 documents the constraint). Externalize scheduler + circuit-breaker state (DB table or Redis) to enable horizontal download workers. **Largest scaling unlock but largest effort** — only justified if 60 k backlog burn-rate proves too slow after P0/P1 fixes. Effort: days. Defer until measured.

### P2-4 · Backup restore is never verified
`db_backup` runs weekly (daemon.py:403) but no restore test. Add a restore-to-scratch-DB check (monthly). Effort ~1 hr. Risk: backups that don't restore = no backups.

### P2-5 · `alembic/` vs `migrations/` directory confusion
Two dirs: `alembic/` (empty `versions/`) and `migrations/` (the real one, configured in `alembic.ini`). The empty `alembic/` is a trap. Remove it or document why it exists. Effort 5 min.

### P2-6 · `download_method` provenance + attempt metadata on `tracks`
Add `last_attempt_at` and `attempt_count` columns so requeue/failed policy is explicit rather than derived by counting `download_attempts`. Migration `0003`. Effort ~30 min. Improves observability + simplifies the ≥9-attempts-→-failed logic.

### P2-7 · Backlog burn-rate / ETA metric
Surface tracks/hour and projected completion (60,970 pending / current rate). Add to `/stats` or a log line per cycle. Effort ~30 min. Tells you if the queue is converging.

### P2-8 · Runbook documentation
`AGENTS.md` is sync-infra boilerplate; there is no musicstream recovery runbook. The recovery logic is excellent but tribal. Write `RUNBOOK.md`: how to restart, where state lives (port 9079, `Authorization: Bearer $DAEMON_API_TOKEN`, tier ladder, boot autostart). Effort ~45 min.

### P2-9 · downloader.py (1,678 LOC) / tagger.py (1,111 LOC) decomposition
Tier logic + scoring + sweep orchestration + session management entangled in one class. Refactor into `tiers/`, `scoring`, `sweeps`, `orchestrator` modules. High refactor risk; do only with the test suite green and coverage measured first (see P2-10). Effort: 1–2 days. Defer.

### P2-10 · Measure test coverage, gate CI
16 test files exist; coverage % unknown. Add `pytest --cov` to `ci.yml`, set a floor, block PRs below it. Effort ~30 min. Prerequisite for safely doing P2-9.

---

## Recommended sequencing

**Batch A — ship today (code-only, no rebuild, ~2 hrs):**
1. P0-1 orphaned-download reset (+ immediate SQL cleanup of the 83)
2. P0-2 drain-on-shutdown
3. P1-1 Alembic PYTHONPATH
4. P1-2 download_method backfill (SQL now, code-path audit follows)
5. P1-5 asyncio deprecation

Verify all against live DB, then `docker compose up -d --force-recreate daemon`.

**Batch B — this week (one rebuild, ~3 hrs):**
6. P1-3 pin backends (rebuild)
7. P1-4 stuck-download watchdog
8. P1-6 deep health endpoint
9. P2-1 chmod secrets
10. P2-5 remove empty alembic dir
11. P2-10 coverage in CI

**Batch C — backlog (measure first, then decide):**
12. P2-2 scorer tests · P2-4 restore test · P2-6 attempt columns · P2-7 burn-rate · P2-8 runbook
13. P2-9 decomposition and P2-3 multi-worker — **only if** burn-rate after Batch A/B proves throughput is the binding constraint. Don't refactor on spec.

---

## What this plan deliberately does NOT include
- No LLM / inference / model-provider layer. None exists; none is warranted. This is a deterministic ETL daemon and its value is being debuggable and predictable.
- No premature horizontal scaling (P2-3) before throughput is measured.
- No decomposition refactor (P2-9) before test coverage exists to catch regressions.

The one item that is a genuine **bug with live evidence** is P0-1 (with P0-2 as its other half). Everything else is improvement. Start there.
