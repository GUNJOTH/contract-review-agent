"""外部模型共享熔断器测试，不连接真实模型或 embedding 服务。"""

from __future__ import annotations

import threading

import httpx
import pytest

from contract_review_app.config import settings
from contract_review_app.services import review_service
from contract_review_app.services import vector_knowledge_index as vector_module
from contract_review_app.services.model_transport import (
    ExternalModelCircuitBreaker,
    ExternalModelCircuitOpenError,
    ExternalModelTransportError,
    HttpxModelTransport,
    shared_model_circuit_breaker,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class _SequencedClient:
    def __init__(self, outcomes: list[object]) -> None:
        self._lock = threading.Lock()
        self._outcomes = list(outcomes)
        self.calls = 0

    def post(self, *args, **kwargs):
        del args, kwargs
        with self._lock:
            self.calls += 1
            if not self._outcomes:
                raise AssertionError("测试客户端收到未预期的请求")
            outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "http://circuit-model.test/v1/chat")
    return httpx.Response(status_code, request=request)


def _send(transport: HttpxModelTransport):
    return transport.post_json(
        "http://circuit-model.test/v1/chat",
        payload={"model": "test-model"},
        headers={},
        timeout=1.0,
        operation="semantic_review",
    )


def test_circuit_opens_after_consecutive_failures_and_allows_one_probe() -> None:
    clock = _Clock()
    breaker = ExternalModelCircuitBreaker(
        failure_threshold=2,
        open_timeout_seconds=10.0,
        now=clock,
    )

    first = breaker.before_call(operation="semantic_review")
    breaker.record_failure(first)
    assert breaker.snapshot()["state"] == "closed"
    assert breaker.snapshot()["failure_count"] == 1

    second = breaker.before_call(operation="semantic_review")
    breaker.record_failure(second)
    assert breaker.snapshot()["state"] == "open"

    with pytest.raises(ExternalModelCircuitOpenError) as error:
        breaker.before_call(operation="semantic_review")
    assert error.value.retry_after_seconds == pytest.approx(10.0)

    clock.advance(10.0)
    probe = breaker.before_call(operation="semantic_review")
    assert probe[1] is True
    assert breaker.snapshot()["state"] == "half_open"
    with pytest.raises(ExternalModelCircuitOpenError):
        breaker.before_call(operation="semantic_review")

    breaker.record_success(probe)
    assert breaker.snapshot()["state"] == "closed"
    assert breaker.before_call(operation="semantic_review")[1] is False


def test_transport_counts_one_logical_failure_after_retry_exhaustion() -> None:
    client = _SequencedClient([_response(503)] * 6)
    breaker = ExternalModelCircuitBreaker(
        failure_threshold=2,
        open_timeout_seconds=30.0,
    )
    transport = HttpxModelTransport(
        max_attempts=3,
        backoff_seconds=0,
        client=client,
        circuit_breaker=breaker,
    )

    with pytest.raises(ExternalModelTransportError) as first_error:
        _send(transport)
    assert first_error.value.attempts == 3
    assert breaker.snapshot()["state"] == "closed"
    assert breaker.snapshot()["failure_count"] == 1

    with pytest.raises(ExternalModelTransportError) as second_error:
        _send(transport)
    assert second_error.value.attempts == 3
    assert breaker.snapshot()["state"] == "open"
    assert client.calls == 6

    with pytest.raises(ExternalModelCircuitOpenError) as open_error:
        _send(transport)
    assert open_error.value.attempts == 0
    assert client.calls == 6


def test_non_retryable_provider_response_does_not_count_as_circuit_failure() -> None:
    client = _SequencedClient([_response(400), _response(503)])
    breaker = ExternalModelCircuitBreaker(
        failure_threshold=1,
        open_timeout_seconds=30.0,
    )
    transport = HttpxModelTransport(
        max_attempts=1,
        backoff_seconds=0,
        client=client,
        circuit_breaker=breaker,
    )

    with pytest.raises(httpx.HTTPStatusError):
        _send(transport)
    assert breaker.snapshot()["state"] == "closed"
    assert breaker.snapshot()["failure_count"] == 0

    with pytest.raises(ExternalModelTransportError):
        _send(transport)
    assert breaker.snapshot()["state"] == "open"
    assert client.calls == 2


