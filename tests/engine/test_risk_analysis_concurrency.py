"""通读风险分析的分片并发：并发上限、合并顺序与失败兜底。

对照 ``test_semantic_concurrency.py``：两条判据都并发调用外部模型，共用同一个
"未经过更高档位压测验收"的 1～3 边界。差别在合并身份——语义判据合并的每条
响应天然携带 rule_id，风险分析的分片响应只带 request_fingerprint，因此合并必须
按分片下标而不是完成顺序，否则同一份合同的两次审查会得到不同的结果指纹。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pymupdf
import pytest

from contract_review import pipeline, run_review
from contract_review.audit import audit_result
from contract_review.models import (
    KnowledgeSourceKind,
    ReviewContext,
    RiskAnalysisItem,
    RiskAnalysisResponse,
    Rule,
    RuleBundle,
    SemanticReviewItem,
    SemanticReviewResponse,
)
from contract_review.pipeline import ReviewPipelineError
from contract_review.playbook import publish_playbook_bundle
from contract_review.risk_analysis import (
    RISK_ANALYSIS_RULE_CHUNK_SIZE,
    RiskAnalysisUnavailableError,
)
from contract_review_app.config import settings
from contract_review_app.services import review_service

RULE_COUNT = 32


def _rules() -> list[Rule]:
    return [
        Rule(
            rule_id=f"risk-{index}",
            version="v1",
            title=f"topic-{index}",
            category="并发测试",
            applies_to=["software"],
            check_method="semantic",
            source_snapshot=f"risk-rules#{index}",
        )
        for index in range(RULE_COUNT)
    ]


def _risk_fixture(tmp_path: Path) -> tuple[Path, RuleBundle, list[Rule]]:
    rules = _rules()
    bundle = publish_playbook_bundle(
        RuleBundle(
            bundle_id="risk-concurrency-rules-v1",
            source_filename="risk-concurrency-rules.xlsx",
            source_sha256="c" * 64,
            source_sheet="Sheet1",
            source_range="A1:C5",
            rules=rules,
        )
    )

    document_path = tmp_path / "risk-concurrency-contract.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    for index in range(RULE_COUNT):
        page.insert_text(
            (60, 80 + index * 20),
            f"topic-{index} 条款要求保留可核验的合同正文依据。",
        )
    document.save(str(document_path))
    document.close()
    return document_path, bundle, rules


def _baseline(document_path: Path, bundle: RuleBundle):
    return run_review(
        [document_path],
        package_id="pkg-risk-concurrency",
        rule_bundle=bundle,
        review_context=ReviewContext(contract_type="software"),
    )


def _chunk_sizes() -> list[int]:
    """分片长度：与定长切片保持一致（末片可能更短）。"""

    sizes: list[int] = []
    remaining = RULE_COUNT
    while remaining > 0:
        sizes.append(min(RISK_ANALYSIS_RULE_CHUNK_SIZE, remaining))
        remaining -= RISK_ANALYSIS_RULE_CHUNK_SIZE
    return sizes


def _response_for(request) -> RiskAnalysisResponse:
    return RiskAnalysisResponse(
        response_id=f"risk-response-{request.rule_hints[0]['rule_id']}",
        provider=request.provider,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        request_fingerprint=request.request_fingerprint,
        items=[
            RiskAnalysisItem(
                item_id=f"item-{hint['rule_id']}",
                title=hint["title"],
                risk_level="PASS",
                reason="并发测试判定：该条款满足规则要求。",
                quote="",
                evidence_ids=[],
                module="并发测试",
                rule_id=hint["rule_id"],
                confidence=0.8,
            )
            for hint in request.rule_hints
        ],
        contract_type={"name": "软件开发/转让服务", "basis": "测试夹具固定类型"},
    )


class _RecordingRiskClient:
    """记录在途峰值的假客户端；每个请求按规则提示回满判定。

    ``release_at`` 给出"并发实现正确时才会达到的在途数"：达不到就超时放行，
    让用例以"峰值不足"失败，而不是把测试挂死。
    """

    def __init__(self, *, release_at: int | None = None) -> None:
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._release_at = release_at
        self.active = 0
        self.max_active = 0
        self.requests: list = []

    def analyze(self, request) -> RiskAnalysisResponse:
        if self._release_at is None:
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.requests.append(request)
            try:
                return _response_for(request)
            finally:
                with self._lock:
                    self.active -= 1

        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.requests.append(request)
            if self.max_active >= self._release_at:
                self._release.set()
        self._release.wait(timeout=1.0)
        try:
            return _response_for(request)
        finally:
            with self._lock:
                self.active -= 1


class _BackpressuredOnceRiskClient:
    """指定分片首轮报"提供方不可用"，模拟并发下没抢到模型闸门槽位。"""

    def __init__(self, *, failing_rule_id: str) -> None:
        self._lock = threading.Lock()
        self._failing_rule_id = failing_rule_id
        self._seen: set[str] = set()
        self.requests: list = []

    def analyze(self, request) -> RiskAnalysisResponse:
        head_rule_id = request.rule_hints[0]["rule_id"]
        with self._lock:
            self.requests.append(request)
            first_call = head_rule_id not in self._seen
            self._seen.add(head_rule_id)
        if head_rule_id == self._failing_rule_id and first_call:
            raise RiskAnalysisUnavailableError(attempts=0)
        return _response_for(request)


class _BrokenRiskClient:
    """抛出非业务异常：程序缺陷不允许被降级成"这一片模型没答出来"。"""

    def analyze(self, request) -> RiskAnalysisResponse:
        raise ValueError("risk analysis client is broken")


def _contract_evidence_id(request) -> str | None:
    candidates = request.candidate_evidence_by_rule.get(request.rule_ids[0]) or []
    for candidate in candidates:
        if candidate.source_kind == KnowledgeSourceKind.CONTRACT:
            return candidate.evidence_ids[0]
    return None


class _StaticSemanticReviewer:
    """最小语义客户端：每条规则回 UNKNOWN，只用于把流水线跑通。"""

    def review(self, request) -> SemanticReviewResponse:
        rule_id = request.rule_ids[0]
        evidence_id = _contract_evidence_id(request)
        return SemanticReviewResponse(
            response_id=f"semantic-response-{rule_id}",
            provider=request.provider,
            model_version=request.model_version,
            prompt_version=request.prompt_version,
            request_fingerprint=request.request_fingerprint,
            items=[
                SemanticReviewItem(
                    rule_id=rule_id,
                    status="UNKNOWN",
                    reason="并发测试响应保持规则上下文隔离。",
                    evidence_ids=[evidence_id] if evidence_id else [],
                    confidence=0.0,
                )
            ],
        )


def test_risk_analysis_chunks_run_in_parallel_and_merge_in_chunk_order(
    tmp_path: Path,
) -> None:
    document_path, bundle, rules = _risk_fixture(tmp_path)
    baseline = _baseline(document_path, bundle)
    client = _RecordingRiskClient(release_at=3)

    result = pipeline._attach_risk_analysis(
        baseline,
        client=client,
        provider="test-provider",
        model_version="test-model",
        prompt_version="test-prompt",
        configuration=None,
        rules=rules,
        max_concurrency=3,
    )

    assert client.max_active == 3
    assert len(client.requests) == len(_chunk_sizes())
    assert sorted(len(request.rule_hints) for request in client.requests) == sorted(
        _chunk_sizes()
    )
    analysis = result.risk_analysis_response
    assert analysis is not None
    assert [item.rule_id for item in analysis.items] == [
        f"risk-{index}" for index in range(RULE_COUNT)
    ]
    coverage = result.run.configuration["risk_analysis"]["coverage"]
    assert coverage["max_concurrency"] == 3
    assert coverage["chunk_count"] == len(_chunk_sizes())
    assert "failed_chunks" not in coverage
    assert "serially_retried_chunks" not in coverage
    assert audit_result(result).passed


def test_single_concurrency_keeps_chunks_serial(tmp_path: Path) -> None:
    document_path, bundle, rules = _risk_fixture(tmp_path)
    baseline = _baseline(document_path, bundle)
    client = _RecordingRiskClient()

    result = pipeline._attach_risk_analysis(
        baseline,
        client=client,
        provider="test-provider",
        model_version="test-model",
        prompt_version="test-prompt",
        configuration=None,
        rules=rules,
        max_concurrency=1,
    )

    assert client.max_active == 1
    assert len(client.requests) == len(_chunk_sizes())
    assert result.risk_analysis_response is not None
    assert len(result.risk_analysis_response.items) == RULE_COUNT
    assert audit_result(result).passed


def test_chunk_without_slot_is_retried_serially(tmp_path: Path) -> None:
    document_path, bundle, rules = _risk_fixture(tmp_path)
    baseline = _baseline(document_path, bundle)
    # 第二片（首条规则 risk-15）首轮模拟"没抢到闸门槽位"。
    client = _BackpressuredOnceRiskClient(
        failing_rule_id=f"risk-{RISK_ANALYSIS_RULE_CHUNK_SIZE}"
    )

    result = pipeline._attach_risk_analysis(
        baseline,
        client=client,
        provider="test-provider",
        model_version="test-model",
        prompt_version="test-prompt",
        configuration=None,
        rules=rules,
        max_concurrency=2,
    )

    analysis = result.risk_analysis_response
    assert analysis is not None
    assert len(analysis.items) == RULE_COUNT
    coverage = result.run.configuration["risk_analysis"]["coverage"]
    assert coverage["serially_retried_chunks"] == [1]
    assert "failed_chunks" not in coverage
    assert audit_result(result).passed


def test_unexpected_failure_is_not_downgraded(tmp_path: Path) -> None:
    document_path, bundle, rules = _risk_fixture(tmp_path)
    baseline = _baseline(document_path, bundle)

    for max_concurrency in (1, 2):
        with pytest.raises(ValueError):
            pipeline._attach_risk_analysis(
                baseline,
                client=_BrokenRiskClient(),
                provider="test-provider",
                model_version="test-model",
                prompt_version="test-prompt",
                configuration=None,
                rules=rules,
                max_concurrency=max_concurrency,
            )


def test_risk_analysis_concurrency_rejects_unvalidated_values() -> None:
    assert pipeline.validate_risk_analysis_concurrency(1) == 1
    assert pipeline.validate_risk_analysis_concurrency(3) == 3

    for invalid in (0, 4, True, "2"):
        with pytest.raises(ReviewPipelineError):
            pipeline.validate_risk_analysis_concurrency(invalid)


def test_configuration_records_risk_analysis_concurrency(tmp_path: Path) -> None:
    document_path, bundle, _ = _risk_fixture(tmp_path)

    result = pipeline.run_review_with_semantic_client(
        [document_path],
        package_id="pkg-risk-concurrency-config",
        rule_bundle=bundle,
        client=_StaticSemanticReviewer(),
        provider="test-provider",
        model_version="test-model",
        prompt_version="test-prompt",
        review_context=ReviewContext(contract_type="software"),
        semantic_max_concurrency=2,
        risk_analysis_client=_RecordingRiskClient(release_at=2),
        risk_analysis_max_concurrency=2,
    )

    assert result.run.configuration["risk_analysis_max_concurrency"] == 2
    assert result.run.configuration["risk_analysis"]["coverage"]["max_concurrency"] == 2
    assert audit_result(result).passed

    with pytest.raises(ReviewPipelineError):
        pipeline.run_review_with_semantic_client(
            [document_path],
            package_id="pkg-risk-concurrency-config",
            rule_bundle=bundle,
            client=_StaticSemanticReviewer(),
            provider="test-provider",
            model_version="test-model",
            prompt_version="test-prompt",
            review_context=ReviewContext(contract_type="software"),
            risk_analysis_max_concurrency=4,
        )


def test_risk_analysis_client_opens_its_gate_for_chunk_concurrency(
    monkeypatch,
) -> None:
    """开启分片并发时闸门容量与排队窗口一起放开，且不牵动全局模型并发。"""

    monkeypatch.setattr(settings, "CONTRACT_RISK_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake-risk-gate-endpoint"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_QUEUE_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_TIMEOUT_SECONDS", 180.0)

    monkeypatch.setattr(settings, "CONTRACT_REVIEW_RISK_ANALYSIS_MAX_CONCURRENCY", 1)
    serial_client = review_service._risk_analysis_client()
    assert serial_client is not None
    serial_gate = serial_client._transport._concurrency_gate
    assert serial_gate.limit == 1
    assert serial_gate.queue_timeout_seconds == 30.0
    serial_client.close()

    monkeypatch.setattr(settings, "CONTRACT_REVIEW_RISK_ANALYSIS_MAX_CONCURRENCY", 3)
    parallel_client = review_service._risk_analysis_client()
    assert parallel_client is not None
    parallel_gate = parallel_client._transport._concurrency_gate
    assert parallel_gate.limit == 3
    assert parallel_gate.queue_timeout_seconds == 180.0
    parallel_client.close()
