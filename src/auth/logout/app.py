
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
    authorizer_context = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )

    jti = authorizer_context.get("jti")
    user_id = authorizer_context.get("userId")

    if not jti or not user_id:
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
            or refresh_record.get("userId") != user_id
            or refresh_record.get("revoked") is not False
        ):
            return response(401, {"message": "Refresh token is invalid or revoked"})
        logger.info(
            "Logout expiryTimestamp type: %s",
            type(refresh_record.get("expiryTimestamp")).__name__,
        )

        now = int(time.time())
        if int(refresh_record.get("expiryTimestamp", 0)) <= now:
            return response(401, {"message": "Refresh token has expired"})

        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": access_tokens.name,
                        "Key": {"jti": {"S": jti}},
                        "UpdateExpression": "SET revoked = :true",
                        "ConditionExpression": (
                            "userId = :user_id AND revoked = :false"
                        ),
                        "ExpressionAttributeValues": {
                            ":true": {"BOOL": True},
                            ":false": {"BOOL": False},
                            ":user_id": {"S": user_id},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": refresh_tokens.name,
                        "Key": {"tokenHash": {"S": token_hash}},
                        "UpdateExpression": "SET revoked = :true",
                        "ConditionExpression": (
                            "userId = :user_id "
                            "AND revoked = :false "
                            "AND expiryTimestamp > :now"
                        ),
                        "ExpressionAttributeValues": {
                            ":true": {"BOOL": True},
                            ":false": {"BOOL": False},
                            ":user_id": {"S": user_id},
                            ":now": {"N": str(now)},
                        },
                    }
                },
            ]
        )

        return response(200, {"message": "Logged out successfully"})

    except client.exceptions.TransactionCanceledException:
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
