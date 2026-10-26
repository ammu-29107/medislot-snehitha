import os
import logging
from pathlib import Path

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """Apply the MediSlot SQL schema to the configured MySQL database."""

    db_host = os.environ["DB_HOST"]
    db_port = int(os.environ.get("DB_PORT", "3306"))
    db_name = os.environ.get("DB_NAME", "medislot")
    db_user = os.environ["DB_USER"]
    password_parameter = os.environ["DB_PASSWORD_PARAMETER"]

    # schema.sql is packaged alongside this handler.
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    ssm = boto3.client("ssm")
    response = ssm.get_parameter(
        Name=password_parameter,
        WithDecryption=True,
    )
    db_password = response["Parameter"]["Value"]

    schema_sql = schema_path.read_text(encoding="utf-8")

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
        # This schema contains ordinary CREATE statements, not stored
        # procedures or triggers. Execute each statement separately.
        statements = [
            statement.strip()
            for statement in schema_sql.split(";")
            if statement.strip()
        ]

        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

        logger.info(
            "Schema applied successfully to database '%s' on configured RDS.",
            db_name,
        )

        return {
            "status": "SUCCESS",
            "database": db_name,
            "statements_executed": len(statements),
        }
    finally:
        connection.close()