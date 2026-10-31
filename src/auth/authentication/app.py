
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
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

ssm = boto3.client("ssm", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
client = dynamodb.meta.client

JWT_SECRET_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

JWT_ISSUER = os.environ["JWT_ISSUER"]
JWT_AUDIENCE = os.environ["JWT_AUDIENCE"]

REFRESH_TOKENS_TABLE = os.environ["REFRESH_TOKENS_TABLE"]
ACCESS_TOKENS_TABLE = os.environ["ACCESS_TOKENS_TABLE"]
SESSIONS_TABLE = os.environ["SESSIONS_TABLE"]

ACCESS_TOKEN_TTL_SECONDS = int(
    os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900")
)
REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", "604800")
)

refresh_table = dynamodb.Table(REFRESH_TOKENS_TABLE)
access_table = dynamodb.Table(ACCESS_TOKENS_TABLE)
sessions_table = dynamodb.Table(SESSIONS_TABLE)

try:
    DB_PASSWORD = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]

    JWT_SECRET = ssm.get_parameter(
        Name=JWT_SECRET_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]

except Exception:
    logger.exception("Failed to load authentication secrets")
    raise


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def parse_body(event):
    body = event.get("body")

    if body is None:
        return {}

    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        return json.loads(body)

    return body


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
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=DB_PASSWORD,
        database=os.getenv("DB_NAME", "medislot"),
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        charset="utf8mb4",
        autocommit=True,
    )


def handle_login(event, body, correlation_id):
    connection = None

    try:
        email = body.get("email")
        password = body.get("password")

        if not isinstance(email, str) or not isinstance(password, str):
            return response(
                400,
                {"message": "Email and password are required"},
            )

        email = email.strip().lower()

        if (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not password
            or len(password) > 72
        ):
            return response(400, {"message": "Invalid login input"})

        route_key = event.get("routeKey", "")

        if route_key == "POST /login/doctor":
            role = "DOCTOR"
            table = "doctors"
            id_column = "doctor_id"

        elif route_key == "POST /login/patient":
            role = "PATIENT"
            table = "patients"
            id_column = "patient_id"

        else:
            return response(404, {"message": "Login route not found"})

        connection = get_db_connection()

        with connection.cursor() as cursor:
            query = f"""
                SELECT {id_column}, status, password_hash
                FROM {table}
                WHERE email = %s
                LIMIT 1
            """
            cursor.execute(query, (email,))
            user = cursor.fetchone()

        if (
            not user
            or user[1] != "ACTIVE"
            or not bcrypt.checkpw(
                password.encode("utf-8"),
                user[2].encode("utf-8"),
            )
        ):
            return response(401, {"message": "Invalid email or password"})

        now = int(time.time())

        user_id = str(user[0])
        session_id = str(uuid.uuid4())
        access_jti = str(uuid.uuid4())

        access_expiry = now + ACCESS_TOKEN_TTL_SECONDS
        session_expiry = now + REFRESH_TOKEN_TTL_SECONDS

        access_token = jwt.encode(
            {
                "sub": user_id,
                "role": role,
                "session_id": session_id,
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

        logger.info(
            "Login transaction types: user_id=%s, access_jti=%s, "
            "refresh_token_hash=%s, session_id=%s",
            type(user_id).__name__,
            type(access_jti).__name__,
            type(refresh_token_hash).__name__,
            type(session_id).__name__,
        )

        # Create all three records atomically.
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": ACCESS_TOKENS_TABLE,
                        "Item": {
                            "jti": {"S": access_jti},
                            "userId": {"S": str(user_id)},
                            "role": {"S": role},
                            "expiryTimestamp": {"N": str(access_expiry)},
                            "createdAt": {"N": str(now)},
                            "revoked": {"BOOL": False},
                            "session_id": {"S": session_id},
                        },
                        "ConditionExpression": "attribute_not_exists(jti)",
                    }
                },
                {
                    "Put": {
                        "TableName": REFRESH_TOKENS_TABLE,
                        "Item": {
                            "tokenHash": {"S": refresh_token_hash},
                            "session_id": {"S": session_id},
                            "userId": {"S": str(user_id)},
                            "role": {"S": role},
                            "status": {"S": "ACTIVE"},
                            "expiryTimestamp": {"N": str(session_expiry)},
                            "createdAt": {"N": str(now)},
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(tokenHash)"
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": SESSIONS_TABLE,
                        "Item": {
                            "session_id": {"S": session_id},
                            "userId": {"S": str(user_id)},
                            "role": {"S": role},
                            "status": {"S": "ACTIVE"},
                            "expiryTimestamp": {"N": str(session_expiry)},
                            "createdAt": {"N": str(now)},
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(session_id)"
                        ),
                    }
                },
            ]
        )

        logger.info(json.dumps({
            "event": "login_succeeded",
            "correlation_id": correlation_id,
            "user_id": user_id,
            "role": role,
        }))

        return response(
            200,
            {
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_TOKEN_TTL_SECONDS,
                "refreshToken": refresh_token,
                "correlationId": correlation_id,
            },
        )

    except ClientError as exc:
        error = exc.response.get("Error", {})

        logger.error(
            "Login dependency failed: code=%s message=%s",
            error.get("Code"),
            error.get("Message"),
        )

        if error.get("Code") == "TransactionCanceledException":
            logger.error(
                "DynamoDB transaction cancellation reasons: %s",
                exc.response.get("CancellationReasons"),
            )

        return response(
            503,
            {"message": "Login is temporarily unavailable"},
        )

    except BotoCoreError:
        logger.exception("Login dependency failed")
        return response(
            503,
            {"message": "Login is temporarily unavailable"},
        )

    except Exception:
        logger.exception("Login operation failed")
        return response(500, {"message": "Login could not be completed"})

    finally:
        if connection:
            connection.close()


