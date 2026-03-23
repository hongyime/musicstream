from __future__ import annotations

import enum
import logging
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

logger = logging.getLogger(__name__)


# ── ORM Base ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────────────────────────────

class TrackStatus(enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    FAILED_VALIDATION = "failed_validation"


class SourceType(enum.Enum):
    PLAYLIST = "playlist"
    LIKED = "liked"


# ── Association table (Track <-> Source many-to-many) ─────────────────────────

track_sources = Table(
    "track_sources",
    Base.metadata,
    Column("track_id", Integer, ForeignKey("tracks.id"), primary_key=True),
    Column("source_id", Integer, ForeignKey("sources.id"), primary_key=True),
)


# ── Source Model (playlist or liked-songs collection) ─────────────────────────

class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    spotify_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType), nullable=False
    )
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=None
    )

    tracks: Mapped[List[Track]] = relationship(
        secondary=track_sources, back_populates="sources"
    )

    def __repr__(self) -> str:
        return f"<Source(id={self.id}, name={self.name!r}, type={self.source_type.value})>"


# ── Track Model ───────────────────────────────────────────────────────────────

class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    spotify_uri: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    track_name: Mapped[str] = mapped_column(String, nullable=False)
    artist_name: Mapped[str] = mapped_column(String, nullable=False)
    album_name: Mapped[Optional[str]] = mapped_column(String, default=None)
    track_number: Mapped[Optional[int]] = mapped_column(default=None)
    duration_ms: Mapped[Optional[int]] = mapped_column(default=None)
    yt_video_id: Mapped[Optional[str]] = mapped_column(String, default=None)
    status: Mapped[TrackStatus] = mapped_column(
        Enum(TrackStatus), default=TrackStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    sources: Mapped[List[Source]] = relationship(
        secondary=track_sources, back_populates="tracks"
    )

    def __repr__(self) -> str:
        return (
            f"<Track(id={self.id}, name={self.track_name!r}, "
            f"artist={self.artist_name!r}, status={self.status.value})>"
        )


# ── Database Manager ──────────────────────────────────────────────────────────

class DatabaseManager:
    def __init__(self, db_path: str = "music-download-code.db") -> None:
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self.SessionFactory = sessionmaker(bind=self.engine)
        self._create_tables()

    def _create_tables(self) -> None:
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        return self.SessionFactory()

    # ── Source operations ─────────────────────────────────────────────────

    def upsert_source(
        self, spotify_id: str, name: str, source_type: SourceType
    ) -> Source:
        with self.get_session() as session:
            session.expire_on_commit = False
            source = session.execute(
                select(Source).where(Source.spotify_id == spotify_id)
            ).scalar_one_or_none()
            if source:
                source.name = name
                session.commit()
                return source
            source = Source(
                spotify_id=spotify_id, name=name, source_type=source_type
            )
            session.add(source)
            session.commit()
            return source

    def mark_source_scraped(self, spotify_id: str) -> None:
        with self.get_session() as session:
            source = session.execute(
                select(Source).where(Source.spotify_id == spotify_id)
            ).scalar_one_or_none()
            if source:
                source.last_scraped_at = datetime.utcnow()
                session.commit()

    def get_all_sources(self) -> Sequence[Source]:
        with self.get_session() as session:
            session.expire_on_commit = False
            return session.execute(select(Source)).scalars().all()

    # ── Track reads ───────────────────────────────────────────────────────

    def get_track_by_spotify_uri(self, spotify_uri: str) -> Optional[Track]:
        with self.get_session() as session:
            session.expire_on_commit = False
            return session.execute(
                select(Track).where(Track.spotify_uri == spotify_uri)
            ).scalar_one_or_none()

    def get_pending_tracks(self) -> Sequence[Track]:
        with self.get_session() as session:
            session.expire_on_commit = False
            return session.execute(
                select(Track).where(Track.status == TrackStatus.PENDING)
            ).scalars().all()

    def get_tracks_by_status(self, status: TrackStatus) -> Sequence[Track]:
        with self.get_session() as session:
            session.expire_on_commit = False
            return session.execute(
                select(Track).where(Track.status == status)
            ).scalars().all()

    # ── Track writes ──────────────────────────────────────────────────────

    def add_track(
        self,
        spotify_uri: str,
        track_name: str,
        artist_name: str,
        album_name: Optional[str] = None,
        track_number: Optional[int] = None,
        duration_ms: Optional[int] = None,
        source_spotify_id: Optional[str] = None,
    ) -> Track:
        with self.get_session() as session:
            session.expire_on_commit = False
            existing = session.execute(
                select(Track).where(Track.spotify_uri == spotify_uri)
            ).scalar_one_or_none()

            if existing:
                if source_spotify_id:
                    self._link_track_source(session, existing.id, source_spotify_id)
                return existing

            track = Track(
                spotify_uri=spotify_uri,
                track_name=track_name,
                artist_name=artist_name,
                album_name=album_name,
                track_number=track_number,
                duration_ms=duration_ms,
            )
            session.add(track)
            session.flush()

            if source_spotify_id:
                self._link_track_source(session, track.id, source_spotify_id)

            session.commit()
            return track

    def _link_track_source(
        self, session: Session, track_id: int, source_spotify_id: str
    ) -> None:
        source = session.execute(
            select(Source).where(Source.spotify_id == source_spotify_id)
        ).scalar_one_or_none()
        if not source:
            return
        exists = session.execute(
            select(track_sources).where(
                track_sources.c.track_id == track_id,
                track_sources.c.source_id == source.id,
            )
        ).first()
        if not exists:
            session.execute(
                track_sources.insert().values(
                    track_id=track_id, source_id=source.id
                )
            )

    def update_track_video_id(
        self, spotify_uri: str, yt_video_id: str
    ) -> Optional[Track]:
        with self.get_session() as session:
            session.expire_on_commit = False
            track = session.execute(
                select(Track).where(Track.spotify_uri == spotify_uri)
            ).scalar_one_or_none()
            if track:
                track.yt_video_id = yt_video_id
                track.status = TrackStatus.RESOLVED
                session.commit()
                return track
            return None

    def update_track_status(
        self, spotify_uri: str, status: TrackStatus
    ) -> Optional[Track]:
        with self.get_session() as session:
            session.expire_on_commit = False
            track = session.execute(
                select(Track).where(Track.spotify_uri == spotify_uri)
            ).scalar_one_or_none()
            if track:
                track.status = status
                session.commit()
                return track
            return None

    def reset_tracks_for_fresh_scrape(self) -> int:
        """Reset all non-DOWNLOADED tracks for a fresh scrape.
        Sets PENDING/RESOLVED/FAILED/FAILED_VALIDATION/DOWNLOADING tracks back to PENDING.
        Clears yt_video_id on reset tracks.
        Returns count of tracks reset.
        """
        with self.get_session() as session:
            session.expire_on_commit = False
            tracks = session.execute(
                select(Track).where(Track.status != TrackStatus.DOWNLOADED)
            ).scalars().all()
            count = len(tracks)
            for track in tracks:
                track.status = TrackStatus.PENDING
                track.yt_video_id = None
            session.commit()
            return count

    def reset_interrupted_downloads(self) -> int:
        """Reset DOWNLOADING tracks back to RESOLVED so they can be re-downloaded.
        Returns count of tracks reset.
        """
        with self.get_session() as session:
            session.expire_on_commit = False
            tracks = session.execute(
                select(Track).where(Track.status == TrackStatus.DOWNLOADING)
            ).scalars().all()
            count = len(tracks)
            for track in tracks:
                track.status = TrackStatus.RESOLVED
            session.commit()
            return count

    def get_interrupted_download_count(self) -> int:
        """Return count of tracks stuck in DOWNLOADING status (from interrupted runs)."""
        with self.get_session() as session:
            return session.execute(
                select(Track).where(Track.status == TrackStatus.DOWNLOADING)
            ).scalars().all().__len__()

    def get_track_counts(self) -> dict[str, int]:
        """Return a dict of {status_value: count} for all statuses plus 'total'.
        Example: {'pending': 5, 'resolved': 10, 'downloaded': 100, 'failed': 2, ...., 'total': 117}
        """
        with self.get_session() as session:
            counts: dict[str, int] = {}
            total = 0
            for status in TrackStatus:
                count = len(
                    session.execute(
                        select(Track).where(Track.status == status)
                    ).scalars().all()
                )
                counts[status.value] = count
                total += count
            counts["total"] = total
            return counts

    def close(self) -> None:
        self.engine.dispose()
