"""Add Wave 3 blocklist columns to tracks (SPEC.md §W3 T12 / invariant V7).

blocked/blocked_reason/blocked_at make failed-track quarantine explicit and
persistent instead of relying on repeated reset-failed cycles.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("blocked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("tracks", sa.Column("blocked_reason", sa.String(), nullable=True))
    op.add_column("tracks", sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("idx_tracks_blocked", "tracks", ["blocked"])


def downgrade() -> None:
    op.drop_index("idx_tracks_blocked", table_name="tracks")
    op.drop_column("tracks", "blocked_at")
    op.drop_column("tracks", "blocked_reason")
    op.drop_column("tracks", "blocked")