def handle_refresh(body):
    refresh_token = body.get("refreshToken")

    if not isinstance(refresh_token, str) or not refresh_token:
        return response(400, {"message": "refreshToken is required"})

    now = int(time.time())

    token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()

    try:
        result = refresh_table.get_item(
            Key={"tokenHash": token_hash},
            ConsistentRead=True,
        )

        refresh_record = result.get("Item")

        if (
            not refresh_record
            or refresh_record.get("status") != "ACTIVE"
            or int(refresh_record.get("expiryTimestamp", 0)) <= now
        ):
            return response(401, {"message": "Invalid refresh token"})

        user_id = str(refresh_record.get("userId"))
        role = refresh_record.get("role")
        session_id = refresh_record.get("session_id")

        if role not in ("DOCTOR", "PATIENT") or not session_id:
            return response(401, {"message": "Invalid refresh token"})

        # Verify the session before issuing another access token.
        session_result = sessions_table.get_item(
            Key={"session_id": session_id},
            ConsistentRead=True,
        )

        session = session_result.get("Item")

        if (
            not session
            or session.get("status") != "ACTIVE"
            or int(session.get("expiryTimestamp", 0)) <= now
            or str(session.get("userId")) != user_id
            or session.get("role") != role
        ):
            return response(401, {"message": "Session is invalid or expired"})

        access_jti = str(uuid.uuid4())
        access_expiry = now + ACCESS_TOKEN_TTL_SECONDS

        access_token = jwt.encode(
            {
                "sub": user_id,
                "role": role,
                "session_id": session_id,
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "iat": now,
                "exp": access_expiry,
                "jti": access_jti,
            },
            JWT_SECRET,
            algorithm="HS256",
        )

        # Persist the new access-token record.
        access_table.put_item(
            Item={
                "jti": access_jti,
                "userId": user_id,
                "role": role,
                "expiryTimestamp": access_expiry,
                "createdAt": now,
                "revoked": False,
                "session_id": session_id,
            },
            ConditionExpression="attribute_not_exists(jti)",
        )

        return response(
            200,
            {
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_TOKEN_TTL_SECONDS,
            },
        )

    except ClientError:
        logger.exception("Refresh-token operation failed")
        return response(500, {"message": "Unable to refresh tokens"})

    except Exception:
        logger.exception("Unexpected refresh-token error")
        return response(500, {"message": "Internal server error"})


