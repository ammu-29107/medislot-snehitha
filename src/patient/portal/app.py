import json
import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
import pymysql
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "medislot")
DB_USER = os.environ["DB_USER"]
DB_PASSWORD_PARAMETER = os.environ["DB_PASSWORD_PARAMETER"]
PRIVATE_FILES_BUCKET = os.environ.get("PRIVATE_FILES_BUCKET", "")
SESSIONS_TABLE = os.environ["SESSIONS_TABLE"]

ssm = boto3.client("ssm")
s3 = boto3.client("s3")

dynamodb = boto3.resource("dynamodb")
sessions_table = dynamodb.Table(SESSIONS_TABLE)


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def _connection():
    password = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER, WithDecryption=True  # gitleaks:allow
    )["Parameter"]["Value"]
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=password,
        database=DB_NAME, connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _patient_id(event):
    ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    if str(ctx.get("role", "")).upper() != "PATIENT":
        return None
    value = ctx.get("userId")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query(event):
    return event.get("queryStringParameters") or {}


def _profile(cursor, patient_id):
    cursor.execute(
        """
        SELECT patient_id, full_name, email, date_of_birth, phone_number,
               gender, city, emergency_contact_name, emergency_contact_phone,
               status, created_at, updated_at
        FROM patients WHERE patient_id=%s AND status='ACTIVE'
        LIMIT 1
        """,
        (patient_id,),
    )
    return cursor.fetchone()


def _get_profile(event):
    patient_id = _patient_id(event)
    if not patient_id:
        return response(401, {"message": "A valid patient session is required."})
    with _connection() as conn:
        with conn.cursor() as cur:
            profile = _profile(cur, patient_id)
    if not profile:
        return response(404, {"message": "Patient profile not found."})
    return response(200, {"profile": profile})


