import os
import logging
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_session import Session


IST = ZoneInfo("Asia/Kolkata")
app = Flask(__name__)

secret_key = os.environ.get("FLASK_SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Set it before starting the dashboard."
    )

API_BASE_URL = os.environ.get("MEDISLOT_API_BASE_URL", "").rstrip("/")
if not API_BASE_URL:
    raise RuntimeError(
        "MEDISLOT_API_BASE_URL is not set. Set it to the MediSlot HTTP API endpoint."
    )

session_directory = Path(
    os.environ.get(
        "FLASK_SESSION_DIR",
        str(Path(app.root_path) / "flask_session"),
    )
)
session_directory.mkdir(parents=True, exist_ok=True)

app.config.update(
    SECRET_KEY=secret_key,
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR=str(session_directory),
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_FILE_THRESHOLD=500,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
    == "true",
)
Session(app)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _decode_response(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"message": "The MediSlot API returned a non-JSON response."}


def _send_api_request(method, path, *, token=None, correlation_id=None, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Accept", "application/json")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "json" in kwargs:
        headers.setdefault("Content-Type", "application/json")
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id

    started = perf_counter()
    response = requests.request(
        method,
        f"{API_BASE_URL}{path}",
        headers=headers,
        timeout=25,
        **kwargs,
    )
    elapsed_ms = round((perf_counter() - started) * 1000)
    api_request_id = (
        response.headers.get("apigw-requestid")
        or response.headers.get("x-amzn-requestid")
        or "not-returned"
    )
    app.logger.info(
        "MediSlot API %s %s -> %s (%sms, correlation_id=%s, api_request_id=%s)",
        method.upper(),
        path,
        response.status_code,
        elapsed_ms,
        correlation_id or "not-set",
        api_request_id,
    )
    return response


def call_doctor_api(method, path, *, retry_after_refresh=True, **kwargs):
    """Call the authenticated API and refresh the short-lived access JWT once."""
    access_token = session.get("access_token")
    if not access_token:
        return {"message": "Your dashboard session has expired. Please sign in again."}, 401

    correlation_id = uuid.uuid4().hex

    try:
        response = _send_api_request(
            method,
            path,
            token=access_token,
            correlation_id=correlation_id,
            **kwargs,
        )

        if response.status_code in (401, 403) and retry_after_refresh:
            refresh_token = session.get("refresh_token")
            if not refresh_token:
                session.clear()
                return {"message": "Your session has expired. Please sign in again."}, 401

            refresh_response = _send_api_request(
                "POST",
                "/refresh",
                json={"refreshToken": refresh_token},
                correlation_id=correlation_id,
            )
            refresh_data = _decode_response(refresh_response)

            if refresh_response.status_code != 200 or not refresh_data.get("accessToken"):
                session.clear()
                return {
                    "message": refresh_data.get(
                        "message", "Your session has expired. Please sign in again."
                    )
                }, 401

            # The MediSlot refresh endpoint deliberately reuses the existing
            # seven-day refresh token and returns only a new access token.
            session["access_token"] = refresh_data["accessToken"]
            response = _send_api_request(
                method,
                path,
                token=session["access_token"],
                correlation_id=correlation_id,
                **kwargs,
            )

        return _decode_response(response), response.status_code

    except requests.RequestException:
        app.logger.exception(
            "MediSlot API connection failed: %s %s (correlation_id=%s)",
            method.upper(),
            path,
            correlation_id,
        )
        return {"message": "Unable to connect to the MediSlot API. Please try again."}, 503


def doctor_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "DOCTOR":
            return redirect(url_for("login", role="doctor"))
        return view(*args, **kwargs)

    return wrapped


def patient_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "PATIENT":
            return redirect(url_for("login", role="patient"))
        return view(*args, **kwargs)

    return wrapped


def get_active_consultation_types():
    data, status = call_doctor_api("GET", "/doctor/consultation-types")
    if status != 200:
        return [], status, data
    types = data.get("consultation_types", [])
    return [item for item in types if item.get("status") == "ACTIVE"], status, data


@app.template_filter("ist_datetime")
def ist_datetime(value, fmt="%d %b %Y, %I:%M %p"):
    if not value:
        return "—"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(IST)
        return parsed.strftime(fmt)
    except (TypeError, ValueError):
        return str(value)


@app.template_filter("ist_time")
def ist_time(value):
    if not value:
        return "—"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(IST)
        return parsed.strftime("%I:%M %p")
    except (TypeError, ValueError):
        return str(value)


@app.route("/")
def index():
    if session.get("access_token"):
        if session.get("role") == "DOCTOR":
            return redirect(url_for("dashboard"))
        if session.get("role") == "PATIENT":
            return redirect(url_for("patient_dashboard"))
    return render_template("index.html")


def _normalise_role(role):
    role = (role or "").strip().lower()
    return role if role in {"doctor", "patient"} else None


def _load_registration_options():
    try:
        response = _send_api_request("GET", "/registration/options")
        data = _decode_response(response)
        if response.status_code == 200:
            return data, None
        return {"hospitals": [], "specialities": []}, data.get(
            "message", "Unable to load hospital and speciality options."
        )
    except requests.RequestException:
        app.logger.exception("Unable to load doctor registration options")
        return {"hospitals": [], "specialities": []}, (
            "Unable to load hospital and speciality options. Please try again."
        )


def _render_registration(role, form_data=None):
    options = {"hospitals": [], "specialities": []}
    options_error = None
    if role == "doctor":
        options, options_error = _load_registration_options()
    return render_template(
        "register.html",
        role=role,
        hospitals=options.get("hospitals", []),
        specialities=options.get("specialities", []),
        options_error=options_error,
        form_data=form_data or {},
        today=date.today().isoformat(),
    )


@app.route("/login", defaults={"role": None}, methods=["GET", "POST"])
@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    role = _normalise_role(role)
    if role is None:
        return redirect(url_for("index"))

    if session.get("access_token"):
        if session.get("role") == "DOCTOR":
            return redirect(url_for("dashboard"))
        if session.get("role") == "PATIENT":
            return redirect(url_for("patient_dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter your email address and password.", "error")
            return render_template("login.html", role=role)

        try:
            response = _send_api_request(
                "POST",
                f"/login/{role}",
                json={"email": email, "password": password},
            )
            data = _decode_response(response)

            if (
                response.status_code == 200
                and data.get("accessToken")
                and data.get("refreshToken")
            ):
                session.clear()
                session["access_token"] = data["accessToken"]
                session["refresh_token"] = data["refreshToken"]
                session["role"] = role.upper()
                flash("Signed in successfully.", "success")
                if role == "doctor":
                    return redirect(url_for("dashboard"))
                return redirect(url_for("patient_dashboard"))

            flash(data.get("message", "Login failed. Check your credentials."), "error")

        except requests.RequestException:
            app.logger.exception("Unable to reach the MediSlot login endpoint")
            flash("Unable to connect to MediSlot. Please try again.", "error")

    return render_template("login.html", role=role)


@app.route("/register/<role>", methods=["GET", "POST"])
def register(role):
    role = _normalise_role(role)
    if role is None:
        return redirect(url_for("index"))

    if session.get("access_token"):
        if session.get("role") == "DOCTOR":
            return redirect(url_for("dashboard"))
        if session.get("role") == "PATIENT":
            return redirect(url_for("patient_dashboard"))

    if request.method == "GET":
        return _render_registration(role)

    full_name = request.form.get("fullName", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not full_name or not email or not password:
        flash("Please complete all required fields.", "error")
        return _render_registration(role, request.form)

    payload = {
        "fullName": full_name,
        "email": email,
        "password": password,
    }

    if role == "doctor":
        hospital_id = request.form.get("hospitalId", "").strip()
        speciality_values = request.form.getlist("specialityIds")
        license_number = request.form.get("licenseNumber", "").strip()
        qualifications = request.form.get("qualifications", "").strip()
        experience_years = request.form.get("experienceYears", "").strip()

        try:
            payload["hospitalId"] = int(hospital_id)
            payload["specialityIds"] = [int(value) for value in speciality_values]
            if experience_years:
                payload["experienceYears"] = int(experience_years)
        except (TypeError, ValueError):
            flash("Select a valid hospital and at least one speciality.", "error")
            return _render_registration(role, request.form)

        payload["licenseNumber"] = license_number
        if qualifications:
            payload["qualifications"] = qualifications

        if not license_number or not payload["specialityIds"]:
            flash("License number and at least one speciality are required.", "error")
            return _render_registration(role, request.form)

    else:
        payload.update({
            "dateOfBirth": request.form.get("dateOfBirth", "").strip(),
            "phoneNumber": request.form.get("phoneNumber", "").strip(),
            "gender": request.form.get("gender", "").strip(),
        })
        if not all(payload[key] for key in ("dateOfBirth", "phoneNumber", "gender")):
            flash("Please complete your date of birth, phone number and gender.", "error")
            return _render_registration(role, request.form)

    try:
        response = _send_api_request("POST", f"/register/{role}", json=payload)
        data = _decode_response(response)

        if response.status_code == 201:
            flash("Your account has been created. Please sign in.", "success")
            return redirect(url_for("login", role=role))

        flash(data.get("message", "Registration failed. Please review your details."), "error")

    except requests.RequestException:
        app.logger.exception("Unable to reach the MediSlot registration endpoint")
        flash("Unable to connect to MediSlot. Please try again.", "error")

    return _render_registration(role, request.form)


@app.route("/patient/dashboard")
@patient_required
def patient_dashboard():
    profile_data, profile_status = call_doctor_api("GET", "/profile")
    if profile_status == 200:
        profile = profile_data.get("profile", {})
    else:
        profile = {}
        flash(
            profile_data.get("message", "Unable to load your patient profile."),
            "error",
        )

    return render_template("patient_dashboard.html", profile=profile)


@app.route("/dashboard")
@doctor_required
def dashboard():
    now = datetime.now(IST)
    today = now.date()
    range_end = today + timedelta(days=7)

    profile, profile_status = call_doctor_api("GET", "/doctor/profile")
    if profile_status != 200:
        flash(profile.get("message", "Unable to load your doctor profile."), "error")
        profile = {}

    slots_data, slots_status = call_doctor_api(
        "GET",
        "/doctor/slots",
        params={
            "start_at": datetime.combine(today, datetime.min.time()).isoformat(timespec="minutes"),
            "end_at": datetime.combine(range_end, datetime.min.time()).isoformat(timespec="minutes"),
        },
    )
    slots = slots_data.get("slots", []) if slots_status == 200 else []
    if slots_status != 200:
        app.logger.warning("Dashboard upcoming slots unavailable: %s", slots_data.get("message"))

    types, types_status, types_data = get_active_consultation_types()
    if types_status != 200:
        types = []
        app.logger.warning("Dashboard consultation types unavailable: %s", types_data.get("message"))
    type_names = {int(item["consultation_type_id"]): item.get("type_name", "Consultation") for item in types}

    patterns_data, patterns_status = call_doctor_api(
        "GET", "/doctor/availability-patterns", params={"status": "ACTIVE"}
    )
    patterns = patterns_data.get("availability_patterns", []) if patterns_status == 200 else []

    today_slots = [
        item for item in slots
        if str(item.get("start_at", ""))[:10] == today.isoformat()
    ]
    booked_count = sum(int(item.get("booked_count", 0)) for item in slots)

    upcoming_slots = []
    for item in slots:
        item = dict(item)
        try:
            item["consultation_type_name"] = type_names.get(
                int(item.get("consultation_type_id")), "Consultation"
            )
        except (TypeError, ValueError):
            item["consultation_type_name"] = "Consultation"
        upcoming_slots.append(item)
    upcoming_slots = upcoming_slots[:6]

    return render_template(
        "dashboard.html",
        profile=profile,
        today=today.isoformat(),
        today_slots_count=len(today_slots),
        booked_count=booked_count,
        consultation_type_count=len(types),
        availability_count=len(patterns),
        upcoming_slots=upcoming_slots,
        slots_loaded=slots_status == 200,
        active_page="dashboard",
    )


@app.route("/profile")
@doctor_required
def profile():
    data, status = call_doctor_api("GET", "/doctor/profile")
    if status != 200:
        flash(data.get("message", "Unable to load profile."), "error")
        return redirect(url_for("dashboard"))
    return render_template("profile.html", profile=data, active_page="profile")


@app.route("/profile/edit", methods=["GET", "POST"])
@doctor_required
def edit_profile():
    if request.method == "POST":
        speciality_text = request.form.get("speciality_ids", "").strip()
        try:
            speciality_ids = [int(value.strip()) for value in speciality_text.split(",") if value.strip()]
        except ValueError:
            flash("Enter valid speciality IDs separated by commas.", "error")
            return redirect(url_for("edit_profile"))

        try:
            experience_years = int(request.form.get("experience_years", "0"))
        except (TypeError, ValueError):
            flash("Enter a valid number of experience years.", "error")
            return redirect(url_for("edit_profile"))

        payload = {
            "full_name": request.form.get("full_name", "").strip(),
            "qualifications": request.form.get("qualifications", "").strip(),
            "experience_years": experience_years,
            "bio": request.form.get("bio", "").strip(),
            "hospital_id": request.form.get("hospital_id", "").strip(),
            "speciality_ids": speciality_ids,
        }

        uploaded_file = request.files.get("profile_photo")
        if uploaded_file and uploaded_file.filename:
            allowed_types = {"image/jpeg", "image/png", "image/webp"}
            if uploaded_file.mimetype not in allowed_types:
                flash("Only JPEG, PNG, and WEBP images are allowed.", "error")
                return redirect(url_for("edit_profile"))

            photo_bytes = uploaded_file.read()
            if not photo_bytes:
                flash("The selected photo is empty.", "error")
                return redirect(url_for("edit_profile"))
            if len(photo_bytes) > 5 * 1024 * 1024:
                flash("Profile photo must be 5 MB or smaller.", "error")
                return redirect(url_for("edit_profile"))

            upload_info, upload_status = call_doctor_api(
                "POST",
                "/doctor/profile/photo-upload-url",
                json={"content_type": uploaded_file.mimetype},
            )
            if upload_status != 200 or not upload_info.get("upload_url") or not upload_info.get("photo_key"):
                flash(upload_info.get("message", "Could not generate a photo upload URL."), "error")
                return redirect(url_for("edit_profile"))

            try:
                upload_response = requests.put(
                    upload_info["upload_url"],
                    data=photo_bytes,
                    headers={"Content-Type": upload_info["content_type"]},
                    timeout=30,
                )
                upload_response.raise_for_status()
            except requests.RequestException:
                app.logger.exception("Profile photo upload to S3 failed")
                flash("Photo upload failed. Please try again.", "error")
                return redirect(url_for("edit_profile"))

            payload["profile_photo_key"] = upload_info["photo_key"]

        data, status = call_doctor_api("PUT", "/doctor/profile", json=payload)
        if status == 200:
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))
        flash(data.get("message", "Unable to update profile."), "error")

    data, status = call_doctor_api("GET", "/doctor/profile")
    if status != 200:
        flash(data.get("message", "Unable to load profile."), "error")
        return redirect(url_for("profile"))
    return render_template("profile_edit.html", profile=data, active_page="profile")


@app.route("/consultation-types", methods=["GET", "POST"])
@doctor_required
def consultation_types():
    if request.method == "POST":
        payload = {
            "type_name": request.form.get("type_name", "").strip(),
            "description": request.form.get("description", "").strip(),
            "duration_minutes": request.form.get("duration_minutes"),
            "capacity": request.form.get("capacity", 1),
            "consultation_fee": request.form.get("consultation_fee", 0),
        }
        data, status = call_doctor_api("POST", "/doctor/consultation-types", json=payload)
        if status == 201:
            flash("Consultation type created successfully.", "success")
            return redirect(url_for("consultation_types"))
        flash(data.get("message", "Unable to create consultation type."), "error")

    data, status = call_doctor_api("GET", "/doctor/consultation-types")
    if status != 200:
        flash(data.get("message", "Unable to load consultation types."), "error")
        types = []
    else:
        types = data.get("consultation_types", [])
    return render_template(
        "consultation_types.html",
        consultation_types=types,
        active_page="consultation_types",
    )


@app.route("/consultation-types/<int:type_id>/update", methods=["POST"])
@doctor_required
def update_consultation_type(type_id):
    payload = {
        "type_name": request.form.get("type_name", "").strip(),
        "description": request.form.get("description", "").strip(),
        "duration_minutes": request.form.get("duration_minutes"),
        "capacity": request.form.get("capacity", 1),
        "consultation_fee": request.form.get("consultation_fee", 0),
    }
    data, status = call_doctor_api(
        "PUT", f"/doctor/consultation-types/{type_id}", json=payload
    )
    if status == 200:
        flash("Consultation type updated.", "success")
    else:
        flash(data.get("message", "Unable to update consultation type."), "error")
    return redirect(url_for("consultation_types"))


@app.route("/consultation-types/<int:type_id>/disable", methods=["POST"])
@doctor_required
def disable_consultation_type(type_id):
    data, status = call_doctor_api("DELETE", f"/doctor/consultation-types/{type_id}")
    if status == 200:
        flash("Consultation type disabled.", "success")
    else:
        flash(data.get("message", "Unable to disable consultation type."), "error")
    return redirect(url_for("consultation_types"))


@app.route("/availability", methods=["GET", "POST"])
@doctor_required
def availability():
    if request.method == "POST":
        try:
            payload = {
                "consultation_type_id": int(request.form.get("consultation_type_id", "")),
                "day_of_week": int(request.form.get("day_of_week", "")),
                "start_time": request.form.get("start_time", "").strip(),
                "end_time": request.form.get("end_time", "").strip(),
                "status": "ACTIVE",
            }
        except (TypeError, ValueError):
            flash("Select a valid consultation type and day.", "error")
            return redirect(url_for("availability"))

        data, status = call_doctor_api(
            "POST", "/doctor/availability-patterns", json=payload
        )
        if status == 201:
            flash("Weekly availability pattern created.", "success")
            return redirect(url_for("availability"))
        flash(data.get("message", "Unable to create availability pattern."), "error")

    patterns_data, patterns_status = call_doctor_api(
        "GET", "/doctor/availability-patterns"
    )
    all_types_data, types_status = call_doctor_api(
        "GET", "/doctor/consultation-types"
    )

    patterns_loaded = patterns_status == 200
    types_loaded = types_status == 200
    patterns = patterns_data.get("availability_patterns", []) if patterns_loaded else []
    all_types = all_types_data.get("consultation_types", []) if types_loaded else []
    type_map = {int(item["consultation_type_id"]): item for item in all_types}
    active_types = [item for item in all_types if item.get("status") == "ACTIVE"]

    for pattern in patterns:
        consultation = type_map.get(int(pattern.get("consultation_type_id", 0)))
        pattern["consultation_type_name"] = (
            consultation.get("type_name") if consultation else "Unavailable consultation type"
        )
        pattern["consultation_type_active"] = bool(
            consultation and consultation.get("status") == "ACTIVE"
        )
        pattern["consultation_type_status"] = consultation.get("status") if consultation else "UNKNOWN"

    return render_template(
        "availability.html",
        availability_patterns=patterns,
        consultation_types=active_types,
        availability_patterns_loaded=patterns_loaded,
        consultation_types_loaded=types_loaded,
        active_page="availability",
    )


@app.route("/availability/<int:pattern_id>/update", methods=["POST"])
@doctor_required
def update_availability(pattern_id):
    try:
        payload = {
            "consultation_type_id": int(request.form.get("consultation_type_id", "")),
            "day_of_week": int(request.form.get("day_of_week", "")),
            "start_time": request.form.get("start_time", "").strip(),
            "end_time": request.form.get("end_time", "").strip(),
            "status": request.form.get("status", "ACTIVE"),
        }
    except (TypeError, ValueError):
        flash("Invalid availability details.", "error")
        return redirect(url_for("availability"))

    data, status = call_doctor_api(
        "PUT", f"/doctor/availability-patterns/{pattern_id}", json=payload
    )
    if status == 200:
        flash("Availability pattern updated.", "success")
    else:
        flash(data.get("message", "Unable to update availability pattern."), "error")
    return redirect(url_for("availability"))


@app.route("/availability/<int:pattern_id>/deactivate", methods=["POST"])
@doctor_required
def deactivate_availability(pattern_id):
    data, status = call_doctor_api(
        "DELETE", f"/doctor/availability-patterns/{pattern_id}"
    )
    if status == 200:
        flash("Availability pattern deactivated.", "success")
    else:
        flash(data.get("message", "Unable to deactivate availability pattern."), "error")
    return redirect(url_for("availability"))


@app.route("/schedule", methods=["GET", "POST"])
@doctor_required
def schedule():
    """Render a day, week, or month calendar using the live doctor day-view API."""
    if request.method == "POST":
        selected_date = (
            request.form.get("schedule_date")
            or datetime.now(IST).date().isoformat()
        )
        selected_view = request.form.get("calendar_view", "day")
        if selected_view not in {"day", "week", "month"}:
            selected_view = "day"

        try:
            consultation_type_id = int(request.form.get("consultation_type_id", ""))
            start_at = datetime.fromisoformat(request.form.get("start_at", "").strip())
            if start_at.tzinfo:
                start_at = start_at.astimezone(IST).replace(tzinfo=None)
            selected_date = start_at.date().isoformat()
            payload = {
                "consultation_type_id": consultation_type_id,
                "start_at": start_at.isoformat(timespec="minutes"),
            }
        except (TypeError, ValueError):
            flash("Choose a consultation type and a valid start date/time.", "error")
            return redirect(url_for(
                "schedule", date=selected_date, view=selected_view
            ))

        data, status = call_doctor_api("POST", "/doctor/slots", json=payload)
        if status == 201:
            flash(
                f"Slot #{data.get('slot_id')} created. It ends at {data.get('end_at')}.",
                "success",
            )
            return redirect(url_for(
                "schedule", date=selected_date, view=selected_view
            ))
        flash(data.get("message", "Unable to create the slot."), "error")

    selected_date_text = (
        request.args.get("date") or datetime.now(IST).date().isoformat()
    )
    try:
        selected_day = date.fromisoformat(selected_date_text)
    except (TypeError, ValueError):
        selected_day = datetime.now(IST).date()
        selected_date_text = selected_day.isoformat()
        flash("Invalid date selected. Showing today instead.", "error")

    view = (request.args.get("view") or "week").lower()
    if view not in {"day", "week", "month"}:
        view = "week"

    # Calculate the exact inclusive date range to display.
    if view == "day":
        range_start = selected_day
        range_end = selected_day
        display_title = selected_day.strftime("%A, %d %B %Y")
    elif view == "week":
        range_start = selected_day - timedelta(days=selected_day.weekday())
        range_end = range_start + timedelta(days=6)
        display_title = (
            f"{range_start.strftime('%d %b')} – "
            f"{range_end.strftime('%d %b %Y')}"
        )
    else:
        month_first = selected_day.replace(day=1)
        next_month = (
            month_first.replace(year=month_first.year + 1, month=1)
            if month_first.month == 12
            else month_first.replace(month=month_first.month + 1)
        )
        month_last = next_month - timedelta(days=1)
        range_start = month_first - timedelta(days=month_first.weekday())
        range_end = month_last + timedelta(days=(6 - month_last.weekday()))
        display_title = selected_day.strftime("%B %Y")

    # The month grid can span at most 42 days (six calendar rows).
    day_count = (range_end - range_start).days + 1
    if day_count > 42:
        flash("The selected calendar range is too large.", "error")
        range_start = selected_day - timedelta(days=selected_day.weekday())
        range_end = range_start + timedelta(days=6)
        view = "week"
        day_count = 7

    api_data, api_status = call_doctor_api(
        "GET",
        "/doctor/day-view",
        params={
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
        },
    )
    slots = api_data.get("slots", []) if api_status == 200 else []
    if api_status != 200:
        flash(
            api_data.get("message", "Unable to load the selected calendar range."),
            "error",
        )

    # Group the API's live slots by local calendar date for the template.
    slots_by_date = {}
    for slot in slots:
        start_value = slot.get("start_at")
        if not start_value:
            continue
        try:
            parsed_start = (
                start_value
                if isinstance(start_value, datetime)
                else datetime.fromisoformat(str(start_value).replace("Z", "+00:00"))
            )
            if parsed_start.tzinfo:
                parsed_start = parsed_start.astimezone(IST)
            slot_day = parsed_start.date()
        except (TypeError, ValueError):
            continue
        slots_by_date.setdefault(slot_day, []).append(slot)

    calendar_days = []
    current_day = range_start
    while current_day <= range_end:
        day_slots = sorted(
            slots_by_date.get(current_day, []),
            key=lambda item: str(item.get("start_at", "")),
        )
        calendar_days.append({
            "date": current_day,
            "date_iso": current_day.isoformat(),
            "is_selected": current_day == selected_day,
            "is_today": current_day == datetime.now(IST).date(),
            "slots": day_slots,
            "appointment_count": sum(
                len(item.get("appointments", [])) for item in day_slots
            ),
        })
        current_day += timedelta(days=1)

    consultation_types, types_status, types_data = get_active_consultation_types()
    if types_status != 200:
        flash(types_data.get("message", "Unable to load consultation types."), "error")

    return render_template(
        "schedule.html",
        selected_date=selected_date_text,
        selected_day=selected_day,
        today_iso=datetime.now(IST).date().isoformat(),
        previous_date=(
            selected_day
            - timedelta(days=1 if view == "day" else 7 if view == "week" else 30)
        ).isoformat(),
        next_date=(
            selected_day
            + timedelta(days=1 if view == "day" else 7 if view == "week" else 30)
        ).isoformat(),
        calendar_view=view,
        display_title=display_title,
        range_start=range_start,
        range_end=range_end,
        calendar_days=calendar_days,
        slots=slots,
        consultation_types=consultation_types,
        schedule_loaded=api_status == 200,
        active_page="schedule",
    )

@app.route("/blackout-dates", methods=["GET", "POST"])
@doctor_required
def blackout_dates():
    if request.method == "POST":
        start_value = request.form.get("start_at", "").strip()
        end_value = request.form.get("end_at", "").strip()
        reason = request.form.get("reason", "").strip()
        try:
            start_at = datetime.fromisoformat(start_value)
            end_at = datetime.fromisoformat(end_value)
            if end_at <= start_at:
                raise ValueError("The end date/time must be later than the start date/time.")
        except ValueError as exc:
            flash(str(exc) or "Enter valid blackout date/time values.", "error")
            return redirect(url_for("blackout_dates"))

        payload = {
            "start_at": start_at.isoformat(timespec="minutes"),
            "end_at": end_at.isoformat(timespec="minutes"),
            "reason": reason or None,
        }
        data, status = call_doctor_api(
            "POST", "/doctor/blackout-dates", json=payload
        )
        if status == 201:
            flash(
                f"Blackout period created. {data.get('slots_blocked', 0)} open slot(s) were blocked.",
                "success",
            )
            return redirect(url_for("blackout_dates"))
        flash(data.get("message", "Unable to create blackout period."), "error")

    data, status = call_doctor_api("GET", "/doctor/blackout-dates")
    blackouts = data.get("blackout_dates", []) if status == 200 else []
    if status != 200:
        flash(data.get("message", "Unable to load blackout dates."), "error")

    return render_template(
        "blackout_dates.html",
        blackout_dates=blackouts,
        blackouts_loaded=status == 200,
        active_page="blackout_dates",
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    refresh_token = session.get("refresh_token")
    logout_status = None
    logout_data = {}

    if session.get("access_token") and refresh_token:
        logout_data, logout_status = call_doctor_api(
            "POST",
            "/logout",
            json={"refreshToken": refresh_token},
        )

    if logout_status == 200:
        session.clear()
        flash("You have been securely signed out.", "success")
    else:
        app.logger.warning(
            "Server-side logout was not confirmed: status=%s, message=%s",
            logout_status,
            logout_data.get("message", "No response"),
        )
        session.clear()

        if logout_status is None:
            flash(
                "You have been signed out locally. Server-side logout could not be reached.",
                "warning",
            )
        else:
            flash(
                "You have been signed out locally. Server-side logout could not be confirmed. Please sign in again.",
                "warning",
            )
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true",
    )
