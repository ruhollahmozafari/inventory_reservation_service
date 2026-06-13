from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from domain.entities.reservation import Reservation
from domain.enums import HoldStatus, ReservationStatus
from infra.db.repositories.reservation_repo import ReservationRepository

router = APIRouter()


# ── Request / Response schemas ──────────────────────────────────────────────

class ReservationItemRequest(BaseModel):
    product_id: UUID
    provider_id: UUID
    qty: int = Field(gt=0)


class CreateReservationRequest(BaseModel):
    user_id: str
    idempotency_key: str = Field(min_length=1, max_length=256)
    items: list[ReservationItemRequest] = Field(min_length=1)


class ReservationItemResponse(BaseModel):
    id: UUID
    product_id: UUID
    provider_id: UUID
    qty: int
    hold_status: HoldStatus
    provider_ref: str | None


class ReservationResponse(BaseModel):
    id: UUID
    user_id: str
    status: ReservationStatus
    expires_at: str
    created_at: str
    confirmed_at: str | None
    items: list[ReservationItemResponse]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=ReservationResponse)
async def create_reservation(
    body: CreateReservationRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ReservationResponse:
    from app.use_cases.create_reservation import CreateReservationUseCase
    use_case = CreateReservationUseCase(db)
    reservation = await use_case.execute(
        user_id=body.user_id,
        idempotency_key=body.idempotency_key,
        items=[(item.product_id, item.provider_id, item.qty) for item in body.items],
    )
    return _to_response(reservation)


@router.get("/{reservation_id}", response_model=ReservationResponse)
async def get_reservation(
    reservation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ReservationResponse:
    repo = ReservationRepository(db)
    reservation = await repo.get_by_id(reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    return _to_response(reservation)


@router.post("/{reservation_id}/confirm", response_model=ReservationResponse)
async def confirm_reservation(
    reservation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ReservationResponse:
    from app.use_cases.confirm_reservation import ConfirmReservationUseCase
    use_case = ConfirmReservationUseCase(db)
    reservation = await use_case.execute(reservation_id=reservation_id)
    return _to_response(reservation)


@router.post("/{reservation_id}/cancel", response_model=ReservationResponse)
async def cancel_reservation(
    reservation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ReservationResponse:
    from app.use_cases.cancel_reservation import CancelReservationUseCase
    use_case = CancelReservationUseCase(db)
    reservation = await use_case.execute(reservation_id=reservation_id)
    return _to_response(reservation)


def _to_response(r: Reservation) -> ReservationResponse:
    return ReservationResponse(
        id=r.id,
        user_id=r.user_id,
        status=r.status,
        expires_at=r.expires_at.isoformat(),
        created_at=r.created_at.isoformat(),
        confirmed_at=r.confirmed_at.isoformat() if r.confirmed_at else None,
        items=[
            ReservationItemResponse(
                id=item.id,
                product_id=item.product_id,
                provider_id=item.provider_id,
                qty=item.qty,
                hold_status=item.hold_status,
                provider_ref=item.provider_ref,
            )
            for item in r.items
        ],
    )
