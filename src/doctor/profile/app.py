
import os
import json
import uuid
import logging

import boto3
import pymysql

from botocore.exceptions import ClientError
from decimal import Decimal, InvalidOperation

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")
s3 = boto3.client("s3")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]
PRIVATE_FILES_BUCKET = os.environ["PRIVATE_FILES_BUCKET"]

ALLOWED_PROFILE_FIELDS = {
    "full_name",
    "qualifications",
    "experience_years",
    "bio",
    "hospital_id",
    "profile_photo_key",
}

ALLOWED_PHOTO_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body, default=str),
    }


def parse_positive_integer(value, field_name):
    if value is None or isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}")

    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError(f"Invalid {field_name}")

    if (
        not number.is_finite()
        or number != number.to_integral_value()
        or number <= 0
    ):
        raise ValueError(f"Invalid {field_name}")

    return int(number)

def get_db_connection():
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
    authorizer = (
        event.get("requestContext", {})
        .get("authorizer", {})
    )

    context = authorizer.get("lambda", authorizer)

    user_id = context.get("userId")
    role = context.get("role")

    if not user_id or role != "DOCTOR":
        return None

    return str(user_id)


def get_body(event):
    body = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    return json.loads(body)


def get_profile(connection, doctor_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                doctor_id,
                full_name,
                email,
                license_number,
                hospital_id,
                qualifications,
                experience_years,
                bio,
                profile_photo_key,
                status
            FROM doctors
            WHERE doctor_id = %s
            """,
            (doctor_id,),
        )

        profile = cursor.fetchone()

        if not profile:
            return None

        cursor.execute(
            """
            SELECT s.speciality_id, s.speciality_name
            FROM doctor_specialities ds
            JOIN specialities s
              ON s.speciality_id = ds.speciality_id
            WHERE ds.doctor_id = %s
            ORDER BY s.speciality_name
            """,
            (doctor_id,),
        )

        profile["specialities"] = cursor.fetchall()

        return profile


def add_profile_photo_url(profile, doctor_id):
    photo_key = profile.get("profile_photo_key")

    if (
        photo_key
        and isinstance(photo_key, str)
        and photo_key.startswith(f"doctor-profiles/{doctor_id}/")
    ):
        profile["profile_photo_url"] = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": PRIVATE_FILES_BUCKET,
                "Key": photo_key,
            },
            ExpiresIn=300,
        )
        profile["profile_photo_url_expires_in"] = 300
    else:
        profile["profile_photo_url"] = None
        profile["profile_photo_url_expires_in"] = None

    return profile


def update_profile(connection, doctor_id, data):
    updates = {
        key: value
        for key, value in data.items()
        if key in ALLOWED_PROFILE_FIELDS
    }

    specialities = data.get("speciality_ids")

    if not updates and specialities is None:
        raise ValueError("No valid profile fields supplied")

    with connection.cursor() as cursor:
        if "hospital_id" in updates:
            try:
                hospital_id = parse_positive_integer(
                    updates["hospital_id"],
                    "hospital_id",
                )
            except (TypeError, ValueError):
                raise ValueError("Invalid hospital_id")

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
                raise ValueError("Selected hospital is not active")

            updates["hospital_id"] = hospital_id

        if updates:
            assignments = ", ".join(
                f"`{column}` = %s" for column in updates
            )

            values = list(updates.values())
            values.append(doctor_id)

            cursor.execute(
                f"""
                UPDATE doctors
                SET {assignments}
                WHERE doctor_id = %s
                """,
                values,
            )

            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT doctor_id FROM doctors WHERE doctor_id = %s",
                    (doctor_id,),
                )
                if not cursor.fetchone():
                    raise LookupError("Doctor profile not found")

        if specialities is not None:
            if not isinstance(specialities, list):
                raise ValueError("speciality_ids must be a list")

            speciality_ids = list(dict.fromkeys(specialities))

            if speciality_ids:
                placeholders = ", ".join(
                    ["%s"] * len(speciality_ids)
                )

                cursor.execute(
                    f"""
                    SELECT speciality_id
                    FROM specialities
                    WHERE speciality_id IN ({placeholders})
                    """,
                    speciality_ids,
                )

                found_ids = {
                    row["speciality_id"]
                    for row in cursor.fetchall()
                }

                if found_ids != set(speciality_ids):
                    raise ValueError("One or more specialities are invalid")

            cursor.execute(
                "DELETE FROM doctor_specialities WHERE doctor_id = %s",
                (doctor_id,),
            )

            for speciality_id in speciality_ids:
                cursor.execute(
                    """
                    INSERT INTO doctor_specialities
                        (doctor_id, speciality_id)
                    VALUES (%s, %s)
                    """,
                    (doctor_id, speciality_id),
                )


def generate_photo_upload_url(doctor_id, data):
    content_type = data.get("content_type")

    try:
        if "content_length" not in data:
            raise ValueError("content_length is required")

        content_length = parse_positive_integer(
            data["content_length"],
            "content_length",
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("content_length is required")

    if not 0 < content_length <= 5 * 1024 * 1024:
        raise ValueError("Profile photo must be 5 MB or smaller")

    if content_type not in ALLOWED_PHOTO_TYPES:
        raise ValueError("Only JPEG, PNG and WEBP images are allowed")

    extension = ALLOWED_PHOTO_TYPES[content_type]
    photo_key = (
        f"doctor-profiles/{doctor_id}/{uuid.uuid4()}.{extension}"
    )

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": PRIVATE_FILES_BUCKET,
            "Key": photo_key,
            "ContentType": content_type,
            "ContentLength": content_length,
        },
        ExpiresIn=300,
    )

    return {
        "upload_url": upload_url,
        "photo_key": photo_key,
        "expires_in": 300,
        "content_type": content_type,
    }


def handler(event, context):
    doctor_id = get_identity(event)

    if not doctor_id:
        return response(403, {"message": "Doctor authorization required"})

    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod", "")
    ).upper()

    path = event.get("rawPath") or event.get("path") or ""

    try:
        if method == "POST" and path.endswith("/profile/photo-upload-url"):
            data = get_body(event)
            result = generate_photo_upload_url(doctor_id, data)
            return response(200, result)

        connection = get_db_connection()

        try:
            if method == "GET":
                profile = get_profile(connection, doctor_id)

                if not profile:
                    return response(404, {"message": "Doctor not found"})

                profile = add_profile_photo_url(profile, doctor_id)

                return response(200, profile)

            if method == "PUT":
                data = get_body(event)

                if not isinstance(data, dict):
                    return response(400, {"message": "Invalid request body"})

                if "profile_photo_key" in data:
                    photo_key = data["profile_photo_key"]
                    if (
                        not isinstance(photo_key, str)
                        or not photo_key.startswith(
                            f"doctor-profiles/{doctor_id}/"
                        )
                    ):
                        return response(400, {
                            "message": "Invalid profile photo key"
                        })

                update_profile(connection, doctor_id, data)
                connection.commit()

                profile = get_profile(connection, doctor_id)

                if profile:
                    profile = add_profile_photo_url(profile, doctor_id)

                return response(200, profile)

            return response(405, {"message": "Method not allowed"})

        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    except ValueError as exc:
        return response(400, {"message": str(exc)})

    except LookupError as exc:
        return response(404, {"message": str(exc)})

    except ClientError:
        logger.exception("AWS service error in doctor profile")
        return response(500, {"message": "Unable to process profile request"})

    except Exception:
        logger.exception("Doctor profile request failed")
        return response(500, {"message": "Internal server error"})
