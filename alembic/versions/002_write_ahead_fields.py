"""write-ahead intent fields

Adds the columns and enum values needed for the three-transaction saga pattern:
- reservation_status: INITIALIZING (written before external API calls; crash-recoverable)
- hold_status: RESERVING (write-ahead intent row for external items)
- reservation.creation_deadline: sweeper rolls back INITIALIZING past this timestamp
- reservation_item.item_idempotency_key: idempotency key stored for RESERVING items

Revision ID: 002
Revises: 001
Create Date: 2026-06-13
"""
import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires enum value additions outside a transaction block.
    op.execute("ALTER TYPE reservation_status ADD VALUE IF NOT EXISTS 'INITIALIZING'")
    op.execute("ALTER TYPE hold_status ADD VALUE IF NOT EXISTS 'RESERVING'")

    op.add_column(
        "reservation",
        sa.Column("creation_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reservation_item",
        sa.Column("item_idempotency_key", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reservation_item", "item_idempotency_key")
    op.drop_column("reservation", "creation_deadline")
    # Note: PostgreSQL does not support removing enum values.
    # To fully downgrade, recreate the enum types without INITIALIZING / RESERVING.
