# Architecture — Inventory Reservation Service

## The core problem

Strip away the CRUD and there is one hard problem: **two kinds of consistency collide in one checkout flow**.

For internal stock, we own the database. A reservation is a conditional `UPDATE` — provably atomic, provably oversell-free. For external stock, the provider is the source of truth. Their API might time out, return a hold, or reject us. We cannot wrap our database write and their HTTP call in one transaction. We never could. The design question isn't whether to accept this — it's what to promise the user when we don't know whether the remote call took effect.

Almost every decision below is really about managing that second boundary.

---

## What we invested in

### 1. Oversell-safe inventory — expressed in the SQL, not the application

The usual mistake is: `SELECT qty_available`, check it, then `UPDATE qty_reserved`. This has a race window. Two transactions can both read "1 unit available" and both succeed.

We do one statement with the guard in the `WHERE`:

```sql
UPDATE inventory
SET qty_reserved = qty_reserved + :q
WHERE id = :id
  AND (qty_on_hand - qty_reserved) >= :q
```

`rowcount == 1` means stock was available and is now held, atomically, without any separate lock. `rowcount == 0` is a clean "insufficient stock" signal, not an error. Release and consume are the same pattern. This is the most important thing in the codebase — everything else can be changed more cheaply than a broken oversell guard.

We chose this over `SELECT FOR UPDATE` (heavier, needed only for multi-statement logic) and optimistic versioning (adds a retry loop at application layer for no correctness gain here).

### 2. Provider abstraction with no type conditionals in the orchestrator

The orchestrator — `CreateReservationUseCase` — has zero `if provider.type == "internal"` branches. Three adapters (`InternalAdapter`, `ExternalReserveAdapter`, `SoftHoldAdapter`) implement the same `ReservableProvider` protocol. The difference between "run a SQL UPDATE" and "make an HTTP call" is entirely inside the adapter. The orchestrator doesn't know.

This matters because type conditionals scatter across the codebase as the number of provider types grows. Every new provider type forces changes in every place that branches on type. With the Strategy pattern, adding a fourth provider type means writing a new adapter class, nothing else.

The decorator chain (`TimeoutDecorator → MetricsDecorator → adapter`) composes cross-cutting concerns without touching adapter logic. Timeout is enforced uniformly whether the underlying operation is a DB query or an HTTP call.

### 3. The timeout/unknown-outcome handling — the design centerpiece

On `reserve()` timeout, we don't know if the hold was placed. Three options:

- **Assume success**: dangerous — may confirm an inventory item that isn't held, creating phantom stock.
- **Assume failure**: safer but leaves an orphaned hold at the provider if the call did succeed, causing stock to be unavailable indefinitely.
- **Fail-closed + reconcile**: what we do. Fail the reservation. Mark the item `PENDING_UNKNOWN`. Enqueue a `RECONCILE` task that releases by the idempotency key — a safe no-op if nothing was held, a release if something was.

The RECONCILE task is safe because we generated the idempotency key before the call: `"{reservation_id}:{product_id}:{provider_id}"`. The provider sees the same key on the reconcile request and treats it as idempotent. Three patterns composing to handle one failure case: saga compensation (release the hold), outbox (make that release durable), idempotency (make the release retry-safe).

We distinguish timeout (unknown outcome → RECONCILE) from other adapter exceptions (connection refused, 4xx, bad JSON → definitive failure, no orphan possible, compensate siblings directly).

### 4. Transaction boundaries made explicit

Every state-changing operation wraps its DB writes in an `atomic()` context manager:

```python
async with atomic(self._session):
    # everything here commits together; any exception rolls back
```

Repositories never call `session.commit()`. The use-case layer owns transaction scope. This enforces the two-phase structure: provider calls happen outside any transaction (no DB lock held during HTTP), then all DB writes commit once. The transaction windows are visible in the code.

This rule — never hold a DB transaction across a network call — is the single most operationally important correctness property after the oversell guard. Violating it causes lock contention that compounds under load.

### 5. Durable side effects via transactional outbox

When a reservation expires, we must mark it `EXPIRED` and release the holds. Two naive approaches both fail:

