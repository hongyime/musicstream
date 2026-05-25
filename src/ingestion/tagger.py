"""
ingestion/tagger.py — MusicBrainz + AcoustID metadata tagger

Applies a three-source tag priority pipeline per field:
  Spotify metadata (already on the Track ORM object)
    → MusicBrainz WS2 (ISRC → AcoustID fingerprint → title+artist search)
      → yt-dlp embedded tags (read via mutagen as last resort)

Writes tags to MP3 (ID3), FLAC (Vorbis comments), and M4A (MP4 atoms)
using mutagen.  Updates mb_recording_id, mb_release_id, acoustid_id, and
cover_art_source on the Track row after a successful tag pass.

Rate limiting:
  MusicBrainz — 1 req/s  (ServiceRateLimiter "musicbrainz")
  AcoustID    — 3 req/s  (ServiceRateLimiter "acoustid")
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from src.exceptions import MusicBrainzError, TaggingError
from src.models import Track
from src.rate_limiter import ServiceRateLimiter

# ── Optional heavy imports (graceful degradation) ─────────────────────────────

try:
    import acoustid  # pyacoustid
    ACOUSTID_AVAILABLE = True
except ImportError:
    ACOUSTID_AVAILABLE = False

try:
    from mutagen.id3 import (
        APIC,
        ID3,
        ID3NoHeaderError,
        TALB,
        TCMP,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TRCK,
    )
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Error logger (errors.log) ─────────────────────────────────────────────────

_error_logger = logging.getLogger("musicstream.errors")

# ── Constants ─────────────────────────────────────────────────────────────────

MB_USER_AGENT = "musicstream/3.0.0 ( github.com/bryanseah234/musicstream )"
MB_WS2_BASE   = "https://musicbrainz.org/ws/2"
CAA_BASE      = "https://coverartarchive.org"
ACOUSTID_BASE = "https://api.acoustid.org/v2"


# ── Data containers ───────────────────────────────────────────────────────────

@dataclass
class MBData:
    """Metadata returned from a MusicBrainz lookup."""
    recording_id: Optional[str] = None
    release_id:   Optional[str] = None
    title:        Optional[str] = None
    artist:       Optional[str] = None
    album:        Optional[str] = None
    year:         Optional[str] = None
    track_number: Optional[int] = None


@dataclass
class TagData:
    """Resolved tag values ready to be written to a file."""
    title:        Optional[str]   = None
    artist:       Optional[str]   = None
    album_artist: Optional[str]   = None
    album:        Optional[str]   = None
    year:         Optional[str]   = None
    track_number: Optional[int]   = None
    cover_art:    Optional[bytes] = None


@dataclass
class TagResult:
    """Records which source was used for each tag field."""
    title_source:        str = "none"
    artist_source:       str = "none"
    album_artist_source: str = "none"
    album_source:        str = "none"
    year_source:         str = "none"
    track_number_source: str = "none"
    cover_art_source:    str = "none"
    mb_used:             bool = False
    mb_recording_id:     Optional[str] = None
    mb_release_id:       Optional[str] = None
    acoustid_id:         Optional[str] = None


# ── SSRF guard ────────────────────────────────────────────────────────────────
#
# All outbound HTTP for cover art / external metadata routes through this
# guard. We resolve the host to its IP via socket.getaddrinfo and refuse if
# any resolved address is in a private/loopback/link-local/multicast/reserved
# range. This blocks the standard SSRF attack surface:
#   - 127.0.0.0/8 (loopback)
#   - 10/172.16/192.168 (RFC1918)
#   - 169.254.0.0/16 (link-local + cloud metadata 169.254.169.254)
#   - ::1, fc00::/7, fe80::/10 (IPv6 equivalents)
#
# Only http/https are allowed as schemes. file://, gopher://, etc. are out.

def _ip_is_safe(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _ssrf_safe(url: str) -> bool:
    """Return True if *url* is http(s) and resolves only to public IPs."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # If the host is already an IP literal, validate directly — getaddrinfo
    # would return it unchanged but being explicit avoids surprises.
    try:
        ipaddress.ip_address(host)
        return _ip_is_safe(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        if not _ip_is_safe(ip):
            return False
    return True


def _ssrf_safe_get(url: str, *, timeout: int = 15, max_redirects: int = 5):
    """GET a URL, validating every redirect hop with the SSRF guard."""
    current = url
    for _ in range(max_redirects + 1):
        if not _ssrf_safe(current):
            logger.warning("SSRF guard rejected %r; aborting fetch chain", current)
            return None
        resp = requests.get(current, timeout=timeout, allow_redirects=False)
        if resp.is_redirect or resp.is_permanent_redirect:
            loc = resp.headers.get("Location")
            if not loc:
                return resp
            # Resolve relative redirects against the current URL.
            from urllib.parse import urljoin
            current = urljoin(current, loc)
            continue
        return resp
    logger.warning("SSRF guard: too many redirects from %r", url)
    return None


# ── Atomic save helper ────────────────────────────────────────────────────────

def _atomic_mutagen_save(audio, file_path: str) -> None:
    """Write mutagen tags to *file_path* atomically.

    mutagen's ``audio.save()`` rewrites the file in place. SIGKILL / power
    loss / OOM during the rewrite truncates the file's header or leaves
    the frame index pointing past EOF — corrupting an otherwise-good
    audio file beyond recovery.

    We protect against that by:
      1. copying the original to a sibling temp file (preserves perms +
         the audio data mutagen needs to read back),
      2. invoking ``audio.save(tmp_path)`` — mutagen writes the new frames
         to the COPY,
      3. fsync'ing the temp file,
      4. ``os.replace`` (atomic rename within the same dir) over the
         original.

    A SIGKILL at any point either leaves the original intact (steps 1–3)
    or fully replaces it (step 4 is atomic on POSIX and on NTFS for
    same-volume renames). The temp file is cleaned up on every error
    path.
    """
    src = Path(file_path)
    dir_ = src.parent

    # tempfile.mkstemp gives us a unique filename in the SAME directory
    # — required for os.replace to be atomic. We don't actually use the
    # fd it returns (mutagen will reopen by path); close it immediately.
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{src.name}.tagging.",
        dir=str(dir_),
    )
    os.close(fd)

    try:
        # Copy bytes + permissions. mutagen reads existing frames from
        # the file before rewriting, so the temp must start as a faithful
        # copy of the source.
        shutil.copy2(file_path, tmp_path)

        # Mutagen rewrites the temp in place — corruption here only ever
        # touches the temp, never the original.
        audio.save(tmp_path)

        # fsync so the bytes are durable before we swap.
        with open(tmp_path, "rb") as fh:
            os.fsync(fh.fileno())

        # Atomic swap. On POSIX os.replace is atomic; on Windows it is
        # atomic for files on the same volume (which is always the case
        # here since we created the temp in the same directory).
        os.replace(tmp_path, file_path)
    except Exception:
        # Best-effort cleanup; never raise from cleanup.
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Main tagger class ─────────────────────────────────────────────────────────

