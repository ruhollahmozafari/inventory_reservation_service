from dataclasses import dataclass


@dataclass(frozen=True)
class Quantity:
    value: int

    def __post_init__(self):
        if self.value <= 0:
            raise ValueError(f"Quantity must be positive, got {self.value}")

    def __add__(self, other: "Quantity") -> "Quantity":
        return Quantity(self.value + other.value)

    def __le__(self, other: "Quantity") -> bool:
        return self.value <= other.value


@dataclass(frozen=True)
class ProviderRef:
    """Remote hold reference returned by an external provider."""
    value: str
