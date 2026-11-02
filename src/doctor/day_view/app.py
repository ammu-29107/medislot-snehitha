
import os
import json
import logging
from datetime import date

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def get_doctor_id(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    context = authorizer.get("lambda", authorizer)

    if context.get("role") != "DOCTOR":
        return None

    return str(context.get("userId", "")) or None


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
        read_timeout=10,
        write_timeout=10,
        autocommit=True,
    )


def handler(event, context):
    doctor_id = get_doctor_id(event)

    if not doctor_id:
        return response(403, {"message": "Doctor authorization required"})

    query = event.get("queryStringParameters") or {}
    day = query.get("date")

    if not day:
        return response(400, {"message": "date query parameter is required (YYYY-MM-DD)"})

    try:
        schedule_date = date.fromisoformat(day)
    except (TypeError, ValueError):
        return response(400, {"message": "date must use YYYY-MM-DD format"})

    connection = None

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT doctor_id
                FROM doctors
                WHERE doctor_id = %s
                  AND status = 'ACTIVE'
                """,
                (doctor_id,),
            )

            if not cursor.fetchone():
                return response(404, {"message": "Active doctor profile not found"})

            cursor.execute(
                """
                SELECT
                    s.slot_id,
                    s.consultation_type_id,
                    ct.type_name AS consultation_type,
                    ct.duration_minutes,
                    s.start_at,
                    s.end_at,
                    s.capacity,
                    s.booked_count,
                    s.status AS slot_status,
                    a.appointment_id,
                    a.booking_reference,
                    a.status AS appointment_status,
                    a.booked_at,
                    p.patient_id,
                    p.full_name AS patient_name,
                    p.phone_number AS patient_phone,
                    p.email AS patient_email
                FROM slots s
                JOIN consultation_types ct
                  ON ct.consultation_type_id = s.consultation_type_id
                 AND ct.doctor_id = s.doctor_id
                LEFT JOIN appointments a
                  ON a.slot_id = s.slot_id
                 AND a.status = 'CONFIRMED'
                LEFT JOIN patients p
                  ON p.patient_id = a.patient_id
                WHERE s.doctor_id = %s
                  AND s.start_at >= %s
                  AND s.start_at < DATE_ADD(%s, INTERVAL 1 DAY)
                ORDER BY s.start_at, a.booked_at
                """,
                (doctor_id, schedule_date, schedule_date),
            )

            rows = cursor.fetchall()

        slots = {}

        for row in rows:
            slot_id = row["slot_id"]

            if slot_id not in slots:
                slots[slot_id] = {
                    "slot_id": slot_id,
                    "consultation_type_id": row["consultation_type_id"],
                    "consultation_type": row["consultation_type"],
                    "duration_minutes": row["duration_minutes"],
                    "start_at": row["start_at"],
                    "end_at": row["end_at"],
                    "capacity": row["capacity"],
                    "booked_count": row["booked_count"],
                    "available_capacity": (
                        row["capacity"] - row["booked_count"]
                    ),
                    "status": row["slot_status"],
                    "appointments": [],
                }

            if row["appointment_id"] is not None:
                slots[slot_id]["appointments"].append({
                    "appointment_id": row["appointment_id"],
                    "booking_reference": row["booking_reference"],
                    "status": row["appointment_status"],
                    "booked_at": row["booked_at"],
                    "patient": {
                        "patient_id": row["patient_id"],
                        "full_name": row["patient_name"],
                        "phone_number": row["patient_phone"],
                        "email": row["patient_email"],
                    },
                })

        return response(200, {
            "date": day,
            "timezone": "Asia/Kolkata",
            "slots": list(slots.values()),
        })

    except Exception:
        logger.exception("Doctor day view failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
