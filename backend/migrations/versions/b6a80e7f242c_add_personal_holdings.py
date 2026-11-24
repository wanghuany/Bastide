"""Add personal fund positions and weekly valuation history."""

import sqlalchemy as sa
from alembic import op

revision = "b6a80e7f242c"
down_revision = "f9c21d7a031b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_holdings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("opening_date", sa.Date(), nullable=False),
        sa.Column("opening_value_minor", sa.BigInteger(), nullable=False),
        sa.Column("current_value_minor", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("account_id", "name"),
    )
    op.create_index("ix_personal_holdings_account_id", "personal_holdings", ["account_id"])
    op.create_table(
        "personal_holding_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "holding_id", sa.String(36), sa.ForeignKey("personal_holdings.id"), nullable=False
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("booked_date", sa.Date(), nullable=False),
        sa.Column("previous_value_minor", sa.BigInteger(), nullable=False),
        sa.Column("value_minor", sa.BigInteger(), nullable=False),
        sa.Column("gain_minor", sa.BigInteger(), nullable=False),
        sa.Column("cash_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("transaction_id", sa.String(36), sa.ForeignKey("transactions.id")),
        sa.Column("undone", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_personal_holding_events_holding_id", "personal_holding_events", ["holding_id"]
    )


def downgrade() -> None:
    op.drop_table("personal_holding_events")
    op.drop_table("personal_holdings")
