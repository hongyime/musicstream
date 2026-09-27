"""Drop plex_verified — dead column, no code path ever set it True."""
import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_column("tracks", "plex_verified")


def downgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("plex_verified", sa.Boolean(), nullable=False, server_default="false"),
    )
