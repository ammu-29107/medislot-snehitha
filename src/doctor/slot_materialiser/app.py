
import os
import json
import logging
from datetime import datetime, date, time, timedelta
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

        connection = get_connection()

        generated_count = 0
        skipped_count = 0

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    d.doctor_id,
                    p.pattern_id,
                    p.day_of_week,
                    p.start_time,
                    p.end_time,
                    c.consultation_type_id,
                    c.duration_minutes,
                    c.capacity
                FROM doctors d
                INNER JOIN doctor_availability_patterns p
                    ON p.doctor_id = d.doctor_id
                INNER JOIN consultation_types c
                    ON c.consultation_type_id = p.consultation_type_id
                   AND c.doctor_id = p.doctor_id
                WHERE d.status = 'ACTIVE'
                  AND p.status = 'ACTIVE'
                  AND c.status = 'ACTIVE'
                ORDER BY d.doctor_id, p.pattern_id
                """
            )

            patterns = cursor.fetchall()

            doctors = {}

            for pattern in patterns:
                doctor_id = pattern["doctor_id"]
                doctors.setdefault(doctor_id, []).append(pattern)

            for doctor_id, doctor_patterns in doctors.items():

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
                        datetime.combine(window_end, time.min),
                        datetime.combine(window_start, time.min),
                    ),
                )
                blackouts = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT start_at, end_at
                    FROM slots
                    WHERE doctor_id = %s
                      AND status <> 'CANCELLED'
                      AND start_at < %s
                      AND end_at > %s
                    """,
                    (
                        doctor_id,
                        datetime.combine(window_end, time.min),
                        datetime.combine(window_start, time.min),
                    ),
                )
                existing_slots = cursor.fetchall()

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
                                current_date, pattern_start
                            )
                            availability_end = datetime.combine(
                                current_date, pattern_end
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

                                if blocked or occupied:
                                    skipped_count += 1
                                    slot_start = slot_end
                                    continue

                                cursor.execute(
                                    """
                                    INSERT IGNORE INTO slots (
                                        doctor_id,
                                        consultation_type_id,
                                        pattern_id,
                                        start_at,
                                        end_at,
                                        capacity,
                                        booked_count,
                                        status
                                    )
                                    VALUES (
                                        %s, %s, %s, %s, %s, %s, 0, 'OPEN'
                                    )
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

                                if cursor.rowcount == 1:
                                    generated_count += 1
                                    existing_slots.append({
                                        "start_at": slot_start,
                                        "end_at": slot_end,
                                    })
                                else:
                                    skipped_count += 1

                                slot_start = slot_end

                        current_date += timedelta(days=1)

        connection.commit()

        result = {
            "message": "Slot materialisation completed",
            "slots_created": generated_count,
            "slots_skipped": skipped_count,
            "window_start": window_start.isoformat(),
            "window_end": (window_end - timedelta(days=1)).isoformat(),
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
