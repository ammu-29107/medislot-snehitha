# MediSlot — RDS MySQL Schema Design

## 1. Purpose and boundaries

RDS MySQL is the durable relational store for core business entities and transactional records. It stores users, reference data, availability rules, appointments, appointment history, waitlist business records, health-camp records, and reschedule operation history.

**Materialised appointment slots do not belong in RDS.** They are generated from availability rules and stored in DynamoDB. This design intentionally has no `doctor_slots` table.

The schema below is a logical design. Exact physical types, lengths, constraints, and migration details are to be finalised during implementation.

## 2. Identity and reference tables

### `doctors`
- `doctor_id` — primary key.
- `full_name`
- `email` — unique within the doctors table.
- `password_hash`
- `phone`
- `hospital_id` — foreign key to `hospitals`.
- `profile_description`
- `consultation_fee`
- `status`
- `created_at`, `updated_at`

### `patients`
- `patient_id` — primary key.
- `full_name`
- `email` — unique within the patients table.
- `password_hash`
- `phone`
- `date_of_birth`
- `gender` (if collected)
- `created_at`, `updated_at`

Doctor and patient email uniqueness is enforced independently in each table. The same email address may exist once in `doctors` and once in `patients`. There is no shared `email_registry` table.

### `hospitals`
- `hospital_id` — primary key.
- `name`
- `address`
- `city`
- `state`
- `status`
- `created_at`, `updated_at`

Hospitals are preconfigured reference records. Doctor registration selects an existing hospital.

### `specialities`
- `speciality_id` — primary key.
- `name` — unique.
- `description`
- `status`

### `doctor_specialities`
- `doctor_id` — foreign key to `doctors`.
- `speciality_id` — foreign key to `specialities`.
- Composite primary key: (`doctor_id`, `speciality_id`).

### `consultation_types`
- `consultation_type_id` — primary key.
- `name` — unique.
- `duration_minutes`
- `default_capacity`
- `description`
- `status`

Planned types: Quick (15 minutes, capacity 1), Standard (30 minutes, capacity 1), Detailed (60 minutes, capacity 1), and Health Camp (60 minutes, capacity 2–10). Health-camp capacity is configured per camp within the permitted range.

## 3. Doctor availability source data

### `doctor_weekly_availability`
Stores recurring weekly availability patterns, not individual generated slots.

- `availability_id` — primary key.
- `doctor_id` — foreign key to `doctors`.
- `day_of_week` — agreed weekday representation.
- `start_time` — local wall-clock time in IST.
- `end_time` — local wall-clock time in IST.
- `effective_from`
- `effective_until` — nullable.
- `is_active`
- `created_at`, `updated_at`

A doctor may have multiple availability windows on a weekday. Validation must prevent invalid or overlapping windows for the same doctor.

### `doctor_blackout_dates`
- `blackout_id` — primary key.
- `doctor_id` — foreign key to `doctors`.
- `start_datetime` — IST appointment-local date/time.
- `end_datetime` — IST appointment-local date/time.
- `reason`
- `created_at`

Blackout periods suppress slot generation for the affected interval.

**Important:** Neither weekly availability nor blackout records are materialised appointment slots. Generated slot inventory belongs in DynamoDB.

## 4. Appointment records

### `appointments`
- `appointment_id` — primary key.
- `patient_id` — foreign key to `patients`.
- `doctor_id` — foreign key to `doctors`.
- `slot_id` — identifier of the corresponding DynamoDB slot.
- `consultation_type_id` — foreign key to `consultation_types`.
- `scheduled_start` — appointment instant, stored consistently and rendered in IST.
- `scheduled_end`
- `status` — for example `CONFIRMED`, `CANCELLED`, `COMPLETED`, `NO_SHOW`, `WITHDRAWN`.
- `booking_idempotency_key` or an idempotency reference where appropriate.
- `reschedule_count` — constrained to the maximum of two.
- `created_at`, `updated_at`

Recommended indexes:
- (`patient_id`, `scheduled_start`)
- (`doctor_id`, `scheduled_start`)
- (`slot_id`)
- (`status`, `scheduled_start`)

Business constraints include one appointment per patient per slot, no booking of past slots, and no more than three upcoming appointments per doctor. Constraints that span records must be enforced transactionally/through coordinated application logic, not assumed to be guaranteed by a simple row-level constraint.

