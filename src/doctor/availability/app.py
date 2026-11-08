
import os
import json
import logging
from datetime import datetime

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

ALLOWED_FIELDS = {
    "consultation_type_id",
    "day_of_week",
    "start_time",
    "end_time",
    "status",
}


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


def validate_pattern(data):
    try:
        consultation_type_id = int(data["consultation_type_id"])
        day_of_week = int(data["day_of_week"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            "consultation_type_id and day_of_week are required integers"
        )

    if consultation_type_id <= 0:
        raise ValueError("Invalid consultation_type_id")

    # Python weekday convention: Monday=0, Sunday=6.
    if not 0 <= day_of_week <= 6:
        raise ValueError("day_of_week must be between 0 and 6")

    start_time = data.get("start_time")
    end_time = data.get("end_time")

    try:
        start = datetime.strptime(start_time, "%H:%M").time()
        end = datetime.strptime(end_time, "%H:%M").time()
    except (TypeError, ValueError):
        raise ValueError("Times must use HH:MM format")

    if end <= start:
        raise ValueError("end_time must be later than start_time")

    status = data.get("status", "ACTIVE")

    if status not in ("ACTIVE", "INACTIVE"):
        raise ValueError("status must be ACTIVE or INACTIVE")

    return {
        "consultation_type_id": consultation_type_id,
        "day_of_week": day_of_week,
        "start_time": start,
        "end_time": end,
        "status": status,
    }


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

def ensure_active_consultation_type(
    cursor, doctor_id, consultation_type_id
):
    cursor.execute(
        """
        SELECT consultation_type_id
        FROM consultation_types
        WHERE consultation_type_id = %s
          AND doctor_id = %s
          AND status = 'ACTIVE'
        """,
        (consultation_type_id, doctor_id),
    )

    if not cursor.fetchone():
        raise LookupError("Active consultation type not found")

def ensure_no_overlap(
    cursor, doctor_id, day_of_week, start_time, end_time,
    exclude_pattern_id=None,
):
    sql = """
        SELECT pattern_id
        FROM doctor_availability_patterns
        WHERE doctor_id = %s
          AND day_of_week = %s
          AND status = 'ACTIVE'
          AND start_time < %s
          AND end_time > %s
    """
    params = [doctor_id, day_of_week, end_time, start_time]

    if exclude_pattern_id is not None:
        sql += " AND pattern_id <> %s"
        params.append(exclude_pattern_id)

    sql += " LIMIT 1 FOR UPDATE"
    cursor.execute(sql, params)

    if cursor.fetchone():
        raise ValueError(
            "This availability pattern overlaps an existing active pattern"
        )

def list_patterns(cursor, doctor_id, query):
    sql = """
        SELECT
            pattern_id,
            consultation_type_id,
            day_of_week,
            TIME_FORMAT(start_time, '%%H:%%i') AS start_time,
            TIME_FORMAT(end_time, '%%H:%%i') AS end_time,
            status,
            created_at,
            updated_at
        FROM doctor_availability_patterns
        WHERE doctor_id = %s
    """

    params = [doctor_id]

    consultation_type_id = query.get("consultation_type_id")
    if consultation_type_id:
        try:
            consultation_type_id = int(consultation_type_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid consultation_type_id")

        sql += " AND consultation_type_id = %s"
        params.append(consultation_type_id)

    status = query.get("status")
    if status:
        if status not in ("ACTIVE", "INACTIVE"):
            raise ValueError("status must be ACTIVE or INACTIVE")

        sql += " AND status = %s"
        params.append(status)

    sql += " ORDER BY day_of_week, start_time"

    cursor.execute(sql, params)
    return cursor.fetchall()


def create_pattern(cursor, doctor_id, data):
    values = validate_pattern(data)

    ensure_active_consultation_type(
        cursor, doctor_id, values["consultation_type_id"]
    )

    if values["status"] == "ACTIVE":
        ensure_no_overlap(
            cursor,
            doctor_id,
            values["day_of_week"],
            values["start_time"],
            values["end_time"],
        )

    cursor.execute(
        """
        INSERT INTO doctor_availability_patterns (
            doctor_id,
            consultation_type_id,
            day_of_week,
            start_time,
            end_time,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            doctor_id,
            values["consultation_type_id"],
            values["day_of_week"],
            values["start_time"],
            values["end_time"],
            values["status"],
        ),
    )

    return cursor.lastrowid, values["status"]


def update_pattern(cursor, doctor_id, pattern_id, data):
    if not data or set(data) - ALLOWED_FIELDS:
        raise ValueError("Invalid or empty update fields")

    cursor.execute(
        """
        SELECT
            consultation_type_id,
            day_of_week,
            TIME_FORMAT(start_time, '%%H:%%i') AS start_time,
            TIME_FORMAT(end_time, '%%H:%%i') AS end_time,
            status
        FROM doctor_availability_patterns
        WHERE pattern_id = %s
          AND doctor_id = %s
        FOR UPDATE
        """,
        (pattern_id, doctor_id),
    )

    existing = cursor.fetchone()

    if not existing:
        raise LookupError("Availability pattern not found")

    merged = {**existing, **data}
    values = validate_pattern(merged)

    ensure_active_consultation_type(
        cursor, doctor_id, values["consultation_type_id"]
    )

    if values["status"] == "ACTIVE":
        ensure_no_overlap(
            cursor,
            doctor_id,
            values["day_of_week"],
            values["start_time"],
            values["end_time"],
            exclude_pattern_id=pattern_id,
        )

    cursor.execute(
        """
        UPDATE doctor_availability_patterns
        SET consultation_type_id = %s,
            day_of_week = %s,
            start_time = %s,
            end_time = %s,
            status = %s
        WHERE pattern_id = %s
          AND doctor_id = %s
        """,
        (
            values["consultation_type_id"],
            values["day_of_week"],
            values["start_time"],
            values["end_time"],
            values["status"],
            pattern_id,
            doctor_id,
        ),
    )


def deactivate_pattern(cursor, doctor_id, pattern_id):
    cursor.execute(
        """
        UPDATE doctor_availability_patterns
        SET status = 'INACTIVE'
        WHERE pattern_id = %s
          AND doctor_id = %s
          AND status = 'ACTIVE'
        """,
        (pattern_id, doctor_id),
    )

    if cursor.rowcount == 0:
        cursor.execute(
            """
            SELECT pattern_id
            FROM doctor_availability_patterns
            WHERE pattern_id = %s
              AND doctor_id = %s
            """,
            (pattern_id, doctor_id),
        )

        if not cursor.fetchone():
            raise LookupError("Availability pattern not found")


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

    path_parameters = event.get("pathParameters") or {}
    pattern_id = path_parameters.get("patternId")
    query = event.get("queryStringParameters") or {}

    connection = None

    try:
        connection = get_connection()

        with connection.cursor() as cursor:
            ensure_active_doctor(cursor, doctor_id)

            if method == "GET":
                result = list_patterns(cursor, doctor_id, query)
                connection.commit()
                return response(200, {"availability_patterns": result})

            if method == "POST":
                pattern_id, pattern_status = create_pattern(
                    cursor, doctor_id, get_body(event)
                )
                connection.commit()

                return response(201, {
                    "message": "Availability pattern created",
                    "pattern_id": pattern_id,
                    "status": pattern_status,
                })

            if method == "PUT":
                if not pattern_id:
                    raise ValueError("patternId is required")

                update_pattern(
                    cursor,
                    doctor_id,
                    pattern_id,
                    get_body(event),
                )
                connection.commit()

                return response(200, {
                    "message": "Availability pattern updated"
                })

            if method == "DELETE":
                if not pattern_id:
                    raise ValueError("patternId is required")

                deactivate_pattern(cursor, doctor_id, pattern_id)
                connection.commit()

                return response(200, {
                    "message": "Availability pattern deactivated"
                })

        return response(405, {"message": "Method not allowed"})

    except ValueError as exc:
        if connection:
            connection.rollback()
        return response(400, {"message": str(exc)})

    except LookupError as exc:
        if connection:
            connection.rollback()
        return response(404, {"message": str(exc)})

    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Availability pattern operation failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
