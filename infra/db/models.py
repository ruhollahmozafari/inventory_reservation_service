"""
SQLAlchemy ORM models — infra layer only.
Schema matches §3 of BUILD-SPEC exactly.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Enum as SAEnum,
    ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domain.enums import (
    HoldStatus, OrderStatus, OutboxStatus, OutboxTaskType,
    ProviderType, ReservationStatus,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProductModel(Base):
    __tablename__ = "product"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    inventory_rows: Mapped[list["InventoryModel"]] = relationship(back_populates="product")


class ProviderModel(Base):
    __tablename__ = "provider"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    type: Mapped[ProviderType] = mapped_column(SAEnum(ProviderType, name="provider_type"), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    # {reserve: bool, confirm: bool, release: bool, unconfirm: bool}
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # pointer to a secret — NOT the secret itself
    secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    inventory_rows: Mapped[list["InventoryModel"]] = relationship(back_populates="provider")


class InventoryModel(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("product_id", "provider_id", name="uq_inventory_product_provider"),
        CheckConstraint("qty_on_hand >= 0", name="ck_inventory_qty_on_hand_nonneg"),
        CheckConstraint("qty_reserved >= 0", name="ck_inventory_qty_reserved_nonneg"),
        CheckConstraint("qty_reserved <= qty_on_hand", name="ck_inventory_reserved_lte_on_hand"),
        Index("ix_inventory_product_provider", "product_id", "provider_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product.id"), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider.id"), nullable=False)
    qty_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qty_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product: Mapped["ProductModel"] = relationship(back_populates="inventory_rows")
    provider: Mapped["ProviderModel"] = relationship(back_populates="inventory_rows")


class ReservationModel(Base):
    __tablename__ = "reservation"
    __table_args__ = (
        Index("ix_reservation_status_expires_at", "status", "expires_at"),
        Index("ix_reservation_idempotency_key", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    status: Mapped[ReservationStatus] = mapped_column(
        SAEnum(ReservationStatus, name="reservation_status"), nullable=False, default=ReservationStatus.PENDING
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["ReservationItemModel"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )
    order: Mapped["OrderModel | None"] = relationship(back_populates="reservation", uselist=False)


class ReservationItemModel(Base):
    __tablename__ = "reservation_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reservation.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product.id"), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hold_status: Mapped[HoldStatus] = mapped_column(
        SAEnum(HoldStatus, name="hold_status"), nullable=False, default=HoldStatus.PENDING_UNKNOWN
    )

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_reservation_item_qty_positive"),
    )

    reservation: Mapped["ReservationModel"] = relationship(back_populates="items")


class OrderModel(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reservation.id"), nullable=False, unique=True
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.CONFIRMED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    reservation: Mapped["ReservationModel"] = relationship(back_populates="order")
    items: Mapped[list["OrderItemModel"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItemModel(Base):
    __tablename__ = "order_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product.id"), nullable=False)
    provider_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)

    order: Mapped["OrderModel"] = relationship(back_populates="items")


class OutboxModel(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_status_next_run_at", "status", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type: Mapped[OutboxTaskType] = mapped_column(
        SAEnum(OutboxTaskType, name="outbox_task_type"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    status: Mapped[OutboxStatus] = mapped_column(
        SAEnum(OutboxStatus, name="outbox_status"), nullable=False, default=OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
