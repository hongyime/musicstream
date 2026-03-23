"""
Custom exceptions for music-download-code
"""


class RateLimitError(Exception):
    """Raised when Spotify or YouTube Music rate limit is hit after max retries."""
    
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
