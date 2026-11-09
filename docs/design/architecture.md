# MediSlot — Architecture Design

## 1. Purpose

MediSlot is a doctor appointment booking and management platform. Patients discover doctors, view availability, book and manage appointments, join waitlists, and register for health camps. Doctors manage profiles, weekly availability, blackout dates, appointments, and slot cancellations.

This document describes the intended architecture before implementation.

## 2. Roles

- `PATIENT`: discover doctors and slots; manage their own profile, appointments, waitlists, and health-camp registrations.
- `DOCTOR`: manage their own profile and availability; view and manage their appointments; cancel their own slots.

There is no admin application role in this design. Hospitals are preconfigured reference records. A doctor selects a hospital during registration; manual approval is not part of the planned registration flow.

## 3. Logical AWS architecture

### Client and API layer
- Web/mobile client communicates over HTTPS.
- Amazon API Gateway exposes the REST API.
- AWS Lambda functions implement authentication, authorisation, doctor, patient, appointment, availability, waitlist, health-camp, and notification operations.
- A Lambda authorizer validates access JWTs and checks the associated session state.

### Networking and data layer
- A VPC contains private application/data networking components.
- Amazon RDS for MySQL stores durable relational records and transactional business data.
- Amazon DynamoDB stores materialised slot inventory and operational coordination records requiring conditional writes and low-latency access.
- Amazon S3 stores appropriate static or generated objects where required; it is not the system of record for appointment transactions.
- AWS Systems Manager Parameter Store stores configuration and secrets. Signing secrets and other sensitive values use `SecureString`.

### Workflow and event layer
- AWS Step Functions coordinates multi-step rescheduling and other workflows that require explicit compensation/recovery.
- Amazon EventBridge distributes domain events.
- Amazon SQS provides durable buffering between event routing and asynchronous consumers.
- AWS Lambda consumers process reminders, waitlist progression, notifications, and other event-driven work.
- Amazon SNS is the planned notification fan-out mechanism. This design does not assume a configured email or SMS delivery gateway.

### Operations
- Amazon CloudWatch provides logs, metrics, alarms, and operational visibility.
- AWS CloudFormation defines infrastructure.
- GitHub Actions validates and deploys CloudFormation stacks using GitHub OIDC federation rather than long-lived AWS access keys.
- AWS X-Ray is not included in the planned architecture.

## 4. Data ownership

| Data | Planned system of record |
|---|---|
| Doctor, patient, hospital and speciality records | RDS MySQL |
| Weekly availability patterns and blackout dates | RDS MySQL |
| Appointments and appointment history | RDS MySQL |
| Reschedule operation history | RDS MySQL |
| Materialised appointment slot inventory and capacity counters | DynamoDB |
| Idempotency records | DynamoDB |
| Waitlist operational queue and time-limited offers/holds | DynamoDB, with durable business records in RDS where required |
| Login session state and refresh-token hash | DynamoDB `SessionsTable` |
| Notification dispatch/idempotency state | DynamoDB |

Materialised slots are not stored in an RDS `doctor_slots` table. RDS holds the source availability rules; DynamoDB holds generated slot inventory.

## 5. Core booking flow

1. The client calls API Gateway with an access JWT.
2. The authorizer validates the JWT and confirms that its `session_id` refers to an active session.
3. The relevant Lambda validates the request and role.
4. The service reads the appointment/slot information from the appropriate data store.
5. A DynamoDB conditional write reserves capacity so concurrent requests cannot exceed slot capacity.
6. The appointment is recorded in RDS.
7. A domain event is published for downstream processing.
8. Asynchronous consumers handle waitlist progression, reminders, and notification dispatch.

The implementation must use idempotency and recovery mechanisms so retries do not create duplicate bookings or capacity changes.

## 6. Slot and capacity model

Weekly doctor availability patterns and blackout dates are maintained in RDS. A scheduled process materialises a rolling 30-day window of appointment slots in DynamoDB. A daily process advances the window by one day and must be idempotent.

Consultation types:
- Quick: 15 minutes, capacity 1.
- Standard: 30 minutes, capacity 1.
- Detailed: 60 minutes, capacity 1.
- Health Camp: 60 minutes, capacity 2–10.

The same slot inventory/capacity model accounts for confirmed bookings and active waitlist holds. A waitlist offer must reserve capacity atomically; an offer record by itself is not sufficient to reserve a seat.

## 7. Rescheduling

Rescheduling is coordinated by Step Functions. The workflow reserves the target slot, updates the appointment, releases the original slot, records the operation outcome, and publishes the event. Failures require explicit compensation or recovery. The invariant is that one appointment remains associated with one confirmed slot; a failed reschedule must not silently lose the original booking.

Rules:
- Maximum two reschedules per appointment.
- Rescheduling is not permitted within six hours of the appointment start.
- No reschedule fee or refund is applied.
- If the target slot cannot be reserved, the original appointment remains unchanged.

## 8. Authentication overview

Custom authentication is planned:
- Passwords hashed using bcrypt with a work factor of at least 12.
- Minimum password length 10 characters, including at least one letter and one digit.
- HS256 access JWT lifetime: 15 minutes.
- Refresh token: opaque, reusable, fixed seven-day session lifetime, no rotation.
- Refresh returns a new access JWT only.
- `SessionsTable` stores session state; logout revokes the current session.
- Protected requests check session status using the JWT `session_id`.

See [authentication design](authentication-design.md).

## 9. Time standard

All scheduling rules and user-facing appointment times use `Asia/Kolkata` (IST, UTC+05:30). The application does not support user-selectable timezones or daylight-saving-time handling.

## 10. Reliability principles

- Conditional writes protect shared slot capacity.
- Idempotency keys protect retryable client operations.
- Event consumers are designed for at-least-once delivery and must be idempotent.
- Saga steps record durable progress and have explicit compensation/recovery paths.
- TTL is a cleanup aid, not the only mechanism for enforcing expiry or releasing reserved capacity.