def _update_profile(event):
    patient_id = _patient_id(event)
    if not patient_id:
        return response(401, {"message": "A valid patient session is required."})
    payload = _body(event)
    if payload is None:
        return response(400, {"message": "Request body must be valid JSON."})

    full_name = str(payload.get("full_name", "")).strip()
    phone = str(payload.get("phone_number", "")).strip()
    gender = str(payload.get("gender", "")).strip().upper()
    city = str(payload.get("city", "")).strip() or None
    emergency_name = str(payload.get("emergency_contact_name", "")).strip() or None
    emergency_phone = str(payload.get("emergency_contact_phone", "")).strip() or None

    if not full_name or len(full_name) > 150:
        return response(400, {"message": "Full name is required and must be at most 150 characters."})
    if not phone or len(phone) > 20:
        return response(400, {"message": "A valid phone number is required."})
    if gender not in {"MALE", "FEMALE", "NON_BINARY", "PREFER_NOT_TO_SAY"}:
        return response(400, {"message": "Select a valid gender value."})
    if (emergency_name and not emergency_phone) or (emergency_phone and not emergency_name):
        return response(400, {"message": "Provide both emergency contact name and phone, or leave both blank."})
    if city and len(city) > 100:
        return response(400, {"message": "City must be at most 100 characters."})

    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE patients
                SET full_name=%s, phone_number=%s, gender=%s, city=%s,
                    emergency_contact_name=%s, emergency_contact_phone=%s
                WHERE patient_id=%s AND status='ACTIVE'
                """,
                (full_name, phone, gender, city, emergency_name, emergency_phone, patient_id),
            )
            if cur.rowcount == 0:
                existing = _profile(cur, patient_id)
                if not existing:
                    conn.rollback()
                    return response(404, {"message": "Patient profile not found."})
            conn.commit()
            profile = _profile(cur, patient_id)
    return response(200, {"message": "Profile updated successfully.", "profile": profile})



def _deactivate_profile(event):
    patient_id = _patient_id(event)

    if not patient_id:
        return response(
            401,
            {"message": "A valid patient session is required."}
        )

    authorizer_context = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("lambda", {})
    )

    session_id = authorizer_context.get("session_id")

    if not session_id:
        return response(
            401,
            {"message": "A valid patient session is required."}
        )

    # Slots are stored as local IST DATETIME values.
    now_ist = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).replace(tzinfo=None)

    try:
        with _connection() as conn:
            try:
                with conn.cursor() as cur:

                    # Lock the patient record while processing
                    # the deactivation request.
                    cur.execute(
                        """
                        SELECT patient_id
                        FROM patients
                        WHERE patient_id=%s
                          AND status='ACTIVE'
                        FOR UPDATE
                        """,
                        (patient_id,)
                    )

                    patient = cur.fetchone()

                    if not patient:
                        conn.rollback()
                        return response(
                            404,
                            {
                                "message": (
                                    "Active patient profile not found."
                                )
                            }
                        )

                    # Check for a confirmed appointment that
                    # has not yet ended.
                    cur.execute(
                        """
                        SELECT a.appointment_id
                        FROM appointments a
                        INNER JOIN slots sl
                            ON sl.slot_id = a.slot_id
                        WHERE a.patient_id=%s
                          AND a.status IN ('CONFIRMED', 'CHECKED_IN')
                          AND sl.end_at > %s
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (patient_id, now_ist)
                    )

                    active_appointment = cur.fetchone()

                    if active_appointment:
                        conn.rollback()

                        return response(
                            409,
                            {
                                "code": "ACTIVE_APPOINTMENTS_EXIST",
                                "message": (
                                    "Your account cannot be deactivated "
                                    "while you have active appointments. "
                                    "Please complete or cancel your "
                                    "appointments before trying again."
                                )
                            }
                        )

                    # Revoke the current authentication session.
                    # The refresh endpoint and authorizer both
                    # require the session status to be ACTIVE.
                    try:
                        sessions_table.update_item(
                            Key={
                                "session_id": session_id
                            },
                            UpdateExpression=(
                                "SET #status=:revoked, revokedAt=:now"
                            ),
                            ConditionExpression=(
                                "#status=:active "
                                "AND userId=:user_id "
                                "AND #role=:patient_role"
                            ),
                            ExpressionAttributeNames={
                                "#status": "status",
                                "#role": "role"
                            },
                            ExpressionAttributeValues={
                                ":revoked": "REVOKED",
                                ":active": "ACTIVE",
                                ":user_id": str(patient_id),
                                ":patient_role": "PATIENT",
                                ":now": int(datetime.now().timestamp())
                            }
                        )

                    except ClientError as exc:
                        error_code = exc.response.get(
                            "Error", {}
                        ).get("Code")

                        if error_code == "ConditionalCheckFailedException":
                            conn.rollback()

                            return response(
                                401,
                                {
                                    "message": (
                                        "Your session is invalid "
                                        "or has already expired."
                                    )
                                }
                            )

                        raise

                    # Deactivate the patient without deleting
                    # historical appointment records.
                    cur.execute(
                        """
                        UPDATE patients
                        SET status='DISABLED'
                        WHERE patient_id=%s
                          AND status='ACTIVE'
                        """,
                        (patient_id,)
                    )

                    if cur.rowcount != 1:
                        conn.rollback()

                        return response(
                            409,
                            {
                                "message": (
                                    "Unable to deactivate this account."
                                )
                            }
                        )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return response(
            200,
            {
                "message": (
                    "Patient account deactivated successfully."
                )
            }
        )

    except Exception:
        logger.exception(
            "Patient account deactivation failed"
        )
        raise


def _add_doctor_photo_url(doctor):
    doctor_id = doctor.get("doctor_id")
    photo_key = doctor.get("profile_photo_key")

    if (
        PRIVATE_FILES_BUCKET
        and photo_key
        and isinstance(photo_key, str)
        and photo_key.startswith(f"doctor-profiles/{doctor_id}/")
    ):
        try:
            doctor["profile_photo_url"] = s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": PRIVATE_FILES_BUCKET,
                    "Key": photo_key,
                },
                ExpiresIn=300,
            )
            doctor["profile_photo_url_expires_in"] = 300
        except ClientError:
            logger.exception("Unable to generate doctor profile photo URL")
            doctor["profile_photo_url"] = None
            doctor["profile_photo_url_expires_in"] = None
    else:
        doctor["profile_photo_url"] = None
        doctor["profile_photo_url_expires_in"] = None

    return doctor

