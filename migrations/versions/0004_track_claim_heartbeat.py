"""Add download claim heartbeat fields to tracks.

These nullable fields make live DOWNLOADING rows inspectable and let stale-row
cleanup clear claim metadata when a worker dies mid-track.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tracks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tracks", sa.Column("claim_owner", sa.String(), nullable=True))
    op.add_column("tracks", sa.Column("daemon_run_id", sa.Integer(), nullable=True))
    op.create_index(
        "idx_tracks_status_heartbeat",
        "tracks",
        ["status", "heartbeat_at"],
    )
    op.create_index("idx_tracks_claim_owner", "tracks", ["claim_owner"])


def downgrade() -> None:
    op.drop_index("idx_tracks_claim_owner", table_name="tracks")
    op.drop_index("idx_tracks_status_heartbeat", table_name="tracks")
    op.drop_column("tracks", "daemon_run_id")
    op.drop_column("tracks", "claim_owner")
    op.drop_column("tracks", "heartbeat_at")
    op.drop_column("tracks", "claimed_at")
