
import os
import json
import logging
from datetime import datetime, timezone
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


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


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
        autocommit=False,
    )


def get_identity(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    context = authorizer.get("lambda", authorizer)

    if context.get("role") != "DOCTOR":
        return None

    return str(context.get("userId", "")) or None


def get_body(event):
    body = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    data = json.loads(body)

    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")

    return data


def parse_datetime(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A valid ISO-8601 date/time is required")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Invalid date/time format")

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(IST).replace(tzinfo=None)

    return parsed


def ensure_active_doctor(cursor, doctor_id):
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
        raise LookupError("Active doctor profile not found")


def create_slot(cursor, doctor_id, data):
    consultation_type_id = data.get("consultation_type_id")

    if not consultation_type_id:
        raise ValueError("consultation_type_id is required")

    try:
        consultation_type_id = int(consultation_type_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid consultation_type_id")

    start_at = parse_datetime(data.get("start_at"))

    if start_at <= datetime.now(IST).replace(tzinfo=None):
        raise ValueError("A slot must start in the future")

    cursor.execute(
        """
        SELECT
            duration_minutes,
            capacity
        FROM consultation_types
        WHERE consultation_type_id = %s
          AND doctor_id = %s
          AND status = 'ACTIVE'
        FOR UPDATE
        """,
        (consultation_type_id, doctor_id),
    )

    consultation = cursor.fetchone()

    if not consultation:
        raise LookupError("Active consultation type not found")

    end_at = start_at.replace() 
    from datetime import timedelta
    end_at = start_at + timedelta(
        minutes=int(consultation["duration_minutes"])
    )

    cursor.execute(
        """
        SELECT blackout_id
        FROM blackout_dates
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
        LIMIT 1
        """,
        (doctor_id, end_at, start_at),
    )

    if cursor.fetchone():
        raise ValueError("Slot overlaps a doctor blackout period")

    cursor.execute(
        """
        SELECT slot_id
        FROM slots
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
          AND status <> 'CANCELLED'
        LIMIT 1
        """,
        (doctor_id, end_at, start_at),
    )

    if cursor.fetchone():
        raise ValueError("Slot overlaps an existing slot")

    cursor.execute(
        """
        INSERT INTO slots (
            doctor_id,
            consultation_type_id,
            start_at,
            end_at,
            capacity,
            booked_count,
            status
        )
        VALUES (%s, %s, %s, %s, %s, 0, 'OPEN')
        """,
        (
            doctor_id,
            consultation_type_id,
            start_at,
            end_at,
            consultation["capacity"],
        ),
    )

    return cursor.lastrowid, end_at


def list_slots(cursor, doctor_id, query):
    start_date = query.get("start_at")
    end_date = query.get("end_at")

    sql = """
        SELECT
            slot_id,
            consultation_type_id,
            start_at,
            end_at,
            capacity,
            booked_count,
            status,
            created_at,
            updated_at
        FROM slots
        WHERE doctor_id = %s
    """

    params = [doctor_id]

    if start_date:
        sql += " AND start_at >= %s"
        params.append(parse_datetime(start_date))

    if end_date:
        sql += " AND start_at < %s"
        params.append(parse_datetime(end_date))

    sql += " ORDER BY start_at"

    cursor.execute(sql, params)
    return cursor.fetchall()


def create_blackout(cursor, doctor_id, data):
    start_at = parse_datetime(data.get("start_at"))
    end_at = parse_datetime(data.get("end_at"))
    reason = data.get("reason")

    if end_at <= start_at:
        raise ValueError("end_at must be later than start_at")

    if reason is not None and (
        not isinstance(reason, str) or len(reason) > 500
    ):
        raise ValueError("reason cannot exceed 500 characters")

    cursor.execute(
        """
        SELECT blackout_id
        FROM blackout_dates
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
        LIMIT 1
        """,
        (doctor_id, end_at, start_at),
    )

    if cursor.fetchone():
        raise ValueError("Blackout overlaps an existing blackout")

    cursor.execute(
        """
        SELECT slot_id, booked_count
        FROM slots
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
          AND status = 'OPEN'
        FOR UPDATE
        """,
        (doctor_id, end_at, start_at),
    )

    affected_slots = cursor.fetchall()

    if any(int(slot["booked_count"]) > 0 for slot in affected_slots):
        raise ValueError(
            "Blackout affects booked slots. "
            "Use the leave cancellation workflow first."
        )

    cursor.execute(
        """
        INSERT INTO blackout_dates (
            doctor_id,
            start_at,
            end_at,
            reason
        )
        VALUES (%s, %s, %s, %s)
        """,
        (doctor_id, start_at, end_at, reason),
    )

    blackout_id = cursor.lastrowid

    cursor.execute(
        """
        UPDATE slots
        SET status = 'BLOCKED'
        WHERE doctor_id = %s
          AND start_at < %s
          AND end_at > %s
          AND status = 'OPEN'
          AND booked_count = 0
        """,
        (doctor_id, end_at, start_at),
    )

    return blackout_id, cursor.rowcount


def list_blackouts(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            blackout_id,
            start_at,
            end_at,
            reason,
            created_at
        FROM blackout_dates
        WHERE doctor_id = %s
        ORDER BY start_at
        """,
        (doctor_id,),
    )

    return cursor.fetchall()


def handler(event, context):
    doctor_id = get_identity(event)

    if not doctor_id:
        return response(403, {
            "message": "Doctor authorization required"
        })

    http = event.get("requestContext", {}).get("http", {})
    method = (
        http.get("method") or event.get("httpMethod", "")
    ).upper()

    path = event.get("rawPath") or event.get("path") or ""
    query = event.get("queryStringParameters") or {}

    is_blackout_route = path.rstrip("/").endswith("/blackout-dates")
    is_slot_route = path.rstrip("/").endswith("/slots")

    if not is_blackout_route and not is_slot_route:
        return response(404, {"message": "Route not found"})

    connection = None

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            ensure_active_doctor(cursor, doctor_id)

            if is_slot_route and method == "GET":
                result = list_slots(cursor, doctor_id, query)
                connection.commit()
                return response(200, {"slots": result})

            if is_slot_route and method == "POST":
                data = get_body(event)
                slot_id, end_at = create_slot(
                    cursor, doctor_id, data
                )
                connection.commit()

                return response(201, {
                    "message": "Slot created",
                    "slot_id": slot_id,
                    "end_at": end_at,
                    "status": "OPEN",
                })

            if is_blackout_route and method == "GET":
                result = list_blackouts(cursor, doctor_id)
                connection.commit()
                return response(200, {"blackout_dates": result})

            if is_blackout_route and method == "POST":
                data = get_body(event)
                blackout_id, blocked_count = create_blackout(
                    cursor, doctor_id, data
                )
                connection.commit()

                return response(201, {
                    "message": "Blackout created",
                    "blackout_id": blackout_id,
                    "slots_blocked": blocked_count,
                })

        return response(405, {"message": "Method not allowed"})

    except ValueError as exc:
        if connection:
            connection.rollback()
        return response(409, {"message": str(exc)})

    except LookupError as exc:
        if connection:
            connection.rollback()
        return response(404, {"message": str(exc)})

    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Scheduling and leave operation failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
