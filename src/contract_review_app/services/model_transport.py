"""外部模型 HTTP 传输边界。

该模块只处理连接层的不稳定性：复用 HTTP 连接，并对明确的传输错误和
临时 HTTP 状态做有限次数重试。不可重试的 HTTP 业务错误、响应结构错误
和模型证据错误交由上层分别处理，不能通过重试把它们伪装成传输问题。
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from collections.abc import Callable, Mapping
from collections.abc import Iterator
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx
from loguru import logger


MODEL_CONCURRENCY_HARD_LIMIT = 3
CIRCUIT_FAILURE_THRESHOLD_HARD_LIMIT = 100
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class ExternalModelTransportError(RuntimeError):
    """外部模型请求在有限重试后仍未完成传输。"""

    def __init__(self, *, operation: str, attempts: int) -> None:
        self.operation = operation
        self.attempts = attempts
        super().__init__(
            f"external model transport failed for {operation} after {attempts} attempts"
        )


class ExternalModelBackpressureError(ExternalModelTransportError):
    """模型并发闸门在有界等待内没有获得执行槽位。"""

    def __init__(self, *, operation: str, wait_timeout_seconds: float) -> None:
        self.wait_timeout_seconds = wait_timeout_seconds
        super().__init__(operation=operation, attempts=0)
        self.args = (
            "external model concurrency limit reached for "
            f"{operation} after waiting {wait_timeout_seconds:.3f} seconds",
        )


def validate_model_concurrency(value: int) -> int:
    """校验模型级并发上限，阶段 2 只开放已验证的 1～3 档。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("model concurrency must be an integer")
    if not 1 <= value <= MODEL_CONCURRENCY_HARD_LIMIT:
        raise ValueError(
            "model concurrency must be between 1 and "
            f"{MODEL_CONCURRENCY_HARD_LIMIT}"
        )
    return value


