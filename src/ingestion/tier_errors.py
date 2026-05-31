"""Normalised failure-reason constants for ``download_attempts.error``.

A download tier calls ``DownloadOrchestrator._note_fail(<one of these>)`` right
before it returns ``None``; the tier loop then persists the value so that a
``SELECT error, count(*) ... GROUP BY error`` gives a clean breakdown (e.g.
``region_unavailable`` vs ``rate_limited`` for librespot) instead of the opaque
``tier_returned_none`` placeholder we used to store for ~99.8% of failures.

Kept as plain module-level strings (not an Enum) deliberately: zero import
friction at the ~40 call sites, and adding a new value never risks a migration
or an enum-membership error. The column is an unbounded nullable String, so
these are written verbatim.
"""

# ── Generic (applicable to any tier) ─────────────────────────────────────────
UNKNOWN_TIER_FAIL = "tier_returned_none"   # fallback: tier returned None without _note_fail
NOT_AVAILABLE     = "not_available"        # optional client/dependency missing
NO_SOURCE_ID      = "no_source_id"         # required spotify_id / spotify_uri absent
CIRCUIT_OPEN      = "circuit_open"         # service circuit breaker tripped
THROTTLE_SKIP     = "throttle_skip"        # throttle window said "skip this round"
NO_CANDIDATES     = "no_candidates"        # search returned nothing usable
RATE_LIMITED      = "rate_limited"         # 429 / explicit per-account rate limit

# ── librespot (Tier 0) specific ──────────────────────────────────────────────
REGION_UNAVAIL    = "region_unavailable"   # Spotify has no playable variant for the account/market
AUTH_FAILURE      = "auth_failure"         # credential / token / 403
FFMPEG_FAIL       = "ffmpeg_fail"          # OGG -> MP3 conversion failed
EMPTY_STREAM      = "empty_stream"         # zero / truncated stream from the CDN
NONMUSIC_SKIP     = "nonmusic_skip"        # pre-filtered skit / interlude
STREAM_ERROR      = "stream_error"         # other transient stream / IO error
