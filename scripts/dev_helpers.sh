#!/usr/bin/env bash
# musicstream dev helpers — source from your shell:
#   source scripts/dev_helpers.sh
#
# Solves two recurring pain points when iterating on the daemon:
#
# 1. MSYS path mangling on Windows Git Bash:
#    `docker exec musicstream-daemon cat /app/logs/x.log` is silently
#    rewritten to `cat C:/Program Files/Git/app/logs/x.log` which doesn't
#    exist. `dexec` sets MSYS_NO_PATHCONV=1 and uses `//app/...` form.
#
# 2. 5-minute image rebuild loop on every code change:
#    `docker-compose.override.yml` (created next to docker-compose.yml,
#    gitignored) bind-mounts src/ alembic/ alembic.ini into the container.
#    Code edits go live with `dreload` (~5 sec), no rebuild.

# Run a command in a container with MSYS path mangling disabled.
# Usage:  dexec musicstream-daemon ls /app/logs
#         dexec musicstream-daemon python -c 'import src.daemon'
dexec() {
    local container="$1"
    shift
    if [ -z "$container" ] || [ -z "$1" ]; then
        echo "usage: dexec <container> <cmd...>" >&2
        return 2
    fi
    # Re-prefix any /app/... arg with //app/... to defeat MSYS rewriting
    local args=()
    for a in "$@"; do
        if [[ "$a" == /app/* ]]; then
            args+=( "/$a" )   # /app/... → //app/...
        else
            args+=( "$a" )
        fi
    done
    MSYS_NO_PATHCONV=1 docker exec "$container" "${args[@]}"
}

# Tail the musicstream daemon file logger (the one that survives uvicorn restart)
dlog() {
    dexec musicstream-daemon tail -f /app/logs/musicstream.log
}

# Reload the daemon — recreates the container in ~5s. Source bind-mount means
# this picks up code changes without `docker compose build`. Health check is
# then verified once on return.
dreload() {
    cd /c/musicstream || return 1
    echo "Recreating musicstream-daemon (bind-mount, no rebuild)..."
    MSYS_NO_PATHCONV=1 docker compose up -d --force-recreate daemon || return $?
    # Wait for healthy
    for i in $(seq 1 30); do
        local hs
        hs=$(docker inspect musicstream-daemon --format '{{.State.Health.Status}}' 2>/dev/null)
        if [ "$hs" = "healthy" ]; then
            echo "✓ daemon healthy after $((i*2))s"
            return 0
        fi
        sleep 2
    done
    echo "⚠ daemon did not reach healthy state in 60s; check 'docker logs musicstream-daemon'"
    return 1
}

# Quick health snapshot
dhealth() {
    cd /c/musicstream || return 1
    docker compose ps
    echo
    curl -sS -m 5 http://localhost:9079/health 2>&1 || echo "(daemon endpoint unreachable)"
}

# Trigger a manual liked-artists-expand run. Default batch=10.
# Usage:  dexpand                  # batch=10
#         dexpand 50               # batch=50
dexpand() {
    cd /c/musicstream || return 1
    local batch="${1:-10}"
    local token
    token=$(grep -E '^DAEMON_API_TOKEN=' .env | cut -d= -f2-)
    if [ -z "$token" ]; then
        echo "DAEMON_API_TOKEN not found in .env" >&2
        return 1
    fi
    curl -sS -m 30 -X POST -H "Authorization: Bearer $token" \
        "http://localhost:9079/api/musicstream/liked-artists-expand?batch=$batch"
    echo
}

# Print short help when sourced
echo "musicstream dev helpers loaded:"
echo "  dexec <container> <cmd...>   — docker exec without MSYS path mangling"
echo "  dlog                          — tail file logger"
echo "  dreload                       — recreate daemon (bind-mount, no rebuild)"
echo "  dhealth                       — health snapshot"
echo "  dexpand [batch]               — trigger liked-artists-expand"
