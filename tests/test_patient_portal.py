import json
import os

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD_PARAMETER", "/test/database/password")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("SESSIONS_TABLE", "medislot-test-sessions")

from src.patient.portal import app as portal

class _FakeCursor:
    def __init__(self, total):
        self.total = total
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params):
        self.executed.append((query, params))

    def fetchone(self):
        return {"total": self.total}

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, total):
        self.fake_cursor = _FakeCursor(total)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.fake_cursor

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


def test_doctor_search_clamps_page_beyond_last_page(monkeypatch):
    fake_connection = _FakeConnection(total=12)
    monkeypatch.setattr(portal, "_connection", lambda: fake_connection)

    event = _event(
        "GET",
        "/doctors",
        query={"page": "5", "page_size": "10"},
        route="GET /doctors",
    )

    result = portal._doctor_search(event)
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["pagination"]["page"] == 2
    assert body["pagination"]["total_pages"] == 2
    assert body["pagination"]["has_previous"] is True
    assert body["pagination"]["has_next"] is False

    # The SQL query should use the corrected page offset.
    page_query_params = fake_connection.fake_cursor.executed[1][1]
    assert page_query_params[-2:] == (10, 10)


def test_doctor_search_empty_results_resets_to_page_one(monkeypatch):
    fake_connection = _FakeConnection(total=0)
    monkeypatch.setattr(portal, "_connection", lambda: fake_connection)

    event = _event(
        "GET",
        "/doctors",
        query={"page": "5", "page_size": "10"},
        route="GET /doctors",
    )

    result = portal._doctor_search(event)
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["doctors"] == []
    assert body["pagination"]["page"] == 1
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["total_pages"] == 0
    assert body["pagination"]["has_previous"] is False
    assert body["pagination"]["has_next"] is False

    page_query_params = fake_connection.fake_cursor.executed[1][1]
    assert page_query_params[-2:] == (10, 0)


def test_doctor_search_preserves_valid_page(monkeypatch):
    fake_connection = _FakeConnection(total=25)
    monkeypatch.setattr(portal, "_connection", lambda: fake_connection)

    event = _event(
        "GET",
        "/doctors",
        query={
            "page": "2",
            "page_size": "10",
            "city": "Hyderabad",
        },
        route="GET /doctors",
    )

    result = portal._doctor_search(event)
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["pagination"]["page"] == 2
    assert body["pagination"]["total"] == 25
    assert body["pagination"]["total_pages"] == 3
    assert body["pagination"]["has_previous"] is True
    assert body["pagination"]["has_next"] is True

    page_query_params = fake_connection.fake_cursor.executed[1][1]
    assert page_query_params[0] == "%Hyderabad%"
    assert page_query_params[-2:] == (10, 10)
