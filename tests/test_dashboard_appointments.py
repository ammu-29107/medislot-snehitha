"""Route-level tests for the dashboard appointment integrations."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FLASK_SECRET_KEY", "dashboard-test-secret")
os.environ.setdefault("MEDISLOT_API_BASE_URL", "https://example.test")
os.environ.setdefault("FLASK_SESSION_DIR", "/tmp/medislot-dashboard-tests")

from dashboard import app as dashboard_module

app = dashboard_module.app


def _client(role, token="test-access"):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["access_token"] = token
        sess["role"] = role
        sess["dashboard_csrf_token"] = "csrf-test-token"
    return client



def test_patient_appointment_list_route(monkeypatch):
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: ({"appointments": []}, 200),
    )
    response = _client("PATIENT").get("/patient/appointments")
    assert response.status_code == 200
    assert b"My Appointments" in response.data



def test_patient_appointment_details_route(monkeypatch):
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: ({"appointment": {
            "appointment_id": 12, "doctor_name": "Test Doctor",
            "booking_reference": "MS-TEST", "start_at": "2026-11-25T10:00:00+05:30",
            "status": "CONFIRMED", "consultation_fee": "500.00",
        }}, 200),
    )
    response = _client("PATIENT").get("/patient/appointments/12")
    assert response.status_code == 200
    assert b"MS-TEST" in response.data
    assert b"Test Doctor" in response.data



def test_patient_booking_sends_slot_and_idempotency_key(monkeypatch):
    captured = {}
    def fake_call(method, path, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return ({"booking_reference": "MS-BOOKED"}, 201)
    monkeypatch.setattr(dashboard_module, "call_doctor_api", fake_call)
    response = _client("PATIENT").post("/appointments/book", data={
        "slot_id": "123", "doctor_id": "7", "selected_date": "2026-11-25",
        "consultation_type_id": "4", "idempotency_key": "repeatable-key-123",
        "csrf_token": "csrf-test-token",
    })
    assert response.status_code == 302
    assert captured["method"] == "POST"
    assert captured["path"] == "/appointments"
    assert captured["kwargs"]["json"] == {"slot_id": 123}
    assert captured["kwargs"]["headers"]["Idempotency-Key"]



def test_patient_cancel_appointment_api_route(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: (calls.append((method, path, kwargs)) or ({}, 200)),
    )
    response = _client("PATIENT").post("/patient/appointments/12/cancel", data={"csrf_token": "csrf-test-token"})
    assert response.status_code == 302
    assert calls[0][0:2] == ("POST", "/appointments/12/cancel")



def test_doctor_appointment_list_route(monkeypatch):
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: ({"appointments": []}, 200),
    )
    response = _client("DOCTOR").get("/doctor/appointments")
    assert response.status_code == 200
    assert b"Appointments" in response.data



def test_doctor_cancel_appointment_route(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: (calls.append((method, path, kwargs)) or ({}, 200)),
    )
    response = _client("DOCTOR").post("/doctor/appointments/12/cancel", data={"csrf_token": "csrf-test-token"})
    assert response.status_code == 302
    assert calls[0][0:2] == ("POST", "/appointments/12/cancel")



def test_doctor_cancel_slot_route(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: (calls.append((method, path, kwargs)) or ({"count": 2}, 200)),
    )
    response = _client("DOCTOR").post("/doctor/slots/77/cancel", data={
        "schedule_date": "2026-11-25", "calendar_view": "day",
        "csrf_token": "csrf-test-token",
    })
    assert response.status_code == 302
    assert calls[0][0:2] == ("POST", "/doctors/me/slots/77/cancel")


def test_patient_appointment_list_shows_refund_estimate_without_booking_reference(monkeypatch):
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: ({"appointments": [{
            "appointment_id": 21,
            "doctor_name": "Test Doctor",
            "booking_reference": "MS-INTERNAL-ONLY",
            "start_at": "2026-11-25T10:00:00",
            "status": "CONFIRMED",
            "refund_quote_amount": "375.00",
            "refund_quote_percentage": "75",
        }]}, 200),
    )
    response = _client("PATIENT").get("/patient/appointments")
    assert response.status_code == 200
    assert b"Estimated refund" in response.data
    assert b"375.00" in response.data
    assert b"Booking Reference" not in response.data
    assert b"MS-INTERNAL-ONLY" not in response.data


def test_doctor_check_in_route_calls_appointment_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: (calls.append((method, path, kwargs)) or ({}, 200)),
    )
    response = _client("DOCTOR").post(
        "/doctor/appointments/42/check-in",
        data={"csrf_token": "csrf-test-token", "schedule_date": "2026-11-25", "calendar_view": "day"},
    )
    assert response.status_code == 302
    assert calls[0][0:2] == ("POST", "/appointments/42/check-in")


def test_doctor_complete_route_calls_appointment_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard_module, "call_doctor_api",
        lambda method, path, **kwargs: (calls.append((method, path, kwargs)) or ({}, 200)),
    )
    response = _client("DOCTOR").post(
        "/doctor/appointments/42/complete",
        data={"csrf_token": "csrf-test-token", "schedule_date": "2026-11-25", "calendar_view": "day"},
    )
    assert response.status_code == 302
    assert calls[0][0:2] == ("POST", "/appointments/42/complete")
