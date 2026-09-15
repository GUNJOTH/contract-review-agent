"""外部模型临时错误重试策略测试，不连接真实服务。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from contract_review_app.services.model_transport import (
    ExternalModelTransportError,
    ExternalModelConcurrencyGate,
    HttpxModelTransport,
)


class _SequencedClient:
    def __init__(self, outcomes: list[object]) -> None:
        self._lock = threading.Lock()
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, *args, **kwargs):
        del args, kwargs
        with self._lock:
            self.calls += 1
            outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(status_code: int, *, headers: dict[str, str] | None = None):
    request = httpx.Request("POST", "http://phase3-model.test/v1/chat")
    return httpx.Response(status_code, request=request, headers=headers)


def _send(transport: HttpxModelTransport):
    return transport.post_json(
        "http://phase3-model.test/v1/chat",
        payload={"model": "test-model"},
        headers={},
        timeout=10.0,
        operation="semantic_review",
    )


def test_retryable_http_status_honors_retry_after_with_backoff_cap() -> None:
    client = _SequencedClient(
        [
            _response(503, headers={"Retry-After": "2"}),
            _response(200),
        ]
    )
    sleeps: list[float] = []
    transport = HttpxModelTransport(
        max_attempts=2,
        backoff_seconds=0.25,
        max_backoff_seconds=1.0,
        sleep=sleeps.append,
        random_value=lambda: 1.0,
        client=client,
    )

    assert _send(transport).status_code == 200
    assert client.calls == 2
    assert sleeps == [1.0]


def test_invalid_retry_after_uses_jittered_exponential_backoff() -> None:
    client = _SequencedClient(
        [
            _response(500, headers={"Retry-After": "not-a-delay"}),
            _response(200),
        ]
    )
    sleeps: list[float] = []
    transport = HttpxModelTransport(
        max_attempts=2,
        backoff_seconds=1.0,
        jitter_ratio=0.2,
        max_backoff_seconds=5.0,
        sleep=sleeps.append,
        random_value=lambda: 0.5,
        client=client,
    )

    assert _send(transport).status_code == 200
    assert sleeps == [pytest.approx(1.1)]


def test_non_retryable_http_error_is_returned_after_one_attempt() -> None:
    client = _SequencedClient([_response(400)])
    transport = HttpxModelTransport(
        max_attempts=5,
        backoff_seconds=0,
        client=client,
    )

    with pytest.raises(httpx.HTTPStatusError):
        _send(transport)
    assert client.calls == 1


def test_retryable_http_exhaustion_reports_attempt_count() -> None:
    client = _SequencedClient([_response(429)] * 3)
    transport = HttpxModelTransport(
        max_attempts=3,
        backoff_seconds=0,
        client=client,
    )

    with pytest.raises(ExternalModelTransportError) as error:
        _send(transport)
    assert error.value.attempts == 3
    assert client.calls == 3


def test_retry_backoff_does_not_hold_concurrency_slot() -> None:
    client = _SequencedClient(
        [
            _response(503),
            _response(200),
            _response(200),
        ]
    )
    gate = ExternalModelConcurrencyGate(limit=1, queue_timeout_seconds=0.2)
    sleep_started = threading.Event()
    release_sleep = threading.Event()

    def blocked_sleep(_delay: float) -> None:
        sleep_started.set()
        assert release_sleep.wait(timeout=1.0)

    transport = HttpxModelTransport(
        max_attempts=2,
        backoff_seconds=0.01,
        client=client,
        concurrency_gate=gate,
        sleep=blocked_sleep,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_send, transport)
        assert sleep_started.wait(timeout=1.0)
        second = executor.submit(_send, transport)
        # 第二个请求应在第一个请求退避时取得唯一槽位并完成。
        assert second.result(timeout=1.0).status_code == 200
        release_sleep.set()
        assert first.result(timeout=1.0).status_code == 200

    assert client.calls == 3
    assert gate.snapshot()["active"] == 0


def test_retry_policy_rejects_nonfinite_values() -> None:
    with pytest.raises(ValueError):
        HttpxModelTransport(backoff_seconds=float("nan"))
    with pytest.raises(ValueError):
        HttpxModelTransport(jitter_ratio=float("nan"))
    with pytest.raises(ValueError):
        HttpxModelTransport(max_backoff_seconds=float("inf"))
