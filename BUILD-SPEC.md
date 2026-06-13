# Build Spec — Inventory Reservation Service

> **How to use this file.** This is a self-contained brief for building the service. Drop it
> into the repo and either (a) paste it as your opening prompt to Claude Code, or (b) save the
> durable parts as `CLAUDE.md` so Claude Code keeps them as persistent project context across
> sessions. Keep `DESIGN-NOTES.md` alongside it — that file holds the long-form *why* behind
> every decision; this file holds the *what to build*. Every choice below is **locked**; do not
> re-litigate them, implement them.

---

## 0. First action before writing code

A project skeleton already exists. **Inspect the current repo structure first** and adapt to
it rather than scaffolding from scratch. Reconcile what's here with the target structure in
§9; if they conflict, prefer the existing layout's conventions and tell me what you changed.

---

## 1. Objective & constraints

Build the backend service that manages **inventory reservations during checkout** for an
e-commerce platform. A reservation temporarily holds stock while the user pays; on payment
success it's confirmed and stock consumed; on abandonment, payment failure, or TTL expiry the
hold is released. Stock can come from several **providers** (the platform's own internal
stock, and external systems reached over an API) that differ in reliability, latency, and
which operations they support.

- **In scope:** create / confirm / cancel / expire reservations; correct inventory across the
  lifecycle; provider integration with real failure handling; reservation status/detail reads.
- **Out of scope (stub):** user auth (assume a verified `user_id` on each request); payment
  processing (assume an inbound payment-outcome event); cart/frontend; provider-onboarding UI.
- **Grading priorities:** correctness, consistency, and design quality — **not** feature count.

**Stack:** Python, FastAPI, SQLAlchemy, **PostgreSQL**, Alembic (migrations), pytest, Docker +
docker-compose (Postgres + app + workers). No message broker — Postgres is the queue. Redis is
used **only** for the shared circuit-breaker state (and optionally read-only availability
cache), never as a system of record.

---

## 2. Architecture

**Clean Architecture + lightweight DDD.** Dependency rule: dependencies point inward; the
domain has no framework imports.

- `api/` — FastAPI routers. Thin: validate input, call a use-case, map result to HTTP.
- `app/` — application/use-case layer: the reservation orchestration (the saga), confirm /
  cancel use-cases, idempotency handling.
- `domain/` — entities, value objects (`Quantity`, `ProviderRef`), the reservation state
  machine, invariants, domain events, repository **interfaces** (ports). No SQLAlchemy here.
- `infra/` — SQLAlchemy models + repository implementations; the provider **port** + adapters
  + registry; provider-call **decorators**; the outbox table + worker; the **fake providers
  with fault injection**; the `SecretProvider` implementations.
- `workers/` — expiry sweeper, outbox/reconciliation drainer, read-only availability sync.
- `tests/` — unit + integration (integration runs against a real Postgres via docker-compose).

**DDD point that is load-bearing, not decoration:** aggregate boundaries = transaction
boundaries. `Reservation + reservation_items` is one aggregate (one transaction). `Inventory`
is a **separate** aggregate, mutated by its own conditional update — never lock the whole
reservation to touch stock. Apply tactical DDD (aggregates, value objects, repositories, domain
events) only where it earns its place; **no** heavy ceremony, no multiple bounded contexts.

---

## 3. Domain model & schema

Source of truth: **internal** provider stock lives in our DB (strong consistency). **External**
provider stock lives in their system; our row is at best a cached projection and the real hold
is the provider call.

