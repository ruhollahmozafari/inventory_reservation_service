# Design Notes — Inventory Reservation Service

> The long-form **why** behind the build. `BUILD-SPEC.md` says *what* to build; this file
> explains the reasoning, the pattern trade-offs, and the alternatives we deliberately
> rejected, so edge cases get resolved consistently with how we decided them. Every decision
> here is **locked** — this is a record, not an open discussion.

---

## 1. The one problem under everything

Strip away the CRUD and there is a single hard problem: **keep an inventory count correct
while two kinds of consistency collide.**

- **Inside our DB** we have ACID. Reserving internal stock is local and strongly consistent —
  it can be made *provably* oversell-free.
- **Across the boundary to an external provider** we have a network. Calls time out, succeed
  late, or fail after partially applying. We cannot wrap our DB and their API in one
  transaction, so this side is only *eventually* consistent. The honest question that drives
  the whole design: **what do we do when we don't know whether the remote call took effect?**

Almost every decision below is really about managing that second boundary. A second, smaller
problem rides along: the **lifecycle state machine**, where releasing a hold on expiry must be
reliable even though releasing an *external* hold is itself a flaky network call.

---

## 2. The domain (and why it IS the TCC pattern)

The business flow — reserve, then confirm on payment or release on failure — *is* **TCC
(Try / Confirm / Cancel)**. We don't bolt it on; we implement Try/Confirm/Cancel per provider
type, and the difference between types is the heart of the consistency story:

| Provider type | Try | Confirm | Cancel | Consistency |
|---|---|---|---|---|
| **Internal** (DB) | conditional reserve | consume | release | **Strong** — all local ACID |
| **External, reserve-capable** | `reserve()` → hold ref | `confirm(ref)` | `release(ref)` | **Eventual** — idempotency + reconcile |
| **External, read-only** | *no remote Try* → local soft-hold vs cache | local re-check + consume | local release | **Weak** — oversell risk, mitigated + documented |

The third row is where the honesty lives: a read-only provider can't actually hold its stock,
so we bet on a cached number and accept residual oversell risk. Everything in §4 exists to make
the second row trustworthy and to make Cancel/release *reliable* rather than best-effort.

---

## 3. The mental model: source of truth

- **Internal** stock: our DB *is* the source of truth → strong consistency, conditional updates.
- **External** stock: the provider is the source of truth; our row is a **cached projection** and
  the real hold is the provider call.

Holding this line explicit is the single most important conceptual decision in the design.

---

## 4. Pattern decision log (all locked)

### 4.1 Two-phase commit / XA — REJECTED
Can't 2PC against an arbitrary REST API; holds locks across the network; trades availability for
consistency — wrong trade for a checkout path. **We accept eventual consistency at the boundary
and design for it.** Being able to state this rejection is itself a senior signal.

### 4.2 Saga — ADOPTED (lightweight, orchestration)
A multi-line cart spans providers, so all-or-nothing needs compensation = a saga. Orchestration
(one coordinator drives the short synchronous flow) beats choreography (scatters logic, needs an
event bus). **Implemented as plain code in `ReservationService`; the reservation row + item
sub-states ARE the saga state.** No framework, no event bus. Caveat that motivates the outbox: a
compensating `release` is itself a network call that can fail, so compensations must be durable.

### 4.3 Idempotency — MANDATORY, everywhere
Checkout retries, provider-call retries, and at-least-once outbox delivery all require it.
Three layers: `idempotency_key` on create (retry returns the existing reservation); idempotency
keys on every outbound provider call; idempotent state transitions (confirm/cancel are no-ops if
already in the target state). This underpins every retry-based pattern.

### 4.4 Transactional outbox — ADOPTED (dedicated table, Postgres, no broker)
Solves the **dual-write**: when one op must both commit a DB change *and* cause a side effect,
doing them as two steps risks losing one on a crash (e.g. reservation expires, we mark `EXPIRED`,
crash before `release()` → leaked remote hold). Fix: write the side-effect intent into an
`outbox` row **in the same transaction** as the state change; a worker drains it with retries
until the provider acks.

