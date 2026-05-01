"""Initial schema -- all tables, indexes, and updated_at trigger.

Revision ID: 0001
Revises: None
Create Date: 2025-01-01 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Dollar-quoted PL/pgSQL body — stored as a constant to avoid f-string issues.
_CREATE_TRIGGER_SQL = (
    "CREATE OR REPLACE FUNCTION update_updated_at()\n"
    "RETURNS TRIGGER AS $body$\n"
    "BEGIN NEW.updated_at = NOW(); RETURN NEW; END;\n"
    "$body$ LANGUAGE plpgsql;\n"
    "\n"
    "CREATE TRIGGER trg_tracks_updated_at\n"
    "BEFORE UPDATE ON tracks\n"
    "FOR EACH ROW EXECUTE FUNCTION update_updated_at();"
)


def upgrade() -> None:
    # ── tracks ────────────────────────────────────────────────────────────────
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), nullable=False),

        # Spotify identifiers
        sa.Column("spotify_uri", sa.String(), nullable=False),
        sa.Column("spotify_id", sa.String(), nullable=True),
        sa.Column("spotify_album_id", sa.String(), nullable=True),

        # Music identifiers
        sa.Column("isrc", sa.String(), nullable=True),

        # Core metadata
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False),
        sa.Column("album_artist", sa.String(), nullable=True),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("year", sa.String(), nullable=True),

        # Track position
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),

        # Cover art
        sa.Column("cover_art_url", sa.String(), nullable=True),
        sa.Column("cover_art_source", sa.String(), nullable=False, server_default="none"),

        # MusicBrainz / AcoustID
        sa.Column("mb_recording_id", sa.String(), nullable=True),
        sa.Column("mb_release_id", sa.String(), nullable=True),
        sa.Column("acoustid_id", sa.String(), nullable=True),

        # Pipeline state
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("download_method", sa.String(), nullable=True),

        # File info
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("file_sha256", sa.String(), nullable=True),

        # Plex
        sa.Column("plex_verified", sa.Boolean(), nullable=False, server_default="false"),

        # Timestamps
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),

        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spotify_uri"),
    )

    # ── sources ───────────────────────────────────────────────────────────────
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("spotify_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("snapshot_id", sa.String(), nullable=True),
        sa.Column("track_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scraped_at", sa.DateTime(timezone=True), nullable=True),

        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spotify_id"),
    )

    # ── track_sources (many-to-many association) ──────────────────────────────
    op.create_table(
        "track_sources",
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),

        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("track_id", "source_id"),
    )

    # ── lb_recommendations ────────────────────────────────────────────────────
    op.create_table(
        "lb_recommendations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recording_mbid", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),

        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recording_mbid"),
    )

    # ── download_attempts ─────────────────────────────────────────────────────
    op.create_table(
        "download_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="false"),

        sa.ForeignKeyConstraint(["track_id"], ["tracks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── daemon_runs ───────────────────────────────────────────────────────────
    op.create_table(
        "daemon_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_type", sa.String(), nullable=False),
        sa.Column("tracks_scraped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tracks_downloaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tracks_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tracks_requeued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(), nullable=True),

        sa.PrimaryKeyConstraint("id"),
    )

    # ── indexes ───────────────────────────────────────────────────────────────
    op.create_index("idx_tracks_status",       "tracks", ["status"])
    op.create_index("idx_tracks_spotify_uri",  "tracks", ["spotify_uri"])
    op.create_index("idx_tracks_isrc",         "tracks", ["isrc"])
    op.create_index("idx_tracks_mb_recording", "tracks", ["mb_recording_id"])

    # ── updated_at trigger ────────────────────────────────────────────────────
    op.execute(_CREATE_TRIGGER_SQL)


def downgrade() -> None:
    # Drop trigger and function first
    op.execute("DROP TRIGGER IF EXISTS trg_tracks_updated_at ON tracks;")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at();")

    # Drop indexes
    op.drop_index("idx_tracks_mb_recording", table_name="tracks")
    op.drop_index("idx_tracks_isrc",         table_name="tracks")
    op.drop_index("idx_tracks_spotify_uri",  table_name="tracks")
    op.drop_index("idx_tracks_status",       table_name="tracks")

    # Drop tables in reverse dependency order
    op.drop_table("daemon_runs")
    op.drop_table("download_attempts")
    op.drop_table("lb_recommendations")
    op.drop_table("track_sources")
    op.drop_table("sources")
    op.drop_table("tracks")
