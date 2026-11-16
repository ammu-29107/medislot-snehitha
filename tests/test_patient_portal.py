import json
import os

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD_PARAMETER", "/test/database/password")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from src.patient.portal import app as portal


def _event(method, path, *, role=None, user_id=None, body=None, query=None, route=""):
    authorizer = {}
    if role is not None:
        authorizer["lambda"] = {"role": role, "userId": user_id}
    return {
        "routeKey": route,
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
        "queryStringParameters": query or {},
        "requestContext": {
            "http": {"method": method},
            "authorizer": authorizer,
        },
        "pathParameters": {},
    }


def test_patient_identity_is_taken_from_authorizer_context():
    event = _event("GET", "/patient/profile", role="PATIENT", user_id="42")
    assert portal._patient_id(event) == 42


def test_doctor_cannot_use_patient_profile_endpoint():
    event = _event("GET", "/patient/profile", role="DOCTOR", user_id="42")
    assert portal._patient_id(event) is None
    result = portal.handler(event, None)
    assert result["statusCode"] == 401


def test_patient_id_must_be_numeric():
    event = _event("GET", "/patient/profile", role="PATIENT", user_id="not-a-number")
    assert portal._patient_id(event) is None


def test_profile_update_rejects_invalid_json():
    event = _event("PUT", "/patient/profile", role="PATIENT", user_id="42")
    event["body"] = "{invalid"
    assert portal._body(event) is None


def test_doctor_search_rejects_non_integer_pagination():
    event = _event(
        "GET", "/doctors", query={"page": "zero"},
        route="GET /doctors",
    )
    result = portal.handler(event, None)
    assert result["statusCode"] == 400


def test_unknown_route_returns_404():
    event = _event("GET", "/not-a-route", route="GET /not-a-route")
    assert portal.handler(event, None)["statusCode"] == 404


def test_patient_profile_update_requires_patient_role():
    event = _event(
        "PUT", "/patient/profile", role="DOCTOR", user_id="7",
        body={"full_name": "Someone", "phone_number": "9999999999", "gender": "MALE"},
        route="PUT /patient/profile",
    )
    assert portal.handler(event, None)["statusCode"] == 401
