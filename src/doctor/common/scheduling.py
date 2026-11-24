"""Transactional helpers for recurring availability and generated slots."""

from datetime import datetime, time, timedelta


def _as_time(value):
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return time.fromisoformat(str(value))


def enqueue_slot_inventory(cursor, slot_id):
    """Write an inventory version/outbox event in the current SQL transaction."""
    cursor.execute(
        """
        UPDATE slots
        SET inventory_version = inventory_version + 1
        WHERE slot_id = %s
        """,
        (slot_id,),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Could not increment inventory version for slot {slot_id}")

    cursor.execute(
        "SELECT inventory_version FROM slots WHERE slot_id = %s",
        (slot_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"Could not read inventory version for slot {slot_id}")

    cursor.execute(
        """
        INSERT INTO slot_inventory_outbox (slot_id, slot_version)
        VALUES (%s, %s)
        """,
        (slot_id, int(row["inventory_version"])),
    )


def _cancel_waitlist(cursor, slot_id, now):
    cursor.execute(
        """
        SELECT waitlist_entry_id
        FROM waitlist_entries
        WHERE slot_id = %s AND status IN ('WAITING', 'OFFERED')
        FOR UPDATE
        """,
        (slot_id,),
    )
    entry_ids = [int(row["waitlist_entry_id"]) for row in cursor.fetchall()]
    if not entry_ids:
        return 0

    placeholders = ",".join(["%s"] * len(entry_ids))
    cursor.execute(
        f"""
        UPDATE waitlist_offers
        SET status = 'CANCELLED', responded_at = %s
        WHERE waitlist_entry_id IN ({placeholders})
          AND status = 'PENDING'
        """,
        (now, *entry_ids),
    )
    cursor.execute(
        f"""
        UPDATE waitlist_entries
        SET status = 'CANCELLED', cancelled_at = %s
        WHERE waitlist_entry_id IN ({placeholders})
          AND status IN ('WAITING', 'OFFERED')
        """,
        (now, *entry_ids),
    )
    return len(entry_ids)


def reconcile_doctor_schedule(cursor, doctor_id, now, window_days=30):
    """Reconcile future generated slots immediately after a pattern change.

    Booked slots and slots with active holds are preserved. Empty generated
    slots that no longer match an active pattern are cancelled; slots closed
    specifically by this reconciliation can be reopened if the same time
    becomes valid again. Doctor-cancelled and blackout slots are never reopened.
    """
    if not isinstance(window_days, int) or not 1 <= window_days <= 90:
        raise ValueError("Slot generation window must be between 1 and 90 days")

    window_start = datetime.combine(now.date(), time.min)
    window_end = datetime.combine(now.date() + timedelta(days=window_days), time.min)

    cursor.execute(
        """
        SELECT
            p.pattern_id,
            p.day_of_week,
            p.start_time,
            p.end_time,
            ct.consultation_type_id,
            ct.capacity,
            catalogue.duration_minutes
        FROM doctor_availability_patterns p
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = p.consultation_type_id
           AND ct.doctor_id = p.doctor_id
        INNER JOIN consultation_type_catalogue catalogue
            ON catalogue.catalogue_id = ct.catalogue_id
        WHERE p.doctor_id = %s
          AND p.status = 'ACTIVE'
          AND ct.status = 'ACTIVE'
        ORDER BY p.pattern_id
        """,
        (doctor_id,),
    )
    patterns = cursor.fetchall()

    cursor.execute(
        """
        SELECT start_at, end_at
        FROM blackout_dates
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
        """,
        (doctor_id, window_end, window_start),
    )
    blackouts = cursor.fetchall()

    def is_blackout(start_at, end_at):
        return any(start_at < item["end_at"] and end_at > item["start_at"] for item in blackouts)

    expected = {}
    current_day = now.date()
    last_day = now.date() + timedelta(days=window_days - 1)
    while current_day <= last_day:
        for pattern in patterns:
            if current_day.weekday() != int(pattern["day_of_week"]):
                continue
            start_time = _as_time(pattern["start_time"])
            end_time = _as_time(pattern["end_time"])
            duration = int(pattern["duration_minutes"])
            slot_start = datetime.combine(current_day, start_time)
            availability_end = datetime.combine(current_day, end_time)
            while slot_start + timedelta(minutes=duration) <= availability_end:
                slot_end = slot_start + timedelta(minutes=duration)
                if slot_start > now and not is_blackout(slot_start, slot_end):
                    expected[(slot_start, slot_end)] = {
                        "pattern_id": int(pattern["pattern_id"]),
                        "consultation_type_id": int(pattern["consultation_type_id"]),
                        "capacity": int(pattern["capacity"]),
                    }
                slot_start = slot_end
        current_day += timedelta(days=1)

    cursor.execute(
        """
        SELECT
            slot_id, pattern_id, consultation_type_id,
            start_at, end_at, capacity, booked_count,
            active_hold_count, status, closure_reason
        FROM slots
        WHERE doctor_id = %s
          AND start_at >= %s
          AND start_at < %s
        ORDER BY start_at
        FOR UPDATE
        """,
        (doctor_id, window_start, window_end),
    )
    existing = cursor.fetchall()
    by_key = {(row["start_at"], row["end_at"]): row for row in existing}

    slots_cancelled = 0
    slots_reopened = 0
    slots_updated = 0
    slots_created = 0
    occupied_ranges = [
        (row["start_at"], row["end_at"])
        for row in existing
        if row["status"] != "CANCELLED"
    ]

    # Close stale generated slots without disturbing appointments or holds.
    for slot in existing:
        key = (slot["start_at"], slot["end_at"])
        wanted = expected.get(key)
        booked = int(slot["booked_count"])
        holds = int(slot["active_hold_count"])
        slot_id = int(slot["slot_id"])

        if (
            slot["status"] == "OPEN"
            and slot["pattern_id"] is not None
            and wanted is None
            and booked == 0
            and holds == 0
        ):
            cursor.execute(
                """
                UPDATE slots
                SET status = 'CANCELLED', closure_reason = 'AVAILABILITY_CHANGED'
                WHERE slot_id = %s AND status = 'OPEN'
                  AND booked_count = 0 AND active_hold_count = 0
                """,
                (slot_id,),
            )
            if cursor.rowcount:
                _cancel_waitlist(cursor, slot_id, now)
                enqueue_slot_inventory(cursor, slot_id)
                occupied_ranges = [
                    item for item in occupied_ranges
                    if item != (slot["start_at"], slot["end_at"])
                ]
                slots_cancelled += 1

        elif wanted and slot["pattern_id"] is not None and booked == 0 and holds == 0:
            should_reopen = (
                slot["status"] == "CANCELLED"
                and slot["closure_reason"] == "AVAILABILITY_CHANGED"
            )
            needs_update = (
                slot["pattern_id"] != wanted["pattern_id"]
                or int(slot["consultation_type_id"]) != wanted["consultation_type_id"]
                or int(slot["capacity"]) != wanted["capacity"]
            )
            if should_reopen or (slot["status"] == "OPEN" and needs_update):
                cursor.execute(
                    """
                    UPDATE slots
                    SET status = 'OPEN',
                        closure_reason = NULL,
                        pattern_id = %s,
                        consultation_type_id = %s,
                        capacity = %s
                    WHERE slot_id = %s
                    """,
                    (
                        wanted["pattern_id"],
                        wanted["consultation_type_id"],
                        wanted["capacity"],
                        slot_id,
                    ),
                )
                enqueue_slot_inventory(cursor, slot_id)
                if should_reopen:
                    slots_reopened += 1
                else:
                    slots_updated += 1

    # Materialise missing intervals. Any overlapping manual, booked, blocked,
    # or cancelled slot continues to reserve that time interval.
    for (slot_start, slot_end), wanted in expected.items():
        existing_slot = by_key.get((slot_start, slot_end))
        if existing_slot:
            continue

        overlaps_existing = any(
            slot_start < other_end and slot_end > other_start
            for other_start, other_end in occupied_ranges
        )
        if overlaps_existing:
            continue

        cursor.execute(
            """
            INSERT INTO slots (
                doctor_id, consultation_type_id, pattern_id,
                start_at, end_at, capacity, booked_count, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, 0, 'OPEN')
            """,
            (
                doctor_id,
                wanted["consultation_type_id"],
                wanted["pattern_id"],
                slot_start,
                slot_end,
                wanted["capacity"],
            ),
        )
        slot_id = int(cursor.lastrowid)
        enqueue_slot_inventory(cursor, slot_id)
        slots_created += 1
        occupied_ranges.append((slot_start, slot_end))

    return {
        "slots_created": slots_created,
        "slots_cancelled": slots_cancelled,
        "slots_reopened": slots_reopened,
        "slots_updated": slots_updated,
        "window_start": window_start.date().isoformat(),
        "window_end": (window_end.date() - timedelta(days=1)).isoformat(),
    }
