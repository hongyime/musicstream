"""Index track paths and support large audio files."""
import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index("ix_tracks_file_path", "tracks", ["file_path"])
    op.alter_column(
        "tracks",
        "file_size_bytes",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tracks",
        "file_size_bytes",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.drop_index("ix_tracks_file_path", table_name="tracks")