- **Mark expired, then call release**: if the process crashes between the DB commit and the release call, the hold leaks permanently. The provider has stock locked forever.
- **Call release, then mark expired**: if release succeeds but the DB commit fails, the reservation stays `PENDING` and the expiry sweeper retries — calling release again on a hold that no longer exists.

The outbox solves this by writing the release intent into the same transaction as the state change. The worker drains it with retries. Durability transfers from "hope the process doesn't crash between two operations" to "at-least-once processing with idempotent operations."

We chose a dedicated outbox table over a status column because a single reservation has N items across M providers, each needing independent retry metadata (attempts, backoff, last_error, lease). The generic worker handles all task types: `RELEASE`, `CONFIRM`, `UNCONFIRM`, `RECONCILE`.

---

## What we kept simple

### Auth — stored and used, not rotated

Provider credentials are stored encrypted in the database (`EnvEncryptedSecretProvider`): the encrypted value is in the `provider.secret_ref` column, the key-encryption key comes from an environment variable. `ProviderCapabilities.auth_type` controls how the decrypted secret is injected — Bearer token, API key header, or none.

The separation is deliberate: where the secret is stored, how it's encrypted, and how it's used in HTTP calls are independent concerns. Adding a new auth scheme is one change in `_auth_headers()`. Swapping to Vault is a new `SecretProvider` implementation, nothing else.

What we didn't build: secret rotation, Vault integration, per-request token refresh for OAuth-style credentials. These are production concerns deferred correctly — the port exists to add them without touching adapters.

### Resilience decorators — timeout only

The decorator chain is in place. `TimeoutDecorator` wraps every adapter call. `MetricsDecorator` logs per-provider latency.

Not implemented: circuit breaker, retry with backoff. The circuit breaker is the more significant gap. Without it, a provider that's reliably slow (4.9s per call, just under a 5s timeout) degrades every checkout touching it. Users wait 5 seconds before seeing a failure. With a three-state circuit breaker, after a failure threshold the breaker opens and subsequent calls fail in ~1ms — fast degradation instead of slow degradation.

The retry decorator is lower priority because the outbox already handles retry for background operations, and retrying synchronous reserve calls has idempotency requirements we'd need to enforce carefully.

### No registry class

The BUILD-SPEC describes a `Registry/Factory` mapping `provider_id → adapter` at runtime. We implemented this as `_build_adapter(provider_id)` on each use case — it reads the provider row, branches on type and capabilities, returns the wrapped adapter.

The result is equivalent (no type conditionals in the flow, adapter construction is encapsulated) but a dedicated registry class would be: independently unit-testable, reusable across use cases without duplication, and the right place to cache provider metadata lookups. The simplification is real; the debt is also real.

### Workers — outbox only, sweeper not implemented

The outbox worker is implemented: claim batch with SKIP LOCKED, process HTTP, commit done/failed with backoff. Not implemented: the expiry sweeper (which scans for `PENDING` reservations past `expires_at`, flips to `EXPIRED`, enqueues releases per line). Without it, reservations expire in state but holds aren't automatically released. The sweeper is a background worker following the exact same SKIP LOCKED pattern as the outbox worker — the design is fully worked out, the implementation was deferred for time.

### Sequential provider calls in Phase 1

The reservation saga calls providers one by one in the `for` loop. For a 3-provider cart at 300ms each, the user waits ~900ms in provider calls alone. With `asyncio.gather()` this becomes ~300ms (max, not sum). The code change is small; the compensation logic gets more complex (parallel failures need coordinated cleanup). Deferred, not forgotten.

---

## Assumptions where the spec was silent

**All-or-nothing reservations.** If a 3-item cart has one item that fails to reserve, the whole reservation fails and all held items are compensated. Partial reservations create UX problems (user pays, only gets some items) and complicate the downstream order flow. We make partial success an explicit non-goal.

**Confirm is forward-only.** Once payment is taken, we drive toward completion — we never roll back a confirmed payment. A confirm failure becomes `PENDING_FULFILMENT` and the outbox retries asynchronously. A definitive rejection becomes `NEEDS_RESOLUTION` — the order exists but requires human intervention or a refund flow. We do not attempt to undo a payment.