### `appointment_history`
- `history_id` — primary key.
- `appointment_id` — foreign key to `appointments`.
- `previous_status`
- `new_status`
- `changed_by_role`
- `changed_by_id`
- `reason`
- `changed_at`

### `appointment_reschedules`
- `reschedule_id` — primary key.
- `appointment_id` — foreign key to `appointments`.
- `from_slot_id`
- `to_slot_id`
- `reschedule_number`
- `status`
- `created_at`, `completed_at`

### `reschedule_operations`
Durable tracking for each saga execution.
- `operation_id` — primary key.
- `appointment_id` — foreign key to `appointments`.
- `source_slot_id`
- `target_slot_id`
- `operation_status` — e.g. `INITIATED`, `TARGET_RESERVED`, `APPOINTMENT_UPDATED`, `ORIGINAL_SLOT_RELEASED`, `COMPLETED`, `COMPENSATING`, `RECOVERY_REQUIRED`, `FAILED`.
- `step_name`
- `retry_count`
- `failure_code`, `failure_detail`
- `created_at`, `updated_at`, `completed_at`

The operation record supports recovery and audit. It must be updated safely as the saga progresses.

## 5. Waitlist business records

Operational queue ordering, time-limited offers, and slot holds are coordinated in DynamoDB. RDS may retain durable business records for reporting and audit.

### `waitlist_entries`
- `waitlist_entry_id` — primary key.
- `patient_id` — foreign key to `patients`.
- `doctor_id` — foreign key to `doctors`.
- `requested_slot_id` or requested availability criteria.
- `status`
- `created_at`, `updated_at`

### `waitlist_offer_history` (optional durable audit projection)
- `offer_history_id` — primary key.
- `waitlist_entry_id` — foreign key to `waitlist_entries`.
- `offer_id` — DynamoDB offer identifier.
- `status`
- `offered_at`, `responded_at`, `expired_at`

Waitlist offers are FIFO. An offer provides a 15-minute exclusive hold. A patient may have at most five active waitlist entries. The operational hold and capacity reservation are enforced in DynamoDB; an RDS history row alone does not reserve capacity.

## 6. Health camps

### `health_camps`
- `camp_id` — primary key.
- `doctor_id` — foreign key to `doctors`.
- `hospital_id` — foreign key to `hospitals`, where applicable.
- `title`, `description`
- `start_datetime`, `end_datetime` — scheduled in IST.
- `capacity` — between 2 and 10.
- `status`
- `created_at`, `updated_at`

### `health_camp_registrations`
- `registration_id` — primary key.
- `camp_id` — foreign key to `health_camps`.
- `patient_id` — foreign key to `patients`.
- `status`
- `registered_at`, `cancelled_at`

Recommended uniqueness: (`camp_id`, `patient_id`) to prevent duplicate active registration, with status-aware handling as required.

## 7. Refund ledger

### `refund_ledger`
- `refund_id` — primary key.
- `appointment_id` — foreign key to `appointments`.
- `patient_id` — foreign key to `patients`.
- `amount`
- `refund_percentage`
- `reason`
- `status`
- `created_at`

This is a ledger-only design. It records refund entitlement/accounting; it does not integrate with or initiate an external payment gateway.

Refund policy:
- Patient cancellation more than 48 hours before start: 100%.
- 24–48 hours: 75%.
- 6–24 hours: 50%.
- Less than 6 hours: 0%.
- Doctor cancellation: 100%.
- `NO_SHOW`: 0%.
- Doctor slot cancellation sets affected appointment status to `WITHDRAWN` and grants a full refund.
- Rescheduling has no fee or refund.

Policy values should be configurable through the agreed configuration mechanism rather than duplicated as unexplained constants.

## 8. Schema-wide considerations

- Use primary keys and foreign keys for relational integrity.
- Add indexes for documented lookup patterns; avoid speculative indexes.
- Store timestamps consistently and convert to/from `Asia/Kolkata` at application boundaries.
- Store monetary values using an exact decimal type, not floating point.
- Use explicit status values and audit timestamps.
- Do not add `doctor_slots` to this schema; materialised slot inventory is in DynamoDB.
