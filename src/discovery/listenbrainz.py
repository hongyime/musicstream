"""
discovery/listenbrainz.py — ListenBrainz Collaborative Filtering recommendation fetcher

Fetches personalised recording recommendations from the ListenBrainz CF API,
resolves each MBID against MusicBrainz WS2, and inserts new tracks into the
database with a synthetic spotify_uri of the form ``mb:{recording_mbid}``.

Behaviour:
  - First run (lb_recommendations table empty): backfill 200 recommendations
  - Subsequent runs: daily poll, fetch 100 recommendations
  - Already-present recording_mbid values are skipped (status → 'skipped')
  - Successfully ingested rows get status → 'ingested'
  - Rows that fail MusicBrainz resolution get status → 'failed'

Rate limiting:
  ListenBrainz — ServiceRateLimiter "listenbrainz"
  MusicBrainz  — ServiceRateLimiter "musicbrainz" (strict 1 req/s)

Environment variables:
  LISTENBRAINZ_TOKEN    — Bearer token for ListenBrainz API auth
  LISTENBRAINZ_USERNAME — Username used in the recommendation URL
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

from src.exceptions import ListenBrainzError, MusicBrainzError
from src.models import LbRecommendation, Track, TrackStatus
from src.rate_limiter import ServiceRateLimiter

logger = logging.getLogger(__name__)

# ── Error logger (errors.log) ─────────────────────────────────────────────────

_error_logger = logging.getLogger("musicstream.errors")

# ── Constants ─────────────────────────────────────────────────────────────────

MB_USER_AGENT  = "musicstream/3.0.0 ( github.com/hongyime/musicstream )"
MB_WS2_BASE    = "https://musicbrainz.org/ws/2"

BACKFILL_COUNT = 200
POLL_COUNT     = 100


# ── Main discovery class ──────────────────────────────────────────────────────

class ListenBrainzDiscovery:
    """
    Fetches ListenBrainz CF recommendations and ingests them into the DB.

    Usage::

        discovery = ListenBrainzDiscovery()
        new_tracks = discovery.run(session)
    """

    API_URL = "https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording"

    def __init__(
        self,
        token: Optional[str] = None,
        username: Optional[str] = None,
        rate_limiter: Optional[ServiceRateLimiter] = None,
    ) -> None:
        self._token    = token    or os.environ.get("LISTENBRAINZ_TOKEN", "")
        self._username = username or os.environ.get("LISTENBRAINZ_USERNAME", "")
        self._rl       = rate_limiter or ServiceRateLimiter()

        # Shared HTTP session for ListenBrainz calls
        self._lb_session = requests.Session()
        if self._token:
            self._lb_session.headers.update({"Authorization": f"Token {self._token}"})

        # Shared HTTP session for MusicBrainz calls
        self._mb_session = requests.Session()
        self._mb_session.headers.update({"User-Agent": MB_USER_AGENT})

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self, session: Session) -> int:
        """
        Backfill 200 recommendations if the lb_recommendations table is empty;
        otherwise perform a daily poll of 100 recommendations.

        Returns the number of new tracks successfully added to the DB.
        """
        is_empty = session.query(LbRecommendation).first() is None
        count    = BACKFILL_COUNT if is_empty else POLL_COUNT

        logger.info(
            "ListenBrainz discovery: %s mode, fetching %d recommendations",
            "backfill" if is_empty else "poll",
            count,
        )

        try:
            recommendations = self._fetch_recommendations(count)
        except ListenBrainzError as exc:
            logger.error("Failed to fetch ListenBrainz recommendations: %s", exc)
            return 0

        new_tracks = 0
        for rec in recommendations:
            mbid = rec.get("recording_mbid") or rec.get("recording", {}).get("mbid")
            if not mbid:
                logger.debug("Skipping recommendation with no recording_mbid: %r", rec)
                continue

            # ── Skip already-present MBIDs ─────────────────────────────────
            existing = (
                session.query(LbRecommendation)
                .filter_by(recording_mbid=mbid)
                .first()
            )
            if existing:
                # Only skip if already successfully ingested - allow retry for pending/skipped/failed
                if existing.status == "ingested":
                    logger.debug("Skipping already-ingested MBID %s", mbid)
                    continue
                
                # Re-process pending/skipped/failed recommendations
                logger.info("Re-processing MBID %s (was %s)", mbid, existing.status)
                existing.status = "pending"  # Reset to pending for re-processing
                try:
                    session.flush()
                except Exception as exc:
                    logger.warning("DB flush failed resetting MBID %s status: %s", mbid, exc)
                continue

            # ── Ingest new recommendation ──────────────────────────────────
            try:
                ingested = self._ingest_recommendation(rec, session)
                if ingested:
                    new_tracks += 1
            except Exception as exc:
                logger.error("Unexpected error ingesting MBID %s: %s", mbid, exc)

        logger.info(
            "ListenBrainz discovery complete: %d new tracks added out of %d recommendations",
            new_tracks,
            len(recommendations),
        )
        return new_tracks

    # ── Internal: fetch recommendations ───────────────────────────────────────

    def _fetch_recommendations(self, count: int) -> list[dict]:
        """
        GET https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording
            ?count={count}&artist_type=top

        Returns a list of recommendation dicts.
        Raises ListenBrainzError on HTTP/network errors.
        """
        if not self._username:
            raise ListenBrainzError("LISTENBRAINZ_USERNAME is not set")

        url = self.API_URL.format(username=self._username)
        params = {"count": count, "artist_type": "top"}

        for attempt in range(3):
            if not self._rl.is_healthy("listenbrainz"):
                raise ListenBrainzError("ListenBrainz service is currently unhealthy (circuit breaker open)")

            if attempt > 0:
                self._rl.wait("listenbrainz", attempt=attempt)

            try:
                resp = self._lb_session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                self._rl.record_failure("listenbrainz")
                if attempt == 2:
                    raise ListenBrainzError(f"Network error fetching recommendations: {exc}") from exc
                logger.warning("ListenBrainz request failed (attempt %d/3): %s", attempt + 1, exc)
                continue

            if resp.status_code == 200:
                self._rl.record_success("listenbrainz")
                data = resp.json()
                # Response shape: {"payload": {"mbids": [...]} } or {"payload": {"recordings": [...]}}
                payload = data.get("payload", {})
                # Try both known response shapes
                recs = (
                    payload.get("mbids")
                    or payload.get("recordings")
                    or []
                )
                logger.debug("Fetched %d recommendations from ListenBrainz", len(recs))
                return recs

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 60))
                self._rl.record_failure("listenbrainz")
                logger.warning("ListenBrainz rate limited; retry-after=%.0fs", retry_after)
                self._rl.wait("listenbrainz", attempt=attempt, retry_after=retry_after)
                continue

            self._rl.record_failure("listenbrainz")
            raise ListenBrainzError(
                f"ListenBrainz API returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        raise ListenBrainzError("ListenBrainz API failed after 3 attempts")

    # ── Internal: fetch MusicBrainz metadata ──────────────────────────────────

    def _fetch_mb_metadata(self, recording_mbid: str) -> Optional[dict]:
        """
        GET https://musicbrainz.org/ws/2/recording/{mbid}?inc=releases+artists&fmt=json

        Returns the parsed JSON dict on success, None if not found.
        Raises MusicBrainzError on HTTP/network errors.
        """
        if not self._rl.is_healthy("musicbrainz"):
            logger.warning("MusicBrainz circuit breaker open; skipping metadata fetch for %s", recording_mbid)
            return None

        self._rl.wait("musicbrainz", attempt=0)

        url = f"{MB_WS2_BASE}/recording/{recording_mbid}"
        try:
            resp = self._mb_session.get(
                url,
                params={"inc": "releases artists", "fmt": "json"},
                timeout=15,
            )
        except requests.RequestException as exc:
            self._rl.record_failure("musicbrainz")
            raise MusicBrainzError(f"MusicBrainz network error for {recording_mbid}: {exc}") from exc

        if resp.status_code == 404:
            self._rl.record_success("musicbrainz")
            _error_logger.warning("[LB_MBID_MISS] mbid=%s | no MusicBrainz record found", recording_mbid)
            return None

        if resp.status_code != 200:
            self._rl.record_failure("musicbrainz")
            raise MusicBrainzError(
                f"MusicBrainz HTTP {resp.status_code} for {recording_mbid}: {resp.text[:200]}"
            )

        self._rl.record_success("musicbrainz")
        return resp.json()

    # ── Internal: ingest a single recommendation ───────────────────────────────

    def _ingest_recommendation(self, rec: dict, session: Session) -> bool:
        """
        Insert a new LbRecommendation row and a corresponding Track row.

        The Track is inserted with:
          - spotify_uri = "mb:{recording_mbid}"
          - status      = 'pending'
          - title/artist populated from MusicBrainz metadata (if available)

        Updates lb_recommendations.status to 'ingested' on success or 'failed'
        on error.

        Returns True if a new track was successfully created, False otherwise.
        """
        mbid  = rec.get("recording_mbid") or rec.get("recording", {}).get("mbid", "")
        score = rec.get("score") or rec.get("latest_listened_at")  # score field varies by API version

        # Normalise score to float if possible
        if isinstance(score, str):
            try:
                score = float(score)
            except (ValueError, TypeError):
                score = None
        elif not isinstance(score, (int, float)):
            score = None

        now = datetime.now(timezone.utc)

        # ── Fetch MusicBrainz metadata ─────────────────────────────────────
        mb_data: Optional[dict] = None
        try:
            mb_data = self._fetch_mb_metadata(mbid)
        except MusicBrainzError as exc:
            logger.warning("MusicBrainz fetch failed for MBID %s: %s", mbid, exc)

        # ── Extract title / artist from MB response ────────────────────────
        title:  Optional[str] = None
        artist: Optional[str] = None

        if mb_data:
            title = mb_data.get("title")
            artist_credits = mb_data.get("artist-credit", [])
            if artist_credits:
                first = artist_credits[0]
                if isinstance(first, dict):
                    artist = (
                        first.get("name")
                        or (first.get("artist") or {}).get("name")
                    )

        # Fall back to any name hints in the recommendation payload itself
        if not title:
            title = rec.get("recording_name") or rec.get("track_name")
        if not artist:
            artist = rec.get("artist_name") or rec.get("artist_credit_name")

        # ── Create LbRecommendation row ────────────────────────────────────
        lb_rec = LbRecommendation(
            recording_mbid=mbid,
            title=title,
            artist=artist,
            score=score,
            fetched_at=now,
            status="pending",
        )
        session.add(lb_rec)

        try:
            session.flush()  # get lb_rec.id without committing
        except Exception as exc:
            logger.error("Failed to insert LbRecommendation for MBID %s: %s", mbid, exc)
            session.rollback()
            return False

        # ── Bail out if we have no usable metadata ─────────────────────────
        if not title or not artist:
            logger.warning(
                "No usable metadata for MBID %s (title=%r, artist=%r); marking failed",
                mbid, title, artist,
            )
            lb_rec.status = "failed"
            try:
                session.flush()
            except Exception:
                pass
            return False

        # ── Create Track row ───────────────────────────────────────────────
        spotify_uri = f"mb:{mbid}"

        # Guard against duplicate spotify_uri (race condition / retry)
        existing_track = (
            session.query(Track)
            .filter_by(spotify_uri=spotify_uri)
            .first()
        )
        if existing_track:
            logger.debug("Track with spotify_uri=%r already exists; linking to LbRecommendation", spotify_uri)
            lb_rec.track_id = existing_track.id
            lb_rec.status   = "ingested"
            
            # Update existing track with MusicBrainz metadata if better than what we have
            if title and (not existing_track.title or len(title) > len(existing_track.title)):
                existing_track.title = title
            if artist and (not existing_track.artist or len(artist) > len(existing_track.artist)):
                existing_track.artist = artist
            # Always update MBID if we have it
            existing_track.mb_recording_id = mbid
            
            try:
                session.flush()
            except Exception as exc:
                logger.warning("DB flush failed linking existing track for MBID %s: %s", mbid, exc)
            return False  # not a *new* track

        track = Track(
            spotify_uri=spotify_uri,
            title=title,
            artist=artist,
            status=TrackStatus.PENDING.value,
            mb_recording_id=mbid,
            cover_art_source="none",
        )
        session.add(track)

        try:
            session.flush()  # get track.id
        except Exception as exc:
            logger.error("Failed to insert Track for MBID %s: %s", mbid, exc)
            lb_rec.status = "failed"
            try:
                session.flush()
            except Exception:
                pass
            return False

        # ── Link recommendation → track ────────────────────────────────────
        lb_rec.track_id = track.id
        lb_rec.status   = "ingested"

        try:
            session.flush()
        except Exception as exc:
            logger.warning("DB flush failed after linking track for MBID %s: %s", mbid, exc)

        logger.info(
            "Ingested LB recommendation: MBID=%s title=%r artist=%r track_id=%d",
            mbid, title, artist, track.id,
        )
        return True
