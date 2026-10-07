"""Track complete pipeline outcomes and transient retry backoff."""
import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("content_failure_passes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tracks",
        sa.Column("transient_failure_passes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tracks", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tracks", sa.Column("last_pipeline_outcome", sa.String(), nullable=True))
    op.add_column("tracks", sa.Column("last_pipeline_error", sa.String(), nullable=True))
    op.add_column("tracks", sa.Column("last_pipeline_pass_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "last_pipeline_pass_at")
    op.drop_column("tracks", "last_pipeline_error")
    op.drop_column("tracks", "last_pipeline_outcome")
    op.drop_column("tracks", "next_retry_at")
    op.drop_column("tracks", "transient_failure_passes")
    op.drop_column("tracks", "content_failure_passes")
