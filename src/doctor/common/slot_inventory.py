
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

dynamodb = boto3.resource("dynamodb")


def get_inventory_table():
    import os

    table_name = os.environ.get("SLOT_INVENTORY_TABLE")

    if not table_name:
        raise RuntimeError("SLOT_INVENTORY_TABLE is not configured")

    return dynamodb.Table(table_name)


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def sync_slot(slot, table=None):
    """
    Create or synchronise a slot inventory record.

    Existing confirmedCount and activeHoldCount are preserved.
    This prevents a metadata update from accidentally resetting
    booking capacity counters.
    """
    table = table or get_inventory_table()

    slot_id = str(slot["slot_id"])
    capacity = int(slot["capacity"])
    booked_count = int(slot.get("booked_count", 0))

    if capacity <= 0 or booked_count < 0 or booked_count > capacity:
        raise ValueError("Invalid slot capacity or booked count")

    now = datetime.now(timezone.utc).isoformat()

    item = {
        "slotId": slot_id,
        "doctorId": str(slot["doctor_id"]),
        "consultationTypeId": str(slot["consultation_type_id"]),
        "startAt": _iso(slot["start_at"]),
        "endAt": _iso(slot["end_at"]),
        "capacity": capacity,
        "confirmedCount": booked_count,
        "activeHoldCount": 0,
        "availableCount": capacity - booked_count,
        "status": str(slot["status"]),
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(slotId)",
        )
        return "CREATED"

    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    table.update_item(
        Key={"slotId": slot_id},
        UpdateExpression="""
            SET doctorId = :doctor,
                consultationTypeId = :consultation,
                startAt = :start,
                endAt = :end,
                #capacity = :capacity,
                #status = :status,
                confirmedCount = if_not_exists(confirmedCount, :booked),
                activeHoldCount = if_not_exists(activeHoldCount, :zero),
                availableCount = :capacity
                    - if_not_exists(confirmedCount, :booked)
                    - if_not_exists(activeHoldCount, :zero),
                #version = if_not_exists(#version, :zero) + :one,
                createdAt = if_not_exists(createdAt, :created),
                updatedAt = :updated
        """,
        ExpressionAttributeNames={
            "#capacity": "capacity",
            "#status": "status",
            "#version": "version",
        },
        ExpressionAttributeValues={
            ":doctor": item["doctorId"],
            ":consultation": item["consultationTypeId"],
            ":start": item["startAt"],
            ":end": item["endAt"],
            ":capacity": capacity,
            ":status": item["status"],
            ":booked": booked_count,
            ":zero": 0,
            ":one": 1,
            ":created": now,
            ":updated": now,
        },
    )

    return "UPDATED"


def update_slot_status(slot_id, status, table=None):
    """Update slot status without changing its capacity counters."""
    table = table or get_inventory_table()

    table.update_item(
        Key={"slotId": str(slot_id)},
        UpdateExpression="""
            SET #status = :status,
                updatedAt = :updated,
                #version = if_not_exists(#version, :zero) + :one
        """,
        ExpressionAttributeNames={
            "#status": "status",
            "#version": "version",
        },
        ExpressionAttributeValues={
            ":status": status,
            ":updated": datetime.now(timezone.utc).isoformat(),
            ":zero": 0,
            ":one": 1,
        },
        ConditionExpression="attribute_exists(slotId)",
    )
