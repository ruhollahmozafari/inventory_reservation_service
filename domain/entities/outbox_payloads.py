from dataclasses import dataclass
from uuid import UUID


@dataclass
class ReconcilePayload:
    reservation_id: UUID
    item_id: UUID
    product_id: UUID
    provider_id: UUID
    qty: int
    idempotency_key: str

    def to_dict(self) -> dict:
        return {
            "reservation_id": str(self.reservation_id),
            "item_id": str(self.item_id),
            "product_id": str(self.product_id),
            "provider_id": str(self.provider_id),
            "qty": self.qty,
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class ReleasePayload:
    item_id: UUID
    provider_id: UUID
    provider_ref: str | None

    def to_dict(self) -> dict:
        return {
            "item_id": str(self.item_id),
            "provider_id": str(self.provider_id),
            "provider_ref": self.provider_ref,
        }


@dataclass
class ConfirmPayload:
    reservation_id: UUID
    item_id: UUID
    provider_id: UUID
    provider_ref: str

    def to_dict(self) -> dict:
        return {
            "reservation_id": str(self.reservation_id),
            "item_id": str(self.item_id),
            "provider_id": str(self.provider_id),
            "provider_ref": self.provider_ref,
        }
