import os

import requests
from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    session,
    request,
    flash,
)
from flask_session import Session

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
    SESSION_FILE_DIR="/opt/medislot/dashboard/flask_session",
    SESSION_TYPE="filesystem",
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
)

Session(app)

API_BASE_URL = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/")

def call_doctor_api(method, path, **kwargs):
    access_token = session.get("access_token")

    if not access_token:
        return None, 401

    try:
        response = requests.request(
            method,
            f"{API_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=15,
            **kwargs,
        )

        try:
            data = response.json()
        except ValueError:
            data = {"message": "Invalid response from MediSlot API"}

        return data, response.status_code

    except requests.RequestException:
        app.logger.exception("MediSlot API request failed: %s", path)
        return {"message": "Unable to connect to MediSlot API"}, 503

@app.route("/")
def index():
    if session.get("access_token") and session.get("role") == "DOCTOR":
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter your email and password.", "error")
            return render_template("login.html")

        try:
            response = requests.post(
                f"{API_BASE_URL}/login/doctor",
                json={
                    "email": email,
                    "password": password,
                },
                timeout=10,
            )

            data = response.json()

            if response.status_code == 200:
                session.clear()
                session["access_token"] = data["accessToken"]
                session["refresh_token"] = data["refreshToken"]
                session["role"] = "DOCTOR"

                return redirect(url_for("dashboard"))

            flash(
                data.get("message", "Login failed. Please try again."),
                "error",
            )

        except requests.RequestException:
            app.logger.exception("Unable to reach MediSlot authentication API")
            flash("Unable to connect to MediSlot. Please try again.", "error")

        except (ValueError, KeyError):
            app.logger.exception("Unexpected authentication API response")
            flash("Unexpected response from the authentication service.", "error")

    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if (
        not session.get("access_token")
        or session.get("role") != "DOCTOR"
    ):
        return redirect(url_for("login"))

    return render_template("dashboard.html")

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if (
        not session.get("access_token")
        or session.get("role") != "DOCTOR"
    ):
        return redirect(url_for("login"))

    if request.method == "POST":
        speciality_text = request.form.get("speciality_ids", "").strip()

        try:
            speciality_ids = [
                int(value.strip())
                for value in speciality_text.split(",")
                if value.strip()
            ]
        except ValueError:
            flash("Enter valid speciality IDs.", "error")
            return redirect(url_for("profile"))

        payload = {
            "full_name": request.form.get("full_name", "").strip(),
            "qualifications": request.form.get("qualifications", "").strip(),
            "experience_years": request.form.get("experience_years", "").strip(),
            "bio": request.form.get("bio", "").strip(),
            "hospital_id": request.form.get("hospital_id", "").strip(),
            "speciality_ids": speciality_ids,
        }

        uploaded_file = request.files.get("profile_photo")

        if uploaded_file and uploaded_file.filename:
            if uploaded_file.mimetype not in {
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                flash("Only JPEG, PNG, and WEBP images are allowed.", "error")
                return redirect(url_for("profile"))

            photo_bytes = uploaded_file.read()

            if not photo_bytes:
                flash("The selected photo is empty.", "error")
                return redirect(url_for("profile"))

            if len(photo_bytes) > 5 * 1024 * 1024:
                flash("Profile photo must be 5 MB or smaller.", "error")
                return redirect(url_for("profile"))

            uploaded_file.seek(0)

            upload_info, upload_status = call_doctor_api(
                "POST",
                "/doctor/profile/photo-upload-url",
                json={"content_type": uploaded_file.mimetype},
            )

            if upload_status != 200 or not upload_info.get("upload_url") or not upload_info.get("photo_key"):
                flash("Could not generate photo upload URL.", "error")
                return redirect(url_for("profile"))

            s3_response = requests.put(
                upload_info["upload_url"],
                data=photo_bytes,
                headers={"Content-Type": upload_info["content_type"]},
                timeout=30,
            )

            if not s3_response.ok:
                flash("Photo upload failed.", "error")
                return redirect(url_for("profile"))

            payload["profile_photo_key"] = upload_info["photo_key"]

        data, status = call_doctor_api(
            "PUT", "/doctor/profile", json=payload
        )

        if status == 200:
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))

        flash(data.get("message", "Unable to update profile."), "error")

    data, status = call_doctor_api("GET", "/doctor/profile")

    if status != 200:
        flash(data.get("message", "Unable to load profile."), "error")
        return redirect(url_for("dashboard"))

    return render_template("profile.html", profile=data)


@app.route("/consultation-types", methods=["GET", "POST"])
def consultation_types():
    if (
        not session.get("access_token")
        or session.get("role") != "DOCTOR"
    ):
        return redirect(url_for("login"))

    if request.method == "POST":
        payload = {
            "type_name": request.form.get("type_name", "").strip(),
            "description": request.form.get("description", "").strip(),
            "duration_minutes": request.form.get("duration_minutes"),
            "capacity": request.form.get("capacity", 1),
            "consultation_fee": request.form.get("consultation_fee", 0),
        }

        data, status = call_doctor_api(
            "POST", "/doctor/consultation-types", json=payload
        )

        if status == 201:
            flash("Consultation type created successfully.", "success")
            return redirect(url_for("consultation_types"))

        flash(data.get("message", "Unable to create consultation type."), "error")

    data, status = call_doctor_api(
        "GET", "/doctor/consultation-types"
    )

    if status != 200:
        flash(data.get("message", "Unable to load consultation types."), "error")
        types = []
    else:
        types = data.get("consultation_types", [])

    return render_template(
        "consultation_types.html",
        consultation_types=types,
    )


@app.route("/consultation-types/<int:type_id>/update", methods=["POST"])
def update_consultation_type(type_id):
    if (
        not session.get("access_token")
        or session.get("role") != "DOCTOR"
    ):
        return redirect(url_for("login"))

    payload = {
        "type_name": request.form.get("type_name", "").strip(),
        "description": request.form.get("description", "").strip(),
        "duration_minutes": request.form.get("duration_minutes"),
        "capacity": request.form.get("capacity", 1),
        "consultation_fee": request.form.get("consultation_fee", 0),
    }

    data, status = call_doctor_api(
        "PUT",
        f"/doctor/consultation-types/{type_id}",
        json=payload,
    )

    if status == 200:
        flash("Consultation type updated.", "success")
    else:
        flash(data.get("message", "Unable to update consultation type."), "error")

    return redirect(url_for("consultation_types"))


@app.route("/consultation-types/<int:type_id>/disable", methods=["POST"])
def disable_consultation_type(type_id):
    if (
        not session.get("access_token")
        or session.get("role") != "DOCTOR"
    ):
        return redirect(url_for("login"))

    data, status = call_doctor_api(
        "DELETE",
        f"/doctor/consultation-types/{type_id}",
    )

    if status == 200:
        flash("Consultation type disabled.", "success")
    else:
        flash(data.get("message", "Unable to disable consultation type."), "error")

    return redirect(url_for("consultation_types"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)