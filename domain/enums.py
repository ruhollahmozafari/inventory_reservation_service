from enum import Enum


class ReservationStatus(str, Enum):
    INITIALIZING = "INITIALIZING"  # write-ahead: intent committed, external calls in flight
    PENDING = "PENDING"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class HoldStatus(str, Enum):
    RESERVING = "RESERVING"  # write-ahead intent persisted, API call not yet made/confirmed
    HELD = "HELD"
    PENDING_UNKNOWN = "PENDING_UNKNOWN"
    RELEASED = "RELEASED"
    FAILED = "FAILED"
    CONFIRMED = "CONFIRMED"


class OrderStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    PENDING_FULFILMENT = "PENDING_FULFILMENT"
    NEEDS_RESOLUTION = "NEEDS_RESOLUTION"
    FAILED = "FAILED"


class ProviderType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class OutboxTaskType(str, Enum):
    RELEASE = "RELEASE"
    CONFIRM = "CONFIRM"
    UNCONFIRM = "UNCONFIRM"
    RECONCILE = "RECONCILE"
