"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enums
    provider_type = postgresql.ENUM("internal", "external", name="provider_type")
    provider_type.create(op.get_bind())

    reservation_status = postgresql.ENUM(
        "PENDING", "CONFIRMING", "CONFIRMED", "CANCELLED", "EXPIRED", "FAILED",
        name="reservation_status",
    )
    reservation_status.create(op.get_bind())

    hold_status = postgresql.ENUM(
        "HELD", "PENDING_UNKNOWN", "RELEASED", "FAILED", "CONFIRMED",
        name="hold_status",
    )
    hold_status.create(op.get_bind())

    order_status = postgresql.ENUM(
        "CONFIRMED", "PENDING_FULFILMENT", "NEEDS_RESOLUTION", "FAILED",
        name="order_status",
    )
    order_status.create(op.get_bind())

    outbox_task_type = postgresql.ENUM(
        "RELEASE", "CONFIRM", "UNCONFIRM", "RECONCILE",
        name="outbox_task_type",
    )
    outbox_task_type.create(op.get_bind())

    outbox_status = postgresql.ENUM(
        "PENDING", "PROCESSING", "DONE", "FAILED",
        name="outbox_status",
    )
    outbox_status.create(op.get_bind())

    # product
    op.create_table(
        "product",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku", sa.String(128), nullable=False, unique=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # provider
    op.create_table(
        "provider",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("type", sa.Enum("internal", "external", name="provider_type"), nullable=False),
        sa.Column("base_url", sa.Text, nullable=True),
        sa.Column("timeout_ms", sa.Integer, nullable=False, server_default="5000"),
        sa.Column("capabilities", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("secret_ref", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # inventory
    op.create_table(
        "inventory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product.id"), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider.id"), nullable=False),
        sa.Column("qty_on_hand", sa.Integer, nullable=False, server_default="0"),
        sa.Column("qty_reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("product_id", "provider_id", name="uq_inventory_product_provider"),
        sa.CheckConstraint("qty_on_hand >= 0", name="ck_inventory_qty_on_hand_nonneg"),
        sa.CheckConstraint("qty_reserved >= 0", name="ck_inventory_qty_reserved_nonneg"),
        sa.CheckConstraint("qty_reserved <= qty_on_hand", name="ck_inventory_reserved_lte_on_hand"),
    )
    op.create_index("ix_inventory_product_provider", "inventory", ["product_id", "provider_id"])

    # reservation
    op.create_table(
        "reservation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("status", sa.Enum(
            "PENDING", "CONFIRMING", "CONFIRMED", "CANCELLED", "EXPIRED", "FAILED",
            name="reservation_status"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_reservation_status_expires_at", "reservation", ["status", "expires_at"])
    op.create_index("ix_reservation_idempotency_key", "reservation", ["idempotency_key"], unique=True)

    # reservation_item
    op.create_table(
        "reservation_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("reservation.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product.id"), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider.id"), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("provider_ref", sa.String(256), nullable=True),
        sa.Column("hold_status", sa.Enum(
            "HELD", "PENDING_UNKNOWN", "RELEASED", "FAILED", "CONFIRMED",
            name="hold_status"), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_reservation_item_qty_positive"),
    )

    # orders
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("reservation.id"), nullable=False, unique=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("status", sa.Enum(
            "CONFIRMED", "PENDING_FULFILMENT", "NEEDS_RESOLUTION", "FAILED",
            name="order_status"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # order_item
    op.create_table(
        "order_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("product.id"), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider.id"), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("unit_ref", sa.String(256), nullable=True),
    )

    # outbox
    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_type", sa.Enum(
            "RELEASE", "CONFIRM", "UNCONFIRM", "RECONCILE",
            name="outbox_task_type"), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("status", sa.Enum(
            "PENDING", "PROCESSING", "DONE", "FAILED",
            name="outbox_status"), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outbox_status_next_run_at", "outbox", ["status", "next_run_at"])


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("order_item")
    op.drop_table("orders")
    op.drop_table("reservation_item")
    op.drop_table("reservation")
    op.drop_table("inventory")
    op.drop_table("provider")
    op.drop_table("product")

    for name in [
        "outbox_status", "outbox_task_type", "order_status",
        "hold_status", "reservation_status", "provider_type",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {name}")
