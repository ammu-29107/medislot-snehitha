# MediSlot — Appointment Reschedule Saga

## 1. Purpose and rules

Rescheduling moves an existing confirmed appointment to another available slot while preserving the invariant that the patient has one appointment, not two.

Rules:
- Maximum two reschedules per appointment.
- Reschedule is not permitted within six hours of the appointment start.
- No reschedule fee or refund.
- If the target slot is unavailable, the original appointment remains unchanged.
- All scheduling decisions use `Asia/Kolkata`.

## 2. Participants

- API Gateway and reschedule Lambda: validate request and initiate operation.
- RDS: authoritative appointment and reschedule-operation history.
- DynamoDB `SlotInventoryTable`: target reservation and original-slot release.
- AWS Step Functions: coordinate saga steps, retries, and compensation.
- EventBridge: publish completed domain event.
- SQS/Lambda consumers: asynchronous downstream processing and notifications.

## 3. Durable operation tracking

Before side effects begin, create a durable `reschedule_operations` record in RDS with a unique `operation_id`, appointment ID, source slot ID, target slot ID, and status `INITIATED`.

Update the operation record after each completed step. Suggested states:

`INITIATED` → `TARGET_RESERVED` → `APPOINTMENT_UPDATED` → `ORIGINAL_SLOT_RELEASED` → `COMPLETED`

Failure/recovery states:
- `COMPENSATING`
- `RECOVERY_REQUIRED`
- `FAILED`

The record includes current step, retry count, timestamps, and failure details. Step Functions execution history is useful operational evidence but is not the only durable business record.

## 4. Forward workflow

1. **Validate request**
   - Authenticate the caller and verify `PATIENT` role.
   - Verify appointment ownership and `CONFIRMED` status.
   - Check reschedule count is below two.
   - Check current time is earlier than the six-hour cutoff.
   - Validate target slot is in the future and is for a compatible doctor/consultation type.

2. **Create operation**
   - Create a unique operation record in RDS.
   - Enforce idempotency for the reschedule request.

3. **Reserve target slot**
   - Use a DynamoDB conditional write/transaction to reserve one unit only if capacity is available.
   - Record `TARGET_RESERVED`.

4. **Update appointment**
   - Update the appointment's slot and scheduled times in RDS transactionally.
   - Increment reschedule count and append reschedule history.
   - Record `APPOINTMENT_UPDATED`.

5. **Release original slot**
   - Decrement the original slot's confirmed reservation exactly once.
   - Record `ORIGINAL_SLOT_RELEASED`.

6. **Complete**
   - Mark the operation `COMPLETED`.
   - Publish `AppointmentRescheduled` using a stable event ID and operation correlation ID.
   - Trigger downstream notification/reminder recalculation.

## 5. Failure handling and compensation

| Failure point | Handling |
|---|---|
| Validation fails | Stop before side effects; original appointment unchanged |
| Operation record creation fails | Do not reserve target |
| Target slot conditional reserve fails | Mark operation failed; original appointment unchanged |
| RDS appointment update fails after target reserve | Mark `COMPENSATING`; release target reservation exactly once; then mark failed |
| Original slot release fails after RDS update | Keep appointment on target; retain operation as `RECOVERY_REQUIRED`; retry release/reconcile; do not create a second appointment |
| Operation status update fails | Recover by inspecting appointment and slot state using operation ID; do not blindly repeat mutations |
| EventBridge publish fails | Retry publication with same event identity; do not repeat booking/capacity mutations |
| Notification consumer fails | Retry independently; do not roll back a completed reschedule |

The brief interval after the RDS appointment update but before original-slot release can temporarily reserve capacity in both slots. This is a safe temporary over-reservation, not permission to create two appointments. A retry/reconciliation process must release the original slot and complete the operation.

## 6. Idempotency

Every saga execution has a stable operation ID. Each capacity mutation must be attributable to that operation and protected against duplicate application. Replayed Step Functions tasks must first determine whether their intended side effect has already completed.

Do not rely on Step Functions' retry configuration alone for exactly-once effects. The underlying data operations must be idempotent.

## 7. Recovery and reconciliation

A recovery process identifies operations stuck in non-terminal states beyond an agreed threshold. It compares:
- operation status,
- current RDS appointment slot,
- target slot reservation,
- original slot reservation,
- reschedule history.

It then resumes a safe forward step or performs the defined compensation. Ambiguous cases are marked `RECOVERY_REQUIRED` for controlled investigation rather than applying speculative counter changes.

## 8. Completion event

After the appointment and slot inventory are consistent and the operation is completed, publish `AppointmentRescheduled`. Include the appointment ID, old and new slot IDs, operation ID, event ID, and occurrence timestamp. Consumers deduplicate using event identity.
