
import os
import json
import logging
import uuid
from datetime import datetime, timedelta
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
NO_SHOW_GRACE_MINUTES = max(0, int(os.getenv("NO_SHOW_GRACE_MINUTES", "15")))


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


def enqueue_slot_inventory(cursor, slot_id):
    """Record a slot inventory change in the current SQL transaction."""
    cursor.execute(
        """
        UPDATE slots
        SET inventory_version = inventory_version + 1
        WHERE slot_id = %s
        """,
        (slot_id,),
    )

    if cursor.rowcount != 1:
        raise RuntimeError(
            f"Could not increment inventory version for slot {slot_id}"
        )

    cursor.execute(
        """
        SELECT inventory_version
        FROM slots
        WHERE slot_id = %s
        """,
        (slot_id,),
    )
    row = cursor.fetchone()

    if not row:
        raise RuntimeError(
            f"Could not read inventory version for slot {slot_id}"
        )

    cursor.execute(
        """
        INSERT INTO slot_inventory_outbox (slot_id, slot_version)
        VALUES (%s, %s)
        """,
        (slot_id, int(row["inventory_version"])),
    )


def enqueue_appointment_event(cursor, appointment_id, event_type, payload):
    cursor.execute(
        """
        INSERT INTO outbox_events (
            event_uuid, aggregate_type, aggregate_id,
            event_type, payload, status
        )
        VALUES (%s, 'APPOINTMENT', %s, %s, %s, 'PENDING')
        """,
        (
            str(uuid.uuid4()),
            str(appointment_id),
            event_type,
            json.dumps(payload, default=str),
        ),
    )


def handler(event, context):
    connection = None

    try:
        # MySQL DATETIME values are stored as IST local time.
        now_ist = datetime.now(IST).replace(tzinfo=None)

        connection = get_connection()

        with connection.cursor() as cursor:
            no_show_cutoff = now_ist - timedelta(
                minutes=NO_SHOW_GRACE_MINUTES
            )
            cursor.execute(
                """
                SELECT a.appointment_id, a.patient_id, a.slot_id, s.doctor_id
                FROM appointments a
                INNER JOIN slots s ON s.slot_id = a.slot_id
                WHERE a.status = 'CONFIRMED'
                  AND s.end_at <= %s
                ORDER BY a.appointment_id
                FOR UPDATE
                """,
                (no_show_cutoff,),
            )
            no_show_rows = cursor.fetchall()
            no_show_count = 0
            for appointment in no_show_rows:
                cursor.execute(
                    """
                    UPDATE appointments
                    SET status = 'NO_SHOW'
                    WHERE appointment_id = %s AND status = 'CONFIRMED'
                    """,
                    (appointment["appointment_id"],),
                )
                if cursor.rowcount != 1:
                    continue
                cursor.execute(
                    """
                    INSERT INTO appointment_status_history (
                        appointment_id, previous_status, new_status,
                        changed_by_role, changed_by_id, reason
                    )
                    VALUES (%s, 'CONFIRMED', 'NO_SHOW', 'SYSTEM', NULL, %s)
                    """,
                    (
                        appointment["appointment_id"],
                        f"No attendance recorded within {NO_SHOW_GRACE_MINUTES} minutes after the slot ended",
                    ),
                )
                enqueue_appointment_event(
                    cursor,
                    int(appointment["appointment_id"]),
                    "APPOINTMENT_NO_SHOW",
                    {
                        "appointment_id": int(appointment["appointment_id"]),
                        "patient_id": int(appointment["patient_id"]),
                        "doctor_id": int(appointment["doctor_id"]),
                        "slot_id": int(appointment["slot_id"]),
                        "status": "NO_SHOW",
                        "refund_percentage": "0",
                    },
                )
                no_show_count += 1

            cursor.execute(
                """
                SELECT slot_id
                FROM slots
                WHERE status = 'OPEN'
                  AND end_at <= %s
                FOR UPDATE
                """,
                (now_ist,),
            )

            completed_slot_ids = [
                str(row["slot_id"])
                for row in cursor.fetchall()
            ]

            cursor.execute(
                """
                UPDATE slots s
                SET s.status = 'COMPLETED'
                WHERE s.status = 'OPEN'
                  AND s.end_at <= %s
                """,
                (now_ist,),
            )

            completed_count = cursor.rowcount

            for slot_id in completed_slot_ids:
                enqueue_slot_inventory(cursor, int(slot_id))

        connection.commit()

        logger.info(
            "Slot maintenance completed. Updated %s slots.",
            completed_count,
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Slot maintenance completed",
                "slots_marked_completed": completed_count,
                "appointments_marked_no_show": no_show_count,
                "no_show_grace_minutes": NO_SHOW_GRACE_MINUTES,
                "processed_at": now_ist.isoformat(),
                "timezone": "Asia/Kolkata",
            }),
        }

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Slot maintenance failed")
        raise

    finally:
        if connection:
            connection.close()