class MetadataTagger:
    """
    Full metadata tagging pipeline.

    Usage::

        tagger = MetadataTagger(acoustid_api_key="...")
        result = tagger.tag_file("/tmp/track.mp3", track, session)
    """

    def __init__(
        self,
        acoustid_api_key: Optional[str] = None,
        rate_limiter: Optional[ServiceRateLimiter] = None,
    ) -> None:
        self._acoustid_key = acoustid_api_key or os.environ.get("ACOUSTID_API_KEY", "")
        self._rl = rate_limiter or ServiceRateLimiter()
        self._mb_session = requests.Session()
        self._mb_session.headers.update({"User-Agent": MB_USER_AGENT})
        self._sp = None
        self._sp_init_attempted = False

    def _get_spotify_client(self):
        """Lazy spotipy client for LB-track Spotify backfill. Returns None if
        no Spotify credentials are configured or token cache is missing."""
        if self._sp is not None or self._sp_init_attempted:
            return self._sp
        self._sp_init_attempted = True
        try:
            import spotipy  # type: ignore[import-untyped]
            from spotipy.cache_handler import CacheFileHandler
            from spotipy.oauth2 import SpotifyPKCE
            client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
            if not client_id:
                logger.info("Spotify backfill disabled: SPOTIFY_CLIENT_ID unset")
                return None
            cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE", "/app/spotify_token.json")
            cache_handler = CacheFileHandler(cache_path=cache_path)
            auth_manager = SpotifyPKCE(
                client_id=client_id,
                redirect_uri="http://127.0.0.1:8888/callback",
                scope="user-library-read",
                open_browser=False,
                cache_handler=cache_handler,
            )
            if auth_manager.validate_token(auth_manager.get_cached_token()) is None:
                logger.warning("Spotify backfill disabled: no valid token at %s", cache_path)
                return None
            self._sp = spotipy.Spotify(auth_manager=auth_manager)
            logger.info("Spotify backfill client initialised for tagger")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not init Spotify backfill client: %s", exc)
            self._sp = None
        return self._sp

    def _backfill_spotify_for_lb_track(self, track: Track, session: Session) -> None:
        """For ListenBrainz-discovered tracks (spotify_uri starts with ``mb:``)
        with no real Spotify metadata, search Spotify by title+artist and
        populate cover_art_url, spotify_id, spotify_album_id, isrc, etc.

        No-op for non-LB tracks or when no client is available.
        """
        if not track.spotify_uri or not track.spotify_uri.startswith("mb:"):
            return
        if track.cover_art_url and track.spotify_id:
            return
        if not (track.title and track.artist):
            return

        sp = self._get_spotify_client()
        if sp is None:
            return

        query = f'track:"{track.title}" artist:"{track.artist}"'
        try:
            self._rl.wait("spotify", attempt=0)
            resp = sp.search(q=query, type="track", limit=1, market="from_token")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Spotify backfill search failed for track %d: %s", track.id, exc)
            return

        items = ((resp or {}).get("tracks") or {}).get("items") or []
        if not items:
            logger.debug("Spotify backfill: no hit for %r — %r", track.title, track.artist)
            return

        item = items[0]
        album = item.get("album") or {}
        images = album.get("images") or []
        cover_url = images[0].get("url") if images else None
        spotify_id = item.get("id")
        spotify_album_id = album.get("id")
        external_ids = item.get("external_ids") or {}
        isrc = external_ids.get("isrc")
        track_number = item.get("track_number")
        disc_number = item.get("disc_number")
        duration_ms = item.get("duration_ms")
        release_date = (album.get("release_date") or "")[:4] or None
        album_name = album.get("name")

        changed = False
        if cover_url and not track.cover_art_url:
            track.cover_art_url = cover_url
            changed = True
        if spotify_id and not track.spotify_id:
            track.spotify_id = spotify_id
            changed = True
        if spotify_album_id and not track.spotify_album_id:
            track.spotify_album_id = spotify_album_id
            changed = True
        if isrc and not track.isrc:
            track.isrc = isrc
            changed = True
        if track_number and not track.track_number:
            track.track_number = track_number
            changed = True
        if disc_number and not track.disc_number:
            track.disc_number = disc_number
            changed = True
        if duration_ms and not track.duration_ms:
            track.duration_ms = duration_ms
            changed = True
        if release_date and not track.year:
            track.year = release_date
            changed = True
        if album_name and not track.album:
            track.album = album_name
            changed = True

        if changed:
            try:
                session.flush()
                logger.info(
                    "Spotify backfill: track %d ('%s' — '%s') resolved to spotify_id=%s",
                    track.id, track.title, track.artist, spotify_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("DB flush after Spotify backfill failed: %s", exc)

    # ── Public API ─────────────────────────────────────────────────────────────

    def tag_file(self, file_path: str, track: Track, session: Session) -> TagResult:
        """
        Apply the full tag pipeline to *file_path*.

        Priority per field: Spotify → MusicBrainz → yt-dlp embed.
        Updates the Track row in the DB with MB/AcoustID identifiers.
        Returns a TagResult describing which source was used per field.

        Raises:
            TaggingError: if mutagen is unavailable or writing tags fails.
        """
        if not MUTAGEN_AVAILABLE:
            raise TaggingError("mutagen is not installed; cannot write tags")

        result = TagResult()

        # Step 0: backfill Spotify metadata for LB-discovered tracks (no-op for
        # tracks already linked to a real Spotify ID). Populates cover_art_url
        # so the cover-art fetch in Step 4 can succeed.
        try:
            self._backfill_spotify_for_lb_track(track, session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spotify backfill error for track %d: %s", track.id, exc)

        # ── Step 1: read yt-dlp embedded tags as fallback baseline ────────────
        ytdlp_tags = self._read_embedded_tags(file_path)

        # ── Step 1b: AcoustID fingerprint (populates track.acoustid_id and
        #             track.mb_recording_id before the MusicBrainz lookup) ────
        if ACOUSTID_AVAILABLE and self._acoustid_key:
            self._fingerprint(file_path, track=track, session=session)

        # ── Step 2: attempt MusicBrainz lookup ────────────────────────────────
        mb_data: Optional[MBData] = None
        if self._rl.is_healthy("musicbrainz"):
            try:
                mb_data = self._fetch_musicbrainz(track)
            except MusicBrainzError as exc:
                logger.warning("MusicBrainz lookup failed for %r: %s", track.title, exc)
                self._rl.record_failure("musicbrainz")
            else:
                if mb_data:
                    self._rl.record_success("musicbrainz")
                    result.mb_recording_id = mb_data.recording_id
                    result.mb_release_id   = mb_data.release_id

        # ── Step 3: resolve each field with priority chain ────────────────────
        tags = TagData()

        tags.title, result.title_source = self._resolve(
            spotify=track.title,
            mb=mb_data.title if mb_data else None,
            embed=ytdlp_tags.get("title"),
        )

        tags.artist, result.artist_source = self._resolve(
            spotify=track.artist,
            mb=mb_data.artist if mb_data else None,
            embed=ytdlp_tags.get("artist"),
        )

        tags.album, result.album_source = self._resolve(
            spotify=track.album,
            mb=mb_data.album if mb_data else None,
            embed=ytdlp_tags.get("album"),
        )

        tags.year, result.year_source = self._resolve(
            spotify=track.year,
            mb=mb_data.year if mb_data else None,
            embed=None,  # year omitted from yt-dlp fallback per spec
        )

        raw_track_num, result.track_number_source = self._resolve(
            spotify=track.track_number,
            mb=mb_data.track_number if mb_data else None,
            embed=None,  # track number omitted from yt-dlp fallback per spec
        )
        tags.track_number = int(raw_track_num) if raw_track_num is not None else None

        # Album artist — special rule (§7.3.1)
        tags.album_artist, result.album_artist_source = self._resolve_album_artist(
            track=track,
            mb_data=mb_data,
        )

        # ── Step 4: cover art ─────────────────────────────────────────────────
        tags.cover_art = self._fetch_cover_art(track, mb_data)
        if tags.cover_art:
            result.cover_art_source = (
                "spotify" if track.cover_art_url else "musicbrainz"
            )
        else:
            result.cover_art_source = "none"

        # ── Step 5: log [TAG_FALLBACK] if MusicBrainz was used ───────────────
        mb_fields = [
            (result.title_source,        "title"),
            (result.artist_source,       "artist"),
            (result.album_artist_source, "album_artist"),
            (result.album_source,        "album"),
            (result.year_source,         "year"),
            (result.track_number_source, "track_number"),
            (result.cover_art_source,    "cover_art"),
        ]
        for source, fname in mb_fields:
            if source == "musicbrainz":
                result.mb_used = True
                _error_logger.warning(
                    "[TAG_FALLBACK] %s | %s | field=%s | source=musicbrainz",
                    track.title,
                    track.artist,
                    fname,
                )

        # ── Step 6: write tags to file ────────────────────────────────────────
        try:
            self._write_tags(file_path, tags)
        except Exception as exc:
            raise TaggingError(f"Failed to write tags to {file_path!r}: {exc}") from exc

        # ── Step 7: update DB ─────────────────────────────────────────────────
        self._update_db(session, track, result)

        return result

    # ── MusicBrainz lookup ─────────────────────────────────────────────────────

    def _fetch_musicbrainz(self, track: Track) -> Optional[MBData]:
        """
        Lookup priority: ISRC → AcoustID fingerprint → title+artist text search.

        Returns MBData on success, None if nothing found.
        Raises MusicBrainzError on HTTP/network errors.
        """
        # 1. ISRC lookup
        if track.isrc:
            mb = self._mb_lookup_isrc(track.isrc)
            if mb:
                return mb

        # 2. MB recording ID from AcoustID fingerprint (set by _fingerprint()
        #    before this call). acoustid_id is the AcoustID ID — NOT a MB MBID.
        #    mb_recording_id is the actual MB recording UUID.
        if track.mb_recording_id:
            mb = self._mb_lookup_recording(track.mb_recording_id)
            if mb:
                return mb

        # 3. Title + artist text search
        if track.title and track.artist:
            mb = self._mb_search(track.title, track.artist)
            if mb:
                return mb

        return None

    def _mb_lookup_isrc(self, isrc: str) -> Optional[MBData]:
        """GET /ws/2/isrc/{isrc}?inc=releases&fmt=json"""
        self._rl.wait("musicbrainz", attempt=0)
        url = f"{MB_WS2_BASE}/isrc/{isrc}"
        try:
            resp = self._mb_session.get(url, params={"inc": "releases", "fmt": "json"}, timeout=10)
        except requests.RequestException as exc:
            raise MusicBrainzError(f"ISRC lookup network error: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MusicBrainzError(f"ISRC lookup HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        # MusicBrainz /isrc/{isrc} returns {"isrc": "...", "recordings": [...]}
        # data["isrc"] is the ISRC STRING — calling .get() on it crashes.
        # Use data["recordings"] directly.
        recordings = data.get("recordings", [])
        if not recordings:
            return None

        rec = recordings[0]
        return self._parse_recording(rec)

    def _mb_lookup_recording(self, recording_id: str) -> Optional[MBData]:
        """GET /ws/2/recording/{id}?inc=releases+artists&fmt=json"""
        self._rl.wait("musicbrainz", attempt=0)
        url = f"{MB_WS2_BASE}/recording/{recording_id}"
        try:
            resp = self._mb_session.get(
                url,
                params={"inc": "releases artists", "fmt": "json"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise MusicBrainzError(f"Recording lookup network error: {exc}") from exc

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MusicBrainzError(f"Recording lookup HTTP {resp.status_code}: {resp.text[:200]}")

        return self._parse_recording(resp.json())

    def _mb_search(self, title: str, artist: str) -> Optional[MBData]:
        """GET /ws/2/recording?query=...&fmt=json — title+artist text search."""
        self._rl.wait("musicbrainz", attempt=0)
        query = f'recording:"{title}" AND artist:"{artist}"'
        url = f"{MB_WS2_BASE}/recording"
        try:
            resp = self._mb_session.get(
                url,
                params={"query": query, "limit": 1, "fmt": "json"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise MusicBrainzError(f"Search network error: {exc}") from exc

        if resp.status_code != 200:
            raise MusicBrainzError(f"Search HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        recordings = data.get("recordings", [])
        if not recordings:
            return None

        return self._parse_recording(recordings[0])

    def _parse_recording(self, rec: dict) -> MBData:
        """Extract MBData from a MusicBrainz recording JSON object."""
        # Add type checking to prevent errors
        if not isinstance(rec, dict):
            logger.warning("Invalid recording data type: %s, expected dict", type(rec))
            logger.debug("Recording data: %s", rec)
            return MBData()

        mb = MBData()
        mb.recording_id = rec.get("id")
        mb.title = rec.get("title")

        # Artist credit
        artist_credits = rec.get("artist-credit", [])
        if artist_credits:
            first = artist_credits[0]
            if isinstance(first, dict):
                mb.artist = first.get("name") or (first.get("artist") or {}).get("name")
            elif isinstance(first, str):
                # Some MusicBrainz responses return artist-credit as string
                mb.artist = first
            else:
                logger.warning("Unexpected artist-credit type: %s", type(first))

        # First release
        releases = rec.get("releases", [])
        if releases:
            # Add type check for release
            first_release = releases[0]
            if not isinstance(first_release, dict):
                logger.warning("Invalid release data type: %s, expected dict", type(first_release))
                logger.debug("Release data: %s", first_release)
            else:
                rel = first_release
                mb.release_id = rel.get("id")
                mb.album = rel.get("title")

                # Year from release date
                date_str = rel.get("date", "")
                if date_str:
                    mb.year = date_str[:4]

                # Track number from media
                media = rel.get("media", [])
                if media:
                    first_media = media[0]
                    if isinstance(first_media, dict):
                        tracks = first_media.get("tracks", [])
                        if tracks:
                            pos = tracks[0].get("position") or tracks[0].get("number")
                            try:
                                mb.track_number = int(pos)
                            except (TypeError, ValueError):
                                pass
                    else:
                        logger.warning("Invalid media data type: %s, expected dict", type(first_media))

        return mb

    # ── AcoustID fingerprinting ────────────────────────────────────────────────

    def _fingerprint(self, file_path: str, track: Optional[Track] = None,
                     session: Optional[Session] = None) -> Optional[str]:
        """
        Generate an AcoustID fingerprint for *file_path* using pyacoustid.

        Calls the AcoustID lookup API and returns the best-match
        MusicBrainz recording ID (or None if no match).

        Side-effects: if *track* and *session* are provided, updates
        ``track.acoustid_id`` and ``track.mb_recording_id`` in the DB.
        """
        if not ACOUSTID_AVAILABLE:
            logger.warning("pyacoustid not installed; skipping fingerprint")
            return None

        if not self._acoustid_key:
            logger.warning("ACOUSTID_API_KEY not set; skipping fingerprint")
            return None

        try:
            duration, fp_data = acoustid.fingerprint_file(file_path)
        except Exception as exc:
            logger.warning("Fingerprinting failed for %r: %s", file_path, exc)
            return None

        self._rl.wait("acoustid", attempt=0)
        try:
            results = acoustid.lookup(self._acoustid_key, fp_data, duration,
                                      meta="recordings")
        except acoustid.WebServiceError as exc:
            logger.warning("AcoustID lookup failed: %s", exc)
            self._rl.record_failure("acoustid")
            return None
        except Exception as exc:
            logger.warning("AcoustID lookup error: %s", exc)
            self._rl.record_failure("acoustid")
            return None

        self._rl.record_success("acoustid")

        best_recording_id: Optional[str] = None
        best_acoustid: Optional[str] = None

        # acoustid.lookup() returns the raw API JSON: {"status": "ok", "results": [...]}.
        # Iterating the dict directly yields its KEYS as strings (a footgun). Always
        # drill into the "results" list, and accept either a list-of-results or that
        # raw envelope so future pyacoustid behaviour shifts don't break us again.
        if isinstance(results, dict):
            results_list = results.get("results", []) or []
        elif isinstance(results, list):
            results_list = results
        else:
            results_list = []

        for result in results_list:
            if not isinstance(result, dict):
                continue
            score = result.get("score", 0)
            if score < 0.5:
                continue
            aid = result.get("id")
            recordings = result.get("recordings", [])
            if recordings and isinstance(recordings[0], dict):
                best_acoustid = aid
                best_recording_id = recordings[0].get("id")
                break

        if track is not None and session is not None:
            if best_acoustid:
                track.acoustid_id = best_acoustid
            if best_recording_id:
                track.mb_recording_id = best_recording_id
            try:
                session.flush()
            except Exception as exc:
                logger.warning("DB flush after fingerprint failed: %s", exc)

        return best_recording_id

    # ── Cover art ──────────────────────────────────────────────────────────────

    def _fetch_cover_art(
        self,
        track: Track,
        mb_data: Optional[MBData],
    ) -> Optional[bytes]:
        """
        Fetch cover art bytes.

        Priority:
          1. Spotify album image URL (track.cover_art_url)
          2. Cover Art Archive /release/{mb_release_id}/front-250

        SSRF guard: any URL must use https and resolve to a public IP that
        is NOT in any private/loopback/link-local range. Both Spotify and
        Cover Art Archive serve from public CDNs so legitimate fetches are
        unaffected; SSRF attempts pointed at 127.0.0.1, 169.254.169.254
        (cloud metadata), or RFC1918 hosts are rejected before the GET.
        """
        # 1. Spotify URL
        if track.cover_art_url:
            try:
                if _ssrf_safe(track.cover_art_url):
                    resp = requests.get(track.cover_art_url, timeout=15, allow_redirects=False)
                    if resp.status_code == 200 and resp.content:
                        return resp.content
                else:
                    logger.warning(
                        "Refusing to fetch cover art from %r — SSRF guard rejected URL",
                        track.cover_art_url,
                    )
            except requests.RequestException as exc:
                logger.debug("Spotify cover art fetch failed: %s", exc)

        # 2. Cover Art Archive
        release_id = (mb_data.release_id if mb_data else None) or track.mb_release_id
        if release_id:
            self._rl.wait("coverart", attempt=0)
            url = f"{CAA_BASE}/release/{release_id}/front-250"
            try:
                # CAA-internal redirects to archive.org are expected and
                # safe; we still validate that each redirect target survives
                # the SSRF guard.
                resp = _ssrf_safe_get(url, timeout=15, max_redirects=5)
                if resp is not None and resp.status_code == 200 and resp.content:
                    return resp.content
            except requests.RequestException as exc:
                logger.debug("Cover Art Archive fetch failed: %s", exc)

        return None

    # ── Tag writing ────────────────────────────────────────────────────────────

    def _write_tags(self, file_path: str, tags: TagData) -> None:
        """Dispatch to the correct format handler based on file extension."""
        ext = Path(file_path).suffix.lower()
        if ext == ".mp3":
            self._tag_mp3(file_path, tags)
        elif ext == ".flac":
            self._tag_flac(file_path, tags)
        elif ext in (".m4a", ".mp4", ".aac"):
            self._tag_m4a(file_path, tags)
        elif ext == ".ogg":
            self._tag_ogg(file_path, tags)
        elif ext == ".opus":
            self._tag_opus(file_path, tags)
        else:
            logger.warning("Unsupported format %r; skipping tag write", ext)

    def _tag_mp3(self, file_path: str, tags: TagData) -> None:
        """Write ID3 tags to an MP3 file using mutagen."""
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()

        if tags.title:
            audio["TIT2"] = TIT2(encoding=3, text=tags.title)
        if tags.artist:
            # Split ", "-joined multi-artist string into separate ID3v2.4 values
            # so Plex links each artist individually. Single artists unaffected.
            artists = [a.strip() for a in tags.artist.split(", ") if a.strip()]
            audio["TPE1"] = TPE1(encoding=3, text=artists)

        # TPE2/ALBUMARTIST is NEVER empty — falls back to TPE1 at minimum
        album_artist = tags.album_artist or tags.artist or ""
        audio["TPE2"] = TPE2(encoding=3, text=album_artist)

        # TCMP=1 marks compilation albums so Plex groups them correctly and
        # still shows individual track artists instead of "Various Artists".
        if album_artist.lower() == "various artists":
            audio["TCMP"] = TCMP(encoding=3, text="1")

        if tags.album:
            audio["TALB"] = TALB(encoding=3, text=tags.album)
        if tags.year:
            audio["TDRC"] = TDRC(encoding=3, text=str(tags.year))
        if tags.track_number is not None:
            audio["TRCK"] = TRCK(encoding=3, text=str(tags.track_number))
        if tags.cover_art:
            audio["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,   # Cover (front)
                desc="Cover",
                data=tags.cover_art,
            )

        _atomic_mutagen_save(audio, file_path)

    def _tag_flac(self, file_path: str, tags: TagData) -> None:
        """Write Vorbis comment tags to a FLAC file using mutagen."""
        audio = FLAC(file_path)

        if tags.title:
            audio["title"] = [tags.title]
        if tags.artist:
            # Multiple Vorbis artist values — one entry per artist
            artists = [a.strip() for a in tags.artist.split(", ") if a.strip()]
            audio["artist"] = artists

        # ALBUMARTIST is NEVER empty — falls back to ARTIST at minimum
        album_artist = tags.album_artist or tags.artist or ""
        audio["albumartist"] = [album_artist]

        # COMPILATION=1 tells Plex this is a Various Artists album
        if album_artist.lower() == "various artists":
            audio["compilation"] = ["1"]

        if tags.album:
            audio["album"] = [tags.album]
        if tags.year:
            audio["date"] = [str(tags.year)]
        if tags.track_number is not None:
            audio["tracknumber"] = [str(tags.track_number)]

        if tags.cover_art:
            pic = Picture()
            pic.type = 3  # Cover (front)
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = tags.cover_art
            audio.clear_pictures()
            audio.add_picture(pic)

        _atomic_mutagen_save(audio, file_path)

    def _tag_m4a(self, file_path: str, tags: TagData) -> None:
        """Write MP4 atom tags to an M4A/MP4 file using mutagen."""
        audio = MP4(file_path)

        if tags.title:
            audio["\xa9nam"] = [tags.title]
        if tags.artist:
            # Multiple MP4 artist values — one entry per artist
            artists = [a.strip() for a in tags.artist.split(", ") if a.strip()]
            audio["\xa9ART"] = artists

        # aART (album artist) is NEVER empty — falls back to artist at minimum
        album_artist = tags.album_artist or tags.artist or ""
        audio["aART"] = [album_artist]

        # cpil=True marks compilation albums in iTunes/Plex for M4A files
        if album_artist.lower() == "various artists":
            audio["cpil"] = [True]

        if tags.album:
            audio["\xa9alb"] = [tags.album]
        if tags.year:
            audio["\xa9day"] = [str(tags.year)]
        if tags.track_number is not None:
            audio["trkn"] = [(tags.track_number, 0)]

        if tags.cover_art:
            audio["covr"] = [MP4Cover(tags.cover_art, imageformat=MP4Cover.FORMAT_JPEG)]

        _atomic_mutagen_save(audio, file_path)

    def _tag_ogg(self, file_path: str, tags: TagData) -> None:
        """Write Vorbis comment tags to an OGG Vorbis file using mutagen."""
        audio = OggVorbis(file_path)

        if tags.title:
            audio["title"] = [tags.title]
        if tags.artist:
            audio["artist"] = [tags.artist]

        album_artist = tags.album_artist or tags.artist or ""
        audio["albumartist"] = [album_artist]

        if tags.album:
            audio["album"] = [tags.album]
        if tags.year:
            audio["date"] = [str(tags.year)]
        if tags.track_number is not None:
            audio["tracknumber"] = [str(tags.track_number)]

        _atomic_mutagen_save(audio, file_path)

    def _tag_opus(self, file_path: str, tags: TagData) -> None:
        """Write Vorbis comment tags to an Opus file using mutagen."""
        audio = OggOpus(file_path)

        if tags.title:
            audio["title"] = [tags.title]
        if tags.artist:
            audio["artist"] = [tags.artist]

        album_artist = tags.album_artist or tags.artist or ""
        audio["albumartist"] = [album_artist]

        if tags.album:
            audio["album"] = [tags.album]
        if tags.year:
            audio["date"] = [str(tags.year)]
        if tags.track_number is not None:
            audio["tracknumber"] = [str(tags.track_number)]

        _atomic_mutagen_save(audio, file_path)

    # ── Album artist rule (§7.3.1) ─────────────────────────────────────────────

    def _album_artist_rule(self, track: Track) -> str:
        """
        Return 'Various Artists' for compilations; otherwise return track.artist.

        A track is considered a compilation if its album_artist field is already
        set to 'Various Artists' (populated by the scraper).
        """
        if track.album_artist and track.album_artist.lower() == "various artists":
            return "Various Artists"
        return track.artist or ""

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _resolve(
        self,
        spotify,
        mb,
        embed,
    ):
        """
        Return (value, source_label) using priority: spotify → musicbrainz → embed.
        Returns (None, 'none') if all sources are empty.
        """
        if spotify is not None and str(spotify).strip():
            return spotify, "spotify"
        if mb is not None and str(mb).strip():
            return mb, "musicbrainz"
        if embed is not None and str(embed).strip():
            return embed, "embed"
        return None, "none"

    def _resolve_album_artist(
        self,
        track: Track,
        mb_data: Optional[MBData],
    ):
        """
        Resolve album artist with the §7.3.1 rule applied first.

        Priority:
          1. §7.3.1 rule applied to Spotify data (track.album_artist)
          2. MusicBrainz artist (used as album artist)
          3. Copy of track.artist (TPE1 fallback — never empty)
        """
        # Source 1: Spotify-derived album artist via §7.3.1 rule
        rule_result = self._album_artist_rule(track)
        if rule_result:
            return rule_result, "spotify"

        # Source 2: MusicBrainz artist as album artist
        if mb_data and mb_data.artist:
            return mb_data.artist, "musicbrainz"

        # Source 3: copy of TPE1 — NEVER empty
        fallback = track.artist or ""
        return fallback, "artist_fallback"

    def _read_embedded_tags(self, file_path: str) -> dict:
        """
        Read existing embedded tags from the file as a fallback source.

        Returns a dict with keys: title, artist, album (all Optional[str]).
        """
        tags: dict = {}
        ext = Path(file_path).suffix.lower()
        try:
            if ext == ".mp3":
                audio = ID3(file_path)
                tags["title"]  = str(audio["TIT2"]) if "TIT2" in audio else None
                tags["artist"] = str(audio["TPE1"]) if "TPE1" in audio else None
                tags["album"]  = str(audio["TALB"]) if "TALB" in audio else None
            elif ext == ".flac":
                audio = FLAC(file_path)
                tags["title"]  = audio.get("title",  [None])[0]
                tags["artist"] = audio.get("artist", [None])[0]
                tags["album"]  = audio.get("album",  [None])[0]
            elif ext in (".m4a", ".mp4", ".aac"):
                audio = MP4(file_path)
                tags["title"]  = (audio.get("\xa9nam") or [None])[0]
                tags["artist"] = (audio.get("\xa9ART") or [None])[0]
                tags["album"]  = (audio.get("\xa9alb") or [None])[0]
        except Exception as exc:
            logger.debug("Could not read embedded tags from %r: %s", file_path, exc)
        return tags

    def _update_db(
        self,
        session: Session,
        track: Track,
        result: TagResult,
    ) -> None:
        """
        Persist MusicBrainz / AcoustID identifiers and cover_art_source to DB.

        Updates:
          - mb_recording_id
          - mb_release_id
          - acoustid_id
          - cover_art_source  ('spotify' | 'musicbrainz' | 'none')
        """
        if result.mb_recording_id:
            track.mb_recording_id = result.mb_recording_id
        if result.mb_release_id:
            track.mb_release_id = result.mb_release_id
        if result.acoustid_id:
            track.acoustid_id = result.acoustid_id

        track.cover_art_source = result.cover_art_source

        try:
            session.flush()
        except Exception as exc:
            logger.warning("DB flush after tagging failed for track %d: %s", track.id, exc)
