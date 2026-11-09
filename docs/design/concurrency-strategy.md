# MediSlot — Concurrency and Capacity Strategy

## 1. Objective

Prevent overbooking and duplicate side effects when multiple patients, waitlist workers, or retrying requests act on the same slot concurrently.

## 2. Shared capacity invariant

For every materialised slot:

`confirmedCount + activeHoldCount <= capacity`

- A confirmed appointment consumes one unit.
- An active waitlist offer/hold consumes one unit for its 15-minute exclusive offer period.
- Releasing a booking or hold returns exactly one unit.
- A slot cannot be made available merely because an offer record expired; the corresponding inventory hold must be released.

Quick, Standard, and Detailed consultation slots have capacity 1. Health Camp slots have capacity between 2 and 10.

## 3. Booking reservation

1. Validate patient identity, slot status, slot time, and business constraints.
2. Require an idempotency key for the booking operation.
3. Conditionally update the slot inventory only if the slot is active, not in the past, and `confirmedCount + activeHoldCount < capacity`.
4. Record the booking operation so retries can be recognised.
5. Create the durable appointment record in RDS.
6. Finalise the idempotency record and publish the appointment event.

Where the slot reservation and idempotency record need atomic coordination, use a DynamoDB transaction. The RDS appointment write and DynamoDB reservation span different data stores, so the workflow must define compensation/recovery: if the RDS write fails after a successful reservation, release the reservation exactly once. If the client retries, return the existing outcome rather than reserving another unit.

## 4. Waitlist offer reservation

Waitlist entries are processed FIFO. A patient can have at most five active waitlist entries. When capacity becomes available:

1. Select the earliest eligible queue entry.
2. Conditionally reserve one unit as an active hold in `SlotInventoryTable`.
3. Create the 15-minute offer record.
4. Notify the patient asynchronously.
5. Accept: atomically convert the held unit into a confirmed booking and create the appointment through an idempotent workflow.
6. Decline/expiry: conditionally mark the offer terminal, release the hold exactly once, then process the next eligible queue entry.

The inventory hold and offer state must be coupled through a transaction or an operation-id-based compensation protocol. Creating an offer item alone is not a reservation.

TTL deletion is asynchronous. An expiry worker must compare the stored expiry timestamp to the current time, transition the offer once, and release the hold. A delayed TTL deletion must not extend the patient's exclusive hold.

## 5. Conditional writes and retries

- Use conditional expressions to ensure capacity is available before incrementing counters.
- Use version attributes where compare-and-swap updates are needed.
- Use unique operation identifiers and idempotency keys to make retries safe.
- Treat conditional-check failures as expected contention outcomes, not unbounded retry signals.
- Apply bounded retries with backoff and jitter for transient failures.
- After the retry limit, return a controlled conflict/retry response or move the operation to recovery.

## 6. Booking constraints

- One appointment per patient per slot.
- No booking of past slots.
- A doctor may have at most three upcoming appointments, according to the agreed business definition of upcoming.
- A patient may have at most five active waitlist entries.
- Health Camp capacity must remain within 2–10.
- All slot comparisons and displayed schedule values use `Asia/Kolkata`.

Constraints spanning RDS and DynamoDB must be enforced through transaction coordination and idempotent workflows. They must not be described as automatically guaranteed by a single DynamoDB condition or a simple SQL constraint.

## 7. Reschedule concurrency

The reschedule saga reserves target capacity before changing the appointment. If the target reservation fails, the original appointment remains unchanged. Once the RDS appointment update succeeds, the original slot is released. If release fails, the operation is marked for retry/reconciliation; the system must not create a second appointment or silently discard the original booking.

## 8. Required recovery cases

| Failure | Required handling |
|---|---|
| Slot condition fails | Do not create appointment; return conflict/unavailable |
| Duplicate request | Return the prior idempotent result |
| RDS appointment insert/update fails after slot reserve | Release target reservation exactly once |
| Event publication fails | Retry publication using stable event/operation identity; do not repeat capacity mutation |
| Waitlist offer expires | Expiry worker marks it expired and releases hold once |
| Consumer receives duplicate event | Consumer deduplicates by event/dispatch identity |
| Compensation repeatedly fails | Mark operation `RECOVERY_REQUIRED`, alert/record for reconciliation |

## 9. Operational invariants

- Capacity counters never exceed configured capacity.
- Every active waitlist offer has exactly one corresponding active inventory hold.
- Every confirmed appointment has one corresponding confirmed inventory reservation.
- Every reservation/release is attributable to an operation or appointment identifier.
- Reprocessing an operation does not apply the same increment/decrement twice.
