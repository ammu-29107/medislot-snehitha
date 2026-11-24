
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import pymysql
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]
SLOT_INVENTORY_TABLE = os.environ["SLOT_INVENTORY_TABLE"]

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "25"))
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
    """Claim due inventory events without competing with other workers."""
    locked_until = (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=LOCK_SECONDS)
    )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT inventory_event_id
            FROM slot_inventory_outbox
            WHERE (
                status = 'PENDING'
                AND next_attempt_at <= UTC_TIMESTAMP()
            )
            OR (
                status = 'PROCESSING'
                AND locked_until <= UTC_TIMESTAMP()
            )
            ORDER BY inventory_event_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (BATCH_SIZE,),
        )

        event_ids = [
            row["inventory_event_id"]
            for row in cursor.fetchall()
        ]

        if not event_ids:
            connection.commit()
            return [], locked_until

        placeholders = ",".join(["%s"] * len(event_ids))

        cursor.execute(
            f"""
            UPDATE slot_inventory_outbox
            SET
                status = 'PROCESSING',
                attempts = attempts + 1,
                locked_until = %s,
                last_error = NULL
            WHERE inventory_event_id IN ({placeholders})
            """,
            (locked_until.replace(tzinfo=None), *event_ids),
        )

        cursor.execute(
            f"""
            SELECT
                inventory_event_id,
                slot_id,
                slot_version,
                attempts
            FROM slot_inventory_outbox
            WHERE inventory_event_id IN ({placeholders})
            ORDER BY inventory_event_id
            """,
            event_ids,
        )

        events = cursor.fetchall()

    connection.commit()
    return events, locked_until


def get_latest_slot(connection, slot_id):
    """
    Read the current committed MySQL slot state.

    Commit after the read so the next event gets a fresh
    transaction snapshot rather than an old repeatable-read snapshot.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                slot_id,
                doctor_id,
                consultation_type_id,
                start_at,
                end_at,
                capacity,
                booked_count,
                active_hold_count,
                status,
                inventory_version
            FROM slots
            WHERE slot_id = %s
            """,
            (slot_id,),
        )
        slot = cursor.fetchone()

    connection.commit()
    return slot


def project_slot(slot, table):
    """Apply a versioned MySQL slot snapshot to DynamoDB."""
    capacity = int(slot["capacity"])
    booked_count = int(slot["booked_count"])
    hold_count = int(slot["active_hold_count"])
    source_version = int(slot["inventory_version"])

    if (
        capacity <= 0
        or booked_count < 0
        or hold_count < 0
        or booked_count + hold_count > capacity
    ):
        raise ValueError(
            f"Invalid inventory values for slot {slot['slot_id']}"
        )

    now = datetime.now(timezone.utc).isoformat()

    start_at = slot["start_at"]
    end_at = slot["end_at"]

    start_at = (
        start_at.isoformat()
        if isinstance(start_at, datetime)
        else str(start_at)
    )
    end_at = (
        end_at.isoformat()
        if isinstance(end_at, datetime)
        else str(end_at)
    )

    try:
        table.update_item(
            Key={"slotId": str(slot["slot_id"])},
            UpdateExpression="""
                SET
                    doctorId = :doctor,
                    consultationTypeId = :consultation,
                    startAt = :start,
                    endAt = :end,
                    #capacity = :capacity,
                    confirmedCount = :booked,
                    activeHoldCount = :holds,
                    availableCount = :available,
                    #status = :status,
                    sourceVersion = :source_version,
                    #version = if_not_exists(#version, :zero) + :one,
                    createdAt = if_not_exists(createdAt, :created),
                    updatedAt = :updated
            """,
            ConditionExpression=(
                Attr("sourceVersion").not_exists()
                | Attr("sourceVersion").lt(source_version)
            ),
            ExpressionAttributeNames={
                "#capacity": "capacity",
                "#status": "status",
                "#version": "version",
            },
            ExpressionAttributeValues={
                ":doctor": str(slot["doctor_id"]),
                ":consultation": str(slot["consultation_type_id"]),
                ":start": start_at,
                ":end": end_at,
                ":capacity": capacity,
                ":booked": booked_count,
                ":holds": hold_count,
                ":available": capacity - booked_count - hold_count,
                ":status": str(slot["status"]),
                ":source_version": source_version,
                ":zero": 0,
                ":one": 1,
                ":created": now,
                ":updated": now,
            },
        )

        return "UPDATED"

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            # An equal or newer MySQL snapshot is already projected.
            return "ALREADY_CURRENT"

        raise


