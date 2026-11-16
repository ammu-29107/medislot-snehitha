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
ssm = boto3.client("ssm")


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def _connection():
    password = ssm.get_parameter(
        Name=DB_PASSWORD_PARAMETER, WithDecryption=True
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
        return response(401, {"message": "A valid patient session is required."})
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE patients SET status='DISABLED' WHERE patient_id=%s AND status='ACTIVE'",
                (patient_id,),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return response(404, {"message": "Active patient profile not found."})
            conn.commit()
    return response(200, {"message": "Patient profile deactivated."})


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
    offset = (page - 1) * page_size

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
            cur.execute(
                f"""
                SELECT d.doctor_id, d.full_name, d.qualifications,
                       d.experience_years, d.bio,
                       h.hospital_name, h.city, h.state,
                       GROUP_CONCAT(DISTINCT s.speciality_name ORDER BY s.speciality_name SEPARATOR ', ') AS specialities
                FROM doctors d
                JOIN hospitals h ON h.hospital_id=d.hospital_id
                LEFT JOIN doctor_specialities ds ON ds.doctor_id=d.doctor_id
                LEFT JOIN specialities s ON s.speciality_id=ds.speciality_id
                WHERE {where}
                GROUP BY d.doctor_id, d.full_name, d.qualifications,
                         d.experience_years, d.bio,
                         h.hospital_name, h.city, h.state
                ORDER BY d.full_name ASC, d.doctor_id ASC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )
            doctors = cur.fetchall()
    return response(200, {
        "doctors": doctors, "pagination": {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": (total + page_size - 1) // page_size,
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
                       d.experience_years, d.bio,
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
    return response(200, {"doctor": doctor})


def _doctor_slots(event):
    doctor_id = event.get("pathParameters", {}).get("doctorId")
    try:
        doctor_id = int(doctor_id)
    except (TypeError, ValueError):
        return response(400, {"message": "A valid doctor ID is required."})
    q = _query(event)
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start_date = q.get("start_date") or today_ist.isoformat()
    end_date = q.get("end_date") or (today_ist + timedelta(days=30)).isoformat()
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        return response(400, {"message": "Dates must use YYYY-MM-DD format."})
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
