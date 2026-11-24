# Milestone 5 Dashboard Appointment Integration

## Patient portal
- Browse doctors and their available slots; booking controls submit the selected `slot_id` to `POST /appointments`.
- Booking requests include a unique `Idempotency-Key` to protect against accidental duplicate submissions.
- View appointments, open appointment detail, and cancel confirmed appointments.

## Doctor portal
- Added an appointments workspace listing bookings and patient contact information (name, phone, email).
- Doctors can cancel a confirmed appointment through `POST /appointments/{appointmentId}/cancel`.
- The schedule view exposes slot cancellation through `POST /doctors/me/slots/{slotId}/cancel`; the backend cancellation workflow handles affected bookings.

## API route mappings
| Dashboard action | API route |
|---|---|
| Patient list | `GET /patients/me/appointments` |
| Patient detail | `GET /patients/me/appointments/{appointmentId}` |
| Book | `POST /appointments` |
| Patient cancel | `POST /appointments/{appointmentId}/cancel` |
| Doctor list | `GET /doctors/me/appointments` |
| Doctor cancel appointment | `POST /appointments/{appointmentId}/cancel` |
| Doctor cancel slot | `POST /doctors/me/slots/{slotId}/cancel` |

## Postman
Import the collection and environment from `postman/`. Set the base URL and access tokens; use a valid `slotId` for booking/cancel-slot and an `appointmentId` for appointment detail/cancel requests.

