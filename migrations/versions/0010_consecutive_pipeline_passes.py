"""Maintain the auto-block failed-pass streak without reading attempt history."""
import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("consecutive_failed_passes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("tracks", "consecutive_failed_passes")
