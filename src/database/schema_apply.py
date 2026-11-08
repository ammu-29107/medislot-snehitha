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
            seed_statements = execute_sql_file(cursor, seed_path)

        logger.info(
            "Schema and reference data applied successfully to '%s'. "
            "schema_statements=%s seed_statements=%s",
            db_name,
            schema_statements,
            seed_statements,
        )

        return {
            "status": "SUCCESS",
            "database": db_name,
            "schema_statements_executed": schema_statements,
            "seed_statements_executed": seed_statements,
        }
    finally:
        connection.close()
