"""可选的 OpenTelemetry 链路追踪。

应用不依赖 OTEL SDK 才能运行。部署时安装 ``contract-review-app[otel]`` 后，
可在宿主进程配置 provider/exporter；本模块只创建 span，不把合同正文、提示词、
凭据或模型响应写入 span 属性。
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from typing import Any

from loguru import logger

from contract_review_app.config import settings

_WARNED_MISSING = False
_SAFE_ATTRIBUTES = {
    "task_id",
    "run_id",
    "request_id",
    "stage",
    "status",
    "provider",
    "model_version",
    "rule_count",
    "context_count",
    "item_count",
    "finding_count",
    "blocked",
    "decision",
    "source",
}


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in _SAFE_ATTRIBUTES or value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            safe[key] = value
    return safe


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any | None]:
    """启用 OTEL 时返回活动 span；未启用或 SDK 缺失时返回 ``None``。"""

    global _WARNED_MISSING
    if not settings.OTEL_ENABLED:
        yield None
        return
    try:
        from opentelemetry import trace
    except ModuleNotFoundError:
        if not _WARNED_MISSING:
            logger.warning(
                "OTEL_ENABLED=true 但未安装 opentelemetry-api；追踪已降级为 no-op。"
            )
            _WARNED_MISSING = True
        yield None
        return

    tracer = trace.get_tracer(settings.OTEL_SERVICE_NAME)
    with tracer.start_as_current_span(name, attributes=_safe_attributes(attributes)) as span:
        try:
            yield span
        except Exception as exc:
            # 只记录异常类型；异常消息可能包含文档片段或供应商响应。
            try:
                span.set_attribute("error.type", type(exc).__name__)
            except Exception:  # pragma: no cover - SDK implementation detail
                pass
            raise


def set_span_attributes(span: Any | None, attributes: Mapping[str, Any]) -> None:
    """向可选 span 写入白名单属性，追踪失败不影响业务流程。"""

    if span is None:
        return
    for key, value in _safe_attributes(attributes).items():
        try:
            span.set_attribute(key, value)
        except Exception:  # pragma: no cover - SDK implementation detail
            continue
