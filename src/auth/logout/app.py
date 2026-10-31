
import os
import json
import hashlib
import logging
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
client = dynamodb.meta.client

access_tokens = dynamodb.Table(os.environ["ACCESS_TOKENS_TABLE"])
refresh_tokens = dynamodb.Table(os.environ["REFRESH_TOKENS_TABLE"])


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    authorizer_context = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    jti = authorizer_context.get("jti")
    user_id = authorizer_context.get("userId")
    session_id = authorizer_context.get("session_id")
    role = authorizer_context.get("role")

    if not jti or not user_id or not session_id or not role:
        return response(401, {"message": "Unauthorized"})

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return response(400, {"message": "Invalid JSON body"})

    refresh_token = body.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return response(400, {"message": "refreshToken is required"})

    token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

    try:
        refresh_result = refresh_tokens.get_item(
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
            or int(refresh_record.get("expiryTimestamp", 0)) <= int(time.time())
        ):
            return response(401, {"message": "Invalid refresh token"})
        logger.info(
            "Logout expiryTimestamp type: %s",
            type(refresh_record.get("expiryTimestamp")).__name__,
        )

        now = int(time.time())
        if int(refresh_record.get("expiryTimestamp", 0)) <= now:
            return response(401, {"message": "Refresh token has expired"})

        logger.info(
            "Logout access key diagnostic: table=%s, region=%s, jti_type=%s, jti_length=%s",
            access_tokens.name,
            client.meta.region_name,
            type(jti).__name__,
            len(jti) if isinstance(jti, str) else None,
        )

        logger.info(
            "Logout transaction key diagnostics: "
            "jti_type=%s, jti_length=%s, "
            "token_hash_type=%s, token_hash_length=%s, "
            "access_table=%s, refresh_table=%s",
            type(jti).__name__,
            len(jti) if isinstance(jti, str) else None,
            type(token_hash).__name__,
            len(token_hash) if isinstance(token_hash, str) else None,
            access_tokens.name,
            refresh_tokens.name,
        )

        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": access_tokens.name,
                        "Key": {"jti": {"S": jti}},
                        "UpdateExpression": "SET revoked = :true",
                        "ConditionExpression": "userId = :user_id AND revoked = :false",
                        "ExpressionAttributeValues": {
                            ":true": {"BOOL": True},
                            ":false": {"BOOL": False},
                            ":user_id": {"S": str(user_id)},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": refresh_tokens.name,
                        "Key": {"tokenHash": {"S": token_hash}},
                        "UpdateExpression": "SET #status = :revoked, revokedAt = :now",
                        "ConditionExpression": (
                            "userId = :user_id AND session_id = :session_id "
                            "AND #status = :active"
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
                        },
                    }
                },
            ]
        )

        return response(200, {"message": "Logged out successfully"})

    except client.exceptions.TransactionCanceledException as exc:
        reasons = [
            reason.get("Code", "Unknown")
            for reason in exc.response.get("CancellationReasons", [])
        ]
        logger.warning(
            "Logout transaction cancellation details: %s",
            exc.response.get("CancellationReasons", []),
        )
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
