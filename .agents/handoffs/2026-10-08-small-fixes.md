# Small fixes bundle completion

- Branch: `feature/small-fixes`, based on `main` at `d29e498`.
- Commits: `4dc092468526332e239fe92592c0c81a53f708cd` (ListenBrainz self-heal), `765394941e6295c61b3447d4f396ffdcc5bcbad9` (scheduler descriptions), `8c94504d291cbc9c5f860b25eedc21dec5ab726c` (orphan inventory).
- Full suite: 409 passed, 1 skipped, 9 subtests passed.
- Deployed: initial `docker compose up -d` kept the running bind-mounted daemon in place, then `docker compose up -d --force-recreate daemon` loaded the changes. `/health` and `/health/deep` are ok, scheduler running, 4 downloads active, 187 successes/hour, provider limiter enforcing, and watchdog startup is logged, and OpenAPI lists both orphan cleanup paths. PostgreSQL ID/start time did not change.
- Fast-forward merged and pushed `origin/main`; local and remote `main` are `8c94504d291cbc9c5f860b25eedc21dec5ab726c`.
- No `.env` values or Spotify secret files changed. Existing unrelated handoff edits, `docker_ports.txt`, and the ignored Compose override were preserved.
