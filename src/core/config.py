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
