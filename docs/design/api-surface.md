# MediSlot — Proposed API Surface

## 1. Conventions

- Base path: `/` (deployment stage prefix is environment-specific).
- Transport: HTTPS through Amazon API Gateway.
- Authentication: bearer access JWT except public discovery and registration/login endpoints.
- Roles: `PUBLIC`, `PATIENT`, `DOCTOR`; `PUBLIC` means no authenticated role is required. There is no admin role.
- Resource ownership is checked in addition to role. A patient/doctor cannot access another user's private records merely by supplying an ID.
- All schedule times are IST (`Asia/Kolkata`).
- This is a proposed contract, not a statement that routes are implemented.

## 2. Authentication

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/auth/register/patient` | PUBLIC | Register patient |
| POST | `/auth/register/doctor` | PUBLIC | Register doctor and select a preconfigured hospital |
| POST | `/auth/login` | PUBLIC | Authenticate and issue access/refresh credentials |
| POST | `/auth/refresh` | PUBLIC with refresh token | Issue a new access token for the existing active session |
| POST | `/auth/logout` | Authenticated | Revoke the caller's current session |

Refresh tokens are reusable for a fixed seven-day session lifetime; refresh does not rotate the refresh token.

## 3. Public discovery

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/doctors` | PUBLIC | Search/list doctors |
| GET | `/doctors/{doctorId}` | PUBLIC | View public doctor profile |
| GET | `/doctors/{doctorId}/availability` | PUBLIC | View available slots |
| GET | `/slots` | PUBLIC | Search available materialised slots |
| GET | `/slots/{slotId}` | PUBLIC | View slot details |
| GET | `/consultation-types` | PUBLIC | List consultation types |
| GET | `/hospitals` | PUBLIC | List preconfigured hospitals |
| GET | `/hospitals/{hospitalId}` | PUBLIC | View hospital details |
| GET | `/specialities` | PUBLIC | List specialities |

## 4. Doctor operations

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/doctors/me` | DOCTOR | View own profile |
| PUT | `/doctors/me` | DOCTOR | Update own profile |
| GET | `/doctors/me/availability` | DOCTOR | View own weekly availability |
| PUT | `/doctors/me/availability` | DOCTOR | Create/replace own weekly availability patterns |
| POST | `/doctors/me/blackout-dates` | DOCTOR | Create a blackout period |
| DELETE | `/doctors/me/blackout-dates/{blackoutId}` | DOCTOR | Remove own blackout period |
| GET | `/doctors/me/appointments` | DOCTOR | View own appointments |
| POST | `/doctors/me/slots/{slotId}/cancel` | DOCTOR | Cancel own slot and process affected appointments |

The blackout-date collection uses POST to create and DELETE by identifier to remove. No separate endpoint is proposed unless a distinct read/update operation is required by the final product contract.

## 5. Patient operations

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/patients/me` | PATIENT | View own profile |
| PUT | `/patients/me` | PATIENT | Update own profile |
| GET | `/patients/me/appointments` | PATIENT | List own appointments |
| GET | `/patients/me/appointments/{appointmentId}` | PATIENT | View own appointment details |
| POST | `/appointments` | PATIENT | Book an available slot |
| POST | `/appointments/{appointmentId}/cancel` | PATIENT | Cancel own appointment |
| POST | `/appointments/{appointmentId}/reschedule` | PATIENT | Reschedule own appointment |

## 6. Waitlist operations

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/waitlists` | PATIENT | Join a waitlist |
| GET | `/patients/me/waitlists` | PATIENT | List own waitlist entries |
| GET | `/waitlists/{waitlistEntryId}` | PATIENT | View own waitlist entry |
| DELETE | `/waitlists/{waitlistEntryId}` | PATIENT | Leave own waitlist |
| POST | `/waitlist-offers/{offerId}/accept` | PATIENT | Accept own active offer |
| POST | `/waitlist-offers/{offerId}/decline` | PATIENT | Decline own active offer |

Waitlist offers are exclusive for 15 minutes. A patient may have at most five active waitlist entries. Offer acceptance/decline verifies ownership, state, and expiry.

## 7. Health-camp operations

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/health-camps` | PUBLIC | List/search health camps |
| GET | `/health-camps/{campId}` | PUBLIC | View camp details |
| POST | `/health-camps/{campId}/registrations` | PATIENT | Register for a camp |
| GET | `/patients/me/health-camps` | PATIENT | List own camp registrations |
| DELETE | `/health-camps/{campId}/registrations/{registrationId}` | PATIENT | Cancel own registration |

Camp capacity is between 2 and 10. Registration must be capacity-safe and idempotent.

## 8. Role and ownership enforcement

- Public discovery endpoints do not expose private patient or doctor data.
- Patient endpoints require `PATIENT` and enforce patient ownership.
- Doctor endpoints require `DOCTOR` and enforce doctor ownership.
- Appointment cancellation/rescheduling requires the owning patient.
- Slot cancellation requires the owning doctor.
- Waitlist and health-camp registration endpoints require `PATIENT`.
- Authentication endpoints validate the relevant credential/session rather than accepting a role supplied by the caller.
- Wrong role or ownership/permission failure returns `403`; missing, invalid, expired, or revoked authentication returns `401`.

## 9. Common response and error expectations

Use consistent JSON response envelopes and structured error codes. Relevant outcomes include:
- `400` invalid request or validation failure.
- `401` missing/invalid/expired/revoked authentication.
- `403` authenticated caller lacks role or ownership permission.
- `404` resource not found or not visible to caller.
- `409` slot conflict, capacity exhausted, duplicate state transition, or idempotency-key payload conflict.
- `429` rate limit exceeded.
- `5xx` unexpected server/dependency failure.

Exact response schemas, pagination conventions, and rate limits should be specified before implementation of each endpoint.