def test_transport_fails_fast_while_circuit_is_open() -> None:
    client = _SequencedClient([httpx.ConnectTimeout("connect")])
    breaker = ExternalModelCircuitBreaker(
        failure_threshold=1,
        open_timeout_seconds=30.0,
    )
    transport = HttpxModelTransport(
        max_attempts=1,
        backoff_seconds=0,
        client=client,
        circuit_breaker=breaker,
    )

    with pytest.raises(ExternalModelTransportError):
        _send(transport)
    with pytest.raises(ExternalModelCircuitOpenError):
        _send(transport)
    assert client.calls == 1


def test_shared_circuit_identity_is_operation_endpoint_and_model() -> None:
    endpoint = "http://circuit-shared.test/v1"
    first = shared_model_circuit_breaker(
        operation="semantic_review",
        endpoint=endpoint,
        model="test-model",
        failure_threshold=4,
        open_timeout_seconds=30.0,
    )
    same = shared_model_circuit_breaker(
        operation="semantic_review",
        endpoint=endpoint + "/",
        model="test-model",
        failure_threshold=4,
        open_timeout_seconds=30.0,
    )
    different_operation = shared_model_circuit_breaker(
        operation="embedding",
        endpoint=endpoint,
        model="test-model",
        failure_threshold=4,
        open_timeout_seconds=30.0,
    )

    assert same is first
    assert different_operation is not first
    assert first.snapshot()["failure_threshold"] == 4


def test_application_wires_circuit_policy_without_calling_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _NoopReviewer:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_ENDPOINT",
        "http://circuit-application.test/v1/chat",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-chat-model")
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED",
        True,
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD",
        5,
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS",
        11.0,
    )
    monkeypatch.setattr(review_service, "RelaySemanticReviewer", _NoopReviewer)

    reviewer = review_service._semantic_client()

    assert reviewer is not None
    circuit_breaker = captured["circuit_breaker"]
    assert isinstance(circuit_breaker, ExternalModelCircuitBreaker)
    assert circuit_breaker.snapshot()["failure_threshold"] == 5
    assert circuit_breaker.snapshot()["open_timeout_seconds"] == 11.0


def test_embedding_transport_wires_the_same_circuit_policy_without_provider(
    monkeypatch,
) -> None:
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT", None)
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT_POLICY", None)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://circuit-embedding.test/v1/embeddings",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_BREAKER_ENABLED",
        True,
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_FAILURE_THRESHOLD",
        4,
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS",
        9.0,
    )

    transport = vector_module._embedding_transport()
    try:
        circuit_breaker = transport._circuit_breaker
        assert isinstance(circuit_breaker, ExternalModelCircuitBreaker)
        assert circuit_breaker.snapshot()["failure_threshold"] == 4
        assert circuit_breaker.snapshot()["open_timeout_seconds"] == 9.0
    finally:
        transport.close()


def test_circuit_policy_changes_review_fingerprint(monkeypatch) -> None:
    from contract_review.models import ReviewContext

    baseline = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-circuit-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_CIRCUIT_OPEN_TIMEOUT_SECONDS",
        31.0,
    )

    changed = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-circuit-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )

    assert changed != baseline


@pytest.mark.parametrize(
    ("setting_name", "changed_value"),
    [
        ("CONTRACT_REVIEW_ENDPOINT", "http://fingerprint-endpoint.test/v2/chat"),
        ("CONTRACT_REVIEW_TIMEOUT_SECONDS", 301),
        ("CONTRACT_REVIEW_JSON_MODE", True),
        ("CONTRACT_REVIEW_EMBEDDING_TIMEOUT_SECONDS", 121.0),
    ],
)
def test_external_model_execution_identity_changes_review_fingerprint(
    monkeypatch,
    setting_name,
    changed_value,
) -> None:
    """外部模型执行身份或超时变化时不能复用旧审查结果。"""

    from contract_review.models import ReviewContext

    baseline = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-external-model-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )
    monkeypatch.setattr(settings, setting_name, changed_value)

    changed = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-external-model-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )

    assert changed != baseline


def test_circuit_policy_rejects_invalid_threshold_and_timeout() -> None:
    with pytest.raises(ValueError):
        ExternalModelCircuitBreaker(
            failure_threshold=0,
            open_timeout_seconds=1.0,
        )
    with pytest.raises(ValueError):
        ExternalModelCircuitBreaker(
            failure_threshold=101,
            open_timeout_seconds=1.0,
        )
    with pytest.raises(ValueError):
        ExternalModelCircuitBreaker(
            failure_threshold=1,
            open_timeout_seconds=float("nan"),
        )
