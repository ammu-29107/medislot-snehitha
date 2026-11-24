import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import boto3
import pymysql
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
IDEMPOTENCY_TABLE = os.environ["IDEMPOTENCY_TABLE"]

inventory_table = dynamodb.Table(SLOT_INVENTORY_TABLE)
idempotency_table = dynamodb.Table(IDEMPOTENCY_TABLE)
IST = ZoneInfo("Asia/Kolkata")
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, default=json_default),
    }


def json_default(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def get_identity(event):
    authorizer = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )
    user_id = authorizer.get("userId")
    role = authorizer.get("role")
    if not user_id or role not in ("PATIENT", "DOCTOR"):
        raise ApiError(403, "Valid authentication is required")
    return str(user_id), role


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
        port=int(os.getenv("DB_PORT", "3306")),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=25,
        write_timeout=25,
        autocommit=False,
        charset="utf8mb4",
    )


def get_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ApiError(400, "Request body must be valid JSON")
    if not isinstance(value, dict):
        raise ApiError(400, "Request body must be a JSON object")
    return value


def get_header(event, name):
    headers = event.get("headers") or {}
    return next(
        (v for k, v in headers.items() if k.lower() == name.lower()),
        None,
    )


def parse_id(value, field):
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be a positive integer")


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def outbox(cursor, aggregate_type, aggregate_id, event_type, payload):
    cursor.execute(
        """
        INSERT INTO outbox_events (
            event_uuid, aggregate_type, aggregate_id,
            event_type, payload, status
        )
        VALUES (%s, %s, %s, %s, %s, 'PENDING')
        """,
        (
            str(uuid.uuid4()),
            aggregate_type,
            str(aggregate_id),
            event_type,
            json.dumps(payload, default=json_default),
        ),
    )


def enqueue_slot_inventory(cursor, slot_id):
    """Record the latest slot inventory version in the SQL transaction."""
    cursor.execute(
        """
        UPDATE slots
        SET inventory_version = inventory_version + 1
        WHERE slot_id = %s
        """,
        (slot_id,),
    )

    if cursor.rowcount != 1:
        raise RuntimeError(
            f"Could not increment inventory version for slot {slot_id}"
        )

    cursor.execute(
        """
        SELECT inventory_version
        FROM slots
        WHERE slot_id = %s
        """,
        (slot_id,),
    )
    row = cursor.fetchone()

    if not row:
        raise RuntimeError(
            f"Could not read inventory version for slot {slot_id}"
        )

    cursor.execute(
        """
        INSERT INTO slot_inventory_outbox (slot_id, slot_version)
        VALUES (%s, %s)
        """,
        (slot_id, int(row["inventory_version"])),
    )

    return int(row["inventory_version"])


