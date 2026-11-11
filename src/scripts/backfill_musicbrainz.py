"""Backfill MusicBrainz IDs for an explicit, operator-supplied set of tracks.

This does not retag audio files or alter delivery state. It only fills missing
``mb_recording_id`` and ``mb_release_id`` values after a transient upstream
failure. The existing tagger limiter is used for every request; an extra second
between tracks leaves headroom for the running daemon.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from collections import Counter

from sqlalchemy import or_, select

from src.db import get_session, init_db
from src.exceptions import MusicBrainzError
from src.ingestion.tagger import MetadataTagger
from src.models import Track

logger = logging.getLogger(__name__)
_ISRC_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\\d{7}", re.IGNORECASE)


def enrich_track(tagger: MetadataTagger, track: Track, *, apply: bool) -> str:
    """Resolve one track and optionally persist only its missing MB identifiers."""
    if not tagger._rl.is_healthy("musicbrainz"):
        return "circuit_open"
    try:
        if track.isrc and not _ISRC_RE.fullmatch(track.isrc):
            logger.warning("Skipping invalid ISRC for track %d: %r", track.id, track.isrc)
            metadata = tagger._mb_search(track.title, track.artist)
        else:
            metadata = tagger._fetch_musicbrainz(track)
    except MusicBrainzError as exc:
        logger.warning("MusicBrainz backfill failed for track %d: %s", track.id, exc)
        tagger._rl.record_failure("musicbrainz")
        return "failed"

    tagger._rl.record_success("musicbrainz")
    if metadata is None:
        return "no_match"

    changed = False
    if metadata.recording_id and not track.mb_recording_id:
        if apply:
            track.mb_recording_id = metadata.recording_id
        changed = True
    if metadata.release_id and not track.mb_release_id:
        if apply:
            track.mb_release_id = metadata.release_id
        changed = True
    return "enriched" if changed else "already_complete"


def _track_ids(value: str) -> list[int]:
    try:
        track_ids = [int(track_id) for track_id in value.split(",") if track_id]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--track-ids must be comma-separated integers") from exc
    if not track_ids or any(track_id <= 0 for track_id in track_ids):
        raise argparse.ArgumentTypeError("--track-ids must contain positive integers")
    return list(dict.fromkeys(track_ids))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track-ids", required=True, type=_track_ids)
    parser.add_argument("--apply", action="store_true", help="persist resolved IDs; default is probe-only")
    args = parser.parse_args()

    init_db()
    tagger = MetadataTagger()
    outcomes: Counter[str] = Counter()
    with get_session() as session:
        tracks = session.scalars(
            select(Track)
            .where(Track.id.in_(args.track_ids))
            .where(or_(Track.mb_recording_id.is_(None), Track.mb_release_id.is_(None)))
            .order_by(Track.id)
        ).all()
        outcomes["already_complete"] = len(args.track_ids) - len(tracks)
        for track in tracks:
            outcome = enrich_track(tagger, track, apply=args.apply)
            outcomes[outcome] += 1
            if outcome == "circuit_open":
                break
            if args.apply and outcome == "enriched":
                session.commit()
            time.sleep(1)

    print("MusicBrainz backfill:", " ".join(f"{key}={value}" for key, value in sorted(outcomes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
