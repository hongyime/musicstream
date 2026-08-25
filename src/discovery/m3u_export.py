"""discovery/m3u_export.py — Portable .m3u playlist writer.

SPEC.md §W3 T15 / invariant V8: every playlist publish writes a UTF-8
#EXTM3U file BEFORE any (optional) Plex push, so playlists survive
independently of Plex and can be consumed by any player that reads m3u.

Public API:
    export_playlist(name, entries, export_dir=None) -> Path | None
        entries: sequence of (file_path, artist, title, duration_ms|None)
    export_weekly_discovery(session, export_dir=None) -> Path | None
    backfill_all_playlists(session, export_dir=None) -> list[Path]
"""

from __future__ import annotations

import logging
import re
from datetime import datetime as dt, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.core import config

logger = logging.getLogger(__name__)

# Windows-illegal filename characters + control chars.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_playlist_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned or "playlist"


def _to_host_path(path_str: str) -> str:
    """Translate container paths (/media/...) to host paths (Y:/music/...).

    m3u files are consumed by players running on the HOST, but the DB stores
    the container-side paths the downloader wrote. Mapping comes from
    MEDIA_DIR (container root) + EXTERNAL_MEDIA_DRIVE (host root).
    """
    # str(Path('/media')) on Windows yields '\media' — normalize separators
    # on BOTH sides or the prefix match silently never fires.
    media_root = str(config.MEDIA_DIR).replace("\\", "/").rstrip("/")
    normalized = path_str.replace("\\", "/")
    drive = config.EXTERNAL_MEDIA_DRIVE
    if drive and normalized.startswith(media_root + "/"):
        return drive.rstrip("/\\") + normalized[len(media_root):]
    return path_str

def _resolve_dir(export_dir: Optional[str]) -> Optional[Path]:
    if export_dir:
        return Path(export_dir)
    if config.PLAYLISTS_EXPORT_DIR:
        return Path(config.PLAYLISTS_EXPORT_DIR)
    return None


def export_playlist(
    name: str,
    entries,
    export_dir: Optional[str] = None,
) -> Optional[Path]:
    """Write ``<name>.m3u`` and return its path, or None when disabled.

    Entries are ``(file_path, artist, title, duration_ms_or_None)`` tuples.
    Paths are translated container→host via _to_host_path before writing.
    """
    target_dir = _resolve_dir(export_dir)
    if target_dir is None:
        logger.info("PLAYLISTS_EXPORT_DIR unset — skipping m3u export for %r", name)
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{sanitize_playlist_name(name)}.m3u"

    lines = ["#EXTM3U"]
    for path, artist, title, duration_ms in entries:
        duration_seconds = int(duration_ms // 1000) if duration_ms else -1
        lines.append(f"#EXTINF:{duration_seconds},{artist} - {title}")
        lines.append(_to_host_path(str(path)))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Exported m3u %s (%d tracks)", out, len(entries))
    return out


def export_weekly_discovery(session, export_dir: Optional[str] = None) -> Optional[Path]:
    """Export the current ISO week's resolved discovery playlist (§W3 T15).

    Same eligibility rules as the Plex weekly playlist, plus blocked tracks
    excluded per V7.
    """
    from src.models import LbRecommendation, Track, TrackStatus

    now = dt.now()
    year, week = now.year, now.isocalendar()[1]
    week_start = dt.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    week_end = week_start + timedelta(days=7)

    rows = (
        session.query(Track)
        .join(LbRecommendation, LbRecommendation.track_id == Track.id)
        .filter(
            LbRecommendation.status == "ingested",
            LbRecommendation.fetched_at >= week_start,
            LbRecommendation.fetched_at < week_end,
            Track.status == TrackStatus.DOWNLOADED.value,
            Track.file_path.isnot(None),
            Track.blocked.is_(False),
        )
        .all()
    )

    entries = [
        (t.file_path, t.artist, t.title, t.duration_ms)
        for t in rows
        if t.file_path
    ]
    return export_playlist(f"Discovered Y{year} W{week}", entries, export_dir=export_dir)


def backfill_all_playlists(session, export_dir: Optional[str] = None) -> list[Path]:
    """One-time backfill: export every source playlist that has downloaded
    tracks (§W3 T15 — covers existing Spotify playlists too). Blocked tracks
    are excluded (V7); sources with zero eligible tracks are skipped.
    """
    from src.models import Source, TrackStatus

    exported: list[Path] = []
    sources = session.query(Source).all()

    for source in sources:
        tracks = [
            t
            for t in source.tracks
            if t.status == TrackStatus.DOWNLOADED.value
            and t.file_path
            and not t.blocked
        ]
        if not tracks:
            continue

        out = export_playlist(
            source.name,
            [(t.file_path, t.artist, t.title, t.duration_ms) for t in tracks],
            export_dir=export_dir,
        )
        if out is not None:
            exported.append(out)

    logger.info("Backfill complete: %d playlist(s) exported", len(exported))
    return exported