def inventory_reserve(slot_id):
    try:
        inventory_table.update_item(
            Key={"slotId": str(slot_id)},
            UpdateExpression=(
                "SET confirmedCount = confirmedCount + :one, "
                "availableCount = availableCount - :one, "
                "updatedAt = :now, #version = if_not_exists(#version, :zero) + :one"
            ),
            ConditionExpression=(
                "#status = :open AND availableCount >= :one "
                "AND attribute_exists(slotId)"
            ),
            ExpressionAttributeNames={
                "#status": "status",
                "#version": "version",
            },
            ExpressionAttributeValues={
                ":one": 1,
                ":zero": 0,
                ":open": "OPEN",
                ":now": datetime.now(IST).isoformat(),
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            raise ApiError(409, "This slot is no longer available")
        raise


def inventory_release(slot_id):
    try:
        inventory_table.update_item(
            Key={"slotId": str(slot_id)},
            UpdateExpression=(
                "SET confirmedCount = confirmedCount - :one, "
                "availableCount = availableCount + :one, "
                "updatedAt = :now, #version = if_not_exists(#version, :zero) + :one"
            ),
            ConditionExpression="confirmedCount >= :one AND attribute_exists(slotId)",
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues={
                ":one": 1,
                ":zero": 0,
                ":now": datetime.now(IST).isoformat(),
            },
        )
    except ClientError:
        logger.exception("Unable to release slot inventory for slot %s", slot_id)
        raise


def claim_idempotency(key, patient_id, request_hash):
    now = int(datetime.now(IST).timestamp())
    item = {
        "idempotencyKey": key,
        "patientId": str(patient_id),
        "requestHash": request_hash,
        "status": "IN_PROGRESS",
        "createdAt": now,
        "expiryTimestamp": now + IDEMPOTENCY_TTL_SECONDS,
    }
    try:
        idempotency_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(idempotencyKey)",
        )
        return None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    existing = idempotency_table.get_item(
        Key={"idempotencyKey": key},
        ConsistentRead=True,
    ).get("Item")
    if not existing:
        raise ApiError(409, "Idempotency request is being processed; retry shortly")
    if str(existing.get("patientId")) != str(patient_id):
        raise ApiError(409, "Idempotency key is already used")
    if existing.get("requestHash") != request_hash:
        raise ApiError(409, "Idempotency key was reused with different request data")
    if existing.get("status") == "COMPLETED" and existing.get("response"):
        return existing["response"]
    raise ApiError(409, "Idempotency request is still being processed; retry shortly")


def complete_idempotency(key, payload):
    idempotency_table.update_item(
        Key={"idempotencyKey": key},
        UpdateExpression="SET #status = :completed, #response = :response",
        ExpressionAttributeNames={
            "#status": "status",
            "#response": "response",
        },
        ExpressionAttributeValues={
            ":completed": "COMPLETED",
            ":response": payload,
        },
    )


def fail_idempotency(key):
    try:
        idempotency_table.delete_item(Key={"idempotencyKey": key})
    except Exception:
        logger.exception("Could not clear failed idempotency record")


def create_appointment(cursor, patient_id, slot_id):
    now = now_ist()

    # Lock the patient row first to serialize concurrent booking attempts
    # for the same patient. This protects the upcoming-appointment limit.
    cursor.execute(
        """
        SELECT patient_id, status
        FROM patients
        WHERE patient_id = %s
        FOR UPDATE
        """,
        (patient_id,),
    )
    patient = cursor.fetchone()

    if not patient:
        raise ApiError(404, "Patient was not found")

    if patient["status"] != "ACTIVE":
        raise ApiError(403, "Patient account is not active")

    cursor.execute(
        """
        SELECT
            s.slot_id, s.doctor_id, s.consultation_type_id,
            s.start_at, s.end_at, s.capacity, s.booked_count, s.status,
            ct.consultation_fee, ct.status AS consultation_status,
            d.status AS doctor_status, p.status AS patient_status
        FROM slots s
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = s.consultation_type_id
           AND ct.doctor_id = s.doctor_id
        INNER JOIN doctors d ON d.doctor_id = s.doctor_id
        INNER JOIN patients p ON p.patient_id = %s
        WHERE s.slot_id = %s
        FOR UPDATE
        """,
        (patient_id, slot_id),
    )
    slot = cursor.fetchone()
    if not slot:
        raise ApiError(404, "Slot or patient was not found")
    if slot["patient_status"] != "ACTIVE":
        raise ApiError(403, "Patient account is not active")
    if slot["doctor_status"] != "ACTIVE" or slot["consultation_status"] != "ACTIVE":
        raise ApiError(409, "Doctor or consultation type is not available")
    if slot["status"] != "OPEN":
        raise ApiError(409, "Slot is not open for booking")
    if slot["start_at"] <= now:
        raise ApiError(409, "Past or already-started slots cannot be booked")
    if int(slot["booked_count"]) >= int(slot["capacity"]):
        raise ApiError(409, "This slot is fully booked")

    cursor.execute(
        """
        SELECT COUNT(*) AS upcoming_count
        FROM appointments a
        INNER JOIN slots s ON s.slot_id = a.slot_id
        WHERE a.patient_id = %s
          AND a.status = 'CONFIRMED'
          AND s.start_at > %s
        """,
        (patient_id, now),
    )
    if int(cursor.fetchone()["upcoming_count"]) >= 3:
        raise ApiError(409, "A patient may have at most 3 upcoming appointments")

    booking_reference = "MS" + uuid.uuid4().hex[:16].upper()
    fee = Decimal(str(slot["consultation_fee"])).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    cursor.execute(
        """
        INSERT INTO appointments (
            booking_reference, patient_id, slot_id, status,
            consultation_fee_snapshot
        )
        VALUES (%s, %s, %s, 'CONFIRMED', %s)
        """,
        (booking_reference, patient_id, slot_id, fee),
    )
    appointment_id = cursor.lastrowid

    cursor.execute(
        "UPDATE slots SET booked_count = booked_count + 1 WHERE slot_id = %s",
        (slot_id,),
    )
    enqueue_slot_inventory(cursor, slot_id)
    cursor.execute(
        """
        INSERT INTO appointment_status_history (
            appointment_id, previous_status, new_status,
            changed_by_role, changed_by_id, reason
        )
        VALUES (%s, NULL, 'CONFIRMED', 'PATIENT', %s, 'Appointment booked')
        """,
        (appointment_id, patient_id),
    )
    outbox(
        cursor,
        "APPOINTMENT",
        appointment_id,
        "APPOINTMENT_CONFIRMED",
        {
            "appointment_id": appointment_id,
            "patient_id": patient_id,
            "doctor_id": slot["doctor_id"],
            "slot_id": slot_id,
            "booking_reference": booking_reference,
        },
    )

    return {
        "appointment_id": int(appointment_id),
        "booking_reference": booking_reference,
        "slot_id": int(slot_id),
        "doctor_id": int(slot["doctor_id"]),
        "start_at": slot["start_at"].isoformat(),
        "end_at": slot["end_at"].isoformat(),
        "status": "CONFIRMED",
        "consultation_fee": str(fee),
    }


def list_appointments(cursor, role, user_id, appointment_id=None):
    owner_column = "a.patient_id" if role == "PATIENT" else "s.doctor_id"
    owner_table = "a.patient_id" if role == "PATIENT" else "s.doctor_id"
    sql = f"""
        SELECT
            a.appointment_id, a.booking_reference, a.status,
            a.booked_at, a.cancelled_at, a.cancellation_reason,
            a.slot_id, s.doctor_id, s.start_at, s.end_at,
            s.status AS slot_status, s.consultation_type_id,
            a.consultation_fee_snapshot AS consultation_fee,
            d.full_name AS doctor_name,
            p.full_name AS patient_full_name,
            p.phone_number AS patient_phone_number,
            p.email AS patient_email,
            h.hospital_name, h.city
        FROM appointments a
        INNER JOIN patients p ON p.patient_id = a.patient_id
        INNER JOIN slots s ON s.slot_id = a.slot_id
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = s.consultation_type_id
        INNER JOIN doctors d ON d.doctor_id = s.doctor_id
        INNER JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE {owner_column} = %s
    """
    params = [user_id]
    if appointment_id is not None:
        sql += " AND a.appointment_id = %s"
        params.append(appointment_id)
    sql += " ORDER BY s.start_at DESC"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    if appointment_id is not None and not rows:
        raise ApiError(404, "Appointment not found")
    if role == "DOCTOR":
        for row in rows:
            row["patient"] = {
                "full_name": row.pop("patient_full_name", None),
                "phone_number": row.pop("patient_phone_number", None),
                "email": row.pop("patient_email", None),
            }
    else:
        # Patient-facing responses only return their own appointments; omit
        # contact fields that are not needed by the patient dashboard.
        for row in rows:
            row.pop("patient_full_name", None)
            row.pop("patient_phone_number", None)
            row.pop("patient_email", None)
    return rows


def refund_percentage(slot_start, now, by_doctor=False, no_show=False):
    if no_show:
        return Decimal("0")
    if by_doctor:
        return Decimal("100")
    hours = Decimal(str((slot_start - now).total_seconds())) / Decimal("3600")
    if hours > 48:
        return Decimal("100")
    if hours > 24:
        return Decimal("75")
    if hours >= 6:
        return Decimal("50")
    return Decimal("0")


def cancel_appointment(cursor, appointment_id, actor_id, actor_role, reason):
    now = now_ist()
    cursor.execute(
        """
        SELECT
            a.appointment_id, a.patient_id, a.slot_id, a.status,
            s.doctor_id, s.start_at,
            a.consultation_fee_snapshot AS consultation_fee
        FROM appointments a
        INNER JOIN slots s ON s.slot_id = a.slot_id
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = s.consultation_type_id
        WHERE a.appointment_id = %s
        FOR UPDATE
        """,
        (appointment_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ApiError(404, "Appointment not found")
    if actor_role == "PATIENT" and int(row["patient_id"]) != int(actor_id):
        raise ApiError(404, "Appointment not found")
    if actor_role == "DOCTOR" and int(row["doctor_id"]) != int(actor_id):
        raise ApiError(404, "Appointment not found")
    if row["status"] != "CONFIRMED":
        raise ApiError(409, "Only confirmed appointments can be cancelled")

    new_status = (
        "CANCELLED_BY_DOCTOR"
        if actor_role == "DOCTOR"
        else "CANCELLED_BY_PATIENT"
    )
    percent = refund_percentage(
        row["start_at"], now, by_doctor=(actor_role == "DOCTOR")
    )
    fee = Decimal(str(row["consultation_fee"]))
    refund_amount = (fee * percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    cursor.execute(
        """
        UPDATE appointments
        SET status = %s, cancelled_at = %s, cancellation_reason = %s
        WHERE appointment_id = %s AND status = 'CONFIRMED'
        """,
        (new_status, now, reason, appointment_id),
    )
    if cursor.rowcount != 1:
        raise ApiError(409, "Appointment was already changed")

    cursor.execute(
        "UPDATE slots SET booked_count = GREATEST(booked_count - 1, 0) WHERE slot_id = %s",
        (row["slot_id"],),
    )
    enqueue_slot_inventory(cursor, int(row["slot_id"]))
    cursor.execute(
        """
        INSERT INTO appointment_status_history (
            appointment_id, previous_status, new_status,
            changed_by_role, changed_by_id, reason
        )
        VALUES (%s, 'CONFIRMED', %s, %s, %s, %s)
        """,
        (appointment_id, new_status, actor_role, actor_id, reason),
    )

    if refund_amount > 0:
        cursor.execute(
            """
            INSERT INTO refund_ledger (
                appointment_id, refund_reference, amount, reason
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                appointment_id,
                "RF" + uuid.uuid4().hex[:18].upper(),
                refund_amount,
                f"{percent}% refund: {reason}",
            ),
        )

    outbox(
        cursor,
        "APPOINTMENT",
        appointment_id,
        "APPOINTMENT_CANCELLED",
        {
            "appointment_id": appointment_id,
            "patient_id": int(row["patient_id"]),
            "doctor_id": int(row["doctor_id"]),
            "slot_id": int(row["slot_id"]),
            "status": new_status,
            "refund_amount": str(refund_amount),
        },
    )
    return {
        "appointment_id": int(appointment_id),
        "status": new_status,
        "refund_percentage": str(percent),
        "refund_amount": str(refund_amount),
    }, int(row["slot_id"])


def cancel_doctor_slot(cursor, slot_id, doctor_id, reason):
    now = now_ist()
    cursor.execute(
        """
        SELECT slot_id, doctor_id, status, start_at
        FROM slots WHERE slot_id = %s FOR UPDATE
        """,
        (slot_id,),
    )
    slot = cursor.fetchone()
    if not slot or int(slot["doctor_id"]) != int(doctor_id):
        raise ApiError(404, "Slot not found")
    if slot["status"] not in ("OPEN", "BLOCKED"):
        raise ApiError(409, "Slot cannot be cancelled in its current state")

    cursor.execute(
        """
        SELECT
            a.appointment_id, a.patient_id, a.status,
            a.consultation_fee_snapshot AS consultation_fee
        FROM appointments a
        INNER JOIN slots s ON s.slot_id = a.slot_id
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = s.consultation_type_id
        WHERE a.slot_id = %s AND a.status = 'CONFIRMED'
        FOR UPDATE
        """,
        (slot_id,),
    )
    appointments = cursor.fetchall()

    cancelled = []
    for appointment in appointments:
        cursor.execute(
            """
            UPDATE appointments
            SET status = 'CANCELLED_BY_DOCTOR',
                cancelled_at = %s,
                cancellation_reason = %s
            WHERE appointment_id = %s AND status = 'CONFIRMED'
            """,
            (now, reason, appointment["appointment_id"]),
        )
        if cursor.rowcount != 1:
            raise ApiError(409, "An appointment changed during slot cancellation")

        fee = Decimal(str(appointment["consultation_fee"])).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if fee > 0:
            cursor.execute(
                """
                INSERT INTO refund_ledger (
                    appointment_id, refund_reference, amount, reason
                )
                VALUES (%s, %s, %s, 'Doctor cancelled slot - full refund')
                """,
                (
                    appointment["appointment_id"],
                    "RF" + uuid.uuid4().hex[:18].upper(),
                    fee,
                ),
            )

        cursor.execute(
            """
            INSERT INTO appointment_status_history (
                appointment_id, previous_status, new_status,
                changed_by_role, changed_by_id, reason
            )
            VALUES (%s, 'CONFIRMED', 'CANCELLED_BY_DOCTOR',
                    'DOCTOR', %s, %s)
            """,
            (appointment["appointment_id"], doctor_id, reason),
        )
        outbox(
            cursor,
            "APPOINTMENT",
            appointment["appointment_id"],
            "APPOINTMENT_CANCELLED",
            {
                "appointment_id": int(appointment["appointment_id"]),
                "patient_id": int(appointment["patient_id"]),
                "doctor_id": int(doctor_id),
                "slot_id": int(slot_id),
                "status": "CANCELLED_BY_DOCTOR",
                "refund_amount": str(fee),
            },
        )
        cancelled.append(int(appointment["appointment_id"]))

    cursor.execute(
        "UPDATE slots SET status = 'CANCELLED', booked_count = 0 WHERE slot_id = %s",
        (slot_id,),
    )
    enqueue_slot_inventory(cursor, int(slot_id))
    return cancelled


def handler(event, context):
    connection = None
    reserved_slot_id = None
    idempotency_key = None
    sql_committed = False

    try:
        user_id, role = get_identity(event)
        http = event.get("requestContext", {}).get("http", {})
        method = (http.get("method") or event.get("httpMethod") or "").upper()
        path = event.get("rawPath") or event.get("path") or ""
        path = path.rstrip("/") or "/"

        if method == "POST" and path == "/appointments":
            if role != "PATIENT":
                raise ApiError(403, "Patient role required")
            data = get_body(event)
            slot_id = parse_id(data.get("slot_id"), "slot_id")
            idempotency_key = get_header(event, "Idempotency-Key")
            if not idempotency_key or len(idempotency_key) > 128:
                raise ApiError(400, "A valid Idempotency-Key header is required")
            request_hash = hashlib.sha256(
                json.dumps(
                    {"patient_id": user_id, "slot_id": slot_id},
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            replay = claim_idempotency(idempotency_key, user_id, request_hash)
            if replay:
                return response(200, replay)

            # Reserve capacity in DynamoDB first. If the SQL transaction fails,
            # release the reservation as compensation.
            inventory_reserve(slot_id)
            reserved_slot_id = slot_id

            connection = get_connection()
            with connection.cursor() as cursor:
                result = create_appointment(cursor, int(user_id), slot_id)
            connection.commit()
            sql_committed = True

            complete_idempotency(idempotency_key, result)
            return response(201, result)

        if method == "GET" and path in (
            "/patients/me/appointments",
            "/doctors/me/appointments",
        ):
            expected = "PATIENT" if path.startswith("/patients/") else "DOCTOR"
            if role != expected:
                raise ApiError(403, f"{expected.title()} role required")
            connection = get_connection()
            with connection.cursor() as cursor:
                rows = list_appointments(cursor, role, int(user_id))
            connection.commit()
            return response(200, {"appointments": rows})

        if method == "GET" and path.startswith("/patients/me/appointments/"):
            if role != "PATIENT":
                raise ApiError(403, "Patient role required")
            appointment_id = parse_id(path.rsplit("/", 1)[-1], "appointmentId")
            connection = get_connection()
            with connection.cursor() as cursor:
                rows = list_appointments(
                    cursor, role, int(user_id), appointment_id
                )
            connection.commit()
            return response(200, {"appointment": rows[0]})

        if method == "POST" and path.startswith("/appointments/") and path.endswith("/cancel"):
            if role not in ("PATIENT", "DOCTOR"):
                raise ApiError(403, "Authenticated role required")
            appointment_id = parse_id(path.split("/")[-2], "appointmentId")
            data = get_body(event)
            reason = data.get("reason") or "Cancelled by user"
            if not isinstance(reason, str) or len(reason) > 500:
                raise ApiError(400, "reason must be a string of at most 500 characters")
            connection = get_connection()
            with connection.cursor() as cursor:
                result, slot_id = cancel_appointment(
                    cursor, appointment_id, int(user_id), role, reason
                )
            connection.commit()
            inventory_release(slot_id)
            return response(200, result)

        if method == "POST" and path.startswith("/doctors/me/slots/") and path.endswith("/cancel"):
            if role != "DOCTOR":
                raise ApiError(403, "Doctor role required")
            slot_id = parse_id(path.split("/")[-2], "slotId")
            data = get_body(event)
            reason = data.get("reason") or "Slot cancelled by doctor"
            if not isinstance(reason, str) or len(reason) > 500:
                raise ApiError(400, "reason must be a string of at most 500 characters")
            connection = get_connection()
            with connection.cursor() as cursor:
                cancelled = cancel_doctor_slot(
                    cursor, slot_id, int(user_id), reason
                )
            connection.commit()
            # Slot remains unavailable; only its status is changed in inventory.
            inventory_table.update_item(
                Key={"slotId": str(slot_id)},
                UpdateExpression=(
                    "SET #status = :cancelled, "
                    "confirmedCount = :zero, "
                    "activeHoldCount = :zero, "
                    "availableCount = :zero, "
                    "updatedAt = :now, "
                    "#version = if_not_exists(#version, :zero) + :one"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#version": "version",
                },
                ExpressionAttributeValues={
                    ":cancelled": "CANCELLED",
                    ":zero": 0,
                    ":one": 1,
                    ":now": datetime.now(IST).isoformat(),
                },
                ConditionExpression="attribute_exists(slotId)",
            )
            return response(200, {
                "slot_id": slot_id,
                "status": "CANCELLED",
                "appointments_cancelled": cancelled,
                "count": len(cancelled),
            })

        raise ApiError(404, "Route not found")

    except ApiError as exc:
        if connection:
            connection.rollback()
        if reserved_slot_id is not None and not sql_committed:
            try:
                inventory_release(reserved_slot_id)
            except Exception:
                logger.exception("Compensation failed for slot %s", reserved_slot_id)
        if idempotency_key and not sql_committed:
            fail_idempotency(idempotency_key)
        return response(exc.status, {"message": exc.message})

    except Exception:
        if connection:
            connection.rollback()
        if reserved_slot_id is not None and not sql_committed:
            try:
                inventory_release(reserved_slot_id)
            except Exception:
                logger.exception("Compensation failed for slot %s", reserved_slot_id)
        if idempotency_key and not sql_committed:
            fail_idempotency(idempotency_key)
        logger.exception("Appointment API request failed")
        return response(500, {"message": "Internal server error"})

    finally:
        if connection:
            connection.close()
