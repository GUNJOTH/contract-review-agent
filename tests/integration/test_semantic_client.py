"""RelaySemanticReviewer 单元测试（mock httpx.post，不打外网）。"""

import httpx
import pytest

from contract_review.models import SemanticModelRequest, SemanticReviewResponse
from contract_review.semantic import SemanticClientError

from contract_review_app.services.semantic_client import RelaySemanticReviewer


def _request() -> SemanticModelRequest:
    return SemanticModelRequest(
        request_id="req-1",
        provider="test-provider",
        model_version="test-model",
        prompt_version="prompt-v1",
        request_fingerprint="f" * 64,
        rule_ids=["R1"],
        system_instruction="请只输出 JSON。",
    )


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _ok_payload(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}}],
    }


def test_review_parses_fenced_json_content(monkeypatch):
    payload = _ok_payload(
        '```json\n{"items": [{"rule_id": "R1", "status": "pass", '
        '"reason": "金额一致", "evidence_ids": ["e1"]}]}\n```'
    )
    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions",
        api_key="k",
        model_version="test-model",
    )
    response = client.review(_request())

    assert isinstance(response, SemanticReviewResponse)
    assert response.provider == "test-provider"
    assert response.model_version == "test-model"
    assert response.request_fingerprint == "f" * 64
    assert response.items[0].rule_id == "R1"
    # 小写状态被归一化为引擎要求的大写枚举
    assert response.items[0].status.value == "PASS"


def test_review_extracts_json_from_preamble(monkeypatch):
    payload = _ok_payload(
        '好的，审查结果如下：{"items": [{"rule_id": "R1", "status": "warn", '
        '"reason": "建议关注", "evidence_ids": ["e1"]}]}'
    )
    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions", model_version="test-model"
    )
    response = client.review(_request())
    assert response.items[0].status.value == "WARN"


def test_review_omits_response_format_unless_json_mode(monkeypatch):
    captured: dict = {}

    def fake_post(endpoint, json=None, headers=None, timeout=None):
        del endpoint, headers, timeout
        captured["body"] = json
        return FakeResponse(_ok_payload('{"items": []}'))

    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions", model_version="test-model"
    )
    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post", fake_post
    )
    client.review(_request())
    assert "response_format" not in captured["body"]

    client_json = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions",
        model_version="test-model",
        json_mode=True,
    )
    client_json.review(_request())
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_review_raises_client_error_on_http_failure(monkeypatch):
    request = httpx.Request("POST", "http://fake/v1/chat/completions")
    response = httpx.Response(400, request=request)

    class FailingResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "bad request", request=request, response=response
            )

    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post",
        lambda *args, **kwargs: FailingResponse(),
    )
    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions", model_version="test-model"
    )
    with pytest.raises(SemanticClientError):
        client.review(_request())


def test_review_raises_client_error_on_non_json_content(monkeypatch):
    payload = _ok_payload("抱歉，我无法回答。")
    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions", model_version="test-model"
    )
    with pytest.raises(SemanticClientError):
        client.review(_request())
