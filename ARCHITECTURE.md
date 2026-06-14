# Architecture — Inventory Reservation Service

## The core problem

Two kinds of consistency collide in one checkout flow. For internal stock we own the database — a reservation is a conditional `UPDATE`, provably atomic, provably oversell-free. For external providers the provider is the source of truth; their API might time out, return a hold, or reject the request. We can never wrap our database write and their HTTP call in one transaction. Every decision below is about managing that external boundary.

---

## What we invested in

### Oversell-safe inventory — expressed in SQL, not the application

Reserve is a single conditional statement:

```sql
UPDATE inventory
SET qty_reserved = qty_reserved + :q
WHERE id = :id AND (qty_on_hand - qty_reserved) >= :q
```

`rowcount == 1` means stock was available and is now held, atomically, without a separate lock or read-then-write race. Release and consume follow the same pattern. This is the most important property in the codebase — everything else can be changed more cheaply than a broken oversell guard.

We chose this over `SELECT FOR UPDATE` (heavier, only needed for multi-statement logic) and optimistic versioning (adds an application-layer retry loop for no correctness gain here).

### Three-transaction saga with write-ahead intent

The create flow uses three transaction windows to survive crashes at any point:

**TX1 (intent-first):** All DB work — provider lookup, internal/soft-hold stock deduction, write-ahead `RESERVING` intent rows for external items, reservation INSERT as `INITIALIZING`. Commits before any external HTTP call. The `INITIALIZING` status and the `idempotency_key` stored on each intent row are the crash-recovery handles: a sweeper finds stale `INITIALIZING` reservations and rolls them back safely.

**TX_n (per external item):** After each provider API call, a short atomic block flips the item from `RESERVING` to `HELD`, `FAILED`, or `PENDING_UNKNOWN`. Crashes between calls leave the intent row in the DB for reconciliation.

**TX_final:** CAS `INITIALIZING → PENDING` (success) or `→ FAILED` (any item failed), with compensation enqueued in the same atomic.

No DB session is active during external HTTP calls. This is the most operationally important rule after the oversell guard — holding a transaction across a network call causes lock contention that compounds under load.

### Timeout handling — the design centerpiece

On a `reserve()` timeout we cannot know if the hold was placed. Three options:

- **Assume success**: may confirm un-held inventory.
- **Assume failure**: leaves an orphaned hold at the provider indefinitely.
- **Fail-closed + reconcile**: what we do. Mark item `PENDING_UNKNOWN`. Enqueue a `RECONCILE` task that releases by idempotency key — safe no-op if nothing was held, release if something was.

Three patterns compose here: saga compensation, transactional outbox (durable enqueue), and idempotency key (release is retry-safe). We distinguish timeout (unknown outcome → RECONCILE) from definitive failures like connection refused or 4xx (no hold possible → no RECONCILE needed).

### Provider abstraction with three adapters

`InternalAdapter`, `ExternalReserveAdapter`, and `SoftHoldAdapter` implement the same `ReservableProvider` protocol. The orchestrator has zero `if provider.type ==` branches in the reservation flow — routing happens once in `_build_adapter()`. Adding a new provider type means a new adapter class, not changes to the orchestrator.

**SoftHoldAdapter** is for read-only external providers that expose only a stock-query API. We mirror their inventory in our DB (kept fresh by a background sync worker) and reserve against the local mirror. No HTTP call during the hot reservation path. Accepted trade-off: sync lag can cause soft-oversell, caught at confirm time and routed to `NEEDS_RESOLUTION`.

**HTTP concerns are separated from provider logic.** `ProviderHttpClient` owns base URL, idempotency header injection, and auth. Auth strategies (`BearerAuth`, `ApiKeyAuth`, `NoAuth`) are selected at adapter construction from the provider's `capabilities.auth_type` field. Adding a new auth scheme is one class, zero adapter changes.

### Transactional outbox for durable side effects

The outbox solves the "two operations, one must not fail" problem. Release intent is written in the same transaction as the status change. The outbox worker drains with retries and exponential backoff. Durability transfers from "hope the process doesn't crash between two operations" to "at-least-once with idempotent operations." A dedicated outbox table (not a status column) is correct because each reservation item needs independent retry metadata.

---

## What we kept simple

**Auth — stored but not rotated.** Provider credentials are stored encrypted (`EnvEncryptedSecretProvider`). The `SecretProvider` port exists to swap in Vault without touching adapters. Secret rotation is a production concern deliberately deferred — the interface boundary is in place.

