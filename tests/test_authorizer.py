
import importlib.util
import time
from pathlib import Path

import boto3
import jwt
import pytest


SECRET = "test-secret-for-authorizer"


class FakeSSM:
    def get_parameter(self, **kwargs):
        return {"Parameter": {"Value": SECRET}}


class FakeSessionsTable:
    def __init__(self):
        self.item = {
            "session_id": "session-123",
            "userId": "user-123",
            "role": "DOCTOR",
            "status": "ACTIVE",
            "expiryTimestamp": int(time.time()) + 3600,
        }

    def get_item(self, **kwargs):
        return {"Item": self.item}


class FakeDynamoDB:
    def __init__(self, table):
        self.table = table

    def Table(self, table_name):
        return self.table


@pytest.fixture
def authorizer(monkeypatch):
    table = FakeSessionsTable()

    monkeypatch.setenv("JWT_SECRET_PARAMETER", "/test/jwt-secret")
    monkeypatch.setenv("SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("JWT_ISSUER", "medislot")
    monkeypatch.setenv("JWT_AUDIENCE", "medislot-api")

    monkeypatch.setattr(
        boto3,
        "client",
        lambda service, **kwargs: FakeSSM()
        if service == "ssm" else None,
    )

    monkeypatch.setattr(
        boto3,
        "resource",
        lambda service, **kwargs: FakeDynamoDB(table)
        if service == "dynamodb" else None,
    )

    file_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "auth"
        / "authorizer"
        / "app.py"
    )

    spec = importlib.util.spec_from_file_location(
        "authorizer_app", file_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module, table


def create_token(
    role="DOCTOR",
    user_id="user-123",
    secret=SECRET,
    expiry=None,
):
    now = int(time.time())

    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "iat": now - 10,
            "exp": expiry if expiry is not None else now + 300,
            "jti": "token-123",
            "session_id": "session-123",
            "iss": "medislot",
            "aud": "medislot-api",
        },
        secret,
        algorithm="HS256",
    )


def invoke(module, token):
    return module.handler(
        {
            "headers": {
                "Authorization": f"Bearer {token}"
            }
        },
        None,
    )


def test_valid_token_and_active_session_are_authorized(authorizer):
    module, _ = authorizer

    result = invoke(module, create_token())

    assert result["isAuthorized"] is True
    assert result["context"]["userId"] == "user-123"
    assert result["context"]["role"] == "DOCTOR"


def test_revoked_session_is_rejected(authorizer):
    module, table = authorizer
    table.item["status"] = "REVOKED"

    result = invoke(module, create_token())

    assert result["isAuthorized"] is False


def test_expired_jwt_is_rejected(authorizer):
    module, _ = authorizer

    expired_token = create_token(
        expiry=int(time.time()) - 60
    )

    result = invoke(module, expired_token)

    assert result["isAuthorized"] is False


def test_expired_session_is_rejected(authorizer):
    module, table = authorizer
    table.item["expiryTimestamp"] = int(time.time()) - 60

    result = invoke(module, create_token())

    assert result["isAuthorized"] is False


def test_user_id_mismatch_is_rejected(authorizer):
    module, _ = authorizer

    result = invoke(
        module,
        create_token(user_id="different-user"),
    )

    assert result["isAuthorized"] is False


def test_role_mismatch_is_rejected(authorizer):
    module, table = authorizer
    table.item["role"] = "PATIENT"

    result = invoke(
        module,
        create_token(role="DOCTOR"),
    )

    assert result["isAuthorized"] is False


def test_invalid_signature_is_rejected(authorizer):
    module, _ = authorizer

    invalid_token = create_token(
        secret="incorrect-secret"
    )

    result = invoke(module, invalid_token)

    assert result["isAuthorized"] is False


def test_missing_bearer_token_is_rejected(authorizer):
    module, _ = authorizer

    result = module.handler(
        {"headers": {}},
        None,
    )

    assert result["isAuthorized"] is False


def test_empty_bearer_token_is_rejected(authorizer):
    module, _ = authorizer

    result = module.handler(
        {"headers": {"Authorization": "Bearer "}},
        None,
    )

    assert result["isAuthorized"] is False
