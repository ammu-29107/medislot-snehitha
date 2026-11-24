
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")
events = boto3.client("events")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

EVENT_BUS_NAME = os.getenv("EVENT_BUS_NAME", "default")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "8"))
LOCK_SECONDS = int(os.getenv("LOCK_SECONDS", "120"))


def get_connection():
    password = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]

    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=password,
        database=DB_NAME,
        port=3306,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=25,
        write_timeout=25,
        autocommit=False,
        charset="utf8mb4",
        init_command="SET time_zone = '+00:00'",
    )


def claim_events(connection):
    """
    Claim due PENDING events and expired PROCESSING events.

    SKIP LOCKED allows multiple publisher invocations to work
    concurrently without claiming the same database rows.
    """
    locked_until = datetime.now(timezone.utc).replace(
        microsecond=0
    ) + timedelta(seconds=LOCK_SECONDS)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id
            FROM outbox_events
            WHERE (
                status = 'PENDING'
                AND next_attempt_at <= UTC_TIMESTAMP()
            )
            OR (
                status = 'PROCESSING'
                AND locked_until <= UTC_TIMESTAMP()
            )
            ORDER BY event_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (BATCH_SIZE,),
        )

        rows = cursor.fetchall()
        event_ids = [row["event_id"] for row in rows]

        if not event_ids:
            connection.commit()
            return [], locked_until

        placeholders = ",".join(["%s"] * len(event_ids))

        cursor.execute(
            f"""
            UPDATE outbox_events
            SET
                status = 'PROCESSING',
                attempts = attempts + 1,
                locked_until = %s,
                last_error = NULL
            WHERE event_id IN ({placeholders})
            """,
            (locked_until.replace(tzinfo=None), *event_ids),
        )

        cursor.execute(
            f"""
            SELECT
                event_id,
                event_uuid,
                aggregate_type,
                aggregate_id,
                event_type,
                payload,
                attempts
            FROM outbox_events
            WHERE event_id IN ({placeholders})
            ORDER BY event_id
            """,
            event_ids,
        )

        claimed = cursor.fetchall()

    connection.commit()
    return claimed, locked_until


def mark_published(connection, event_id, locked_until):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE outbox_events
            SET
                status = 'PUBLISHED',
                published_at = UTC_TIMESTAMP(),
                locked_until = NULL,
                last_error = NULL
            WHERE event_id = %s
              AND status = 'PROCESSING'
              AND locked_until = %s
            """,
            (event_id, locked_until.replace(tzinfo=None)),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not mark event {event_id} as PUBLISHED; "
                "its processing lock may have expired."
            )

    connection.commit()


def mark_failed(connection, event, locked_until, error):
    attempts = int(event["attempts"])

    # Exponential retry delay, capped at one hour.
    delay_seconds = min(60 * (2 ** max(attempts - 1, 0)), 3600)

    new_status = (
        "FAILED"
        if attempts >= MAX_ATTEMPTS
        else "PENDING"
    )

    error_message = str(error)[:4000]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE outbox_events
            SET
                status = %s,
                next_attempt_at = DATE_ADD(
                    UTC_TIMESTAMP(),
                    INTERVAL %s SECOND
                ),
                locked_until = NULL,
                last_error = %s
            WHERE event_id = %s
              AND status = 'PROCESSING'
              AND locked_until = %s
            """,
            (
                new_status,
                delay_seconds,
                error_message,
                event["event_id"],
                locked_until.replace(tzinfo=None),
            ),
        )

    connection.commit()

    logger.error(
        "Event %s publication failed. Status=%s Attempts=%s Error=%s",
        event["event_id"],
        new_status,
        attempts,
        error_message,
    )


def publish_event(event):
    payload = event["payload"]

    if isinstance(payload, str):
        payload = json.loads(payload)

    detail = {
        "event_id": int(event["event_id"]),
        "event_uuid": event["event_uuid"],
        "aggregate_type": event["aggregate_type"],
        "aggregate_id": event["aggregate_id"],
        "event_type": event["event_type"],
        "payload": payload,
    }

    result = events.put_events(
        Entries=[
            {
                "EventBusName": EVENT_BUS_NAME,
                "Source": "medislot.appointments",
                "DetailType": event["event_type"],
                "Detail": json.dumps(
                    detail,
                    separators=(",", ":"),
                ),
            }
        ]
    )

    if result.get("FailedEntryCount", 0) != 0:
        entry = result.get("Entries", [{}])[0]
        raise RuntimeError(
            entry.get("ErrorMessage", "EventBridge rejected the event")
        )

    return result["Entries"][0].get("EventId")


def handler(event, context):
    connection = get_connection()

    published = 0
    failed = 0

    try:
        claimed, locked_until = claim_events(connection)

        logger.info("Claimed %s outbox events", len(claimed))

        for outbox_event in claimed:
            try:
                eventbridge_id = publish_event(outbox_event)

                mark_published(
                    connection,
                    outbox_event["event_id"],
                    locked_until,
                )

                published += 1

                logger.info(
                    "Published outbox event %s as EventBridge event %s",
                    outbox_event["event_id"],
                    eventbridge_id,
                )

            except Exception as exc:
                connection.rollback()

                try:
                    mark_failed(
                        connection,
                        outbox_event,
                        locked_until,
                        exc,
                    )
                except Exception:
                    connection.rollback()
                    logger.exception(
                        "Could not update failure state for outbox event %s",
                        outbox_event["event_id"],
                    )

                failed += 1

        return {
            "statusCode": 200,
            "body": json.dumps({
                "claimed": len(claimed),
                "published": published,
                "failed": failed,
            }),
        }

    finally:
        connection.close()
