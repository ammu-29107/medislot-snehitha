# MediSlot — Timezone and Slot Materialisation Strategy

## 1. Timezone standard

MediSlot uses **India Standard Time only: `Asia/Kolkata` (UTC+05:30)**.

- All doctor availability schedules are entered and interpreted in IST.
- All patient-facing slot and appointment times are displayed in IST.
- All business cutoffs, reminder schedules, waitlist expiry displays, and no-show calculations use the same IST scheduling context.
- The product does not support user-selectable timezones.
- No daylight-saving-time conversion is required or supported.

If timestamps are stored internally in UTC for consistency, the application must convert explicitly to/from `Asia/Kolkata` at input, query, and display boundaries. The timezone must not be inferred from the server's local timezone.

## 2. Availability source

Recurring weekly doctor availability is stored in RDS in `doctor_weekly_availability`. Blackout dates and periods are stored in `doctor_blackout_dates`.

A doctor may define multiple availability windows per weekday. Validation prevents invalid start/end times and overlapping windows. Effective dates determine when a recurring pattern applies.

## 3. Slot generation

A scheduled generation process materialises a rolling 30-day window of appointment slots in DynamoDB.

1. Read active weekly availability patterns.
2. Expand patterns into concrete IST dates/times within the rolling window.
3. Apply effective date ranges and blackout periods.
4. Apply consultation type duration and capacity.
5. Generate stable slot identifiers.
6. Write slots idempotently to `SlotInventoryTable`.
7. Avoid duplicate generation when a schedule is retried.

A daily scheduler advances the rolling window by one day. It must not regenerate existing slots in a way that resets capacity or overwrites booking/hold state.

## 4. Slot attributes

Each materialised slot should identify:
- `slotId`
- `doctorId`
- consultation type
- start and end timestamp
- configured capacity
- confirmed booking count
- active waitlist hold count
- status/version
- creation/update metadata

Capacity must be shared between confirmed bookings and active waitlist holds.

## 5. Consultation types

| Type | Duration | Capacity |
|---|---:|---:|
| Quick | 15 minutes | 1 |
| Standard | 30 minutes | 1 |
| Detailed | 60 minutes | 1 |
| Health Camp | 60 minutes | 2–10 |

## 6. Scheduling rules

- Do not expose or permit booking of past slots.
- A patient can have only one appointment for a given slot.
- A doctor can have at most three upcoming appointments.
- A waitlist offer creates a 15-minute exclusive hold.
- A waitlist patient may have at most five active waitlist entries.
- No-show is determined 30 minutes after the appointment slot ends.
- Rescheduling is allowed at most twice and is blocked within six hours of appointment start.

## 7. Idempotency and stale data

Slot generation and rolling-window advancement must be idempotent. A retry must not reset counters, reactivate a cancelled slot, or overwrite a reservation. Slot status and capacity changes are handled by the booking/availability workflows, not by blindly rerunning generation.

All comparisons use a single well-defined timestamp representation and explicit IST conversion. No multi-timezone or DST rules are part of this design.
