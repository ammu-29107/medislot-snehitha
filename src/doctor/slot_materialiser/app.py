
import os
import json
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

IST = ZoneInfo("Asia/Kolkata")
WINDOW_DAYS = int(
    ssm.get_parameter(
        Name=os.environ["SLOT_WINDOW_DAYS_PARAMETER"],
        WithDecryption=True
    )["Parameter"]["Value"]
)


def get_connection():
    password = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]

    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=password,
        database=DB_NAME,
        port=int(os.getenv("DB_PORT", "3306")),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
    )


def db_time_to_time(value):
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        return time(
            seconds // 3600,
            (seconds % 3600) // 60,
            seconds % 60,
        )

    if isinstance(value, time):
        return value

    return time.fromisoformat(str(value))


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and end_a > start_b


def handler(event, context):
    connection = None

    try:
        now = datetime.now(IST).replace(tzinfo=None)
        window_start = now.date()
        window_end = window_start + timedelta(days=WINDOW_DAYS)

        window_start_dt = datetime.combine(window_start, time.min)
        window_end_dt = datetime.combine(window_end, time.min)

        connection = get_connection()

        generated_count = 0
        skipped_count = 0

        with connection.cursor() as cursor:
            # Discover eligible doctors.
            cursor.execute(
                """
                SELECT DISTINCT d.doctor_id
                FROM doctors d
                INNER JOIN doctor_availability_patterns p
                    ON p.doctor_id = d.doctor_id
                INNER JOIN consultation_types ct
                    ON ct.consultation_type_id = p.consultation_type_id
                   AND ct.doctor_id = p.doctor_id
                WHERE d.status = 'ACTIVE'
                  AND p.status = 'ACTIVE'
                  AND ct.status = 'ACTIVE'
                ORDER BY d.doctor_id
                """
            )

            doctor_ids = [
                row["doctor_id"] for row in cursor.fetchall()
            ]

            # End the discovery transaction. Each doctor gets
            # an independent transaction and a fresh read snapshot.
            connection.commit()

            for doctor_id in doctor_ids:

                # Acquire the same doctor-row lock used by
                # the scheduling and blackout POST operations.
                cursor.execute(
                    """
                    SELECT doctor_id
                    FROM doctors
                    WHERE doctor_id = %s
                      AND status = 'ACTIVE'
                    FOR UPDATE
                    """,
                    (doctor_id,),
                )

                if not cursor.fetchone():
                    connection.commit()
                    continue

                # Fetch patterns only after acquiring the lock.
                cursor.execute(
                    """
                    SELECT
                        p.pattern_id,
                        p.day_of_week,
                        p.start_time,
                        p.end_time,
                        ct.consultation_type_id,
                        c.duration_minutes,
                        ct.capacity
                    FROM doctor_availability_patterns p
                    INNER JOIN consultation_types ct
                        ON ct.consultation_type_id =
                           p.consultation_type_id
                       AND ct.doctor_id = p.doctor_id
                    INNER JOIN consultation_type_catalogue c
                        ON c.catalogue_id = ct.catalogue_id
                    WHERE p.doctor_id = %s
                      AND p.status = 'ACTIVE'
                      AND ct.status = 'ACTIVE'
                    ORDER BY p.pattern_id
                    """,
                    (doctor_id,),
                )

                doctor_patterns = cursor.fetchall()

                if not doctor_patterns:
                    connection.commit()
                    continue

                cursor.execute(
                    """
                    SELECT start_at, end_at
                    FROM blackout_dates
                    WHERE doctor_id = %s
                      AND start_at < %s
                      AND end_at > %s
                    """,
                    (
                        doctor_id,
                        window_end_dt,
                        window_start_dt,
                    ),
                )

                blackouts = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT start_at, end_at, status
                    FROM slots
                    WHERE doctor_id = %s
                    AND start_at < %s
                    AND end_at > %s
                    """,
                    (
                        doctor_id,
                        window_end_dt,
                        window_start_dt,
                    ),
                )

                all_slots = cursor.fetchall()

                # Only non-cancelled slots block overlapping availability.
                existing_slots = [
                    {
                        "start_at": slot["start_at"],
                        "end_at": slot["end_at"],
                    }
                    for slot in all_slots
                    if slot["status"] != "CANCELLED"
                ]

                # Track all existing time ranges because the database
                # unique constraint also applies to cancelled slots.
                existing_slot_keys = {
                    (slot["start_at"], slot["end_at"])
                    for slot in all_slots
                }

                for pattern in doctor_patterns:
                    pattern_start = db_time_to_time(
                        pattern["start_time"]
                    )
                    pattern_end = db_time_to_time(
                        pattern["end_time"]
                    )
                    duration = int(pattern["duration_minutes"])

                    current_date = window_start

                    while current_date < window_end:

                        if current_date.weekday() == int(
                            pattern["day_of_week"]
                        ):
                            slot_start = datetime.combine(
                                current_date,
                                pattern_start,
                            )

                            availability_end = datetime.combine(
                                current_date,
                                pattern_end,
                            )

                            while True:
                                slot_end = slot_start + timedelta(
                                    minutes=duration
                                )

                                if slot_end > availability_end:
                                    break

                                if slot_start <= now:
                                    slot_start = slot_end
                                    continue

                                blocked = any(
                                    overlaps(
                                        slot_start,
                                        slot_end,
                                        blackout["start_at"],
                                        blackout["end_at"],
                                    )
                                    for blackout in blackouts
                                )

                                occupied = any(
                                    overlaps(
                                        slot_start,
                                        slot_end,
                                        existing["start_at"],
                                        existing["end_at"],
                                    )
                                    for existing in existing_slots
                                )

                                if (
                                    blocked
                                    or occupied
                                    or (slot_start, slot_end) in existing_slot_keys
                                ):
                                    skipped_count += 1
                                    slot_start = slot_end
                                    continue

                                cursor.execute(
                                    """
                                    INSERT INTO slots (
                                        doctor_id,
                                        consultation_type_id,
                                        pattern_id,
                                        start_at,
                                        end_at,
                                        capacity,
                                        booked_count,
                                        status
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, 0, 'OPEN')
                                    """,
                                    (
                                        doctor_id,
                                        pattern["consultation_type_id"],
                                        pattern["pattern_id"],
                                        slot_start,
                                        slot_end,
                                        pattern["capacity"],
                                    ),
                                )

                                generated_count += 1

                                existing_slots.append({
                                    "start_at": slot_start,
                                    "end_at": slot_end,
                                })

                                existing_slot_keys.add((slot_start, slot_end))

                                slot_start = slot_end

                        current_date += timedelta(days=1)

                # Release this doctor's lock before processing
                # the next doctor.
                connection.commit()

        result = {
            "message": "Slot materialisation completed",
            "slots_created": generated_count,
            "slots_skipped": skipped_count,
            "window_start": window_start.isoformat(),
            "window_end": (
                window_end - timedelta(days=1)
            ).isoformat(),
            "timezone": "Asia/Kolkata",
        }

        logger.info(json.dumps(result))

        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Slot materialisation failed")
        raise

    finally:
        if connection:
            connection.close()
