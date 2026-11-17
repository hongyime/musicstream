"""Persist download move intent for crash recovery."""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "download_intents",
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("staged_path", sa.String(), nullable=False),
        sa.Column("target_path", sa.String(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("download_intents")