**Service-to-service calls, not user-facing auth.** `user_id` comes from a trusted upstream service in the request body. The spec says auth is out of scope; we assume the calling service has already validated the user and is passing a verified identifier.

**Internal adapter is fully atomic with Phase 2.** `InternalAdapter` shares the use-case SQLAlchemy session. The inventory `UPDATE` in Phase 1 and the reservation `INSERT` in Phase 2 commit in the same transaction. This is not obvious from the code structure but is a load-bearing correctness property: if Phase 2 fails, Phase 1's inventory change rolls back automatically.

**Provider capability as configuration, not code.** `ProviderCapabilities` is a struct populated from the provider's `capabilities` JSONB column. Adding `unconfirm=true` to a provider's DB record gives it unconfirm behavior without a code deployment. This lets provider capability evolve without service restarts.

---

## Why these scenarios

The task asks us to choose the most important scenarios to demonstrate. We implemented four:

**1. Internal reserve with no-oversell concurrency proof.** This is the most critical correctness property. We run 5 concurrent reservations on 1 unit of stock and assert exactly 1 succeeds. This is not a unit test with mocks — it exercises real concurrent Postgres transactions and proves the conditional update holds under contention. If this test fails, nothing else matters.

**2. External reserve with mocked HTTP.** Demonstrates the provider abstraction — the orchestrator doesn't change, only the adapter. Mocking at `ExternalReserveAdapter.reserve` (class level) ensures the mock is reached even through the `MetricsDecorator → TimeoutDecorator → adapter` chain. `assert_called_once()` proves the execution path is correct.

**3. External reserve + confirm full happy path.** The end-to-end TCC lifecycle: reserve → confirm → CONFIRMED + order created. This proves the two-window atomic design (CAS PENDING→CONFIRMING, then confirm + order commit) works correctly.

**4. Concurrent reserves on last unit (no-oversell with internal provider).** Separate from scenario 1 — this uses the use case layer directly (not the HTTP API) to test true concurrent sessions. It confirms the no-oversell property holds across different code paths.

Not demonstrated with a test: the timeout → `PENDING_UNKNOWN` → `RECONCILE` path (scenario B from BUILD-SPEC). This is architecturally the most interesting case — the code is fully implemented — but writing an integration test for it requires either a mock that simulates a `TimeoutError` partway through, or a real slow provider. Deferred for time; the design reasoning is in DESIGN-NOTES.md §4.5.

---

## What we'd do differently with more time

**Event-sourced inventory ledger.** Replace `qty_on_hand`/`qty_reserved` counters with append-only movement rows:

```sql
inventory_movement(id, inventory_id, type ENUM('reserve','release','consume'), qty, reservation_item_id, created_at)
```

Available balance = `SUM(qty ON_HAND) - SUM(qty RESERVED)`. This solves hot-row contention at the root (each reserve is a new INSERT, no row lock contention), gives a free audit trail, and makes idempotency natural (duplicate movement_id = unique constraint violation, not a double-count). The SCALABILITY.md section explains why this matters more than people expect at scale.

**Circuit breaker with Redis-backed shared state.** Three-state (closed → open → half-open), state in Redis so all app instances coordinate. The `CircuitBreakerDecorator` wraps any adapter. Open threshold: N failures in a rolling window (`INCR`/`EXPIRE`). Open flag: `SET key EX cooldown` (TTL gives free auto-cooldown). Half-open trial: `SET NX`. This is the highest-impact missing piece for production reliability.

**Parallel provider calls.** `asyncio.gather()` in Phase 1 with structured compensation on partial failure. For a multi-provider cart, this changes user-facing latency from `sum(latencies)` to `max(latencies)`.

**Vault + secret rotation.** The `SecretProvider` port exists. Add `VaultSecretProvider` that fetches secrets with a short TTL and re-fetches on cache miss. Rotation happens outside the service.

**Alembic migrations.** Currently `Base.metadata.create_all()` for dev. Alembic `--autogenerate` + reviewed migration scripts for every schema change. Non-negotiable for production.
