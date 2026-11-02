
import json
import logging
import os

import boto3
import pymysql
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

ssm = boto3.client("ssm")


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def handler(event, context):
    auth_context = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )

    user_id = auth_context.get("userId")
    role = auth_context.get("role")

    if not user_id or role not in ("DOCTOR", "PATIENT"):
        return response(401, {"message": "Unauthorized"})

    if role == "DOCTOR":
        table = "doctors"
        id_column = "doctor_id"
        profile_columns = (
            "doctor_id, full_name, email, license_number, "
            "hospital_id, qualifications, experience_years, "
            "bio, profile_photo_key, status"
        )
    else:
        table = "patients"
        id_column = "patient_id"
        profile_columns = (
            "patient_id, full_name, email, date_of_birth, "
            "phone_number, status"
        )

    try:
        password = ssm.get_parameter(
            Name=DB_PASSWORD_PARAMETER,
            WithDecryption=True,
        )["Parameter"]["Value"]

        connection = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=password,
            database=DB_NAME,
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT {profile_columns}
                    FROM {table}
                    WHERE {id_column} = %s
                    LIMIT 1
                    """,
                    (user_id,),
                )
                profile = cursor.fetchone()
                if profile and role == "DOCTOR":
                    cursor.execute(
                        """
                        SELECT s.speciality_id, s.speciality_name
                        FROM doctor_specialities ds
                        JOIN specialities s
                            ON s.speciality_id = ds.speciality_id
                        WHERE ds.doctor_id = %s
                        ORDER BY s.speciality_name
                        """,
                        (user_id,),
                    )
                    profile["specialities"] = cursor.fetchall()
        finally:
            connection.close()

        if not profile:
            return response(404, {"message": "Profile not found"})

        profile["userId"] = str(profile.pop(id_column))
        profile["role"] = role

        return response(200, {"profile": profile})

    except (ClientError, pymysql.MySQLError):
        logger.exception("Unable to retrieve user profile")
        return response(500, {"message": "Unable to retrieve profile"})
