"""Protected API authentication regression tests."""

import asyncio
import json

from fastapi.testclient import TestClient
from starlette.requests import Request

from contract_review_app.config import settings
from contract_review_app.main import app, general_exception_handler

client = TestClient(app)


def test_protected_api_rejects_missing_and_invalid_tokens():
    request_id = "auth-regression-request"
    missing = client.get(
        "/api/v1/contract-review/rule-bundle",
        headers={"X-Request-ID": request_id},
    )
    assert missing.status_code == 401, missing.text
    assert missing.json()["Response"]["Error"]["Code"] == "AuthFailure.InvalidToken"
    assert missing.json()["Response"]["RequestId"] == request_id
    assert missing.headers["X-Request-ID"] == request_id

    invalid = client.get(
        "/api/v1/contract-review/rule-bundle",
        headers={settings.AUTH_HEADER_NAME: "incorrect-test-token"},
    )
    assert invalid.status_code == 401, invalid.text
    assert "test-api-token" not in invalid.text


def test_protected_api_fails_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "API_TOKEN", "")
    response = client.get(
        "/api/v1/contract-review/rule-bundle",
        headers={settings.AUTH_HEADER_NAME: "test-api-token"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["Response"]["Error"]["Code"] == "FailedOperation.UnOpenError"


def test_unhandled_exception_response_is_generic_and_correlatable():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
    }
    request = Request(scope)
    request.state.request_id = "generic-error-request"

    response = asyncio.run(
        general_exception_handler(request, RuntimeError("internal secret path"))
    )

    assert response.status_code == 500
    assert b"internal secret path" not in response.body
    payload = json.loads(response.body)
    assert payload["Response"]["Error"]["Code"] == "FailedOperation.UnKnowError"
    assert payload["Response"]["RequestId"] == "generic-error-request"
