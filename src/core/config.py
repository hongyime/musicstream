import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media"))

# ── Logging ───────────────────────────────────────────────────────────────────

MAX_LOG_BYTES = 5 * 1024 * 1024   # 5 MB
BACKUP_COUNT = 3

# ── Backups ───────────────────────────────────────────────────────────────────

MAX_BACKUPS = 14

# ── Scheduler ─────────────────────────────────────────────────────────────────

TIMEZONE = "Asia/Singapore"

# ── Spotify ───────────────────────────────────────────────────────────────────

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_TOKEN_CACHE = os.environ.get("SPOTIFY_TOKEN_CACHE", "/app/spotify_token.json")

# ── App ───────────────────────────────────────────────────────────────────────

DISABLE_DOWNLOADS = os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on")
MAX_CONCURRENT_WORKERS = int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))
DAEMON_API_TOKEN = os.environ.get("DAEMON_API_TOKEN") or None

# ── Wave 3 (SPEC.md §W3) ───────────────────────────────────────────────────────

# Final stored audio format: 'mp3_320' (FLAC sources transcoded down) or 'flac'.
QUALITY_CUTOFF = os.environ.get("QUALITY_CUTOFF", "mp3_320").strip().lower()
KEEP_FLAC_MASTER = os.environ.get("KEEP_FLAC_MASTER", "").lower() in ("1", "true", "yes", "on")
PLAYLISTS_EXPORT_DIR = os.environ.get("PLAYLISTS_EXPORT_DIR") or None
# Container→host path mapping for m3u exports (DB stores container paths like /media/...).
EXTERNAL_MEDIA_DRIVE = os.environ.get("EXTERNAL_MEDIA_DRIVE") or None

# Notifications (T17). WEBHOOK_URL empty ⇒ notifier disabled entirely.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or None
NOTIFY_ON = os.environ.get("NOTIFY_ON", "failures").strip().lower()

# Auto-block after this many consecutive failed full-chain download passes (T14).
AUTO_BLOCK_THRESHOLD = int(os.environ.get("AUTO_BLOCK_THRESHOLD", "6"))

# Spotify token early-warning window in hours (T18).
TOKEN_WARN_HOURS = float(os.environ.get("TOKEN_WARN_HOURS", "48"))

# Max tracks requeued per upgrade-pass run (§W3 T20) — trickle, don't stampede.
UPGRADE_PASS_LIMIT = int(os.environ.get("UPGRADE_PASS_LIMIT", "500"))