**Dedicated table, not a status column** (decided): a reservation has N items across M providers,
so a single status can't track per-line progress, and retry metadata (`attempts`, `next_run_at`
backoff, `last_error`, lease) doesn't belong on a domain entity. One generic worker handles all
task types (`RELEASE`, `CONFIRM`, `UNCONFIRM`, `RECONCILE`). **No Kafka/RabbitMQ** — a Postgres
table polled with `SKIP LOCKED` is sufficient and keeps the repo to docker-compose Postgres. A
broker does **not** replace the outbox; you'd still need the outbox to safely get into the broker.
Graduating to a broker is a scale decision, documented in SCALABILITY with its trigger conditions.

The happy-path **reserve is NOT routed through the outbox** — it's synchronous so checkout fails
fast. The outbox is only for durable effects nobody is waiting on (release, unconfirm, reconcile,
retried confirms).

### 4.5 Timeout / unknown-outcome on reserve — the centerpiece
On `reserve(key)` timeout we don't know if the hold took. Policy: mark the line
`PENDING_UNKNOWN`; **fail the reservation** (all-or-nothing — never confirm on an uncertain
hold); enqueue a `RECONCILE` task that ensures no orphan hold remains (release-by-key, a safe
no-op if nothing was held). This is the saga compensation made durable by the outbox and safe by
idempotency — the three patterns doing one job.

### 4.6 Inventory concurrency — conditional atomic update
The oversell guard is one statement:
`UPDATE inventory SET qty_reserved = qty_reserved + :q WHERE id=:id AND (qty_on_hand - qty_reserved) >= :q`.
`rowcount==1` ⇒ success; `0` ⇒ insufficient. No explicit lock; the `WHERE` is the guard.
Chosen over `SELECT FOR UPDATE` (needed only for multi-statement logic) and optimistic-version
(adds a retry loop for no gain here). Consume/release are analogous single statements.

### 4.7 Expiry — sweeper + lazy, releases via outbox
Sweeper claims expired `PENDING` (`FOR UPDATE SKIP LOCKED`), flips to `EXPIRED`, and **enqueues**
release tasks — all local, fast, **no HTTP in the sweeper**. Lazy check on read as a cheap
backstop. The slow release calls run in the outbox workers (scalable, retried per line). Rejected
per-reservation schedulers/delay-queues (extra infra + their own dual-write).

### 4.8 Inventory representation — counter now, ledger noted
Counter columns (`qty_on_hand`/`qty_reserved`) are correct, simple, trivially testable. An
event-sourced **ledger** (append-only movements, balance as projection) gives free audit +
natural idempotency but needs projections/snapshots — named in ARCHITECTURE.md as the strongest
"with more time / at scale" upgrade, not built now.

### 4.9 CQRS / system-wide event sourcing — REJECTED
Status reads are a row lookup; no read/write asymmetry under pressure yet. Splitting models would
multiply moving parts for no benefit. Considered and rejected deliberately.

### 4.10 Resilience — timeouts + bounded retries + circuit breaker
Timeouts on every provider call (always). Bounded retry with backoff for idempotent ops + safe
error classes only. **Circuit breaker: three-state (closed/open/half-open), Redis-backed shared
state** so all instances coordinate — `INCR`+`EXPIRE` for counts, `SET … EX cooldown` for the
open flag (Redis TTL = free auto-cooldown), `SET NX` to gate the half-open trial. Implemented as a
`CircuitBreakerDecorator` over the provider port. Bulkheads are noted as a scale concern.

---

## 5. No type conditionals — the patterns that replace them

The internal/external/read-only difference is **encapsulated inside adapters** so the
orchestrator has **zero `if type ==` branches** (a scattered-conditional design is an explicit
failure here):

- **Strategy** — provider port + `InternalAdapter`, `ExternalReserveAdapter`, `SoftHoldAdapter`.
  The read-only adapter implements the *same* reserve/confirm/release interface but does it
  **locally against cache**, keeping the orchestrator path uniform.
- **Interface Segregation** — `ReadableProvider` (availability) vs `ReservableProvider`
  (reserve/confirm/release). Capability = which interface an adapter implements.
- **Registry/Factory** — `provider_id → adapter` at runtime. No switch.
- **Decorator** — timeout / retry / circuit-breaker / metrics composed around any adapter.

The only acceptable capability check is at registration/resolution time, never in the flow.

---

## 6. Concurrency & correctness — three mechanisms, one rule

