"""Add attempt_count + last_attempt_at to tracks for explicit requeue/give-up.

Previously give-up was derived by COUNT(download_attempts WHERE NOT success) on
every check. These columns make the requeue/failed policy explicit and cheap.
attempt_count is backfilled from download_attempts (count of FAILED attempts) so
existing tracks keep their give-up state when the orchestrator switches to the
column; last_attempt_at is backfilled to the most recent attempt timestamp.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tracks",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill from download_attempts so existing give-up state is preserved
    # (attempt_count == number of FAILED attempts, matching the old COUNT query).
    # Set-based GROUP BY + join — single pass, no per-row correlated subquery.
    op.execute(
        """
        UPDATE tracks t SET
            attempt_count   = sub.fail_cnt,
            last_attempt_at = sub.last_at
        FROM (
            SELECT track_id,
                   count(*) FILTER (WHERE NOT success) AS fail_cnt,
                   max(attempted_at)                   AS last_at
            FROM download_attempts
            GROUP BY track_id
        ) AS sub
        WHERE sub.track_id = t.id
        """
    )


def downgrade() -> None:
    op.drop_column("tracks", "last_attempt_at")
    op.drop_column("tracks", "attempt_count")
