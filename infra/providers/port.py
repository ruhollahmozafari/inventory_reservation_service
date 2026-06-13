"""
Provider port — Interface Segregation applied.

ReadableProvider: any provider that can report availability.
ReservableProvider: providers that support the full Try/Confirm/Cancel lifecycle.

An adapter implements one or both. The orchestrator only calls through these
interfaces — zero if-provider-type branches upstream.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass
class AvailabilityResult:
    product_id: UUID
    provider_id: UUID
    qty_available: int
    is_stale: bool = False  # flagged by read-only providers


@dataclass
class ReserveResult:
    success: bool
    provider_ref: str | None = None  # remote hold id; None for internal/soft-hold
    error: str | None = None


@dataclass
class ConfirmResult:
    success: bool
    definitive_rejection: bool = False  # True = provider will never accept; False = transient
    error: str | None = None


@dataclass
class ReleaseResult:
    success: bool
    error: str | None = None


@dataclass
class ProviderCapabilities:
    reserve: bool = False
    confirm: bool = False
    release: bool = False
    unconfirm: bool = False


@runtime_checkable
class ReadableProvider(Protocol):
    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        ...


@runtime_checkable
class ReservableProvider(Protocol):
    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        ...

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        ...

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        ...

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        """Optional capability — call only if capabilities.unconfirm is True."""
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        ...
