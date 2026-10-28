import os
import json
import time
import uuid
import hashlib
import secrets
import logging

import boto3
import jwt
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")
dynamodb = boto3.client("dynamodb")

JWT_SECRET_PARAMETER = os.environ["JWT_SECRET_PARAMETER"]
REFRESH_TOKENS_TABLE = os.environ["REFRESH_TOKENS_TABLE"]
ACCESS_TOKENS_TABLE = os.environ["ACCESS_TOKENS_TABLE"]
JWT_ISSUER = os.environ["JWT_ISSUER"]
JWT_AUDIENCE = os.environ["JWT_AUDIENCE"]

ACCESS_TOKEN_TTL_SECONDS = int(
    os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900")
)
REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("REFRESH_TOKEN_TTL_SECONDS", "604800")
)

JWT_SECRET = ssm.get_parameter(
    Name=JWT_SECRET_PARAMETER,
    WithDecryption=True,
)["Parameter"]["Value"]


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, json.JSONDecodeError):
        return response(400, {"message": "Invalid JSON body"})

    refresh_token = body.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return response(400, {"message": "refreshToken is required"})

    now = int(time.time())
    old_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

    try:
        result = dynamodb.get_item(
            TableName=REFRESH_TOKENS_TABLE,
            Key={"tokenHash": {"S": old_hash}},
            ConsistentRead=True,
        )
        old_record = result.get("Item")

        if not old_record:
            return response(401, {"message": "Invalid refresh token"})

        if (
            old_record.get("revoked", {}).get("BOOL", False)
            or int(old_record["expiryTimestamp"]["N"]) <= now
        ):
            return response(401, {"message": "Invalid refresh token"})

        user_id = old_record["userId"]["S"]
        role = old_record["role"]["S"]

        if role not in ("DOCTOR", "PATIENT"):
            return response(401, {"message": "Invalid refresh token"})

        access_jti = str(uuid.uuid4())
        access_expiry = now + ACCESS_TOKEN_TTL_SECONDS

        claims = {
            "sub": user_id,
            "role": role,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": now,
            "exp": access_expiry,
            "jti": access_jti,
        }
        access_token = jwt.encode(
            claims, JWT_SECRET, algorithm="HS256"
        )

        new_refresh_token = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(
            new_refresh_token.encode()
        ).hexdigest()
        refresh_expiry = now + REFRESH_TOKEN_TTL_SECONDS

        # Atomically consume the old token and create both new records.
        dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": REFRESH_TOKENS_TABLE,
                        "Key": {"tokenHash": {"S": old_hash}},
                        "UpdateExpression": "SET revoked = :true",
                        "ConditionExpression": (
                            "revoked = :false AND expiryTimestamp > :now"
                        ),
                        "ExpressionAttributeValues": {
                            ":true": {"BOOL": True},
                            ":false": {"BOOL": False},
                            ":now": {"N": str(now)},
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": REFRESH_TOKENS_TABLE,
                        "Item": {
                            "tokenHash": {"S": new_hash},
                            "userId": {"S": user_id},
                            "role": {"S": role},
                            "expiryTimestamp": {
                                "N": str(refresh_expiry)
                            },
                            "createdAt": {"N": str(now)},
                            "revoked": {"BOOL": False},
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(tokenHash)"
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": ACCESS_TOKENS_TABLE,
                        "Item": {
                            "jti": {"S": access_jti},
                            "userId": {"S": user_id},
                            "role": {"S": role},
                            "expiryTimestamp": {
                                "N": str(access_expiry)
                            },
                            "createdAt": {"N": str(now)},
                            "revoked": {"BOOL": False},
                        },
                        "ConditionExpression": "attribute_not_exists(jti)",
                    }
                },
            ]
        )

        return response(
            200,
            {
                "accessToken": access_token,
                "tokenType": "Bearer",
                "expiresIn": ACCESS_TOKEN_TTL_SECONDS,
                "refreshToken": new_refresh_token,
            },
        )

    except dynamodb.exceptions.TransactionCanceledException:
        # Another concurrent refresh may have consumed the old token.
        return response(401, {"message": "Invalid or already-used refresh token"})
    except ClientError:
        logger.exception("Refresh-token operation failed")
        return response(500, {"message": "Unable to refresh tokens"})
    except Exception:
        logger.exception("Unexpected refresh-token error")
        return response(500, {"message": "Internal server error"})