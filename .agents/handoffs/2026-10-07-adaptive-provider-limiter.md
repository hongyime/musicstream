# Adaptive provider limiter deployment record

- Branch: `feature/adaptive-provider-limiter`
- Commit: `617dd41d8ccd90a475f168e374eff5255728cc69`
- Verification: `python -m pytest tests/ -q --tb=no` — 382 passed, 1 skipped; compileall and `git diff --check` passed.
- Enforcement: `PROVIDER_LIMITER_ENABLED` is unset in `.env` and the running container; code defaults to shadow mode. Runtime status confirms `enabled=false`, `mode=shadow`.

## Deployment and verification

- Executed the approved `docker restart musicstream-daemon` exactly once. No Compose command was run and Postgres was not restarted.
- Before restart: daemon healthy, started `2026-10-07T10:25:56Z`, up 4 hours, 558.5 MiB / 2 GiB. Postgres started `2026-10-05T14:35:01Z` and was healthy.
- After restart: daemon healthy, started `2026-10-07T14:05:14Z`. `/health` returns `ok`; `/health/deep` returns `ok` with `scheduler_running=true`, 7 active downloads, and last successful download 28 seconds ago.
- `/api/musicstream/metrics` reports YouTube request_count=13, success_count=12, failure_count=1, throttle_signal_count=1, and `failure_reasons.bot_challenge=1`; limiter is still disabled in shadow mode.
- Daemon memory after restart: 377 MiB / 2 GiB. Postgres remained healthy with its original start time.