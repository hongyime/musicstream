"""Add kind column to lb_recommendations (SPEC.md §W3 T22).

Distinguishes classic CF recommendations from weekly-playlist discoveries:
'cf' | 'weekly_jams' | 'weekly_exploration'.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lb_recommendations",
        sa.Column("kind", sa.String(), nullable=False, server_default="cf"),
    )
    op.create_index("idx_lb_recommendations_kind", "lb_recommendations", ["kind"])


def downgrade() -> None:
    op.drop_index("idx_lb_recommendations_kind", table_name="lb_recommendations")
    op.drop_column("lb_recommendations", "kind")
