#!/usr/bin/env bash
# musicstream — stuck-download watchdog (P1-4)
#
# Catches the failure mode behind P0-1: rows stranded in status='downloading'.
# P0-1/P0-2 now reset orphans on boot/shutdown, but this is the monitoring
# backstop that would have caught the original 3-day-stuck incident in minutes
# instead of by manual inspection.
#
# Behaviour: SILENT unless anomalous. Alerts (stderr + appends to a log file)
# only when MORE THAN $WATCHDOG_THRESHOLD rows are 'downloading' with updated_at
# older than $WATCHDOG_STALE_MINUTES. A healthy active download bumps updated_at
# on every tier transition, so a stale timestamp means a genuinely stuck row.
#
# This bash session cannot register Scheduled Tasks (schtasks //create -> Access
# denied), so schedule it via the Windows Startup folder. Drop a .bat in
# shell:startup  (Win+R -> shell:startup):
#   @echo off
#   "C:\Program Files\Git\bin\bash.exe" -lc "while true; do /c/musicstream/scripts/watchdog_stuck_downloads.sh; sleep 600; done"
#
# Exit codes: 0 = healthy or alert emitted; 2 = could not query the DB.
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-musicstream-postgres}"
PG_USER="${PG_USER:-musicstream}"
PG_DB="${PG_DB:-musicstream}"
THRESHOLD="${WATCHDOG_THRESHOLD:-0}"            # alert when stuck count exceeds this
STALE_MINUTES="${WATCHDOG_STALE_MINUTES:-30}"
LOG_FILE="${WATCHDOG_LOG:-/c/musicstream/logs/watchdog_stuck_downloads.log}"

# MSYS path-mangling guard for docker on Git Bash (see scripts/dev_helpers.sh).
export MSYS_NO_PATHCONV=1

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Count rows stuck in 'downloading' with a stale heartbeat.
sql="SELECT count(*) FROM tracks WHERE status='downloading' AND updated_at < now() - interval '${STALE_MINUTES} minutes';"

if ! stuck="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$sql" 2>/dev/null)"; then
    echo "[$(ts)] watchdog: ERROR querying ${PG_CONTAINER}" >&2
    exit 2
fi

stuck="$(printf '%s' "$stuck" | tr -d '[:space:]')"
stuck="${stuck:-0}"

if [ "$stuck" -gt "$THRESHOLD" ]; then
    msg="[$(ts)] ALERT: ${stuck} track(s) stuck in 'downloading' >${STALE_MINUTES}min (threshold ${THRESHOLD}). Check: docker logs musicstream-daemon; 'docker compose restart daemon' requeues them (P0-1 boot reset)."
    echo "$msg" >&2
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
fi

# Silent on healthy.
exit 0
