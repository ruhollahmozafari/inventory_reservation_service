from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def atomic(session: AsyncSession):
    """Commit on clean exit, rollback on any exception."""
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