def handle_logout(event, body):
    authorizer_context = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )

    jti = authorizer_context.get("jti")
    user_id = authorizer_context.get("userId")
    session_id = authorizer_context.get("session_id")
    role = authorizer_context.get("role")

    if not jti or not user_id or not session_id or not role:
        return response(401, {"message": "Unauthorized"})

    refresh_token = body.get("refreshToken")

    if not isinstance(refresh_token, str) or not refresh_token:
        return response(400, {"message": "refreshToken is required"})

    token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()

    now = int(time.time())

    try:
        refresh_result = refresh_table.get_item(
            Key={"tokenHash": token_hash},
            ConsistentRead=True,
        )

        refresh_record = refresh_result.get("Item")

        if (
            not refresh_record
            or str(refresh_record.get("userId")) != str(user_id)
            or refresh_record.get("session_id") != session_id
            or refresh_record.get("role") != role
            or refresh_record.get("status") != "ACTIVE"
            or int(refresh_record.get("expiryTimestamp", 0)) <= now
        ):
            return response(401, {"message": "Invalid refresh token"})

        # Recheck the session before attempting the transaction.
        session_result = sessions_table.get_item(
            Key={"session_id": session_id},
            ConsistentRead=True,
        )

        session = session_result.get("Item")

        if (
            not session
            or session.get("status") != "ACTIVE"
            or str(session.get("userId")) != str(user_id)
            or session.get("role") != role
            or int(session.get("expiryTimestamp", 0)) <= now
        ):
            return response(401, {"message": "Session is invalid or expired"})

        # Revoke the access token, refresh token and entire session atomically.
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": ACCESS_TOKENS_TABLE,
                        "Key": {"jti": {"S": jti}},
                        "UpdateExpression": "SET revoked = :true",
                        "ConditionExpression": (
                            "userId = :user_id "
                            "AND role = :role "
                            "AND revoked = :false"
                        ),
                        "ExpressionAttributeValues": {
                            ":true": {"BOOL": True},
                            ":false": {"BOOL": False},
                            ":user_id": {"S": str(user_id)},
                            ":role": {"S": role},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": REFRESH_TOKENS_TABLE,
                        "Key": {"tokenHash": {"S": token_hash}},
                        "UpdateExpression": (
                            "SET #status = :revoked, revokedAt = :now"
                        ),
                        "ConditionExpression": (
                            "userId = :user_id "
                            "AND session_id = :session_id "
                            "AND role = :role "
                            "AND #status = :active "
                            "AND expiryTimestamp > :now"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                        },
                        "ExpressionAttributeValues": {
                            ":revoked": {"S": "REVOKED"},
                            ":active": {"S": "ACTIVE"},
                            ":now": {"N": str(now)},
                            ":user_id": {"S": str(user_id)},
                            ":session_id": {"S": session_id},
                            ":role": {"S": role},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": SESSIONS_TABLE,
                        "Key": {"session_id": {"S": session_id}},
                        "UpdateExpression": (
                            "SET #status = :revoked, revokedAt = :now"
                        ),
                        "ConditionExpression": (
                            "userId = :user_id "
                            "AND role = :role "
                            "AND #status = :active "
                            "AND expiryTimestamp > :now"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                        },
                        "ExpressionAttributeValues": {
                            ":revoked": {"S": "REVOKED"},
                            ":active": {"S": "ACTIVE"},
                            ":now": {"N": str(now)},
                            ":user_id": {"S": str(user_id)},
                            ":role": {"S": role},
                        },
                    }
                },
            ]
        )

        return response(200, {"message": "Logged out successfully"})

    except client.exceptions.TransactionCanceledException:
        logger.warning("Logout transaction was cancelled")
        return response(
            401,
            {"message": "Session is invalid, expired, or already revoked"},
        )

    except ClientError:
        logger.exception("Logout operation failed")
        return response(500, {"message": "Unable to log out"})

    except Exception:
        logger.exception("Unexpected logout error")
        return response(500, {"message": "Internal server error"})


def handler(event, context):
    route_key = event.get("routeKey", "")
    correlation_id = get_correlation_id(event, context)

    try:
        body = parse_body(event)

        if not isinstance(body, dict):
            return response(
                400,
                {"message": "Request body must be a JSON object"},
            )

    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return response(400, {"message": "Invalid JSON body"})

    if route_key in (
        "POST /login/doctor",
        "POST /login/patient",
    ):
        return handle_login(event, body, correlation_id)

    if route_key == "POST /refresh":
        return handle_refresh(body)

    if route_key == "POST /logout":
        return handle_logout(event, body)

    return response(404, {"message": "Route not found"})
