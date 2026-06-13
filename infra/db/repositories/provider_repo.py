from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.provider import Provider
from domain.enums import ProviderType
from domain.repositories.provider_repo import AbstractProviderRepository
from infra.db.models import ProviderModel


class ProviderRepository(AbstractProviderRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, provider_id: UUID) -> Provider | None:
        row = await self._session.get(ProviderModel, provider_id)
        return self._to_domain(row) if row else None

    async def list_by_type(self, provider_type: str) -> list[Provider]:
        rows = await self._session.scalars(
            select(ProviderModel).where(ProviderModel.type == provider_type)
        )
        return [self._to_domain(r) for r in rows.all()]

    @staticmethod
    def _to_domain(row: ProviderModel) -> Provider:
        return Provider(
            id=row.id,
            type=row.type,
            base_url=row.base_url,
            timeout_ms=row.timeout_ms,
            capabilities=dict(row.capabilities),
            secret_ref=row.secret_ref,
        )
