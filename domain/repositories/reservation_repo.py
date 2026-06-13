from abc import ABC, abstractmethod
from uuid import UUID

from domain.entities.reservation import Reservation
from domain.enums import ReservationStatus


class AbstractReservationRepository(ABC):
    @abstractmethod
    async def get_by_id(self, reservation_id: UUID) -> Reservation | None:
        ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> Reservation | None:
        ...

    @abstractmethod
    async def save(self, reservation: Reservation) -> Reservation:
        ...

    @abstractmethod
    async def cas_status(
        self,
        reservation_id: UUID,
        expected: ReservationStatus,
        new: ReservationStatus,
    ) -> bool:
        """Compare-and-swap status. Returns True if the update applied."""
        ...

    @abstractmethod
    async def claim_expired(self, batch_size: int) -> list[Reservation]:
        """Claim PENDING reservations past expires_at → set EXPIRED. Returns claimed rows."""
        ...
