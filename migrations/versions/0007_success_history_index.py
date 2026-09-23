"""Index successful-attempt history used by health and burn-rate queries."""
import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_download_attempts_success_at",
            "download_attempts",
            ["attempted_at"],
            postgresql_where=sa.text("success IS TRUE"),
            sqlite_where=sa.text("success IS TRUE"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_download_attempts_success_at",
            table_name="download_attempts",
            postgresql_concurrently=True,
        )