**Rule 0: never hold a DB lock across a provider HTTP call.** Three distinct mechanisms, each
for a different job — conflating them is the usual mistake:

1. **Inventory mutation → conditional atomic update** (§4.6). Makes oversell impossible without
   explicit locks.

2. **Reservation transitions → compare-and-swap on `status`.** The confirm-vs-expiry race is
   resolved not by a held lock but by atomic guarded transitions; first committer wins:
   - confirm claims the entity: `UPDATE reservation SET status='CONFIRMING' WHERE id=:id AND status='PENDING'`
   - sweeper: `UPDATE reservation SET status='EXPIRED' WHERE id=:id AND status='PENDING' AND expires_at<now()`

   `rowcount=0` ⇒ someone else won → back off. The transient **`CONFIRMING`** status is the
   "hands off, I'm processing this entity" flag (analogous to `PROCESSING` on a task row). If
   expiry wins first, confirm's CAS fails → paid-but-expired edge → `NEEDS_RESOLUTION`.

3. **Worker coordination → lease + `SKIP LOCKED`**, in three phases, **HTTP never inside a held
   lock**:
   - **Claim (short tx):** `SELECT … FOR UPDATE SKIP LOCKED LIMIT N`; set `status='PROCESSING'`,
     `locked_until=now()+lease`, bump `attempts`; **commit** (lock released here).
   - **Process (no lock):** the provider HTTP call(s).
   - **Finalize (short tx):** `DONE`, or `FAILED` + `next_run_at=now()+backoff` + `last_error`.

   Other workers claim `status='PENDING' OR (status='PROCESSING' AND locked_until<now())`, so they
   skip in-flight rows and reclaim leases from dead workers. Idempotency makes reclaim-after-crash
   safe. `SKIP LOCKED`'s only job is letting workers claim **disjoint batches** without blocking
   or double-claiming — it is *not* a processing lock.

---

## 7. The flows (transaction boundaries + sync/async)

**Create — SYNCHRONOUS (the result is the response):** resolve adapters; run **Try** per line
(internal conditional reserve; external `reserve()` over HTTP — sync, bounded by timeout +
breaker; read-only local soft-hold). Any failure → compensate held lines (internal inline,
external enqueued); a reserve **timeout** is fail-closed → `PENDING_UNKNOWN` + `RECONCILE`. All ok
→ `PENDING`, `expires_at = now+TTL`. Idempotency-keyed.

**Confirm (payment-success event) — SYNC happy path, failures → background.** Asymmetry that the
original draft missed: **once payment is taken the direction flips from compensation to
forward-completion.** Steps: CAS `PENDING→CONFIRMING`; consume internal + attempt external
`confirm()`; all ok → create order (`CONFIRMED`). On confirm **timeout/failure** → order
`PENDING_FULFILMENT` + enqueue `CONFIRM` tasks to **forward-retry** (idempotent) to completion —
never rolled back. On **definitive rejection** (e.g. not-enough-stock) → `NEEDS_RESOLUTION`; for
all-or-nothing, compensate already-confirmed sibling lines via `UNCONFIRM` *if the provider
advertises that capability*, else release + refund-needed event. `unconfirm` is an **optional
capability**; the spec doesn't promise it, so absence ⇒ forward-retry + ops/refund.

**Cancel (abandon / payment failed) — BACKGROUND:** CAS to `CANCELLED`, enqueue releases.

**Expire — BACKGROUND:** sweeper claims, flips to `EXPIRED`, enqueues releases.

**Read-only specifics:** no remote reserve, no remote confirm — everything local. "Confirm" =
final availability **re-check** + local consume. If the re-check fails, that line **fails
locally** (roll back the local cache decrement); the compensation then applies to the **sibling**
reserve-capable lines, not to the read-only line itself (nothing remote was ever confirmed there).
Oversell mitigation: short cache TTL + periodic availability-sync worker + confirm-time re-check;
residual risk routes to `NEEDS_RESOLUTION`. (We chose re-check-at-confirm as the primary defense;
a safety buffer is an easy optional add.)

---

## 8. Sync vs. async (rule: anything the caller doesn't need in the response goes background)

**Synchronous:** create reservation incl. `reserve()` HTTP (the one accepted network call in the
hot path); confirm happy path (local consume + order + best-effort external confirms); availability
checks during create.