**Resilience — timeout + circuit breaker implemented, not wired.** `TimeoutDecorator` wraps every adapter call. `CircuitBreakerDecorator` is also implemented — three-state (closed/open/half-open), Redis-backed so state is shared across instances. Failure counter uses `INCR + EXPIRE` (rolling window); open flag uses `SET ex cooldown` (TTL gives free auto-cooldown); half-open probe uses `SET NX`. It is not yet injected into `_build_adapter` because it requires a Redis client to be available at construction time — wiring it in is the next step once Redis is provisioned.

**Expiry sweeper implemented, kept simple.** The sweeper (`workers/expiry_sweeper.py`) scans for `PENDING` reservations past `expires_at`, atomically flips them to `EXPIRED` in a single `UPDATE ... RETURNING` (no SKIP LOCKED needed — the UPDATE itself is the claim), then releases internal stock inline and enqueues `RELEASE` outbox tasks for external items. All DB work, no HTTP. The slow HTTP release calls run in the outbox worker. The sweeper follows the same polling loop pattern as the outbox worker.

**No registry class.** `_build_adapter()` on each use case is equivalent (no type-conditionals in flow, adapter construction is encapsulated) but duplicated. A dedicated registry would be independently testable and reusable. Simplification was intentional; the debt is real.

**Sequential external calls.** For a multi-provider cart, provider calls execute one-by-one. `asyncio.gather()` would reduce latency from `sum(latencies)` to `max(latencies)` — the code change is small, the compensation logic for parallel partial failures is more complex.

---

## Assumptions where the spec was silent

**All-or-nothing reservations.** If any item in a cart fails to reserve, the whole reservation fails and all held items are compensated. Partial reservations create downstream UX problems and complicate the order flow.

**Confirm is forward-only.** Once payment is taken, we drive toward completion. A confirm failure becomes `PENDING_FULFILMENT` with outbox retry; a definitive rejection becomes `NEEDS_RESOLUTION` requiring human intervention. We do not attempt to undo a payment.

**Service-to-service auth, not user auth.** `user_id` comes from a trusted upstream service. The spec marks user auth as out of scope.

**Provider capability as configuration, not code.** `ProviderCapabilities` is populated from a JSONB column. Setting `unconfirm=true` on a provider record changes its behavior without a code deployment.

---

## Scenarios demonstrated

We chose the four scenarios that cover the most distinct failure modes:

1. **Internal reserve with concurrency proof.** 5 concurrent reserves on 1 unit of stock → exactly 1 PENDING, 4 FAILED. Exercises real concurrent Postgres transactions, not mocks. Proves the conditional UPDATE holds under contention.

2. **External reserve (mocked HTTP).** Proves the provider abstraction — the orchestrator is unchanged whether the adapter does a DB write or an HTTP call. `assert_called_once()` verifies the execution path is correct.

3. **External reserve + confirm, full happy path.** End-to-end TCC lifecycle: reserve → confirm → `CONFIRMED` + order created. Proves the two CAS windows (`PENDING→CONFIRMING`, then confirm + order) work correctly.

4. **Timeout → PENDING_UNKNOWN + RECONCILE.** Simulates a provider timeout and asserts the item is marked `PENDING_UNKNOWN` and a `RECONCILE` outbox task is enqueued. This is the architecturally most interesting failure case — the system must survive not knowing whether a hold was placed.

Additional tests cover: provider rejection (definitive failure, no RECONCILE), idempotency (duplicate key returns same reservation), soft-hold (read-only provider reserves against local DB mirror), and insufficient stock (TX1 rollback, nothing persisted).

---

## What we'd do differently with more time

**Event-sourced inventory ledger.** Replace `qty_on_hand`/`qty_reserved` counters with append-only movement rows. Available balance = `SUM(on_hand) - SUM(reserved)`. Each reserve is an INSERT (no hot-row lock contention), the audit trail is free, and idempotency is a unique constraint violation.

**Circuit breaker.** Three-state, state in Redis (shared across instances). Open threshold: N failures in a rolling window. This is the highest-impact missing piece for production reliability.

**Alembic migrations.** Currently `Base.metadata.create_all()` for dev. Production requires reviewed migration scripts for every schema change.

**Parallel provider calls.** `asyncio.gather()` across external items in the same cart. Changes user-facing latency from `sum(latencies)` to `max(latencies)`.
