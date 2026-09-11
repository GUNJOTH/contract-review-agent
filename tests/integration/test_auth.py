"""Protected API authentication regression tests."""

from fastapi.testclient import TestClient

from contract_review_app.config import settings
from contract_review_app.main import app

client = TestClient(app)


def test_protected_api_rejects_missing_and_invalid_tokens():
    missing = client.get("/api/v1/ai-rules")
    assert missing.status_code == 401, missing.text
    assert missing.json()["Response"]["Error"]["Code"] == "AuthFailure.InvalidToken"

    invalid = client.get(
        "/api/v1/ai-rules",
        headers={settings.AUTH_HEADER_NAME: "incorrect-test-token"},
    )
    assert invalid.status_code == 401, invalid.text
    assert "test-api-token" not in invalid.text


def test_protected_api_fails_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "API_TOKEN", "")
    response = client.get(
        "/api/v1/ai-rules",
        headers={settings.AUTH_HEADER_NAME: "test-api-token"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["Response"]["Error"]["Code"] == "FailedOperation.UnOpenError"
