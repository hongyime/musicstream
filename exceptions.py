"""
Custom exceptions for musicstream.
All exceptions inherit from MusicStreamError for easy catch-all handling.
"""


class MusicStreamError(Exception):
    """Base exception for all musicstream errors."""


# ── Rate limiting ──────────────────────────────────────────────────────────────

class RateLimitError(MusicStreamError):
    """Raised when a service rate limit is hit after the maximum number of retries."""

    def __init__(self, service: str, message: str = "Rate limit exceeded after all retries"):
        self.service = service
        self.message = f"{service}: {message}"
        super().__init__(self.message)


class SpotifyRateLimitError(RateLimitError):
    """Spotify-specific rate limit error."""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__("Spotify", message)


class YouTubeMusicRateLimitError(RateLimitError):
    """YouTube Music-specific rate limit error with helpful suggestion."""

    def __init__(self, message: str = "Rate limit exceeded. Add cookies.txt or try again later"):
        super().__init__("YouTube Music", message)


# ── Download pipeline ──────────────────────────────────────────────────────────

class DownloadError(MusicStreamError):
    """Raised when all download tiers fail for a track."""


class TaggingError(MusicStreamError):
    """Raised when metadata tagging fails for a downloaded audio file."""


class OrganiserError(MusicStreamError):
    """Raised when a file cannot be moved into the Plex directory structure."""


# ── Integrity ──────────────────────────────────────────────────────────────────

class IntegrityError(MusicStreamError):
    """Raised when a file integrity check detects a missing or corrupt file."""


# ── External services ──────────────────────────────────────────────────────────

class ListenBrainzError(MusicStreamError):
    """Raised when a ListenBrainz API request fails."""


class MusicBrainzError(MusicStreamError):
    """Raised when a MusicBrainz API request fails."""


class SpotiFLACError(MusicStreamError):
    """Raised when SpotiFLAC fails or is unavailable for Tier 1 download."""


# ── Database ───────────────────────────────────────────────────────────────────

class DatabaseError(MusicStreamError):
    """Raised when a database operation fails (connection, query, migration)."""