**Background (outbox + workers):** expiry; cancel; all release/unconfirm HTTP; forward-retry of
failed confirms; reconciliation of `PENDING_UNKNOWN`; periodic read-only availability sync.

---

## 9. Clean Architecture + lightweight DDD

Dependency rule: domain at the center (no framework imports), infra at the edges. Tactical DDD
only where it earns its place: aggregates, value objects (`Quantity`, `ProviderRef`),
repositories (ports), domain events — **no** heavy ceremony, **no** multiple bounded contexts.
Load-bearing DDD point: **aggregate boundaries = transaction boundaries.** `Reservation + items`
is one aggregate (one tx); `Inventory` is a separate aggregate mutated by its own conditional
update — you never lock the reservation to touch stock.

---

## 10. Secrets

Separate **config** (url/timeouts/capabilities — DB plaintext, fine) from **secrets** (API
keys — encrypted at rest with the key held *outside* the DB; table permissions don't protect
backups/replicas/dumps, encryption does). Access via a **`SecretProvider` port** (Strategy). Ship
`EnvEncryptedSecretProvider` now; `VaultSecretProvider` is a documented seam — **Vault deferred**.

---

## 11. Decisions at a glance

| Concern | Choice | Pattern(s) | Rejected |
|---|---|---|---|
| Cross-system atomicity | accept eventual consistency | — | 2PC/XA |
| Reserve→confirm→release | Try/Confirm/Cancel | **TCC** | — |
| Multi-provider all-or-nothing | orchestrated steps + compensation | **Saga (lightweight)** | choreography, saga framework |
| Reliable external effects | durable work ledger | **Outbox (dedicated table, Postgres)** | status column; Kafka/RabbitMQ |
| Safe retries | dedupe at create/call/transition | **Idempotency** | — |
| Reserve timeout | fail-closed + reconcile | saga+outbox+idempotency | "assume success" |
| No oversell (internal) | guarded atomic decrement | **conditional UPDATE** | read-then-write; heavy locks |
| Reservation race (confirm vs expire) | guarded transition | **CAS on status** (`CONFIRMING`) | held row lock |
| Worker coordination | claim + lease, no lock over HTTP | **lease + `SKIP LOCKED`** | locking across HTTP |
| Confirm failure | forward-retry; optional unconfirm | — | rollback/release |
| Expiry | sweeper + lazy, release via outbox | — | per-reservation scheduler |
| Type dispatch | adapters, no conditionals | **Strategy+Registry+Decorator+ISP** | if/else on type |
| Inventory model | counter columns | — (ledger = future) | event-sourced ledger now |
| Read/write split | none | — | CQRS / event sourcing |
| Resilience | timeout + retry + breaker | Decorator; **Redis-shared breaker** | unbounded calls |
| Secrets | encrypted in DB behind a port | Strategy | plaintext; Vault now |
| DB | Postgres, sole source of truth | — | NoSQL; broker |

---

## 12. Resolved assumptions (were "open", now locked)

1. **All-or-nothing** reservations — no partials.
2. **Confirm-time external failure:** timeout → `PENDING_FULFILMENT` + forward-retry; definitive
   rejection / paid-but-expired → `NEEDS_RESOLUTION` + compensation (optional `unconfirm`, else
   release/refund event).
3. **Read-only oversell:** confirm-time re-check primary; short TTL + periodic sync;
   buffer optional; residual risk → resolution path.
4. **Demonstrated scenarios:** (A) external reserve success → held; (B) external reserve timeout
   → fail-closed + reconcile. Bonus: internal no-oversell concurrency; read-only stale data.
5. **Credentials:** encrypted in DB behind `SecretProvider`; Vault deferred.

---

## 13. Kept simple vs. invested (for ARCHITECTURE.md)

**Invested:** oversell-safe inventory; provider abstraction with no type conditionals;
timeout/reconcile path; reservation state machine + concurrency mechanisms; idempotency.
**Kept simple:** stubbed auth/payment; encrypted-DB secrets (no Vault); in-process fakes with
fault injection (no separate HTTP service); minimal HTTP surface; counter inventory (no ledger).
**With more time:** event-sourced inventory ledger; broker-backed outbox; Vault + rotation;
read-replica status reads; per-provider bulkheads.