```
product(id pk, sku unique, name, created_at)

provider(id pk, name, type ENUM('internal','external'),
         base_url, timeout_ms, capabilities JSONB,     -- {reserve, confirm, release, unconfirm}
         secret_ref,                                    -- pointer to a secret, NOT the secret
         created_at)

inventory(id pk, product_id fk, provider_id fk,
          qty_on_hand int CHECK (qty_on_hand >= 0),
          qty_reserved int CHECK (qty_reserved >= 0),
          version int default 0,                        -- optional optimistic backup
          UNIQUE (product_id, provider_id),
          CHECK (qty_reserved <= qty_on_hand))

reservation(id pk, user_id, idempotency_key unique,
            status ENUM('PENDING','CONFIRMING','CONFIRMED','CANCELLED','EXPIRED','FAILED'),
            expires_at, created_at, confirmed_at)

reservation_item(id pk, reservation_id fk, product_id fk, provider_id fk,
                 qty int CHECK (qty > 0),
                 provider_ref,                          -- remote hold id, if any
                 hold_status ENUM('HELD','PENDING_UNKNOWN','RELEASED','FAILED','CONFIRMED'))

"order"(id pk, reservation_id fk unique, user_id,
        status ENUM('CONFIRMED','PENDING_FULFILMENT','NEEDS_RESOLUTION','FAILED'),
        created_at)
order_item(id pk, order_id fk, product_id, provider_id, qty, unit_ref)

outbox(id pk,
       task_type ENUM('RELEASE','CONFIRM','UNCONFIRM','RECONCILE'),
       payload JSONB, idempotency_key,
       status ENUM('PENDING','PROCESSING','DONE','FAILED'),
       attempts int default 0, next_run_at, locked_until, last_error, created_at)
```

Indexes that matter: `reservation(status, expires_at)` (sweeper), `outbox(status, next_run_at)`
(drainer), `inventory(product_id, provider_id)` unique, `reservation.idempotency_key`.

**Inventory invariants (never break):** `qty_reserved >= 0`, `qty_reserved <= qty_on_hand`,
`available = qty_on_hand - qty_reserved >= 0`.
Operations: **reserve** `qty_reserved += q` (guarded by `available >= q`); **confirm/consume**
`qty_on_hand -= q` **and** `qty_reserved -= q`; **release** `qty_reserved -= q`.

---

## 4. Reservation state machine

```
PENDING ──claim for confirm──▶ CONFIRMING ──all confirms ok──▶ CONFIRMED
   │                               └────────partial fail──────▶ (order: PENDING_FULFILMENT)
   ├──abandon / payment failed──▶ CANCELLED
   ├──TTL elapsed (sweeper)─────▶ EXPIRED
   └──holds couldn't be placed──▶ FAILED
```

`CONFIRMING` is a **transient claim state** — it's how the confirm path tells the expiry
sweeper "hands off." `CANCELLED`/`EXPIRED`/`FAILED`/`CONFIRMED` are terminal. Each
`reservation_item` carries its own `hold_status` because external holds can lag the reservation.

---

## 5. Patterns to apply (and the anti-pattern to avoid)

This domain **is TCC** (Try / Confirm / Cancel) — reserve, then confirm or release. Implement
Try/Confirm/Cancel per provider type, but **encapsulate the type difference inside adapters so
the orchestrator has ZERO `if internal / external / read-only` branches.** That requirement is
central — scattered type conditionals are an explicit failure here.

- **Strategy — provider port + adapters.** `InternalAdapter`, `ExternalReserveAdapter`,
  `SoftHoldAdapter` (read-only). The read-only adapter implements the *same* reserve/confirm/
  release interface but does it **locally against cache**, so the orchestrator path stays
  uniform.
- **Interface Segregation.** Split `ReadableProvider` (check availability) from
  `ReservableProvider` (reserve/confirm/release). Capability = which interface an adapter
  implements, expressed via the `capabilities` descriptor, not a type field.
- **Registry / Factory.** `provider_id → adapter`, resolved at runtime. No switch statements.
- **Decorator.** Wrap any adapter with `TimeoutDecorator`, `RetryDecorator`,
  `CircuitBreakerDecorator`, `MetricsDecorator`. Cross-cutting concerns compose without
  touching core logic.
- **Saga (orchestration, lightweight).** Multi-line carts span providers → all-or-nothing via
  ordered Try steps + compensation. Implement as plain application code in
  `ReservationService`; the reservation row + item sub-states **are** the saga state. **No**
  saga framework, **no** event bus, **no** choreography.
- **Transactional outbox (Postgres).** For durable external side effects only (release,
  unconfirm, reconcile, retried confirms). A dedicated table (chosen over a status column) so
  retry metadata (attempts, backoff, last_error, lease) lives off the domain entities and one
  generic worker handles all task types.
