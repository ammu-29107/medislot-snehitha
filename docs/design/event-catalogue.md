# MediSlot — Domain Event Catalogue

## 1. Event design principles

Events communicate completed domain changes to asynchronous consumers. Core booking and appointment records remain authoritative in RDS/DynamoDB; an event is not a replacement for a transaction.

Planned flow:

`Domain Lambda / workflow → EventBridge → SQS → consumer Lambda → SNS (where notification fan-out is required)`

Consumers must tolerate at-least-once delivery. Use stable event IDs and idempotent dispatch/processing records. Do not assume exactly-once delivery from EventBridge, SQS, or Lambda.

## 2. Common event envelope

Each event uses a common envelope:

```json
{
  "eventId": "unique-event-id",
  "eventType": "AppointmentBooked",
  "version": 1,
  "occurredAt": "2026-01-15T10:30:00+05:30",
  "source": "medislot.appointments",
  "correlationId": "operation-or-request-id",
  "data": {}
}
```

`occurredAt` is expressed with an explicit IST offset. Payloads contain identifiers and the minimum information consumers need; avoid secrets and unnecessary personal data.

## 3. Event catalogue

| Event | Publisher | Main consumers | Purpose |
|---|---|---|---|
| `AppointmentBooked` | Booking workflow after durable booking completion | Notification/reminder scheduler; reporting | Confirm a new appointment |
| `AppointmentRescheduled` | Reschedule saga after completion | Reminder scheduler; notification consumer | Replace reminders and notify parties |
| `AppointmentCancelled` | Patient cancellation workflow after durable completion | Waitlist processor; refund ledger; notifications | Process cancellation effects |
| `DoctorSlotCancelled` | Doctor slot-cancellation workflow after affected appointments are updated | Refund ledger; notification consumer; waitlist/slot processor as applicable | Record doctor-initiated cancellation and full refund entitlement |
| `SlotSeatFreed` | Booking/cancellation/hold-release workflow after inventory capacity is released | Waitlist processor | Trigger next eligible FIFO waitlist candidate |
| `WaitlistOfferCreated` | Waitlist processor after offer and inventory hold are created | Notification consumer | Notify patient of 15-minute offer |
| `WaitlistOfferExpired` | Expiry worker after offer is expired and hold released | Waitlist processor | Continue FIFO queue |
| `AppointmentReminderDue` | Reminder scheduler | Notification consumer | Dispatch 24-hour or 2-hour reminder |
| `AppointmentNoShow` | No-show evaluator after slot end plus 30 minutes | Appointment status/refund ledger/reporting | Record no-show; refund entitlement is 0% |

## 4. Payload contracts

### `AppointmentBooked`
```json
{
  "appointmentId": "appointment-id",
  "patientId": "patient-id",
  "doctorId": "doctor-id",
  "slotId": "slot-id",
  "scheduledStart": "2026-01-15T10:30:00+05:30",
  "scheduledEnd": "2026-01-15T11:00:00+05:30"
}
```

### `AppointmentRescheduled`
```json
{
  "operationId": "reschedule-operation-id",
  "appointmentId": "appointment-id",
  "patientId": "patient-id",
  "doctorId": "doctor-id",
  "previousSlotId": "old-slot-id",
  "newSlotId": "new-slot-id",
  "scheduledStart": "2026-01-15T10:30:00+05:30"
}
```

### `AppointmentCancelled`
```json
{
  "appointmentId": "appointment-id",
  "patientId": "patient-id",
  "doctorId": "doctor-id",
  "slotId": "slot-id",
  "cancelledByRole": "PATIENT",
  "refundPercentage": 75
}
```

### `DoctorSlotCancelled`
```json
{
  "doctorId": "doctor-id",
  "slotId": "slot-id",
  "affectedAppointmentIds": ["appointment-id"],
  "refundPercentage": 100,
  "appointmentStatus": "WITHDRAWN"
}
```

### `SlotSeatFreed`
```json
{
  "slotId": "slot-id",
  "releasedCapacity": 1,
  "releaseReason": "APPOINTMENT_CANCELLED",
  "operationId": "operation-id"
}
```

### `WaitlistOfferCreated` / `WaitlistOfferExpired`
```json
{
  "offerId": "offer-id",
  "waitlistEntryId": "waitlist-entry-id",
  "patientId": "patient-id",
  "slotId": "slot-id",
  "expiresAt": "2026-01-15T10:45:00+05:30"
}
```

### `AppointmentReminderDue`
```json
{
  "appointmentId": "appointment-id",
  "patientId": "patient-id",
  "doctorId": "doctor-id",
  "reminderType": "24_HOUR",
  "scheduledStart": "2026-01-15T10:30:00+05:30"
}
```

### `AppointmentNoShow`
```json
{
  "appointmentId": "appointment-id",
  "patientId": "patient-id",
  "doctorId": "doctor-id",
  "slotId": "slot-id",
  "evaluatedAt": "2026-01-15T11:30:00+05:30",
  "refundPercentage": 0
}
```

Payloads are illustrative contracts; identifiers and exact optional fields are to be finalised with the API and schema design.

## 5. Publisher and consumer responsibilities

- Booking publisher emits `AppointmentBooked` only after appointment persistence and capacity reservation are complete.
- Reschedule publisher emits `AppointmentRescheduled` only after the saga reaches `COMPLETED` and slot state is consistent.
- Cancellation publisher emits cancellation events only after appointment state, refund ledger entitlement, and slot capacity changes have been recorded.
- `SlotSeatFreed` is emitted only after capacity has actually been released.
- Waitlist processor must reserve capacity before creating an offer and must release the hold before emitting `WaitlistOfferExpired`/advancing the queue.
- Reminder scheduler checks that the appointment is still eligible before emitting a reminder event.
- No-show evaluator runs 30 minutes after the slot ends and must not mark a cancelled, withdrawn, or otherwise ineligible appointment as no-show.
- Notification consumers deduplicate dispatches and do not mutate booking capacity.

## 6. Retry and failure rules

- Use stable event IDs and correlation IDs.
- Configure SQS redrive/dead-letter handling for repeatedly failing consumers.
- Consumers record processing/dispatch state so duplicate deliveries do not duplicate side effects.
- A failed notification does not roll back a completed booking or reschedule.
- A failed event publication is retried independently; it must not repeat the underlying capacity mutation.
- Reconciliation should identify completed domain operations whose event publication remains pending.