def validate_model_queue_timeout(value: float) -> float:
    """校验模型并发等待时间，禁止无界等待。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("model queue timeout must be a number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise ValueError("model queue timeout must be positive")
    return numeric_value


def validate_retry_jitter_ratio(value: float) -> float:
    """校验指数退避抖动比例，确保退避时间仍然有界。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("retry jitter ratio must be a number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or not 0 <= numeric_value <= 1:
        raise ValueError("retry jitter ratio must be between 0 and 1")
    return numeric_value


def validate_max_backoff_seconds(value: float) -> float:
    """校验单次重试退避上限，禁止无限等待。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max backoff seconds must be a number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise ValueError("max backoff seconds must be positive")
    return numeric_value


def validate_circuit_failure_threshold(value: int) -> int:
    """校验熔断连续失败阈值，避免误配成零或无界值。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("circuit failure threshold must be an integer")
    if not 1 <= value <= CIRCUIT_FAILURE_THRESHOLD_HARD_LIMIT:
        raise ValueError(
            "circuit failure threshold must be between 1 and "
            f"{CIRCUIT_FAILURE_THRESHOLD_HARD_LIMIT}"
        )
    return value


class ExternalModelCircuitOpenError(ExternalModelTransportError):
    """外部模型熔断期间拒绝新的请求。"""

    def __init__(
        self,
        *,
        operation: str,
        retry_after_seconds: float,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(operation=operation, attempts=0)
        self.args = (
            f"external model circuit is open for {operation}; "
            f"retry after {retry_after_seconds:.3f} seconds",
        )


class AdaptiveConcurrencyController:
    """按最近模型调用窗口选择下一次审查的规则级并发。"""

    def __init__(
        self,
        *,
        min_limit: int,
        initial_limit: int,
        max_limit: int,
        target_latency_seconds: float,
        success_window: int = 4,
    ) -> None:
        min_limit = validate_model_concurrency(min_limit)
        initial_limit = validate_model_concurrency(initial_limit)
        max_limit = validate_model_concurrency(max_limit)
        if not min_limit <= initial_limit <= max_limit:
            raise ValueError("adaptive concurrency limits must be ordered")
        target_latency_seconds = validate_max_backoff_seconds(
            target_latency_seconds
        )
        if isinstance(success_window, bool) or not isinstance(success_window, int):
            raise ValueError("adaptive success window must be an integer")
        if success_window < 1:
            raise ValueError("adaptive success window must be positive")
        self._min_limit = min_limit
        self._initial_limit = initial_limit
        self._max_limit = max_limit
        self._target_latency_seconds = target_latency_seconds
        self._success_window = success_window
        self._lock = threading.Lock()
        self._current_limit = initial_limit
        self._recent_latencies: deque[float] = deque(maxlen=success_window)
        self._successes_since_change = 0
        self._cooldown_remaining = 0
        self._observations = 0

    @property
    def current_limit(self) -> int:
        """返回下一次审查应使用的规则级并发。"""

        with self._lock:
            return self._current_limit

    def observe(self, *, latency_seconds: float, success: bool) -> None:
        """根据一次完整模型调用结果更新下一窗口的并发建议。"""

        if (
            isinstance(latency_seconds, bool)
            or not isinstance(latency_seconds, (int, float))
            or not math.isfinite(float(latency_seconds))
            or latency_seconds < 0
        ):
            return
        with self._lock:
            self._observations += 1
            if not success:
                self._decrease_locked()
                return
            self._recent_latencies.append(float(latency_seconds))
            if self._p95_locked() > self._target_latency_seconds:
                self._decrease_locked()
                return
            if self._cooldown_remaining:
                self._cooldown_remaining -= 1
                return
            self._successes_since_change += 1
            if (
                self._successes_since_change >= self._success_window
                and self._current_limit < self._max_limit
            ):
                self._current_limit += 1
                self._successes_since_change = 0
                self._recent_latencies.clear()
                self._cooldown_remaining = self._success_window

    def snapshot(self) -> dict[str, int | float]:
        """返回不含正文、凭据和完整时间序列的控制器状态。"""

        with self._lock:
            return {
                "min_limit": self._min_limit,
                "initial_limit": self._initial_limit,
                "current_limit": self._current_limit,
                "max_limit": self._max_limit,
                "target_latency_seconds": self._target_latency_seconds,
                "success_window": self._success_window,
                "cooldown_remaining": self._cooldown_remaining,
                "observations": self._observations,
            }

    def _p95_locked(self) -> float:
        if not self._recent_latencies:
            return 0.0
        ordered = sorted(self._recent_latencies)
        index = min(
            len(ordered) - 1,
            max(0, math.ceil(len(ordered) * 0.95) - 1),
        )
        return ordered[index]

    def _decrease_locked(self) -> None:
        self._current_limit = max(self._min_limit, self._current_limit - 1)
        self._successes_since_change = 0
        self._recent_latencies.clear()
        self._cooldown_remaining = self._success_window


class ExternalModelCircuitBreaker:
    """按连续临时失败控制 closed/open/half-open 三态。"""

    def __init__(
        self,
        *,
        failure_threshold: int,
        open_timeout_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = validate_circuit_failure_threshold(
            failure_threshold
        )
        self._open_timeout_seconds = validate_model_queue_timeout(
            open_timeout_seconds
        )
        self._now = now
        self._lock = threading.Lock()
        self._state = "closed"
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._generation = 0

    def before_call(self, *, operation: str) -> tuple[int, bool]:
        """在调用前检查熔断状态并返回本次调用令牌。"""

        with self._lock:
            if self._state == "open":
                assert self._opened_at is not None
                elapsed = max(0.0, self._now() - self._opened_at)
                if elapsed < self._open_timeout_seconds:
                    raise ExternalModelCircuitOpenError(
                        operation=operation,
                        retry_after_seconds=self._open_timeout_seconds - elapsed,
                    )
                self._state = "half_open"
                self._generation += 1
                self._probe_in_flight = False
            if self._state == "half_open":
                if self._probe_in_flight:
                    raise ExternalModelCircuitOpenError(
                        operation=operation,
                        retry_after_seconds=0.0,
                    )
                self._probe_in_flight = True
                return self._generation, True
            return self._generation, False

    def record_success(self, permit: tuple[int, bool]) -> None:
        """记录 provider 已返回的成功或非临时业务响应。"""

        generation, is_probe = permit
        with self._lock:
            if generation != self._generation:
                return
            if is_probe and self._state == "half_open":
                self._state = "closed"
                self._failure_count = 0
                self._opened_at = None
                self._probe_in_flight = False
            elif self._state == "closed":
                self._failure_count = 0

    def record_failure(self, permit: tuple[int, bool]) -> None:
        """记录一次已耗尽重试的临时 provider 失败。"""

        generation, is_probe = permit
        with self._lock:
            if generation != self._generation:
                return
            if is_probe and self._state == "half_open":
                self._open_locked()
                return
            if self._state != "closed":
                return
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_locked()

    def abort(self, permit: tuple[int, bool]) -> None:
        """调用未触达 provider 时释放 half-open 探针，不伪造失败。"""

        generation, is_probe = permit
        if not is_probe:
            return
        with self._lock:
            if generation == self._generation and self._state == "half_open":
                self._probe_in_flight = False

    def snapshot(self) -> dict[str, int | float | str | bool]:
        """返回熔断状态摘要，不包含端点、正文和凭据。"""

        with self._lock:
            remaining = 0.0
            if self._state == "open" and self._opened_at is not None:
                remaining = max(
                    0.0,
                    self._open_timeout_seconds
                    - max(0.0, self._now() - self._opened_at),
                )
            return {
                "state": self._state,
                "failure_count": self._failure_count,
                "failure_threshold": self._failure_threshold,
                "open_timeout_seconds": self._open_timeout_seconds,
                "open_remaining_seconds": remaining,
                "probe_in_flight": self._probe_in_flight,
            }

    def _open_locked(self) -> None:
        self._state = "open"
        self._failure_count = self._failure_threshold
        self._opened_at = self._now()
        self._probe_in_flight = False
        self._generation += 1


def _retry_after_seconds(
    response: httpx.Response,
    *,
    now: Callable[[], float],
) -> float | None:
    """解析 Retry-After 的秒数或 HTTP 日期，非法值返回 None。"""

    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value.strip())
    except (AttributeError, ValueError):
        seconds = None
    if seconds is not None:
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
        return None
    try:
        retry_at = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, retry_at.timestamp() - now())


class ExternalModelConcurrencyGate:
    """进程内共享的模型并发闸门，按物理 HTTP 尝试占用执行槽位。"""

    def __init__(self, *, limit: int, queue_timeout_seconds: float) -> None:
        self._limit = validate_model_concurrency(limit)
        self._queue_timeout_seconds = validate_model_queue_timeout(
            queue_timeout_seconds
        )
        self._state_lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(self._limit)
        self._active = 0
        self._waiting = 0

    @property
    def limit(self) -> int:
        """返回当前闸门容量。"""

        with self._state_lock:
            return self._limit

    @property
    def queue_timeout_seconds(self) -> float:
        """返回当前有界等待时长。"""

        with self._state_lock:
            return self._queue_timeout_seconds

    def update_policy(self, *, limit: int, queue_timeout_seconds: float) -> None:
        """在没有活动请求或等待者时更新闸门配置。"""

        limit = validate_model_concurrency(limit)
        queue_timeout_seconds = validate_model_queue_timeout(queue_timeout_seconds)
        with self._state_lock:
            if (
                self._limit == limit
                and self._queue_timeout_seconds == queue_timeout_seconds
            ):
                return
            if self._active or self._waiting:
                raise RuntimeError(
                    "model concurrency policy cannot change while requests are active"
                )
            self._limit = limit
            self._queue_timeout_seconds = queue_timeout_seconds
            self._semaphore = threading.BoundedSemaphore(limit)

    def snapshot(self) -> dict[str, int | float]:
        """返回不含正文和凭据的闸门状态，供观测和隔离测试使用。"""

        with self._state_lock:
            return {
                "limit": self._limit,
                "queue_timeout_seconds": self._queue_timeout_seconds,
                "active": self._active,
                "waiting": self._waiting,
            }

    @contextmanager
    def slot(
        self,
        *,
        operation: str,
        request_timeout_seconds: float | None,
    ) -> Iterator[None]:
        """在有界时间内取得一个 HTTP 执行槽位。"""

        with self._state_lock:
            wait_timeout = self._queue_timeout_seconds
            if request_timeout_seconds is not None:
                wait_timeout = min(
                    wait_timeout,
                    max(0.0, float(request_timeout_seconds)),
                )
            self._waiting += 1
        try:
            acquired = self._semaphore.acquire(timeout=wait_timeout)
        finally:
            with self._state_lock:
                self._waiting -= 1
        if not acquired:
            raise ExternalModelBackpressureError(
                operation=operation,
                wait_timeout_seconds=wait_timeout,
            )
        with self._state_lock:
            self._active += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._active -= 1
            self._semaphore.release()


_SHARED_GATES: dict[tuple[str, str, str], ExternalModelConcurrencyGate] = {}
_SHARED_GATES_LOCK = threading.Lock()
_SHARED_ADAPTIVE_CONTROLLERS: dict[
    tuple[str, str, str], AdaptiveConcurrencyController
] = {}
_SHARED_ADAPTIVE_LOCK = threading.Lock()
_SHARED_CIRCUIT_BREAKERS: dict[
    tuple[str, str, str], ExternalModelCircuitBreaker
] = {}
_SHARED_CIRCUIT_BREAKERS_LOCK = threading.Lock()


def shared_model_concurrency_gate(
    *,
    operation: str,
    endpoint: str,
    model: str,
    limit: int,
    queue_timeout_seconds: float,
) -> ExternalModelConcurrencyGate:
    """按操作、端点和模型复用进程内并发闸门，不把密钥纳入身份。"""

    limit = validate_model_concurrency(limit)
    queue_timeout_seconds = validate_model_queue_timeout(queue_timeout_seconds)
    key = (operation, endpoint.rstrip("/"), model)
    with _SHARED_GATES_LOCK:
        gate = _SHARED_GATES.get(key)
        if gate is None:
            gate = ExternalModelConcurrencyGate(
                limit=limit,
                queue_timeout_seconds=queue_timeout_seconds,
            )
            _SHARED_GATES[key] = gate
        else:
            gate.update_policy(
                limit=limit,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        return gate


def shared_adaptive_concurrency_controller(
    *,
    operation: str,
    endpoint: str,
    model: str,
    min_limit: int,
    initial_limit: int,
    max_limit: int,
    target_latency_seconds: float,
    success_window: int,
) -> AdaptiveConcurrencyController:
    """按操作、端点和模型复用自适应控制器，不把密钥纳入身份。"""

    key = (operation, endpoint.rstrip("/"), model)
    with _SHARED_ADAPTIVE_LOCK:
        controller = _SHARED_ADAPTIVE_CONTROLLERS.get(key)
        if controller is None:
            controller = AdaptiveConcurrencyController(
                min_limit=min_limit,
                initial_limit=initial_limit,
                max_limit=max_limit,
                target_latency_seconds=target_latency_seconds,
                success_window=success_window,
            )
            _SHARED_ADAPTIVE_CONTROLLERS[key] = controller
        return controller


def shared_model_circuit_breaker(
    *,
    operation: str,
    endpoint: str,
    model: str,
    failure_threshold: int,
    open_timeout_seconds: float,
) -> ExternalModelCircuitBreaker:
    """按操作、端点和模型复用熔断器，不把密钥纳入身份。"""

    key = (operation, endpoint.rstrip("/"), model)
    with _SHARED_CIRCUIT_BREAKERS_LOCK:
        breaker = _SHARED_CIRCUIT_BREAKERS.get(key)
        if breaker is None:
            breaker = ExternalModelCircuitBreaker(
                failure_threshold=failure_threshold,
                open_timeout_seconds=open_timeout_seconds,
            )
            _SHARED_CIRCUIT_BREAKERS[key] = breaker
        return breaker


class HttpxModelTransport:
    """复用连接并为外部模型 POST 提供有限传输重试。"""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        backoff_seconds: float = 0.25,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        concurrency_gate: ExternalModelConcurrencyGate | None = None,
        jitter_ratio: float = 0.2,
        max_backoff_seconds: float = 30.0,
        random_value: Callable[[], float] = random.random,
        now: Callable[[], float] = time.time,
        adaptive_controller: AdaptiveConcurrencyController | None = None,
        circuit_breaker: ExternalModelCircuitBreaker | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("model transport max_attempts must be positive")
        if not math.isfinite(backoff_seconds) or backoff_seconds < 0:
            raise ValueError(
                "model transport backoff_seconds must be finite and non-negative"
            )
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._sleep = sleep
        self._concurrency_gate = concurrency_gate
        self.jitter_ratio = validate_retry_jitter_ratio(jitter_ratio)
        self.max_backoff_seconds = validate_max_backoff_seconds(
            max_backoff_seconds
        )
        self._random_value = random_value
        self._now = now
        self._adaptive_controller = adaptive_controller
        self._circuit_breaker = circuit_breaker

    def post_json(
        self,
        endpoint: str,
        *,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
        operation: str,
    ) -> httpx.Response:
        """发送 JSON POST；只对明确的临时错误进入有限重试。"""

        last_error: BaseException | None = None
        started_at = time.monotonic()
        circuit_permit = None
        circuit_finalized = False
        if self._circuit_breaker is not None:
            circuit_permit = self._circuit_breaker.before_call(operation=operation)
        try:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    slot = (
                        self._concurrency_gate.slot(
                            operation=operation,
                            request_timeout_seconds=timeout,
                        )
                        if self._concurrency_gate is not None
                        else nullcontext()
                    )
                    with slot:
                        response = self._client.post(
                            endpoint,
                            json=payload,
                            headers=headers,
                            timeout=timeout,
                        )
                        # 只有下面白名单中的临时 HTTP 状态会进入有限重试。
                        response.raise_for_status()
                        if self._circuit_breaker is not None:
                            assert circuit_permit is not None
                            self._circuit_breaker.record_success(circuit_permit)
                            circuit_finalized = True
                        self._observe(started_at, success=True)
                        return response
                except ExternalModelBackpressureError:
                    self._observe(started_at, success=False)
                    raise
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    if status_code not in RETRYABLE_HTTP_STATUS_CODES:
                        if self._circuit_breaker is not None:
                            assert circuit_permit is not None
                            self._circuit_breaker.record_success(circuit_permit)
                            circuit_finalized = True
                        raise
                    if attempt >= self.max_attempts:
                        if self._circuit_breaker is not None:
                            assert circuit_permit is not None
                            self._circuit_breaker.record_failure(circuit_permit)
                            circuit_finalized = True
                        self._observe(started_at, success=False)
                        raise ExternalModelTransportError(
                            operation=operation,
                            attempts=attempt,
                        ) from exc
                    last_error = exc
                    delay = self._retry_delay(attempt, response=exc.response)
                    self._log_retry(
                        operation=operation,
                        attempt=attempt,
                        error_type=f"HTTP_{status_code}",
                        delay=delay,
                    )
                    if delay:
                        self._sleep(delay)
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ) as exc:
                    last_error = exc
                    if attempt >= self.max_attempts:
                        if self._circuit_breaker is not None:
                            assert circuit_permit is not None
                            self._circuit_breaker.record_failure(circuit_permit)
                            circuit_finalized = True
                        self._observe(started_at, success=False)
                        raise ExternalModelTransportError(
                            operation=operation,
                            attempts=attempt,
                        ) from exc
                    delay = self._retry_delay(attempt)
                    self._log_retry(
                        operation=operation,
                        attempt=attempt,
                        error_type=type(exc).__name__,
                        delay=delay,
                    )
                    if delay:
                        self._sleep(delay)
        finally:
            if (
                self._circuit_breaker is not None
                and circuit_permit is not None
                and not circuit_finalized
            ):
                self._circuit_breaker.abort(circuit_permit)

        # 循环必定在成功或异常时结束，保留显式保护以避免未来改动产生静默返回。
        raise ExternalModelTransportError(
            operation=operation,
            attempts=self.max_attempts,
        ) from last_error

    def _observe(self, started_at: float, *, success: bool) -> None:
        """把一次完整逻辑请求的耗时和结果交给自适应控制器。"""

        if self._adaptive_controller is not None:
            self._adaptive_controller.observe(
                latency_seconds=time.monotonic() - started_at,
                success=success,
            )

    def _retry_delay(
        self,
        attempt: int,
        *,
        response: httpx.Response | None = None,
    ) -> float:
        """按 Retry-After 或有抖动的指数退避计算下一次等待。"""

        if response is not None:
            retry_after = _retry_after_seconds(response, now=self._now)
            if retry_after is not None:
                return min(self.max_backoff_seconds, retry_after)
        exponential = min(
            self.max_backoff_seconds,
            self.backoff_seconds * (2 ** (attempt - 1)),
        )
        if exponential <= 0:
            return 0.0
        jitter = exponential * self.jitter_ratio * self._random_value()
        return min(self.max_backoff_seconds, exponential + max(0.0, jitter))

    def _log_retry(
        self,
        *,
        operation: str,
        attempt: int,
        error_type: str,
        delay: float,
    ) -> None:
        """记录不含端点、正文和凭据的重试摘要。"""

        logger.warning(
            "外部模型请求准备重试：operation={} attempt={}/{} "
            "error_type={} delay_seconds={:.3f}",
            operation,
            attempt,
            self.max_attempts,
            error_type,
            delay,
        )

    def close(self) -> None:
        """关闭本传输实例创建的连接池；外部注入的 client 由调用方管理。"""

        if self._owns_client:
            self._client.close()
