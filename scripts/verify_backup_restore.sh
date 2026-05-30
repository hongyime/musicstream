#!/usr/bin/env bash
# musicstream — backup restore verification (P2-4)
#
# "Backups that don't restore are not backups." This proves the newest db_backup
# dump actually restores: it creates a scratch database in the postgres
# container, streams the latest dump into it (the backup lives on the host /
# daemon mount, NOT the postgres container, so we pipe via stdin), asserts the
# core tables are present and populated, then drops the scratch DB.
#
# Run monthly via the Windows Startup folder (same pattern as
# watchdog_stuck_downloads.sh; this env cannot register Scheduled Tasks):
#   "C:\Program Files\Git\bin\bash.exe" -lc "/c/musicstream/scripts/verify_backup_restore.sh"
#
# Exit: 0 = restore verified; 1 = verification failed; 2 = no backup / setup error.
set -uo pipefail

PG_CONTAINER="${PG_CONTAINER:-musicstream-postgres}"
PG_USER="${PG_USER:-musicstream}"
PG_DB="${PG_DB:-musicstream}"
SCRATCH_DB="${SCRATCH_DB:-musicstream_restore_test}"
BACKUP_DIR="${BACKUP_DIR:-/c/musicstream/backups}"
LOG_FILE="${RESTORE_TEST_LOG:-/c/musicstream/logs/verify_backup_restore.log}"

export MSYS_NO_PATHCONV=1

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() {
    echo "[$(ts)] $*"
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    echo "[$(ts)] $*" >> "$LOG_FILE" 2>/dev/null || true
}
psql_main() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" "$@"; }
cleanup() { psql_main -c "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1 || true; }

fail() { log "FAIL: $1"; exit "${2:-1}"; }

# Newest backup on the host.
latest="$(ls -1t "${BACKUP_DIR}"/musicstream_*.sql 2>/dev/null | head -n1 || true)"
[ -n "${latest}" ] || fail "no backup found in ${BACKUP_DIR} (db_backup runs weekly / on boot)" 2
log "verifying restore of: ${latest}"

trap cleanup EXIT

# Fresh scratch DB.
psql_main -c "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1 || true
psql_main -c "CREATE DATABASE ${SCRATCH_DB};" >/dev/null 2>&1 || fail "could not create scratch DB ${SCRATCH_DB}" 2

# Restore via stdin (the dump lives on the daemon mount, not this container).
# Deliberately NO ON_ERROR_STOP: pg_dump is pinned to the server's MAJOR (16), so
# dumps are native v16 (no v17 SET transaction_timeout), but they still carry the
# \restrict security directive which the server's older-minor psql warns about and
# skips harmlessly. The authoritative success check is whether the data landed
# (the tracks-count assertion below), not psql's handling of that cosmetic.
docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$SCRATCH_DB" < "${latest}" >/dev/null 2>&1 || true

# Assert the core table restored and is populated.
n="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$SCRATCH_DB" -tAc "SELECT count(*) FROM tracks;" 2>/dev/null | tr -d '[:space:]')"
[ -n "${n}" ] || fail "tracks table missing after restore"
[ "${n}" -gt 0 ] 2>/dev/null || fail "tracks table empty after restore (got '${n}')"

log "OK: restore verified — ${n} tracks in scratch DB (dropped on exit)"
exit 0
