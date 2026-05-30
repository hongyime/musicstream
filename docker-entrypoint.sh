#!/usr/bin/env bash
# Audit #30 follow-up: entrypoint that fixes bind-mount ownership before
# dropping to the runtime user.
#
# Why: Docker Desktop on Windows/macOS bind-mounts directories with
# host-derived UIDs (often 0 or whatever the host user maps to) which the
# in-container `musicstream` UID 1000 can't write to.  Pre-creating the
# dirs in the Dockerfile and `chown`ing them works for first-run with a
# fresh `data:`-style volume but fails the moment a host bind-mount
# overlays the mountpoint.
#
# We run as root just long enough to chown the mount roots, then exec
# into uvicorn under the unprivileged user via gosu.
#
# Idempotent — safe to run repeatedly. Errors on chown are non-fatal
# because read-only mounts (cookies.txt, spotify_token.json) legitimately
# can't be chowned.

set -e

RUNTIME_USER="${RUNTIME_USER:-musicstream}"
RUNTIME_UID="${RUNTIME_UID:-1000}"
RUNTIME_GID="${RUNTIME_GID:-1000}"

# Best-effort fix on writable mount targets + the in-container temp/ workdir.
# /app/temp is NOT a bind mount; it must be writable by the runtime user or the
# download tiers (spotiflac/yt-dlp/librespot/spotdl) fail with EACCES. The image
# ships it musicstream-owned; re-assert here in case a stray root-owned temp/
# was created (e.g. by a root `docker exec ... python` that imported the
# orchestrator, whose __init__ does os.makedirs(TEMP_DIR)).
for d in /app/logs /app/backups /app/data /app/temp; do
    if [ -d "$d" ]; then
        # Don't fail if chown returns non-zero — read-only filesystems
        # legitimately reject the call. The daemon's first write will
        # surface the real error if it can't actually use the dir.
        chown -R "$RUNTIME_UID:$RUNTIME_GID" "$d" 2>/dev/null || true
    fi
done

# OAuth token cache files are bind-mounted as individual files, NOT
# directories — the loop above misses them.  Spotipy/spotdl rewrite
# these in place after every refresh, so they MUST be writable by the
# runtime user.  Without this fix, a fresh OAuth flow logs
# "[OAUTH] exchange OK" but Spotipy silently fails the cache write
# (PermissionError, eaten by its CacheFileHandler) and the next request
# loads the stale on-disk token — manifesting as "needs_auth" forever.
# P2-1: tighten credential files to 0600 (owner rw only) on every start.
# They are secrets in a single-tenant container; 0600 clears the daemon's own
# permissive-mode warning and re-applies after bind-mount remounts that reset
# host-derived perms to 0777. Still owner-writable so OAuth/credential refresh
# (Spotipy/spotdl/librespot rewrite these in place) keeps working.
for f in /app/spotify_token.json /app/cookies.txt /app/data/librespot_credentials.json; do
    if [ -e "$f" ]; then
        chown "$RUNTIME_UID:$RUNTIME_GID" "$f" 2>/dev/null || true
        chmod 600 "$f" 2>/dev/null || true
    fi
done

# Drop to non-root via gosu and exec — exec keeps us as PID 1 child of
# tini (which is the real PID 1 from the Dockerfile ENTRYPOINT).
exec gosu "$RUNTIME_USER" "$@"
