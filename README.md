# Inventory Reservation Service

Backend service that manages inventory holds during e-commerce checkout. When a user starts checkout, the service temporarily reserves the requested stock across one or more inventory providers. Once payment succeeds the reservation is confirmed and stock is consumed. If checkout is abandoned or payment fails, the hold is released.

## How it works

**Three provider types:**
- **Internal** — stock lives in our own database. Reserve, confirm, and release are plain SQL UPDATEs, fully atomic.
- **External** — stock lives in a third-party warehouse system. Reserve/confirm/release call the provider's HTTP API.
- **Soft-hold** — read-only provider (no reserve API). We mirror their inventory in our DB and hold against the local copy; a background sync worker keeps it fresh.

**Reservation lifecycle:**
```
INITIALIZING → PENDING → CONFIRMING → CONFIRMED
                       ↘ CANCELLED
                       ↘ FAILED / EXPIRED
```

**Crash safety:** Before any external HTTP call, a `RESERVING` intent row is committed with an idempotency key. A timeout leaves the key in the DB; the RECONCILE worker uses it to release the hold by key rather than by outcome. An `INITIALIZING` reservation past its `creation_deadline` is rolled back by the expiry sweeper.

## Project layout

```
api/v1/              HTTP layer — request/response schemas, FastAPI routes
app/use_cases/       Application logic (create, confirm, cancel reservation)
domain/              Entities, enums, repository ports — no framework dependencies
infra/
  db/                SQLAlchemy models, repositories, transaction helper
  http/              ProviderHttpClient + auth strategies (Bearer, API key, none)
  providers/
    adapters/        InternalAdapter, ExternalReserveAdapter, SoftHoldAdapter
    decorators/      TimeoutDecorator, MetricsDecorator, CircuitBreakerDecorator
workers/
  outbox_worker.py   Drains RELEASE/CONFIRM/RECONCILE tasks with retry + backoff
  expiry_sweeper.py  Flips stale PENDING reservations to EXPIRED, enqueues releases
  availability_sync.py  Syncs external stock into the local DB mirror
```

## Running locally

**Prerequisites:** Docker (for Postgres), Python 3.10+

```bash
# Start Postgres
docker run -d --name pg \
  -e POSTGRES_DB=inventory_db \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 postgres:16-alpine

# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Seed example data (optional)
python -m scripts.seed

# Start the API
uvicorn main:app --reload
```

API docs available at `http://localhost:8000/docs`.

## Running workers

Each worker is a standalone process:

```bash
python -m workers.outbox_worker
python -m workers.expiry_sweeper
python -m workers.availability_sync
```

## Configuration

All settings are in `config.py` and can be overridden via environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/inventory_db` | Async Postgres URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis (for circuit breaker state) |
| `RESERVATION_TTL_SECONDS` | `900` | How long a PENDING reservation is valid |
| `RESERVATION_CREATE_GRACE_SECONDS` | `30` | Crash-recovery window for INITIALIZING reservations |
| `SECRET_KEK` | — | Fernet key for decrypting provider credentials |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `2.0` | Outbox worker poll frequency |
| `SWEEPER_POLL_INTERVAL_SECONDS` | `10.0` | Expiry sweeper poll frequency |

To generate a `SECRET_KEK`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Running tests

Tests use [testcontainers](https://testcontainers-python.readthedocs.io/) — Docker must be running. No manual setup needed.

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The test suite covers: internal reserve (concurrency proof, no-oversell), external reserve (mocked HTTP), full reserve→confirm lifecycle, timeout → PENDING_UNKNOWN + RECONCILE, provider rejection, idempotency, soft-hold, and insufficient stock rollback.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/reservations` | Create a reservation |
| `GET` | `/api/v1/reservations/{id}` | Get reservation status |
| `POST` | `/api/v1/reservations/{id}/confirm` | Confirm a reservation (after payment) |
| `POST` | `/api/v1/reservations/{id}/cancel` | Cancel a reservation |
| `GET` | `/api/v1/inventory/{product_id}/{provider_id}` | Check stock levels |
| `GET` | `/health` | Health check |

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and [SCALABILITY.md](SCALABILITY.md) for scaling analysis.
