
import os
import json
import logging
from decimal import Decimal, InvalidOperation

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

ALLOWED_UPDATE_FIELDS = {
    "type_name",
    "description",
    "duration_minutes",
    "capacity",
    "consultation_fee",
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
    data = json.loads(body)

    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")

    return data


def validate_type(data):
    name = data.get("type_name")
    duration = data.get("duration_minutes")
    capacity = data.get("capacity", 1)
    fee = data.get("consultation_fee", 0)

    if not isinstance(name, str) or not name.strip():
        raise ValueError("type_name is required")

    if len(name.strip()) > 100:
        raise ValueError("type_name cannot exceed 100 characters")

    if duration is None or isinstance(duration, bool):
        raise ValueError("duration_minutes is required")

    try:
        duration = int(duration)
        capacity = int(capacity)
        fee = Decimal(str(fee))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError("Invalid duration, capacity or consultation fee")

    if duration <= 0:
        raise ValueError("duration_minutes must be greater than zero")

    if fee < 0 or fee > Decimal("99999999.99"):
        raise ValueError("Invalid consultation_fee")

    if name.strip() == "Health Camp Session":
        if not 2 <= capacity <= 10:
            raise ValueError(
                "Health Camp Session capacity must be between 2 and 10"
            )
    elif capacity != 1:
        raise ValueError(
            "Only Health Camp Session can have capacity greater than 1"
        )

    description = data.get("description")

    if description is not None and (
        not isinstance(description, str) or len(description) > 500
    ):
        raise ValueError("description cannot exceed 500 characters")

    return {
        "type_name": name.strip(),
        "description": description,
        "duration_minutes": duration,
        "capacity": capacity,
        "consultation_fee": fee,
    }


def list_types(connection, doctor_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                consultation_type_id,
                type_name,
                description,
                duration_minutes,
                capacity,
                consultation_fee,
                status,
                created_at,
                updated_at
            FROM consultation_types
            WHERE doctor_id = %s
            ORDER BY created_at DESC
            """,
            (doctor_id,),
        )
        return cursor.fetchall()


def create_type(connection, doctor_id, data):
    values = validate_type(data)

    with connection.cursor() as cursor:
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

        cursor.execute(
            """
            SELECT COUNT(*) AS type_count
            FROM consultation_types
            WHERE doctor_id = %s
            """,
            (doctor_id,),
        )
        is_first_type = cursor.fetchone()["type_count"] == 0

        cursor.execute(
            """
            INSERT INTO consultation_types (
                doctor_id,
                type_name,
                description,
                duration_minutes,
                capacity,
                consultation_fee
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                doctor_id,
                values["type_name"],
                values["description"],
                values["duration_minutes"],
                values["capacity"],
                values["consultation_fee"],
            ),
        )

        consultation_type_id = cursor.lastrowid

        if is_first_type:
            default_patterns = [
                (0, "09:00", "17:00"),
                (1, "09:00", "17:00"),
                (2, "09:00", "17:00"),
                (3, "09:00", "17:00"),
                (4, "09:00", "17:00"),
                (5, "09:00", "13:00"),
            ]

            cursor.executemany(
                """
                INSERT INTO doctor_availability_patterns (
                    doctor_id,
                    consultation_type_id,
                    day_of_week,
                    start_time,
                    end_time,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, 'ACTIVE')
                """,
                [
                    (doctor_id, consultation_type_id, day, start, end)
                    for day, start, end in default_patterns
                ],
            )

        return consultation_type_id


def update_type(connection, doctor_id, type_id, data):
    if not data or set(data) - ALLOWED_UPDATE_FIELDS:
        raise ValueError("Invalid or empty update fields")

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                type_name,
                description,
                duration_minutes,
                capacity,
                consultation_fee
            FROM consultation_types
            WHERE consultation_type_id = %s
              AND doctor_id = %s
            FOR UPDATE
            """,
            (type_id, doctor_id),
        )

        existing = cursor.fetchone()

        if not existing:
            raise LookupError("Consultation type not found")

        merged = {**existing, **data}
        values = validate_type(merged)

        cursor.execute(
            """
            UPDATE consultation_types
            SET type_name = %s,
                description = %s,
                duration_minutes = %s,
                capacity = %s,
                consultation_fee = %s
            WHERE consultation_type_id = %s
              AND doctor_id = %s
            """,
            (
                values["type_name"],
                values["description"],
                values["duration_minutes"],
                values["capacity"],
                values["consultation_fee"],
                type_id,
                doctor_id,
            ),
        )


def disable_type(connection, doctor_id, type_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE consultation_types
            SET status = 'DISABLED'
            WHERE consultation_type_id = %s
              AND doctor_id = %s
              AND status = 'ACTIVE'
            """,
            (type_id, doctor_id),
        )

        if cursor.rowcount == 0:
            cursor.execute(
                """
                SELECT consultation_type_id
                FROM consultation_types
                WHERE consultation_type_id = %s
                  AND doctor_id = %s
                """,
                (type_id, doctor_id),
            )

            if not cursor.fetchone():
                raise LookupError("Consultation type not found")


def handler(event, context):
    doctor_id = get_identity(event)

    if not doctor_id:
        return response(403, {"message": "Doctor authorization required"})

    http = event.get("requestContext", {}).get("http", {})
    method = (http.get("method") or event.get("httpMethod", "")).upper()

    path_parameters = event.get("pathParameters") or {}
    type_id = path_parameters.get("consultationTypeId")

    connection = None

    try:
        connection = get_connection()

        if method == "GET":
            result = list_types(connection, doctor_id)
            return response(200, {"consultation_types": result})

        data = get_body(event)

        if method == "POST":
            type_id = create_type(connection, doctor_id, data)
            connection.commit()

            return response(201, {
                "message": "Consultation type created",
                "consultation_type_id": type_id,
            })

        if method == "PUT":
            if not type_id:
                raise ValueError("consultationTypeId is required")

            update_type(connection, doctor_id, type_id, data)
            connection.commit()

            return response(200, {
                "message": "Consultation type updated"
            })

        if method == "DELETE":
            if not type_id:
                raise ValueError("consultationTypeId is required")

            disable_type(connection, doctor_id, type_id)
            connection.commit()

            return response(200, {
                "message": "Consultation type disabled"
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
        logger.exception("Consultation types operation failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
