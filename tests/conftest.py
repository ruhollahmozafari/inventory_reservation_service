"""
Test configuration using testcontainers.

Key design:
- pg_url: session-scoped SYNC fixture — starts container once, creates tables via psycopg2.
  No async event loop is touched at session scope → no deadlock with pytest-asyncio.
- Everything async is function-scoped.
  Every test gets its own event loop (pytest-asyncio 0.24 default). All async fixtures
  (db_session, session_factory, clean_tables) share that test-local loop → zero cross-loop
  "Future attached to a different loop" errors from asyncpg.
- NullPool: no connection sharing between operations, consistent with per-test isolation.
"""
import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from infra.db.models import Base
from infra.db.session import get_db
from main import app


# ── Container (session-scoped, fully synchronous) ────────────────────────────

@pytest.fixture(scope="session")
def pg_url():
    """
    Spin up one Postgres container per test session.
    Tables are created via a sync psycopg2 engine here so no async event loop
    is involved at session scope — preventing the pytest-asyncio session-loop /
    function-loop deadlock.
    """
    with PostgresContainer("postgres:16-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)

        sync_url = f"postgresql://test:test@{host}:{port}/test"
        async_url = f"postgresql+asyncpg://test:test@{host}:{port}/test"

        sync_engine = create_engine(sync_url)
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        yield async_url


# ── Per-test async fixtures (all function-scoped → same event loop as test) ──

@pytest_asyncio.fixture
async def db_session(pg_url):
    """
    Fresh AsyncSession per test. Engine + session live in the test's own event loop,
    so setup and teardown both run in that loop — no asyncpg cross-loop errors.
    """
    engine = create_async_engine(pg_url, poolclass=NullPool, echo=False)
    session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()
    yield session
    await session.close()
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(pg_url):
    """Session factory for tests that open multiple concurrent sessions."""
    engine = create_async_engine(pg_url, poolclass=NullPool, echo=False)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(pg_url):
    """Truncate all tables after every test regardless of which fixtures it used."""
    yield
    engine = create_async_engine(pg_url, poolclass=NullPool, echo=False)
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    await engine.dispose()


# ── FastAPI test client ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    from unittest.mock import AsyncMock, patch
    from httpx import AsyncClient, ASGITransport

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    # Patch the lifespan DB calls so tests don't touch the production database.
    # The test DB is already set up by pg_url; the app's engine is not needed here.
    with patch("main.create_all_tables", new_callable=AsyncMock), \
         patch("main.dispose_engine", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True) as ac:
            yield ac

    app.dependency_overrides.clear()
