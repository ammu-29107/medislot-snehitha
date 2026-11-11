
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ssm = boto3.client("ssm")

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.getenv("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]

IST = ZoneInfo("Asia/Kolkata")


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
        read_timeout=30,
        write_timeout=30,
        autocommit=False,
    )


def handler(event, context):
    connection = None

    try:
        # MySQL DATETIME values are stored as IST local time.
        now_ist = datetime.now(IST).replace(tzinfo=None)

        connection = get_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE slots s
                SET s.status = 'COMPLETED'
                WHERE s.status = 'OPEN'
                  AND s.end_at <= %s
                """,
                (now_ist,),
            )

            completed_count = cursor.rowcount

        connection.commit()

        logger.info(
            "Slot maintenance completed. Updated %s slots.",
            completed_count,
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Slot maintenance completed",
                "slots_marked_completed": completed_count,
                "processed_at": now_ist.isoformat(),
                "timezone": "Asia/Kolkata",
            }),
        }

    except Exception:
        if connection:
            connection.rollback()

        logger.exception("Slot maintenance failed")
        raise

    finally:
        if connection:
            connection.close()
