"""
musicstream/models.py — SQLAlchemy 2.0 ORM models (PostgreSQL)

All models use Mapped/mapped_column syntax with PostgreSQL as the backend.
The DatabaseManager class from the prototype has been removed; session
management lives in db.py.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


# ── ORM Base ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── String Enums (str + enum.Enum — NOT SQLAlchemy Enum type) ─────────────────

class TrackStatus(str, enum.Enum):
    """Valid values for Track.status.  Stored as plain VARCHAR in PostgreSQL."""
    PENDING           = "pending"
    RESOLVING         = "resolving"
    DOWNLOADING       = "downloading"
    DOWNLOADED        = "downloaded"
    FAILED            = "failed"
    FAILED_VALIDATION = "failed_validation"
    TIMED_OUT         = "timed_out"
    MISSING           = "missing"


class SourceType(str, enum.Enum):
    """Valid values for Source.source_type.  Stored as plain VARCHAR in PostgreSQL."""
    PLAYLIST     = "playlist"
    LIKED        = "liked"
    ALBUM        = "album"
    ARTIST       = "artist"
    HISTORY      = "history"
    LISTENBRAINZ = "listenbrainz"


# ── Association table (Track <-> Source many-to-many) ─────────────────────────

track_sources = Table(
    "track_sources",
    Base.metadata,
    Column("track_id",  Integer, ForeignKey("tracks.id"),  primary_key=True),
    Column("source_id", Integer, ForeignKey("sources.id"), primary_key=True),
)


# ── Track Model ───────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


class Track(Base):
    """Represents a single music track ingested from Spotify or ListenBrainz."""

    __tablename__ = "tracks"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Spotify identifiers
    spotify_uri:      Mapped[str]           = mapped_column(String, unique=True, nullable=False)
    spotify_id:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    spotify_album_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Music identifiers
    isrc: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Core metadata
    title:        Mapped[str]           = mapped_column(String, nullable=False)
    artist:       Mapped[str]           = mapped_column(String, nullable=False)
    album_artist: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    album:        Mapped[Optional[str]] = mapped_column(String, nullable=True)
    year:         Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Track position
    track_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disc_number:  Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms:  Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cover art
    cover_art_url:    Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cover_art_source: Mapped[str]           = mapped_column(String, nullable=False, default="none")

    # MusicBrainz / AcoustID
    mb_recording_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mb_release_id:   Mapped[Optional[str]] = mapped_column(String, nullable=True)
    acoustid_id:     Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Pipeline state — stored as plain String (see TrackStatus enum)
    status:          Mapped[str]           = mapped_column(String, nullable=False, default=TrackStatus.PENDING.value)
    download_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Download attempt accounting (migration 0003) — explicit requeue/give-up
    # policy instead of COUNT(download_attempts) on every check. attempt_count
    # tracks FAILED tier attempts; backfilled from download_attempts.
    attempt_count:   Mapped[int]                = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Download claim observability/recovery (migration 0004). These fields are
    # nullable so older queued/downloaded rows do not need a backfill.
    claimed_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_owner:    Mapped[Optional[str]]      = mapped_column(String, nullable=True)
    daemon_run_id:  Mapped[Optional[int]]      = mapped_column(Integer, nullable=True)

    # File info
    format:          Mapped[Optional[str]] = mapped_column(String, nullable=True)   # 'flac' | 'mp3'
    file_path:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_sha256:     Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Plex
    plex_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps (timezone-aware)
    created_at:      Mapped[datetime]           = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at:      Mapped[datetime]           = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    sources:           Mapped[List["Source"]]          = relationship(secondary=track_sources, back_populates="tracks")
    download_attempts: Mapped[List["DownloadAttempt"]] = relationship(back_populates="track", cascade="all, delete-orphan")
    lb_recommendations: Mapped[List["LbRecommendation"]] = relationship(back_populates="track")

    def __repr__(self) -> str:
        return (
            f"<Track(id={self.id}, spotify_uri={self.spotify_uri!r}, "
            f"title={self.title!r}, artist={self.artist!r}, status={self.status!r})>"
        )


# ── Source Model ──────────────────────────────────────────────────────────────

class Source(Base):
    """Represents a Spotify playlist, liked-songs collection, or ListenBrainz source."""

    __tablename__ = "sources"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True)
    spotify_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name:       Mapped[str] = mapped_column(String, nullable=False)

    # Stored as plain String (see SourceType enum)
    source_type: Mapped[str] = mapped_column(String, nullable=False)

    snapshot_id:     Mapped[Optional[str]] = mapped_column(String, nullable=True)
    track_count:     Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    tracks: Mapped[List["Track"]] = relationship(secondary=track_sources, back_populates="sources")

    def __repr__(self) -> str:
        return (
            f"<Source(id={self.id}, spotify_id={self.spotify_id!r}, "
            f"name={self.name!r}, source_type={self.source_type!r})>"
        )


# ── LbRecommendation Model ────────────────────────────────────────────────────

class LbRecommendation(Base):
    """A music recommendation fetched from the ListenBrainz Collaborative Filtering API."""

    __tablename__ = "lb_recommendations"

    id:             Mapped[int] = mapped_column(Integer, primary_key=True)
    recording_mbid: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    title:  Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    artist: Mapped[Optional[str]]   = mapped_column(String, nullable=True)
    score:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Optional FK to a Track that was created from this recommendation
    track_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tracks.id"), nullable=True)
    track:    Mapped[Optional["Track"]] = relationship(back_populates="lb_recommendations")

    # Stored as plain String; expected values: pending | ingested | failed | skipped
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")

    def __repr__(self) -> str:
        return (
            f"<LbRecommendation(id={self.id}, recording_mbid={self.recording_mbid!r}, "
            f"title={self.title!r}, status={self.status!r})>"
        )


# ── DownloadAttempt Model ─────────────────────────────────────────────────────

class DownloadAttempt(Base):
    """Audit record for every individual tier attempt made for a track download."""

    __tablename__ = "download_attempts"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True)
    track_id:     Mapped[int]      = mapped_column(Integer, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    method:  Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error:   Mapped[Optional[str]] = mapped_column(String, nullable=True)
    success: Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)

    # Relationship
    track: Mapped["Track"] = relationship(back_populates="download_attempts")

    def __repr__(self) -> str:
        return (
            f"<DownloadAttempt(id={self.id}, track_id={self.track_id}, "
            f"method={self.method!r}, success={self.success})>"
        )


# ── DaemonRun Model ───────────────────────────────────────────────────────────

class DaemonRun(Base):
    """History record for each daemon pipeline execution."""

    __tablename__ = "daemon_runs"

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True)
    started_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stored as plain String; expected values: scheduled | manual | integrity | discovery
    run_type: Mapped[str] = mapped_column(String, nullable=False)

    # Counters
    tracks_scraped:     Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tracks_downloaded:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tracks_failed:      Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tracks_requeued:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DaemonRun(id={self.id}, run_type={self.run_type!r}, "
            f"started_at={self.started_at!r}, downloaded={self.tracks_downloaded}, "
            f"failed={self.tracks_failed})>"
        )
