# SCALABILITY.md

How the design holds up as traffic grows, what breaks first, and where we kept things simple
on purpose.

## What breaks first

**1. A hot SKU.** Every reservation for one product updates the same inventory row, and Postgres
applies those updates one at a time so we never oversell. That's fine normally, but in a flash
sale that single row becomes the limit. Sharding and replicas don't help — a hot SKU is still one
row on one machine. The fix is to split the SKU's stock into several rows ("buckets") so the load
spreads out; for the rare SKU that's still too hot, a Redis counter — accepting that Redis and
Postgres can then disagree and need reconciling. Buckets can be added later without changing the
reservation logic.

**2. Availability reads.** Showing "in stock" on every product page is far more traffic than
checkouts. Cache it — display availability can be slightly stale, so a short-TTL cache (or a read
replica) absorbs the load. Only the display read is cached; the reservation still checks the real
number. External stock is already served from our local copy, so this never hits the provider's
API.

**3. Provider calls.** A reservation waits on the provider, so a slow or rate-limited provider
slows checkouts. We keep it from blocking with async I/O, fail fast with a timeout and circuit
breaker, and isolate each provider so a bad one can't drag down the rest. Past that there's a
ceiling we don't control, since it's their system.

**4. Database load.** The app and the background workers all share one Postgres, so connections
can run out before raw capacity does. A connection pooler fixes that, and status reads can go to a
read replica. The workers scale out easily, but they all hit the same database and the outbox
table grows fast — so we delete finished rows quickly and, if polling ever gets expensive, move
the outbox to a message broker.

## Trade-offs we made on purpose

**One Postgres as the source of truth.** Right call — it gives us correctness for free and is easy
to run. It holds until a SKU gets hot or we run out of connections, and the fixes above come well
before a second database.

**Synchronous reservation.** Right call — a reservation has to tell the user "yes, you have it"
before they pay, so doing it in the background would just move failures past payment where they
cost more. The wait on the provider is bounded by the timeout and breaker.

**Polling the outbox instead of a broker.** Right call at this scale — a Postgres table with
workers needs no extra infrastructure. We'd add a broker only when polling load or latency makes
it worth it.