import json
import logging
import boto3
import os
import re
import uuid

import bcrypt
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

ssm = boto3.client("ssm", region_name=AWS_REGION)

DB_PASSWORD = ssm.get_parameter(
    Name=DB_PASSWORD_PARAMETER,
    WithDecryption=True,
)["Parameter"]["Value"]

BCRYPT_WORK_FACTOR = 12


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def get_correlation_id(event, context):
    headers = event.get("headers") or {}
    return (
        headers.get("x-correlation-id")
        or headers.get("X-Correlation-Id")
        or getattr(context, "aws_request_id", str(uuid.uuid4()))
    )


def get_db_connection():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=DB_PASSWORD,
        database=os.environ.get("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        charset="utf8mb4",
        autocommit=False,
    )


def validate_password(password):
    if not isinstance(password, str) or len(password) < 12:
        return False

    if len(password) > 72:
        # bcrypt only considers at most 72 password bytes.
        return False

    return (
        re.search(r"[a-z]", password) is not None
        and re.search(r"[A-Z]", password) is not None
        and re.search(r"\d", password) is not None
        and re.search(r"[^A-Za-z0-9]", password) is not None
    )


def handler(event, context):
    correlation_id = get_correlation_id(event, context)
    connection = None

    try:
        route_key = event.get("routeKey", "")
        raw_path = event.get("rawPath", "")

        # Support API Gateway HTTP API and REST API event formats.
        path = route_key.split(" ")[-1] if route_key else raw_path
        path = path.rstrip("/").lower()

        if path.endswith("/register/doctor"):
            role = "DOCTOR"
        elif path.endswith("/register/patient"):
            role = "PATIENT"
        else:
            return response(404, {"message": "Registration route not found"})

        body = event.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                return response(400, {"message": "Request body must be valid JSON"})
        elif body is None:
            body = {}

        if not isinstance(body, dict):
            return response(400, {"message": "Request body must be a JSON object"})

        full_name = body.get("fullName")
        email = body.get("email")
        password = body.get("password")

        if not all(isinstance(value, str) for value in (full_name, email, password)):
            return response(
                400,
                {"message": "fullName, email, and password are required"},
            )

        full_name = full_name.strip()
        email = email.strip().lower()

        if not full_name or len(full_name) > 150:
            return response(400, {"message": "Invalid fullName"})

        if (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            return response(400, {"message": "Invalid email address"})

        if not validate_password(password):
            return response(
                400,
                {
                    "message": (
                        "Password must be 12–72 characters and contain "
                        "an uppercase letter, a lowercase letter, a number, "
                        "and a special character"
                    )
                },
            )

        # Validate role-specific fields before opening a database connection.
        specialization = None
        license_number = None
        date_of_birth = None
        phone_number = None

        if role == "DOCTOR":
            specialization = body.get("specialization")
            license_number = body.get("licenseNumber")

            if not isinstance(specialization, str) or not specialization.strip():
                return response(400, {"message": "specialization is required"})

            if not isinstance(license_number, str) or not license_number.strip():
                return response(400, {"message": "licenseNumber is required"})

            specialization = specialization.strip()
            license_number = license_number.strip()

            if len(specialization) > 150 or len(license_number) > 100:
                return response(400, {"message": "Invalid doctor profile fields"})

        else:
            date_of_birth = body.get("dateOfBirth")
            phone_number = body.get("phoneNumber")

            if date_of_birth is not None:
                if not isinstance(date_of_birth, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", date_of_birth
                ):
                    return response(
                        400,
                        {"message": "dateOfBirth must use YYYY-MM-DD format"},
                    )

            if phone_number is not None:
                if not isinstance(phone_number, str) or len(phone_number) > 20:
                    return response(400, {"message": "Invalid phoneNumber"})

        # Hash before storing; plaintext passwords are never persisted.
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=BCRYPT_WORK_FACTOR),
        ).decode("utf-8")

        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (role, full_name, email)
                VALUES (%s, %s, %s)
                """,
                (role, full_name, email),
            )
            user_id = cursor.lastrowid

            cursor.execute(
                """
                INSERT INTO user_credentials (user_id, password_hash)
                VALUES (%s, %s)
                """,
                (user_id, password_hash),
            )

            if role == "DOCTOR":
                cursor.execute(
                    """
                    INSERT INTO doctor_profiles
                        (user_id, specialization, license_number)
                    VALUES (%s, %s, %s)
                    """,
                    (user_id, specialization, license_number),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO patient_profiles
                        (user_id, date_of_birth, phone_number)
                    VALUES (%s, %s, %s)
                    """,
                    (user_id, date_of_birth, phone_number),
                )

        connection.commit()

        logger.info(json.dumps({
            "event": "registration_succeeded",
            "correlation_id": correlation_id,
            "user_id": user_id,
            "role": role,
        }))

        return response(
            201,
            {
                "message": "Registration successful",
                "userId": user_id,
                "role": role,
                "correlationId": correlation_id,
            },
        )

    except pymysql.err.IntegrityError as exc:
        if connection:
            connection.rollback()

        # MySQL error 1062 indicates a unique-key conflict.
        if exc.args and exc.args[0] == 1062:
            return response(
                409,
                {"message": "Email or doctor license number already exists"},
            )

        logger.exception(json.dumps({
            "event": "registration_failed",
            "correlation_id": correlation_id,
            "error_type": type(exc).__name__,
        }))
        return response(500, {"message": "Registration could not be completed"})

    except Exception as exc:
        if connection:
            connection.rollback()

        logger.exception(json.dumps({
            "event": "registration_failed",
            "correlation_id": correlation_id,
            "error_type": type(exc).__name__,
        }))
        return response(500, {"message": "Registration could not be completed"})

    finally:
        if connection:
            connection.close()
