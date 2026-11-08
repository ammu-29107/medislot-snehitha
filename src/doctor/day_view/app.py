
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
        read_timeout=20,
        write_timeout=10,
        autocommit=True,
    )


def parse_date(value, field_name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must use YYYY-MM-DD format")


def get_requested_range(query):
    """Return an inclusive date range, retaining backward compatibility with ?date=."""
    single_day = query.get("date")
    start_value = query.get("start_date")
    end_value = query.get("end_date")

    if single_day and (start_value or end_value):
        raise ValueError("Use either date or start_date/end_date, not both")

    if single_day:
        start_day = parse_date(single_day, "date")
        end_day = start_day
    elif start_value or end_value:
        if not start_value or not end_value:
            raise ValueError("Both start_date and end_date are required")
        start_day = parse_date(start_value, "start_date")
        end_day = parse_date(end_value, "end_date")
    else:
        raise ValueError(
            "Provide date=YYYY-MM-DD or both start_date and end_date"
        )

    if end_day < start_day:
        raise ValueError("end_date must be on or after start_date")

    inclusive_days = (end_day - start_day).days + 1
    if inclusive_days > 42:
        raise ValueError("Date range cannot exceed 42 days")

    return start_day, end_day


def handler(event, context):
    doctor_id = get_doctor_id(event)

    if not doctor_id:
        return response(403, {"message": "Doctor authorization required"})

    query = event.get("queryStringParameters") or {}
    try:
        range_start, range_end = get_requested_range(query)
    except ValueError as exc:
        return response(400, {"message": str(exc)})

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
                (doctor_id, range_start, range_end),
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

        result = {
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "timezone": "Asia/Kolkata",
            "slots": list(slots.values()),
        }
        if range_start == range_end:
            result["date"] = range_start.isoformat()

        return response(200, result)

    except Exception:
        logger.exception("Doctor day view failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