- **Idempotency everywhere.** `idempotency_key` on reservation create (retried create returns
  the existing reservation); idempotency keys on all outbound provider calls; idempotent state
  transitions (confirm/cancel are no-ops if already in the target state).

Explicitly **rejected** (don't add): 2PC/XA, CQRS, system-wide event sourcing, a message
broker. Inventory is counter-based now; an event-sourced ledger is noted only as a future
upgrade in ARCHITECTURE.md.

---

## 6. Concurrency & correctness rules (strict)

**Rule 0: never hold a DB transaction/row lock open across a network call to a provider.**
Three mechanisms, each for a different job:

1. **Inventory mutation — conditional atomic update.** Reserve is one statement:
   ```sql
   UPDATE inventory SET qty_reserved = qty_reserved + :q
   WHERE id = :id AND (qty_on_hand - qty_reserved) >= :q;
   ```
   `rowcount == 1` ⇒ success; `0` ⇒ insufficient stock. This is what makes oversell impossible
   without explicit locks. Consume/release are analogous single statements.

2. **Reservation transitions — compare-and-swap on `status`.** Confirm vs. expiry is resolved
   not by a held lock but by atomic guarded transitions; first committer wins:
   ```sql
   -- confirm claims the reservation:
   UPDATE reservation SET status='CONFIRMING' WHERE id=:id AND status='PENDING';
   -- expiry sweeper:
   UPDATE reservation SET status='EXPIRED'
   WHERE id=:id AND status='PENDING' AND expires_at < now();
   ```
   `rowcount=0` ⇒ someone else won; back off. If expiry wins first, confirm's CAS fails →
   route to the resolution path (paid-but-expired edge → `NEEDS_RESOLUTION`). The transient
   `CONFIRMING` status is the "I'm processing this entity" flag.

3. **Worker coordination — lease + `SKIP LOCKED`.** The outbox/sweeper claim in three phases,
   **HTTP never inside a held lock**:
   - **Claim (short tx):** `SELECT … WHERE status='PENDING' AND next_run_at<=now() FOR UPDATE
     SKIP LOCKED LIMIT N`; set `status='PROCESSING'`, `locked_until=now()+lease`, bump
     `attempts`; **commit** (lock released).
   - **Process (no lock held):** make the provider HTTP call(s).
   - **Finalize (short tx):** set `DONE`, or `FAILED` + `next_run_at=now()+backoff` + `last_error`.
   Other workers' claim query is `status='PENDING' OR (status='PROCESSING' AND
   locked_until < now())`, so they skip in-flight rows and reclaim leases from dead workers.
   Idempotency makes reclaim-after-crash safe.

The **expiry sweeper itself does no HTTP**: it claims expired reservations, flips status, and
**enqueues** one release task per held line — all local, fast — then the outbox workers do the
slow release calls.

---

## 7. The flows (with transaction boundaries & sync/async)

**Create reservation — SYNCHRONOUS (result is the response):**
1. Resolve each line to its adapter via the registry.
2. Run **Try** per line: internal → conditional reserve (short tx); external reserve-capable →
   `provider.reserve(key)` (HTTP, sync, bounded by timeout + breaker) → store `provider_ref` +
   `HELD`; read-only → local soft-hold against cache.
3. Any Try fails → **compensate** already-held lines (internal release inline; external release
   **enqueued to outbox**) → reservation `FAILED`. A reserve **timeout** is fail-closed: mark
   line `PENDING_UNKNOWN`, fail the reservation, enqueue a `RECONCILE` task to ensure no orphan
   hold remains (release-by-key, safe no-op if nothing held).
4. All Try succeed → `PENDING`, `expires_at = now + TTL`. `idempotency_key` dedupes retries.

**Confirm (on payment-success event) — SYNCHRONOUS happy path, failures → background:**
1. CAS `PENDING → CONFIRMING` (claims the entity; if it fails, reservation isn't pending → handle).
2. Consume internal lines; attempt external `confirm()` calls.
3. All ok → create `order` (`CONFIRMED`), reservation `CONFIRMED`.
4. Any external confirm **times out / fails** → order `PENDING_FULFILMENT`; enqueue `CONFIRM`
   outbox tasks to **forward-retry** (idempotent) to completion. Confirm is never rolled back —
   payment is taken, so the direction is drive-to-completion, not compensation.
5. Any external confirm **definitively rejects** (e.g. not-enough-stock) → order
   `NEEDS_RESOLUTION`; for all-or-nothing, compensate already-confirmed sibling lines via
   `UNCONFIRM` (if the provider advertises that capability) else release + emit refund-needed
   event. `unconfirm` is an **optional provider capability**; absence ⇒ forward to ops/refund.

**Cancel (abandon / payment failed) — BACKGROUND:** CAS to `CANCELLED`, enqueue releases. No
user waiting.

**Expire (sweeper) — BACKGROUND:** claim expired `PENDING`, flip to `EXPIRED`, enqueue releases.

**Read-only specifics:** read-only providers have **no** remote reserve and **no** remote
confirm — everything is local. "Confirm" = a final availability **re-check** + local consume.
If the re-check fails, that line **fails locally** (roll back the local cache decrement); the
compensation (`unconfirm`/release) then applies to the **sibling** reserve-capable lines, not to
the read-only line itself. Residual oversell risk is accepted and documented; mitigate with a
short cache TTL + a periodic availability sync worker + confirm-time re-check.

---

## 8. Sync vs. async summary

**Synchronous (request/event path — caller needs the result):** create reservation (incl.
`provider.reserve()` HTTP — the one accepted network call in the hot path); confirm happy path
(local consume + order creation + best-effort external confirms); availability checks during
create.

**Background (outbox + workers — nobody waiting):** expiry; cancel; all release/unconfirm HTTP;
forward-retry of failed confirms; reconciliation of `PENDING_UNKNOWN`; periodic read-only
availability sync.

Rule of thumb: anything whose result the caller doesn't need in the response goes to the
background.

---

## 9. Provider integration details

- **Port** exposes `check_availability`, and (for reservable providers) `reserve`, `confirm`,
  `release`, optional `unconfirm`, plus a `capabilities` descriptor.
- **Three adapters:** `InternalAdapter` (DB), `ExternalReserveAdapter` (HTTP, full lifecycle),
  `SoftHoldAdapter` (read-only, local soft-hold + availability check).
- **Fakes with fault injection** stand in for real external systems (no separate HTTP service):
  configurable latency, error rate, timeout, and stale-data responses, switchable per test.
- **Resilience decorators:** timeout (always), bounded retry with backoff (idempotent ops + safe
  error classes only), **circuit breaker** (see §10), metrics. Composed around the adapter,
  zero conditionals in core logic.

---

## 10. Circuit breaker

Three-state (closed → open → half-open), **Redis-backed shared state** so all app instances
coordinate. Open the breaker after a failure threshold within a window (`INCR` + `EXPIRE` for
counts); while open, fail fast without calling (`SET key EX cooldown` — Redis TTL gives free
auto-cooldown); after cooldown allow one half-open trial gated by `SET NX`. Implement as a
`CircuitBreakerDecorator` over the provider port, per provider.

---

## 11. Secrets

Separate **config** from **secrets**. Provider config (url, timeouts, capabilities) lives in the
`provider` table in plaintext. Secrets (API keys/tokens) are stored **encrypted at rest**
(app-level envelope encryption) with the key-encryption key held **outside** the DB (env var
now; KMS later). Access all secrets through a **`SecretProvider` port** (Strategy). Ship
`EnvEncryptedSecretProvider` (config + `secret_ref` in DB, secret decrypted with a KEK from
env). Leave `VaultSecretProvider` as a documented seam — **do not** build Vault now.

---

## 12. Scenarios to implement & test

**Required demonstrations (≥2 incl. non-happy):**
- **A — External reserve success → hold placed.** (happy path provider call)
- **B — External reserve timeout → fail-closed + `RECONCILE` cleanup.** (the centerpiece)

**Bonus (implement if time allows):**
- Internal concurrency: parallel reserves on the last unit → **no oversell** (the proof test).
- Read-only provider returns stale data → confirm-time re-check rejects the line → sibling
  compensation.
- Multi-provider cart, one line fails → saga compensates the rest.
- Confirm external timeout → `PENDING_FULFILMENT` → outbox forward-retry succeeds.

Integration tests run against a **real Postgres** via docker-compose. The no-oversell test must
exercise true concurrency (multiple threads/connections), not a mocked race.

---

## 13. Deliverables

1. **Working service** + docker-compose (Postgres, app, workers) + Alembic migrations + seed
   data + a README with run/test instructions.
2. **`ARCHITECTURE.md`** — reasoning, not a diagram dump. Must cover: what was kept simple vs.
   invested in (invested: oversell-safe inventory, provider abstraction, timeout/reconcile path,
   state machine, idempotency; simple: auth/payment stubs, encrypted-DB secrets, fakes, minimal
   HTTP surface); assumptions made where the spec was silent (the locked decisions below); what
   you'd do differently with more time (e.g. event-sourced inventory ledger, broker-backed
   outbox, Vault).
3. **`SCALABILITY.md`** — where it holds up and where it breaks **first**, with *why* and *when*,
   no generic "add caching/use a queue." Lead with **hot-row contention on popular SKUs**
   (conditional updates serialize per row): name the threshold and the trade (Redis atomic
   counters, stock bucket-sharding, and their consistency costs). Then: single-sweeper/worker
   throughput (partition + `SKIP LOCKED`), synchronous provider calls in the create path
   (bulkheads, breaker, the latency coupling), single-Postgres write ceiling (read replicas for
   status reads; partition by product/region later), outbox throughput and when it graduates to
   a broker. For each trade-off favoring simplicity, state whether it was the right call at this
   scale and the breaking point.

---

## 14. Locked decisions & assumptions (do not re-open)

- **All-or-nothing** reservations (no partials).
- **Postgres** is the system of record; no NoSQL, no broker. Redis only for breaker state
  (+ optional read-only cache).
- **Outbox = dedicated table**, not a status column.
- **No type conditionals** in the orchestrator — Strategy + Registry + Decorator + ISP.
- **Clean Architecture + lightweight DDD**; aggregate boundaries define transaction boundaries.
- Concurrency: **conditional update** (inventory), **CAS-on-status** (reservation transitions),
  **lease + `SKIP LOCKED`** (workers); **never** lock across an HTTP call.
- Confirm is **forward-retry**, not rollback; `unconfirm` is an **optional** provider capability;
  definitive confirm rejection + paid-but-expired → **`NEEDS_RESOLUTION`** + compensation/refund
  event.
- Read-only providers: local soft-hold + confirm-time re-check; residual oversell accepted +
  documented.
- Secrets: encrypted in DB behind a `SecretProvider` port; **Vault deferred**.
- Sync vs async exactly per §8.

---

## 15. Suggested build order

1. Domain + schema + Alembic migrations + docker-compose Postgres + seed data.
2. Inventory conditional-update ops + **no-oversell concurrency test** (prove correctness early).
3. Provider port + ISP interfaces + three adapters + registry + fakes with fault injection.
4. Resilience decorators (timeout, retry, Redis circuit breaker).
5. Create-reservation saga (sync) + idempotency + compensation + reserve-timeout reconcile.
6. Outbox table + worker (claim/process/finalize, lease, backoff) + `RELEASE`/`RECONCILE`.
7. Confirm flow (CAS transition, order creation, forward-retry via outbox, optional `unconfirm`).
8. Cancel + expiry sweeper (background, enqueue releases).
9. Read-only availability sync worker + confirm-time re-check.
10. Scenarios A & B as integration tests + bonus tests.
11. `ARCHITECTURE.md` + `SCALABILITY.md` + README.

## 16. Definition of done

- `docker-compose up` brings up Postgres + app + workers; README documents run + test.
- Migrations + seed data load cleanly.
- All tests green, including the concurrency no-oversell test and scenario B
  (reserve-timeout → reconcile).
- No `if provider.type ==` branches in the orchestration layer.
- No DB lock is ever held across a provider HTTP call.
- `ARCHITECTURE.md` and `SCALABILITY.md` reason about *why/when*, not generic advice.