def mark_applied(connection, event, locked_until):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE slot_inventory_outbox
            SET
                status = 'APPLIED',
                applied_at = UTC_TIMESTAMP(),
                locked_until = NULL,
                last_error = NULL
            WHERE inventory_event_id = %s
              AND status = 'PROCESSING'
              AND locked_until = %s
            """,
            (
                event["inventory_event_id"],
                locked_until.replace(tzinfo=None),
            ),
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Could not mark inventory event "
                f"{event['inventory_event_id']} as APPLIED; "
                "its processing lock may have expired."
            )

    connection.commit()


def mark_failed(connection, event, locked_until, error):
    attempts = int(event["attempts"])
    delay_seconds = min(
        60 * (2 ** max(attempts - 1, 0)),
        3600,
    )

    new_status = (
        "FAILED"
        if attempts >= MAX_ATTEMPTS
        else "PENDING"
    )

    error_message = str(error)[:4000]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE slot_inventory_outbox
            SET
                status = %s,
                next_attempt_at = DATE_ADD(
                    UTC_TIMESTAMP(),
                    INTERVAL %s SECOND
                ),
                locked_until = NULL,
                last_error = %s
            WHERE inventory_event_id = %s
              AND status = 'PROCESSING'
              AND locked_until = %s
            """,
            (
                new_status,
                delay_seconds,
                error_message,
                event["inventory_event_id"],
                locked_until.replace(tzinfo=None),
            ),
        )

    connection.commit()

    logger.error(
        "Inventory event %s failed. Status=%s Attempts=%s Error=%s",
        event["inventory_event_id"],
        new_status,
        attempts,
        error_message,
    )


def process_event(connection, event, locked_until, table):
    slot = get_latest_slot(connection, event["slot_id"])

    if not slot:
        raise RuntimeError(
            f"Slot {event['slot_id']} was not found in MySQL"
        )

    current_version = int(slot["inventory_version"])
    event_version = int(event["slot_version"])

    if current_version < event_version:
        raise RuntimeError(
            f"Slot {event['slot_id']} has inventory version "
            f"{current_version}, older than event version {event_version}"
        )

    result = project_slot(slot, table)

    mark_applied(connection, event, locked_until)

    logger.info(
        "Inventory event %s applied. Slot=%s MySQLVersion=%s Result=%s",
        event["inventory_event_id"],
        event["slot_id"],
        current_version,
        result,
    )

    return result


def handler(event, context):
    connection = get_connection()
    table = dynamodb.Table(SLOT_INVENTORY_TABLE)

    applied = 0
    failed = 0
    already_current = 0

    try:
        claimed, locked_until = claim_events(connection)

        logger.info("Claimed %s inventory events", len(claimed))

        for inventory_event in claimed:
            try:
                result = process_event(
                    connection,
                    inventory_event,
                    locked_until,
                    table,
                )

                applied += 1

                if result == "ALREADY_CURRENT":
                    already_current += 1

            except Exception as exc:
                connection.rollback()

                try:
                    mark_failed(
                        connection,
                        inventory_event,
                        locked_until,
                        exc,
                    )
                except Exception:
                    connection.rollback()
                    logger.exception(
                        "Could not update failure state for inventory "
                        "event %s",
                        inventory_event["inventory_event_id"],
                    )

                failed += 1

        return {
            "statusCode": 200,
            "body": {
                "claimed": len(claimed),
                "applied": applied,
                "already_current": already_current,
                "failed": failed,
            },
        }

    finally:
        connection.close()
