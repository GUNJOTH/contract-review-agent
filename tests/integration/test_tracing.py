"""可选 OTEL 追踪的 no-op 与属性白名单测试。"""

from contract_review_app.config import settings
from contract_review_app.telemetry.tracing import _safe_attributes, start_span


def test_tracing_is_noop_by_default(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    with start_span("test.span", attributes={"status": "ok"}) as span:
        assert span is None


def test_tracing_attribute_allowlist_drops_content_and_secrets():
    safe = _safe_attributes(
        {
            "run_id": "run-1",
            "status": "ok",
            "prompt": "合同正文不应进入 span",
            "api_key": "secret",
            "finding_count": 2,
        }
    )
    assert safe == {"run_id": "run-1", "status": "ok", "finding_count": 2}