def _doctor_search(event):
    q = _query(event)
    name = (q.get("name") or "").strip()
    city = (q.get("city") or "").strip()
    hospital = (q.get("hospital") or "").strip()
    speciality = (q.get("speciality") or "").strip()
    try:
        page = max(1, int(q.get("page", "1")))
        page_size = min(50, max(1, int(q.get("page_size", "10"))))
    except (TypeError, ValueError):
        return response(400, {"message": "page and page_size must be integers."})

    clauses = ["d.status='ACTIVE'", "h.status='ACTIVE'"]
    params = []
    if name:
        clauses.append("d.full_name LIKE %s")
        params.append(f"%{name}%")
    if city:
        clauses.append("h.city LIKE %s")
        params.append(f"%{city}%")
    if hospital:
        clauses.append("h.hospital_name LIKE %s")
        params.append(f"%{hospital}%")
    if speciality:
        clauses.append("s.speciality_name LIKE %s")
        params.append(f"%{speciality}%")
    where = " AND ".join(clauses)

    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(DISTINCT d.doctor_id) AS total
                FROM doctors d
                JOIN hospitals h ON h.hospital_id=d.hospital_id
                LEFT JOIN doctor_specialities ds ON ds.doctor_id=d.doctor_id
                LEFT JOIN specialities s ON s.speciality_id=ds.speciality_id
                WHERE {where}
                """,
                tuple(params),
            )
            total = int(cur.fetchone()["total"])
            total_pages = (total + page_size - 1) // page_size

            # Keep the requested page within the available result range.
            if total_pages == 0:
                page = 1
            else:
                page = min(page, total_pages)

            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT d.doctor_id, d.full_name, d.qualifications,
                       d.experience_years, d.bio, d.profile_photo_key,
                       h.hospital_name, h.city, h.state,
                       GROUP_CONCAT(DISTINCT s.speciality_name ORDER BY s.speciality_name SEPARATOR ', ') AS specialities
                FROM doctors d
                JOIN hospitals h ON h.hospital_id=d.hospital_id
                LEFT JOIN doctor_specialities ds ON ds.doctor_id=d.doctor_id
                LEFT JOIN specialities s ON s.speciality_id=ds.speciality_id
                WHERE {where}
                GROUP BY d.doctor_id, d.full_name, d.qualifications,
                         d.experience_years, d.bio, d.profile_photo_key,
                         h.hospital_name, h.city, h.state
                ORDER BY d.full_name ASC, d.doctor_id ASC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )
            doctors = cur.fetchall()
            doctors = [_add_doctor_photo_url(doctor) for doctor in doctors]
    return response(200, {
        "doctors": doctors, "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page * page_size < total,
        }
    })


def _doctor_detail(event):
    doctor_id = event.get("pathParameters", {}).get("doctorId")
    try:
        doctor_id = int(doctor_id)
    except (TypeError, ValueError):
        return response(400, {"message": "A valid doctor ID is required."})
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.doctor_id, d.full_name, d.qualifications,
                       d.experience_years, d.bio, d.profile_photo_key,
                       h.hospital_name, h.address_line, h.city, h.state,
                       h.postal_code, h.contact_number
                FROM doctors d JOIN hospitals h ON h.hospital_id=d.hospital_id
                WHERE d.doctor_id=%s AND d.status='ACTIVE' AND h.status='ACTIVE'
                LIMIT 1
                """, (doctor_id,)
            )
            doctor = cur.fetchone()
            if not doctor:
                return response(404, {"message": "Doctor not found."})
            cur.execute(
                """
                SELECT s.speciality_id, s.speciality_name
                FROM doctor_specialities ds
                JOIN specialities s ON s.speciality_id=ds.speciality_id
                WHERE ds.doctor_id=%s AND s.status='ACTIVE'
                ORDER BY s.speciality_name
                """, (doctor_id,)
            )
            doctor["specialities"] = cur.fetchall()
            cur.execute(
                """
                SELECT ct.consultation_type_id, c.type_name,
                       ct.description, c.duration_minutes,
                       ct.capacity, ct.consultation_fee
                FROM consultation_types ct
                JOIN consultation_type_catalogue c ON c.catalogue_id=ct.catalogue_id
                WHERE ct.doctor_id=%s AND ct.status='ACTIVE'
                ORDER BY c.type_name
                """, (doctor_id,)
            )
            doctor["consultation_types"] = cur.fetchall()
            doctor = _add_doctor_photo_url(doctor)
    return response(200, {"doctor": doctor})


