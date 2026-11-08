import json
import logging
import boto3
import os
import re
import uuid

from datetime import date

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
        request_context = event.get("requestContext") or {}
        http_context = request_context.get("http") or {}

        # Support API Gateway HTTP API and REST API event formats.
        if route_key:
            parts = route_key.split(" ", 1)
            method = parts[0].upper()
            path = parts[1] if len(parts) > 1 else raw_path
        else:
            method = str(http_context.get("method") or event.get("httpMethod") or "POST").upper()
            path = raw_path or (event.get("path") or "")
        path = path.rstrip("/").lower()

        if method == "GET" and path.endswith("/registration/options"):
            connection = get_db_connection()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT hospital_id, hospital_name, city
                    FROM hospitals
                    WHERE status = 'ACTIVE'
                    ORDER BY hospital_name
                    """
                )
                hospitals = [
                    {"hospitalId": row[0], "hospitalName": row[1], "city": row[2]}
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    """
                    SELECT speciality_id, speciality_name
                    FROM specialities
                    WHERE status = 'ACTIVE'
                    ORDER BY speciality_name
                    """
                )
                specialities = [
                    {"specialityId": row[0], "specialityName": row[1]}
                    for row in cursor.fetchall()
                ]

            return response(
                200,
                {"hospitals": hospitals, "specialities": specialities},
            )

        if method != "POST":
            return response(405, {"message": "Method not allowed"})

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
        license_number = None
        hospital_id = None
        speciality_ids = None
        qualifications = None
        experience_years = None

        date_of_birth = None
        phone_number = None
        gender = None

        if role == "DOCTOR":
            license_number = body.get("licenseNumber")
            hospital_id = body.get("hospitalId")
            speciality_ids = body.get("specialityIds")
            qualifications = body.get("qualifications")
            experience_years = body.get("experienceYears")

            if (
                not isinstance(license_number, str)
                or not license_number.strip()
            ):
                return response(400, {"message": "licenseNumber is required"})

            license_number = license_number.strip()

            if len(license_number) > 100:
                return response(400, {"message": "Invalid licenseNumber"})

            if (
                isinstance(hospital_id, bool)
                or not isinstance(hospital_id, int)
                or hospital_id <= 0
            ):
                return response(400, {"message": "Valid hospitalId is required"})

            if (
                not isinstance(speciality_ids, list)
                or not speciality_ids
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value <= 0
                    for value in speciality_ids
                )
                or len(speciality_ids) != len(set(speciality_ids))
            ):
                return response(
                    400,
                    {
                        "message": (
                            "specialityIds must be a non-empty array "
                            "of unique positive integers"
                        )
                    },
                )

            if qualifications is not None:
                if (
                    not isinstance(qualifications, str)
                    or len(qualifications.strip()) > 500
                ):
                    return response(400, {"message": "Invalid qualifications"})

                qualifications = qualifications.strip() or None

            if experience_years is not None:
                if (
                    isinstance(experience_years, bool)
                    or not isinstance(experience_years, int)
                    or not 0 <= experience_years <= 80
                ):
                    return response(
                        400,
                        {"message": "experienceYears must be an integer from 0 to 80"},
                    )

        else:
            date_of_birth = body.get("dateOfBirth")
            phone_number = body.get("phoneNumber")
            gender = body.get("gender")

            # All three patient-specific fields are mandatory.
            if not all(
                isinstance(value, str) and value.strip()
                for value in (date_of_birth, phone_number, gender)
            ):
                return response(
                    400,
                    {
                        "message": (
                            "dateOfBirth, phoneNumber, and gender are required"
                        )
                    },
                )

            # Enforce exact YYYY-MM-DD format.
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_of_birth.strip()):
                return response(
                    400,
                    {"message": "dateOfBirth must use YYYY-MM-DD format"},
                )

            # Validate date of birth.
            try:
                date_of_birth = date.fromisoformat(date_of_birth.strip())

                if date_of_birth >= date.today():
                    return response(
                        400,
                        {"message": "dateOfBirth must be in the past"},
                    )

            except ValueError:
                return response(
                    400,
                    {"message": "dateOfBirth must use YYYY-MM-DD format"},
                )

            # Validate phone number.
            phone_number = phone_number.strip()

            if not re.fullmatch(r"\+?[0-9]{10,15}", phone_number):
                return response(
                    400,
                    {"message": "Invalid phoneNumber"},
                )

            # Validate gender.
            gender = gender.strip().upper()

            allowed_genders = {
                "MALE",
                "FEMALE",
                "NON_BINARY",
                "PREFER_NOT_TO_SAY",
            }

            if gender not in allowed_genders:
                return response(
                    400,
                    {"message": "Invalid gender"},
                )

        # Hash before storing; plaintext passwords are never persisted.
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=BCRYPT_WORK_FACTOR),
        ).decode("utf-8")

        connection = get_db_connection()

        with connection.cursor() as cursor:

            # Validate hospital and speciality references for doctors.
            if role == "DOCTOR":

                cursor.execute(
                    """
                    SELECT hospital_id
                    FROM hospitals
                    WHERE hospital_id = %s
                      AND status = 'ACTIVE'
                    """,
                    (hospital_id,),
                )

                if not cursor.fetchone():
                    connection.rollback()
                    return response(
                        400,
                        {"message": "Hospital does not exist or is inactive"},
                    )

                placeholders = ", ".join(["%s"] * len(speciality_ids))

                cursor.execute(
                    f"""
                    SELECT speciality_id
                    FROM specialities
                    WHERE speciality_id IN ({placeholders})
                      AND status = 'ACTIVE'
                    """,
                    tuple(speciality_ids),
                )

                active_speciality_ids = {
                    row[0] for row in cursor.fetchall()
                }

                if active_speciality_ids != set(speciality_ids):
                    connection.rollback()
                    return response(
                        400,
                        {
                            "message": (
                                "One or more specialities do not exist "
                                "or are inactive"
                            )
                        },
                    )

            if role == "DOCTOR":

                cursor.execute(
                    """
                    INSERT INTO doctors (
                        full_name,
                        email,
                        password_hash,
                        license_number,
                        hospital_id,
                        qualifications,
                        experience_years
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        full_name,
                        email,
                        password_hash,
                        license_number,
                        hospital_id,
                        qualifications,
                        experience_years,
                    ),
                )

                user_id = cursor.lastrowid

                # Associate the doctor with all selected specialities.
                cursor.executemany(
                    """
                    INSERT INTO doctor_specialities (
                        doctor_id,
                        speciality_id
                    )
                    VALUES (%s, %s)
                    """,
                    [
                        (user_id, speciality_id)
                        for speciality_id in speciality_ids
                    ],
                )

            else:

                cursor.execute(
                    """
                    INSERT INTO patients (
                        full_name,
                        email,
                        password_hash,
                        date_of_birth,
                        phone_number,
                        gender
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        full_name,
                        email,
                        password_hash,
                        date_of_birth,
                        phone_number,
                        gender,
                    ),
                )

                user_id = cursor.lastrowid

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
