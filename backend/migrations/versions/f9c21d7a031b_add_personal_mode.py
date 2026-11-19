"""Add personal-mode audit history, drafts, and private settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9c21d7a031b"
down_revision: str | Sequence[str] | None = "a26ae97e35f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, columns in (
        (
            "personal_actions",
            [
                sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("kind", sa.String(40), nullable=False),
                sa.Column("payload", sa.JSON(), nullable=False),
                sa.Column("undone", sa.Boolean(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            ],
        ),
        (
            "personal_drafts",
            [
                sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("kind", sa.String(40), nullable=False),
                sa.Column("payload", sa.JSON(), nullable=False),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("consumed", sa.Boolean(), nullable=False),
            ],
        ),
        (
            "personal_settings",
            [
                sa.Column("key", sa.String(160), primary_key=True),
                sa.Column("value", sa.JSON(), nullable=False),
            ],
        ),
    ):
        op.create_table(
            name,
            *columns,
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        )
        op.create_index(f"ix_{name}_user_id", name, ["user_id"])


def downgrade() -> None:
    for name in ("personal_settings", "personal_drafts", "personal_actions"):
        op.drop_table(name)
