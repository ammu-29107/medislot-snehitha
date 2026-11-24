
import logging
import os
from pathlib import Path

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def execute_sql_file(cursor, sql_path):
    if not sql_path.is_file():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    sql_text = sql_path.read_text(encoding="utf-8")
    sql_lines = [
        line
        for line in sql_text.splitlines()
        if not line.lstrip().startswith("--")
        and not line.lstrip().startswith("#")
    ]
    sql_without_comments = "\n".join(sql_lines)
    statements = [
        statement.strip()
        for statement in sql_without_comments.split(";")
        if statement.strip()
    ]

    for statement in statements:
        cursor.execute(statement)

    return len(statements)


def ensure_patient_domain_schema(cursor):
    changes = 0

    columns = [
        ("patients", "city", "VARCHAR(100) NULL"),
        (
            "patients",
            "emergency_contact_name",
            "VARCHAR(150) NULL",
        ),
        (
            "patients",
            "emergency_contact_phone",
            "VARCHAR(20) NULL",
        ),
    ]

    for table_name, column_name, definition in columns:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = %s
            """,
            (table_name, column_name),
        )

        if cursor.fetchone()[0] == 0:
            cursor.execute(
                f"ALTER TABLE `{table_name}` "
                f"ADD COLUMN `{column_name}` {definition}"
            )
            changes += 1

    indexes = [
        (
            "doctors",
            "idx_doctors_status_name",
            "status, full_name",
        ),
        (
            "hospitals",
            "idx_hospitals_status_name",
            "status, hospital_name",
        ),
        (
            "slots",
            "idx_slots_doctor_status_start",
            "doctor_id, status, start_at",
        ),
    ]

    for table_name, index_name, columns_sql in indexes:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = %s
            """,
            (table_name, index_name),
        )

        if cursor.fetchone()[0] == 0:
            cursor.execute(
                f"ALTER TABLE `{table_name}` "
                f"ADD INDEX `{index_name}` ({columns_sql})"
            )
            changes += 1

    return changes


def ensure_appointments_schema(cursor):
    """
    Add and backfill the consultation fee snapshot.

    Existing appointments receive the consultation fee associated
    with their slot. New bookings will populate this column directly.
    """
    changes = 0

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'appointments'
          AND COLUMN_NAME = 'consultation_fee_snapshot'
        """
    )

    column_exists = cursor.fetchone()[0] > 0

    if not column_exists:
        cursor.execute(
            """
            ALTER TABLE appointments
            ADD COLUMN consultation_fee_snapshot DECIMAL(10,2) NULL
            AFTER slot_id
            """
        )
        changes += 1

    cursor.execute(
        """
        UPDATE appointments a
        INNER JOIN slots sl
            ON sl.slot_id = a.slot_id
        INNER JOIN consultation_types ct
            ON ct.consultation_type_id = sl.consultation_type_id
        SET a.consultation_fee_snapshot = ct.consultation_fee
        WHERE a.consultation_fee_snapshot IS NULL
        """
    )
    changes += max(cursor.rowcount, 0)

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM appointments
        WHERE consultation_fee_snapshot IS NULL
        """
    )

    missing_fees = cursor.fetchone()[0]

    if missing_fees:
        raise RuntimeError(
            f"{missing_fees} appointments have no consultation fee "
            "snapshot. The migration was stopped rather than "
            "assigning an incorrect refund amount."
        )

    cursor.execute(
        """
        ALTER TABLE appointments
        MODIFY COLUMN consultation_fee_snapshot DECIMAL(10,2) NOT NULL
        """
    )

    return changes

def ensure_slot_inventory_schema(cursor):
    """Add durable MySQL-backed slot inventory state and backfill events."""
    changes = 0

    columns = [
        (
            "active_hold_count",
            "SMALLINT UNSIGNED NOT NULL DEFAULT 0",
        ),
        (
            "inventory_version",
            "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        ),
    ]

    # Add missing columns to existing databases.
    for column_name, definition in columns:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'slots'
              AND COLUMN_NAME = %s
            """,
            (column_name,),
        )

        if cursor.fetchone()[0] == 0:
            cursor.execute(
                f"ALTER TABLE slots "
                f"ADD COLUMN `{column_name}` {definition}"
            )
            changes += 1

    # Add the inventory capacity constraint if it is missing.
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND TABLE_NAME = 'slots'
          AND CONSTRAINT_NAME = 'chk_slots_inventory_capacity'
          AND CONSTRAINT_TYPE = 'CHECK'
        """
    )

    if cursor.fetchone()[0] == 0:
        cursor.execute(
            """
            ALTER TABLE slots
            ADD CONSTRAINT chk_slots_inventory_capacity
            CHECK (
                booked_count + active_hold_count <= capacity
            )
            """
        )
        changes += 1

    # Give existing slots an initial source version.
    cursor.execute(
        """
        UPDATE slots
        SET inventory_version = 1
        WHERE inventory_version = 0
        """
    )
    changes += cursor.rowcount

    # Ensure every existing slot/version has a durable inventory event.
    # The unique key on (slot_id, slot_version) prevents duplicates.
    cursor.execute(
        """
        INSERT IGNORE INTO slot_inventory_outbox (
            slot_id,
            slot_version
        )
        SELECT
            slot_id,
            inventory_version
        FROM slots
        """
    )
    changes += cursor.rowcount

    return changes

def handler(event, context):
    """Apply the MediSlot schema and idempotent reference-data seed."""

    db_host = os.environ["DB_HOST"]
    db_port = int(os.environ.get("DB_PORT", "3306"))
    db_name = os.environ.get("DB_NAME", "medislot")
    db_user = os.environ["DB_USER"]
    password_parameter = os.environ["DB_PASSWORD_PARAMETER"]

    package_dir = Path(__file__).parent
    schema_path = package_dir / "schema.sql"
    seed_path = package_dir / "seed.sql"

    ssm = boto3.client("ssm")
    parameter = ssm.get_parameter(
        Name=password_parameter,
        WithDecryption=True,
    )
    db_password = parameter["Parameter"]["Value"]

    connection = pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
        charset="utf8mb4",
    )

    try:
        with connection.cursor() as cursor:
            schema_statements = execute_sql_file(cursor, schema_path)

            patient_migration_changes = (
                ensure_patient_domain_schema(cursor)
            )
            appointment_migration_changes = (
                ensure_appointments_schema(cursor)
            )

            slot_inventory_migration_changes = (
                ensure_slot_inventory_schema(cursor)
            )

            seed_statements = execute_sql_file(cursor, seed_path)

        migration_changes = (
            patient_migration_changes
            + appointment_migration_changes
            + slot_inventory_migration_changes
        )

        logger.info(
            "Schema and reference data applied successfully to '%s'. "
            "schema_statements=%s seed_statements=%s "
            "patient_migration_changes=%s "
            "appointment_migration_changes=%s "
            "slot_inventory_migration_changes=%s",
            db_name,
            schema_statements,
            seed_statements,
            patient_migration_changes,
            appointment_migration_changes,
            slot_inventory_migration_changes,
        )

        return {
            "status": "SUCCESS",
            "database": db_name,
            "schema_statements_executed": schema_statements,
            "seed_statements_executed": seed_statements,
            "patient_migration_changes": patient_migration_changes,
            "appointment_migration_changes": (
                appointment_migration_changes
            ),
            "slot_inventory_migration_changes": slot_inventory_migration_changes,
            "migration_changes": migration_changes,
        }
    finally:
        connection.close()
