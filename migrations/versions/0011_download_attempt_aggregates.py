"""Preserve lifetime attempt metrics while pruning detailed history."""
import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "download_attempt_aggregates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "method", "success", name="uq_download_attempt_aggregate_method_success",
        ),
    )


def downgrade() -> None:
    op.drop_table("download_attempt_aggregates")
