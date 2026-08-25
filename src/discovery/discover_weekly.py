"""discovery/discover_weekly.py — ListenBrainz weekly-playlist discovery.

SPEC.md §W3 T21–T23. Fetches the user's troi-generated weekly playlists
(Weekly Jams / Weekly Exploration) from the ListenBrainz playlist API,
resolves every entry against the local library (MusicBrainz recording MBID
first, then artist+title+duration±5s fuzzy), auto-queues missing tracks
through the normal 5-tier downloader, and exports each resolved playlist as
a portable .m3u (§W3 T15/V8).

This is what Navidrome's LB plugin cannot do: it only resolves in-library
tracks, while we ACQUIRE the missing ones.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

from src.core import config
from src.models import LbRecommendation, Track, TrackStatus
from src.rate_limiter import ServiceRateLimiter

logger = logging.getLogger(__name__)

_LB_BASE = "https://api.listenbrainz.org"
_MBID_URI_RE = re.compile(r"musicbrainz\.org/recording/([0-9a-f-]{36})", re.I)

WEEKLY_KINDS = {
    "weekly jams": "weekly_jams",
    "weekly exploration": "weekly_exploration",
    "weekly discovery": "weekly_exploration",  # older troi naming
}

DURATION_TOLERANCE_MS = 5000


def _norm(s: str) -> str:
    """Casefold + strip accents/punctuation for fuzzy matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class DiscoverWeekly:
    """Fetch → resolve → queue → export pipeline for LB weekly playlists."""

    def __init__(
        self,
        token: Optional[str] = None,
        username: Optional[str] = None,
        rate_limiter: Optional[ServiceRateLimiter] = None,
        http_session: Optional[requests.Session] = None,
    ) -> None:
        self._token = token or os.environ.get("LISTENBRAINZ_TOKEN", "")
        self._username = username or os.environ.get("LISTENBRAINZ_USERNAME", "")
        self._rl = rate_limiter or ServiceRateLimiter()
        self._http = http_session or requests.Session()
        if self._token:
            self._http.headers.update({"Authorization": f"Token {self._token}"})

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, session: Session) -> dict:
        """Process all available weekly playlists. Returns a summary dict."""
        if not self._username:
            raise RuntimeError("LISTENBRAINZ_USERNAME is not set")

        playlists = self.find_weekly_playlists()
        results = []
        m3u_paths = []

        for meta in playlists:
            summary = self.process_playlist(session, meta)
            results.append(summary)
            if summary.get("m3u_path"):
                m3u_paths.append(summary["m3u_path"])

        session.commit()
        return {"playlists": results, "m3u_paths": m3u_paths}

    # ── Playlist discovery ────────────────────────────────────────────────────

    def find_weekly_playlists(self) -> list[dict]:
        """Return metadata for the latest weekly-jams/exploration playlists.

        troi-bot delivers to TWO feeds: the user's own playlist list AND the
        'Created for You' feed (/playlists/createdfor). Scan both, newest-first,
        dedupe by kind.
        """
        feeds = [
            f"{_LB_BASE}/1/user/{self._username}/playlists",
            f"{_LB_BASE}/1/user/{self._username}/playlists/createdfor",
        ]

        found: dict[str, dict] = {}
        for feed_url in feeds:
            resp = self._get(feed_url)
            items = resp.get("payload", {}).get("playlists", []) or []
            for item in items:
                title = (item.get("playlist") or {}).get("title") or item.get("title") or ""
                matched_kind = self._match_weekly(title)
                if not matched_kind or matched_kind in found:
                    continue
                mbid = self._mbid_from_identifier(
                    item.get("identifier") or item.get("mbid") or ""
                )
                if mbid:
                    found[matched_kind] = {
                        "kind": matched_kind,
                        "title": title,
                        "mbid": mbid,
                        "feed": "createdfor" if "createdfor" in feed_url else "own",
                    }

        out = list(found.values())
        logger.info("Found %d weekly playlist(s): %s", len(out), [p["title"] for p in out])
        return out

    def fetch_jspf(self, playlist_mbid: str) -> dict:
        url = f"{_LB_BASE}/1/playlist/{playlist_mbid}"
        resp = self._get(url)
        return resp.get("playlist", {})

    # ── Core pipeline ─────────────────────────────────────────────────────────

    def process_playlist(self, session: Session, meta: dict) -> dict:
        jspf = self.fetch_jspf(meta["mbid"])
        title = jspf.get("title") or meta["title"]
        tracks = jspf.get("track", []) or []

        resolved_members = []   # (track, entry) already on disk
        queued_missing = 0
        unresolved_failures = 0

        from src.discovery.listenbrainz import ListenBrainzDiscovery
        ingester = ListenBrainzDiscovery()

        for entry in tracks:
            mbid, etitle, eartist, edur = self._parse_track(entry)

            local = self._resolve_local(session, mbid, etitle, eartist, edur)

            if local is not None:
                resolved_members.append((local, entry))
                continue

            # Not in library at all → create synthetic pending Track via the
            # existing CF ingestion machinery (uri `mb:{mbid}`, V9-idempotent).
            rec = {
                "recording_mbid": mbid,
                "recording_name": etitle,
                "artist_name": eartist,
            }
            try:
                created = ingester._ingest_recommendation(rec, session, kind=meta["kind"])
            except Exception as exc:
                logger.warning("Weekly ingest failed for MBID %s: %s", mbid, exc)
                created = False

            if created:
                queued_missing += 1
            elif not etitle or not eartist:
                unresolved_failures += 1
            else:
                # Track row existed but wasn't a download-ready hit (e.g.
                # still pending from an earlier run) — count as member-pending.
                pending = self._find_any_track(session, mbid, etitle, eartist, edur)
                if pending is not None:
                    resolved_members.append((pending, entry))
                else:
                    unresolved_failures += 1

        # Export portable m3u of everything already on disk (V8).
        m3u_path = None
        members_on_disk = [
            (t.file_path, t.artist, t.title, t.duration_ms)
            for t, _e in resolved_members
            if t.file_path and t.status == TrackStatus.DOWNLOADED.value
        ]
        if members_on_disk:
            from src.discovery.m3u_export import export_playlist
            out = export_playlist(title, members_on_disk)
            m3u_path = str(out) if out else None

        summary = {
            "name": title,
            "entries": len(tracks),
            "resolved_local": len(resolved_members),
            "queued_missing": queued_missing,
            "unresolved": unresolved_failures,
            "m3u_path": m3u_path,
        }
        logger.info("[DISCOVER_WEEKLY] %s", summary)
        return summary

    # ── Resolution ────────────────────────────────────────────────────────────

    def _resolve_local(
        self, session: Session, mbid: str, title: str, artist: str, duration_ms: Optional[int]
    ) -> Optional[Track]:
        """Download-ready local hit? MBID exact first, then fuzzy ±5s (V7: skip blocked)."""
        if mbid:
            t = (
                session.query(Track)
                .filter(
                    Track.mb_recording_id == mbid,
                    Track.status == TrackStatus.DOWNLOADED.value,
                    Track.file_path.isnot(None),
                    Track.blocked.is_(False),
                )
                .first()
            )
            if t is not None:
                return t

        return self._fuzzy_match(session, title, artist, duration_ms)

    def _fuzzy_match(
        self, session: Session, title: str, artist: str, duration_ms: Optional[int]
    ) -> Optional[Track]:
        if not title or not artist:
            return None

        candidates = (
            session.query(Track)
            .filter(
                Track.status == TrackStatus.DOWNLOADED.value,
                Track.file_path.isnot(None),
                Track.blocked.is_(False),
            )
            .yield_per(500)
        )
        n_title = _norm(title)
        n_artist = _norm(artist)

        for t in candidates:
            if _norm(t.title) != n_title or _norm(t.artist) != n_artist:
                continue
            if duration_ms and t.duration_ms:
                if abs(t.duration_ms - duration_ms) > DURATION_TOLERANCE_MS:
                    continue
            logger.info(
                "[DISCOVER_WEEKLY] fuzzy-resolved '%s' by '%s' → track %d",
                title, artist, t.id,
            )
            return t
        return None

    def _find_any_track(
        self, session: Session, mbid: str, title: str, artist: str, duration_ms: Optional[int]
    ) -> Optional[Track]:
        if mbid:
            t = session.query(Track).filter(Track.mb_recording_id == mbid).first()
            if t is not None and not t.blocked:
                return t
        return self._fuzzy_pending(session, title, artist, duration_ms)

    def _fuzzy_pending(
        self, session: Session, title: str, artist: str, duration_ms: Optional[int]
    ) -> Optional[Track]:
        """Any-status fuzzy match (used to avoid duplicate synthetic rows)."""
        if not title or not artist:
            return None
        n_title, n_artist = _norm(title), _norm(artist)
        for t in session.query(Track).yield_per(500):
            if t.blocked:
                continue
            if _norm(t.title) == n_title and _norm(t.artist) == n_artist:
                return t
        return None

    # ── Parsing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_track(entry: dict) -> tuple[str, str, str, Optional[int]]:
        """JSPF track → (recording_mbid, title, artist, duration_ms|None)."""
        identifier = entry.get("identifier")
        if isinstance(identifier, list):
            identifier = identifier[0] if identifier else ""
        m = _MBID_URI_RE.search(identifier or "")
        mbid = m.group(1) if m else ""

        title = entry.get("title") or ""
        creator = entry.get("creator") or ""

        ext = entry.get("extension") or {}
        lb_ext = next(
            (v for k, v in ext.items() if "listenbrainz" in k.lower()), {}
        )
        artists = lb_ext.get("artists") or []
        if not creator and artists:
            name = (artists[0].get("artist") or {}).get("name")
            creator = name or ""

        duration = entry.get("duration")
        duration_ms = int(duration) if duration else None

        return mbid, title, creator, duration_ms

    @staticmethod
    def _match_weekly(title: str) -> Optional[str]:
        low = (title or "").lower()
        for needle, kind in WEEKLY_KINDS.items():
            if needle in low:
                return kind
        return None

    @staticmethod
    def _mbid_from_identifier(identifier: str) -> str:
        m = re.search(r"/playlist/([0-9a-f-]{36})", identifier or "", re.I)
        return m.group(1) if m else ""

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, url: str) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            if not self._rl.is_healthy("listenbrainz"):
                break
            try:
                resp = self._http.get(url, timeout=30)
            except requests.RequestException as exc:
                last_exc = exc
                self._rl.record_failure("listenbrainz")
                continue

            if resp.status_code == 200:
                self._rl.record_success("listenbrainz")
                return resp.json()

            self._rl.record_failure("listenbrainz")
            if resp.status_code == 429:
                import time as _time
                retry_after = float(resp.headers.get("Retry-After", 5))
                _time.sleep(min(retry_after, 10))
                continue

            raise RuntimeError(f"ListenBrainz HTTP {resp.status_code} for {url}")

        raise RuntimeError(f"ListenBrainz unreachable after retries: {last_exc}")