def _doctor_slots(event):
    doctor_id = event.get("pathParameters", {}).get("doctorId")
    try:
        doctor_id = int(doctor_id)
    except (TypeError, ValueError):
        return response(400, {"message": "A valid doctor ID is required."})
    q = _query(event)
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    today_ist = now_ist.date()
    start_date = q.get("start_date") or today_ist.isoformat()
    end_date = q.get("end_date") or (today_ist + timedelta(days=30)).isoformat()
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return response(400, {"message": "Dates must use YYYY-MM-DD format."})

    requested_end_date = (end - timedelta(days=1)).date()

    # For today's availability, only return slots that have not started yet.
    # Future dates continue to use midnight as their inclusive lower bound.
    if start.date() == today_ist:
        start = now_ist

    if start.date() < today_ist or requested_end_date < today_ist:
        return response(
            400,
            {"message": "Past dates are not allowed. Select today or a future date."}
        )
    if end <= start or (end - start).days > 92:
        return response(400, {"message": "Choose a date range of 1 to 92 days."})
    try:
        consultation_type_id = q.get("consultation_type_id")
        if consultation_type_id:
            consultation_type_id = int(consultation_type_id)
    except ValueError:
        return response(400, {"message": "consultation_type_id must be an integer."})

    sql = """
        SELECT sl.slot_id, sl.doctor_id, sl.consultation_type_id,
               sl.start_at, sl.end_at, sl.capacity, sl.booked_count,
               (sl.capacity - sl.booked_count) AS remaining_seats,
               ct.consultation_fee, cat.type_name AS consultation_type,
               cat.duration_minutes
        FROM slots sl
        JOIN doctors d ON d.doctor_id=sl.doctor_id
        JOIN consultation_types ct ON ct.consultation_type_id=sl.consultation_type_id
        JOIN consultation_type_catalogue cat ON cat.catalogue_id=ct.catalogue_id
        WHERE sl.doctor_id=%s AND d.status='ACTIVE'
          AND sl.status='OPEN' AND sl.start_at >= %s AND sl.start_at < %s
          AND sl.booked_count < sl.capacity AND ct.status='ACTIVE'
    """
    args = [doctor_id, start, end]
    if consultation_type_id:
        sql += " AND sl.consultation_type_id=%s"
        args.append(consultation_type_id)
    sql += " ORDER BY sl.start_at ASC LIMIT 500"
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(args))
            slots = cur.fetchall()
    return response(200, {
        "doctor_id": doctor_id, "start_date": start_date, "end_date": end_date,
        "slots": slots,
    })


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    route = event.get("routeKey", "")
    try:
        if route == "GET /patient/profile" or (method == "GET" and event.get("rawPath") == "/patient/profile"):
            return _get_profile(event)
        if route == "PUT /patient/profile" or (method == "PUT" and event.get("rawPath") == "/patient/profile"):
            return _update_profile(event)
        if route == "DELETE /patient/profile" or (method == "DELETE" and event.get("rawPath") == "/patient/profile"):
            return _deactivate_profile(event)
        if route == "GET /doctors" or (method == "GET" and event.get("rawPath") == "/doctors"):
            return _doctor_search(event)
        if route == "GET /doctors/{doctorId}" or "/doctors/" in event.get("rawPath", "") and not event.get("rawPath", "").endswith("/slots"):
            return _doctor_detail(event)
        if route == "GET /doctors/{doctorId}/slots" or event.get("rawPath", "").endswith("/slots"):
            return _doctor_slots(event)
        return response(404, {"message": "Route not found."})
    except (ClientError, pymysql.MySQLError):
        logger.exception("Patient portal database operation failed")
        return response(500, {"message": "Unable to complete the patient portal request."})
    except Exception:
        logger.exception("Unexpected patient portal error")
        return response(500, {"message": "An unexpected error occurred."})
