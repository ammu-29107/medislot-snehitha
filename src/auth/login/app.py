import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid

import boto3
import bcrypt
import jwt
import pymysql
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ssm = boto3.client("ssm", region_name=AWS_REGION)
JWT_SECRET_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
REFRESH_TOKENS_TABLE = os.environ["REFRESH_TOKENS_TABLE"]
JWT_ISSUER = os.environ["JWT_ISSUER"]
JWT_AUDIENCE = os.environ["JWT_AUDIENCE"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

ACCESS_TOKEN_TTL_SECONDS = int(
    os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "900")
)
REFRESH_TOKEN_TTL_SECONDS = int(
    os.environ.get("REFRESH_TOKEN_TTL_SECONDS", "2592000")
)
DB_PASSWORD = ssm.get_parameter(
    Name=DB_PASSWORD_PARAMETER,
    WithDecryption=True,
)["Parameter"]["Value"]

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
refresh_table = dynamodb.Table(REFRESH_TOKENS_TABLE)

# Loaded at Lambda cold start; never log or return this value.
JWT_SECRET = ssm.get_parameter(
    Name=JWT_SECRET_PARAMETER,
    WithDecryption=True,
)["Parameter"]["Value"]


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
        autocommit=True,
    )


def parse_body(event):
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, str):
        return json.loads(body)
    return body


def handler(event, context):
    correlation_id = get_correlation_id(event, context)
    connection = None

    try:
        try:
            body = parse_body(event)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return response(400, {"message": "Request body must be valid JSON"})

        if not isinstance(body, dict):
            return response(400, {"message": "Request body must be a JSON object"})

        email = body.get("email")
        password = body.get("password")

        if not isinstance(email, str) or not isinstance(password, str):
            return response(400, {"message": "Email and password are required"})

        email = email.strip().lower()
        if (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not password
            or len(password) > 72
        ):
            return response(400, {"message": "Invalid login input"})

        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.user_id, u.role, u.status, c.password_hash
                FROM users AS u
                JOIN user_credentials AS c ON c.user_id = u.user_id
                WHERE u.email = %s
                LIMIT 1
                """,
                (email,),
            )
            user = cursor.fetchone()

        # Use a generic error for unknown email and wrong password.
        if (
            not user
            or user[2] != "ACTIVE"
            or not bcrypt.checkpw(
                password.encode("utf-8"),
                user[3].encode("utf-8"),
            )
        ):
            return response(401, {"message": "Invalid email or password"})

        now = int(time.time())
        access_jti = str(uuid.uuid4())
        access_expiry = now + ACCESS_TOKEN_TTL_SECONDS

        access_token = jwt.encode(
            {
                "sub": str(user[0]),
                "role": user[1],
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "iat": now,
                "exp": access_expiry,
                "jti": access_jti,
            },
            JWT_SECRET,
            algorithm="HS256",
        )

        refresh_token = secrets.token_urlsafe(32)
        refresh_token_hash = hashlib.sha256(
            refresh_token.encode("utf-8")
        ).hexdigest()

        refresh_table.put_item(
            Item={
                "tokenHash": refresh_token_hash,
                "userId": str(user[0]),
                "role": user[1],
                "expiryTimestamp": now + REFRESH_TOKEN_TTL_SECONDS,
                "createdAt": now,
                "revoked": False,
            },
            ConditionExpression="attribute_not_exists(tokenHash)",
        )

        logger.info(json.dumps({
            "event": "login_succeeded",
            "correlation_id": correlation_id,
            "user_id": str(user[0]),
            "role": user[1],
        }))

        return response(200, {
            "accessToken": access_token,
            "tokenType": "Bearer",
            "expiresIn": ACCESS_TOKEN_TTL_SECONDS,
            "refreshToken": refresh_token,
            "correlationId": correlation_id,
        })

    except (BotoCoreError, ClientError):
        logger.exception(json.dumps({
            "event": "login_dependency_failed",
            "correlation_id": correlation_id,
        }))
        return response(503, {"message": "Login is temporarily unavailable"})

    except Exception as exc:
        logger.exception(json.dumps({
            "event": "login_failed",
            "correlation_id": correlation_id,
            "error_type": type(exc).__name__,
        }))
        return response(500, {"message": "Login could not be completed"})

    finally:
        if connection:
            connection.close()