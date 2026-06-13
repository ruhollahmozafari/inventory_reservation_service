from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class Product:
    id: UUID
    sku: str
    name: str
    created_at: datetime
