# MediSlot — DynamoDB Design

## 1. Purpose

DynamoDB stores operational data that benefits from predictable key-based access, conditional writes, TTL-assisted cleanup, and high-concurrency coordination. RDS remains the durable relational store for core business records.

## 2. `SlotInventoryTable`

**Purpose:** Materialised appointment slots and atomic capacity accounting.

Suggested key:
- Partition key: `slotId` (string).

Core attributes:
- `doctorId`
- `consultationTypeId`
- `startAt`, `endAt` (canonical timestamp values; interpreted and displayed as IST)
- `capacity`
- `confirmedCount`
- `activeHoldCount`
- `availableCount` (if maintained, it must remain consistent with the other counters)
- `status`
- `version`
- `createdAt`, `updatedAt`

Access patterns:
1. Get a slot by `slotId`.
2. Query a doctor's slots for a time range using a doctor/time GSI, e.g. partition key `doctorId`, sort key `startAt`.
3. Find available slots for discovery by doctor and time range.
4. Atomically reserve a confirmed booking.
5. Atomically reserve a temporary waitlist offer hold.
6. Release a booking or expired/declined hold exactly once.

Capacity invariant:

`confirmedCount + activeHoldCount <= capacity`

A confirmed booking and an active waitlist offer both consume the same slot capacity. The conditional update must verify that capacity remains before incrementing the relevant counter. A waitlist offer record without a corresponding inventory hold does not reserve a seat.

Where multiple records must change together, use DynamoDB transactions or an equivalent conditional, idempotent protocol. Counter updates must be protected against duplicate retries using operation identifiers/idempotency records.

## 3. `IdempotencyKeysTable`

**Purpose:** Prevent duplicate effects when clients retry booking, cancellation, reschedule, registration, or other supported operations.

Suggested key:
- Partition key: `idempotencyKey` (string).

Attributes:
- `operationType`
- `requestHash`
- `userId`
- `status` (`IN_PROGRESS`, `COMPLETED`, `FAILED` as appropriate)
- `resourceId`
- `responseSnapshot` where safe and useful
- `createdAt`
- `expiryTimestamp` (Unix epoch seconds; TTL attribute)

Access patterns:
1. Get idempotency record by key.
2. Conditionally create the key before performing the operation.
3. Return the prior result for a repeated matching request.
4. Reject reuse of the same key with a different request payload.
5. Expire records after 24 hours.

TTL deletion is asynchronous. Application logic must enforce the 24-hour validity window using the stored expiry value; it must not rely on physical deletion happening at the exact expiry second.

## 4. `WaitlistQueueTable`

**Purpose:** Maintain operational waitlist ordering and eligibility.

Suggested key:
- Partition key: `slotId` (string).
- Sort key: `queueKey` (string), constructed to preserve FIFO ordering, e.g. timestamp plus unique entry identifier.

Attributes:
- `waitlistEntryId`
- `patientId`
- `doctorId`
- `status`
- `createdAt`
- `priorityTimestamp`
- `requestedSlotId` or matching criteria
- `version`

Access patterns:
1. Query entries for a slot in FIFO order.
2. Retrieve an entry by its slot and queue key.
3. Update an entry's state conditionally.
4. Enforce a maximum of five active waitlist entries per patient using a patient-scoped count/coordination item or transaction-safe mechanism.

The queue must skip ineligible, cancelled, expired, or already-served entries without changing FIFO ordering for eligible entries.

## 5. `WaitlistOffersTable`

**Purpose:** Track offers and their 15-minute expiry.

Suggested key:
- Partition key: `offerId` (string).

Attributes:
- `waitlistEntryId`
- `patientId`
- `slotId`
- `status` (`OFFERED`, `ACCEPTED`, `DECLINED`, `EXPIRED`, `CANCELLED`)
- `offeredAt`
- `expiresAt` (human-readable timestamp)
- `expiryTimestamp` (Unix epoch seconds; DynamoDB TTL attribute)
- `operationId` or idempotency reference

Access patterns:
1. Get an offer by `offerId`.
2. Find an active offer by waitlist entry or slot if required by the chosen GSI.
3. Conditionally accept/decline an offer only while it is active and unexpired.
4. Expire the offer and release its associated inventory hold exactly once.

**Capacity coupling:** Creating an offer must atomically reserve one unit in `SlotInventoryTable` and create/update the offer record, using a transaction or an idempotent two-step protocol with compensation. Accepting converts the held unit into a confirmed booking without temporarily double-counting capacity. Declining/expiry releases the hold and allows the next FIFO candidate to be considered.

Use `expiryTimestamp` as the TTL attribute consistently. TTL is cleanup support only; an expiry worker/process must enforce offer expiry and release capacity even if TTL deletion is delayed.

## 6. `SessionsTable`

**Purpose:** Store session-level authentication state and support logout/revocation. Access JWTs remain stateless; there is no `AccessTokensTable`.

Suggested key:
- Partition key: `sessionId` (string).

Attributes:
- `userId`
- `role` (`DOCTOR` or `PATIENT`)
- `refreshTokenHash`
- `status` (`ACTIVE` or `REVOKED`)
- `createdAt`
- `expiresAt` (timestamp used for application validation)
- `revokedAt` (nullable)
- `expiryTimestamp` (Unix epoch seconds; TTL attribute)

Access patterns:
1. Get session by `sessionId` from the access JWT's `session_id` claim.
2. Validate that the session is `ACTIVE`, not expired, and matches the JWT's `sub` and `role`.
3. Find/validate the session during refresh using the refresh-token hash and session data. If reverse lookup by token hash is required, add a GSI with `refreshTokenHash` as its partition key; otherwise use a deterministic session reference carried by the opaque refresh-token record/format. Do not store plaintext refresh tokens.
4. Revoke the current session by setting status to `REVOKED` and recording `revokedAt`.

Refresh tokens are reusable and are not rotated. They remain valid only for the fixed seven-day lifetime of their session. Refresh returns a new 15-minute access JWT only. The session record is the revocation authority. The authorizer must check the session on protected requests; authorizer result caching is configured to zero so a revoked session is not accepted from a cached authorisation result.

## 7. `NotificationDispatchTable`

**Purpose:** Prevent duplicate notification side effects and track dispatch processing.

Suggested key:
- Partition key: `dispatchId` (string), derived from event/recipient/channel or another deterministic dispatch identity.

Attributes:
- `eventId`
- `recipientId`
- `notificationType`
- `status` (`PENDING`, `PROCESSING`, `SENT`, `FAILED`)
- `attemptCount`
- `lastError`
- `createdAt`, `updatedAt`
- `expiryTimestamp` where retention/TTL is appropriate

Access patterns:
1. Get dispatch state by deterministic `dispatchId`.
2. Conditionally claim a pending dispatch.
3. Mark completion or record a retryable failure.
4. Prevent duplicate processing of the same logical notification.

## 8. Table and index summary

| Table | Primary key | Main access pattern |
|---|---|---|
| `SlotInventoryTable` | `slotId` | Get/reserve/release slot; doctor/time query through GSI |
| `IdempotencyKeysTable` | `idempotencyKey` | Deduplicate operation |
| `WaitlistQueueTable` | `slotId` + `queueKey` | FIFO queue query |
| `WaitlistOffersTable` | `offerId` | Offer lookup and conditional response |
| `SessionsTable` | `sessionId` | Authorizer session check and session revocation |
| `NotificationDispatchTable` | `dispatchId` | Idempotent notification dispatch |

GSI definitions should be added only for the access patterns the implementation needs. Every GSI must specify partition key, sort key, projection, and query pattern in the physical table design.
