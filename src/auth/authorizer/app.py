
import json
import logging
import os
import time

import boto3
import jwt
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

JWT_SECRET_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
JWT_ISSUER = os.environ.get("JWT_ISSUER", "medislot")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "medislot-api")
ACCESS_TOKENS_TABLE = os.environ["ACCESS_TOKENS_TABLE"]

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
access_table = dynamodb.Table(ACCESS_TOKENS_TABLE)

try:
    JWT_SIGNING_SECRET = ssm.get_parameter(
        Name=JWT_SECRET_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]
except Exception:
    logger.exception("Failed to load JWT signing secret")
    raise


def handler(event, context):
    headers = event.get("headers") or {}
    authorization = next(
        (
            value
            for key, value in headers.items()
            if key.lower() == "authorization"
        ),
        "",
    )

    if not authorization.lower().startswith("bearer "):
        return {"isAuthorized": False}

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return {"isAuthorized": False}

    try:
        claims = jwt.decode(
            token,
            JWT_SIGNING_SECRET,
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "role", "exp", "iat", "jti"]},
        )

        role = claims.get("role")
        user_id = claims.get("sub")
        jti = claims.get("jti")

        if role not in ("DOCTOR", "PATIENT") or not user_id or not jti:
            return {"isAuthorized": False}

        result = access_table.get_item(
            Key={"jti": jti},
            ConsistentRead=True,
        )
        item = result.get("Item")

        if (
            not item
            or item.get("revoked") is True
            or int(item.get("expiryTimestamp", 0)) <= int(time.time())
            or str(item.get("userId")) != str(user_id)
            or item.get("role") != role
        ):
            return {"isAuthorized": False}

        return {
            "isAuthorized": True,
            "context": {
                "userId": str(user_id),
                "role": role,
                "jti": jti,
            },
        }

    except (jwt.InvalidTokenError, ValueError, TypeError):
        return {"isAuthorized": False}
    except ClientError:
        logger.exception("Unable to verify access-token record")
        # Fail closed if DynamoDB cannot be checked.
        return {"isAuthorized": False}
