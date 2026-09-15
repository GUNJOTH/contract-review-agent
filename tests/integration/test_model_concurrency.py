"""模型级进程内并发闸门测试，不连接真实模型或 embedding 服务。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from contract_review_app.config import settings
from contract_review_app.services import review_service
from contract_review_app.services import vector_knowledge_index as vector_module
from contract_review.models import ReviewContext
from contract_review_app.services.model_transport import (
    AdaptiveConcurrencyController,
    ExternalModelBackpressureError,
    ExternalModelConcurrencyGate,
    ExternalModelTransportError,
    HttpxModelTransport,
    shared_model_concurrency_gate,
)


class _Response:
    def raise_for_status(self) -> None:
        pass


class _BlockingClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.started = threading.Event()
        self.two_active = threading.Event()
        self.release = threading.Event()

    def post(self, *args, **kwargs):
        del args, kwargs
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
            self.started.set()
            if self.active >= 2:
                self.two_active.set()
        try:
            if not self.release.wait(timeout=2.0):
                raise AssertionError("测试客户端没有等到释放信号")
            return _Response()
        finally:
            with self._lock:
                self.active -= 1


def _post(transport: HttpxModelTransport, operation: str = "semantic_review"):
    return transport.post_json(
        "http://phase2-model.test/v1/chat/completions",
        payload={"model": "test-model"},
        headers={},
        timeout=2.0,
        operation=operation,
    )


def test_shared_gate_bounds_calls_across_transport_instances() -> None:
    client = _BlockingClient()
    gate = ExternalModelConcurrencyGate(limit=2, queue_timeout_seconds=2.0)
    transports = [
        HttpxModelTransport(
            max_attempts=1,
            backoff_seconds=0,
            client=client,
            concurrency_gate=gate,
        )
        for _ in range(4)
    ]

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_post, transport) for transport in transports]
        assert client.two_active.wait(timeout=1.0)
        assert gate.snapshot()["active"] == 2
        assert gate.snapshot()["waiting"] >= 1
        client.release.set()
        assert all(future.result() is not None for future in futures)

    assert client.calls == 4
    assert client.max_active == 2
    assert gate.snapshot()["active"] == 0
    assert gate.snapshot()["waiting"] == 0


def test_gate_timeout_fails_without_retrying_or_exceeding_budget() -> None:
    client = _BlockingClient()
    gate = ExternalModelConcurrencyGate(limit=1, queue_timeout_seconds=0.05)
    transport = HttpxModelTransport(
        max_attempts=5,
        backoff_seconds=0,
        client=client,
        concurrency_gate=gate,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_post, transport)
        assert client.started.wait(timeout=1.0)
        assert gate.snapshot()["active"] == 1
        with pytest.raises(ExternalModelBackpressureError) as error:
            _post(transport)
        assert error.value.attempts == 0
        assert client.calls == 1
        client.release.set()
        assert first.result() is not None

    assert gate.snapshot()["active"] == 0


def test_chat_and_embedding_use_separate_shared_gate_identities() -> None:
    endpoint = "http://phase2-shared-gate.test/v1"
    chat_gate = shared_model_concurrency_gate(
        operation="semantic_review",
        endpoint=endpoint,
        model="test-chat-model",
        limit=2,
        queue_timeout_seconds=3.0,
    )
    same_chat_gate = shared_model_concurrency_gate(
        operation="semantic_review",
        endpoint=endpoint + "/",
        model="test-chat-model",
        limit=2,
        queue_timeout_seconds=3.0,
    )
    embedding_gate = shared_model_concurrency_gate(
        operation="embedding",
        endpoint=endpoint,
        model="test-chat-model",
        limit=1,
        queue_timeout_seconds=3.0,
    )

    assert same_chat_gate is chat_gate
    assert embedding_gate is not chat_gate
    assert chat_gate.snapshot()["limit"] == 2
    assert embedding_gate.snapshot()["limit"] == 1


def test_application_passes_chat_concurrency_policy_without_calling_provider(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _NoopReviewer:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    endpoint = "http://phase2-application-policy.test/v1/chat"
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", endpoint)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-chat-model")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS", 7.0)
    monkeypatch.setattr(review_service, "RelaySemanticReviewer", _NoopReviewer)

    reviewer = review_service._semantic_client()

    assert reviewer is not None
    assert captured["max_concurrency"] == 2
    assert captured["queue_timeout_seconds"] == 7.0


def test_embedding_transport_builds_an_independent_gate_without_calling_provider(
    monkeypatch,
) -> None:
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT", None)
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT_POLICY", None)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://phase2-embedding-policy.test/v1/embeddings",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_QUEUE_TIMEOUT_SECONDS",
        6.0,
    )

    transport = vector_module._embedding_transport()
    try:
        gate = transport._concurrency_gate
        assert gate is not None
        assert gate.snapshot()["limit"] == 2
        assert gate.snapshot()["queue_timeout_seconds"] == 6.0
    finally:
        transport.close()


def test_review_fingerprint_includes_model_concurrency_policy(monkeypatch) -> None:
    baseline = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-model-concurrency-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY", 2)

    changed = review_service._review_fingerprint(
        [("contract.pdf", b"isolated contract")],
        package_id="pkg-model-concurrency-fingerprint",
        review_context=ReviewContext(contract_type="software"),
    )

    assert changed != baseline


def test_model_concurrency_policy_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError):
        ExternalModelConcurrencyGate(limit=0, queue_timeout_seconds=1.0)
    with pytest.raises(ValueError):
        ExternalModelConcurrencyGate(limit=4, queue_timeout_seconds=1.0)
    with pytest.raises(ValueError):
        ExternalModelConcurrencyGate(limit=1, queue_timeout_seconds=0)
    with pytest.raises(ValueError):
        ExternalModelConcurrencyGate(limit=1, queue_timeout_seconds=float("nan"))


def test_gate_releases_slot_when_http_request_raises() -> None:
    class _FailingClient:
        def post(self, *args, **kwargs):
            del args, kwargs
            raise httpx.ConnectTimeout("connect")

    gate = ExternalModelConcurrencyGate(limit=1, queue_timeout_seconds=1.0)
    transport = HttpxModelTransport(
        max_attempts=1,
        backoff_seconds=0,
        client=_FailingClient(),
        concurrency_gate=gate,
    )

    with pytest.raises(ExternalModelTransportError):
        _post(transport)
    assert gate.snapshot()["active"] == 0
    assert gate.snapshot()["waiting"] == 0


def test_adaptive_controller_only_scales_up_after_a_stable_success_window() -> None:
    controller = AdaptiveConcurrencyController(
        min_limit=1,
        initial_limit=1,
        max_limit=3,
        target_latency_seconds=1.0,
        success_window=2,
    )

    controller.observe(latency_seconds=0.1, success=True)
    assert controller.current_limit == 1
    controller.observe(latency_seconds=0.1, success=True)
    assert controller.current_limit == 2

    # 升档后必须经过冷却和新的稳定窗口，不能连续放大到最大值。
    controller.observe(latency_seconds=0.1, success=True)
    controller.observe(latency_seconds=0.1, success=True)
    assert controller.current_limit == 2
    controller.observe(latency_seconds=0.1, success=True)
    controller.observe(latency_seconds=0.1, success=True)
    assert controller.current_limit == 3


def test_adaptive_controller_decreases_on_failure_or_high_latency() -> None:
    controller = AdaptiveConcurrencyController(
        min_limit=1,
        initial_limit=3,
        max_limit=3,
        target_latency_seconds=1.0,
        success_window=4,
    )

    controller.observe(latency_seconds=2.0, success=True)
    assert controller.current_limit == 2
    controller.observe(latency_seconds=0.1, success=False)
    assert controller.current_limit == 1
    controller.observe(latency_seconds=0.1, success=False)
    assert controller.current_limit == 1


def test_transport_reports_one_complete_request_to_adaptive_controller() -> None:
    controller = AdaptiveConcurrencyController(
        min_limit=1,
        initial_limit=1,
        max_limit=2,
        target_latency_seconds=1.0,
        success_window=1,
    )
    client = _BlockingClient()
    client.release.set()
    transport = HttpxModelTransport(
        max_attempts=2,
        backoff_seconds=0,
        client=client,
        adaptive_controller=controller,
    )
    assert _post(transport) is not None
    assert controller.snapshot()["observations"] == 1
    assert controller.current_limit == 2


def test_transport_failure_causes_adaptive_downshift() -> None:
    class _FailingClient:
        def post(self, *args, **kwargs):
            del args, kwargs
            raise httpx.ConnectTimeout("connect")

    controller = AdaptiveConcurrencyController(
        min_limit=1,
        initial_limit=2,
        max_limit=3,
        target_latency_seconds=1.0,
        success_window=2,
    )
    transport = HttpxModelTransport(
        max_attempts=1,
        backoff_seconds=0,
        client=_FailingClient(),
        adaptive_controller=controller,
    )

    with pytest.raises(ExternalModelTransportError):
        _post(transport)
    assert controller.current_limit == 1
    assert controller.snapshot()["observations"] == 1


def test_adaptive_application_policy_is_bounded_and_does_not_call_provider(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_ENDPOINT",
        "http://phase4-adaptive-policy.test/v1/chat",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-chat-model")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY", 3)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_ADAPTIVE_CONCURRENCY_ENABLED",
        True,
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_ADAPTIVE_MIN_CONCURRENCY", 1)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_ADAPTIVE_INITIAL_CONCURRENCY",
        2,
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_ADAPTIVE_MAX_CONCURRENCY", 3)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_MODEL_ADAPTIVE_TARGET_LATENCY_SECONDS",
        12.0,
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_ADAPTIVE_SUCCESS_WINDOW", 3)

    selected, controller = review_service._semantic_concurrency_policy()

    assert selected == 2
    assert controller is not None
    assert controller.snapshot()["min_limit"] == 1
    assert controller.snapshot()["max_limit"] == 3
    assert controller.snapshot()["target_latency_seconds"] == 12.0
