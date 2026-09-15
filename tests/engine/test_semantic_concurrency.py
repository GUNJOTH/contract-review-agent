from __future__ import annotations

import threading
from pathlib import Path

import pymupdf
import pytest

from contract_review import run_review
from contract_review.audit import audit_result
from contract_review.models import (
    KnowledgeSourceKind,
    Rule,
    RuleBundle,
    ReviewContext,
    ReviewStatus,
    SemanticReviewItem,
    SemanticReviewResponse,
)
from contract_review.pipeline import (
    ReviewPipelineError,
    replay_review,
    run_review_with_semantic_client,
    validate_semantic_rule_concurrency,
)
from contract_review.playbook import publish_playbook_bundle
from contract_review.ocr import StaticOCRProvider
from contract_review.semantic import (
    SemanticClientError,
    SemanticProviderUnavailableError,
)
from contract_review_app.config import settings
from contract_review_app.services import review_service


def _semantic_fixture(tmp_path: Path, rule_count: int = 4) -> tuple[Path, RuleBundle]:
    rules = [
        Rule(
            rule_id=f"semantic-{index}",
            version="v1",
            title=f"topic-{index}",
            category="并发测试",
            applies_to=["software"],
            check_method="semantic",
            source_snapshot=f"concurrency-rules#{index}",
        )
        for index in range(rule_count)
    ]
    bundle = publish_playbook_bundle(
        RuleBundle(
            bundle_id="semantic-concurrency-rules-v1",
            source_filename="semantic-concurrency-rules.xlsx",
            source_sha256="c" * 64,
            source_sheet="Sheet1",
            source_range="A1:C5",
            rules=rules,
        )
    )

    document_path = tmp_path / "semantic-concurrency-contract.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    for index in range(rule_count):
        page.insert_text(
            (60, 80 + index * 30),
            f"topic-{index} 条款要求保留可核验的合同正文依据。",
        )
    document.save(str(document_path))
    document.close()
    return document_path, bundle


def _contract_evidence_id(request) -> str:
    rule_id = request.rule_ids[0]
    candidate = next(
        candidate
        for candidate in request.candidate_evidence_by_rule[rule_id]
        if candidate.source_kind == KnowledgeSourceKind.CONTRACT
    )
    return candidate.evidence_ids[0]


def _response_for_request(
    request,
    *,
    evidence_ids: list[str] | None = None,
) -> SemanticReviewResponse:
    rule_id = request.rule_ids[0]
    return SemanticReviewResponse(
        response_id=f"response-{rule_id}",
        provider=request.provider,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        request_fingerprint=request.request_fingerprint,
        items=[
            SemanticReviewItem(
                rule_id=rule_id,
                status="UNKNOWN",
                reason="并发测试响应保持规则上下文隔离。",
                evidence_ids=(
                    [_contract_evidence_id(request)]
                    if evidence_ids is None
                    else evidence_ids
                ),
                confidence=0.0,
            )
        ],
    )


class _RecordingSemanticReviewer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.requests = []

    def review(self, request):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.requests.append(request)
            if self.max_active >= 3:
                self._release.set()
        # 只有达到三个在途任务才立即放行；并发实现错误时超时放行，避免测试死锁。
        self._release.wait(timeout=1.0)
        try:
            return _response_for_request(request)
        finally:
            with self._lock:
                self.active -= 1


def test_rule_semantic_calls_are_bounded_and_merged_in_request_order(
    tmp_path: Path,
) -> None:
    document_path, bundle = _semantic_fixture(tmp_path)
    reviewer = _RecordingSemanticReviewer()

    result = run_review_with_semantic_client(
        [document_path],
        package_id="pkg-semantic-concurrency",
        rule_bundle=bundle,
        client=reviewer,
        provider="test-provider",
        model_version="test-model-v1",
        prompt_version="test-prompt-v1",
        review_context=ReviewContext(contract_type="software"),
        semantic_max_concurrency=3,
    )

    assert reviewer.max_active == 3
    assert len(reviewer.requests) == 4
    assert all(len(request.rule_ids) == 1 for request in reviewer.requests)
    assert all(
        set(request.candidate_evidence_by_rule) == set(request.rule_ids)
        and set(request.retrieval_queries_by_rule) == set(request.rule_ids)
        for request in reviewer.requests
    )
    assert result.semantic_request is not None
    assert result.semantic_response is not None
    assert [item.rule_id for item in result.semantic_response.items] == (
        result.semantic_request.rule_ids
    )
    assert result.run.configuration["semantic_rule_max_concurrency"] == 3
    assert audit_result(result).passed
    assert (
        replay_review(result, [document_path], rule_bundle=bundle)
        .run.result_fingerprint
        == result.run.result_fingerprint
    )


def test_context_error_does_not_merge_partial_semantic_results(
    tmp_path: Path,
) -> None:
    document_path, bundle = _semantic_fixture(tmp_path, rule_count=3)

    class _OutOfContextReviewer:
        def review(self, request):
            if request.rule_ids[0] == "semantic-1":
                return _response_for_request(
                    request,
                    evidence_ids=["forged-evidence-id"],
                )
            return _response_for_request(request)

    result = run_review_with_semantic_client(
        [document_path],
        package_id="pkg-semantic-concurrency-fallback",
        rule_bundle=bundle,
        client=_OutOfContextReviewer(),
        provider="test-provider",
        model_version="test-model-v1",
        prompt_version="test-prompt-v1",
        review_context=ReviewContext(contract_type="software"),
        semantic_max_concurrency=3,
    )

    assert result.semantic_request is None
    assert result.semantic_response is None
    assert result.run.status == ReviewStatus.HUMAN_REVIEW
    assert result.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "evidence_outside_context",
    }
    assert audit_result(result).passed
    assert (
        replay_review(result, [document_path], rule_bundle=bundle)
        .run.result_fingerprint
        == result.run.result_fingerprint
    )


def test_parallel_generic_client_error_is_not_hidden_by_provider_fallback(
    tmp_path: Path,
) -> None:
    document_path, bundle = _semantic_fixture(tmp_path, rule_count=2)
    provider_failed = threading.Event()

    class _MixedFailureReviewer:
        def review(self, request):
            if request.rule_ids[0] == "semantic-0":
                provider_failed.set()
                raise SemanticProviderUnavailableError(attempts=5)
            provider_failed.wait(timeout=1.0)
            raise SemanticClientError("provider returned invalid JSON")

    with pytest.raises(SemanticClientError, match="invalid JSON"):
        run_review_with_semantic_client(
            [document_path],
            package_id="pkg-semantic-concurrency-mixed-failure",
            rule_bundle=bundle,
            client=_MixedFailureReviewer(),
            provider="test-provider",
            model_version="test-model-v1",
            prompt_version="test-prompt-v1",
            review_context=ReviewContext(contract_type="software"),
            semantic_max_concurrency=2,
        )


def test_semantic_rule_concurrency_rejects_unvalidated_values() -> None:
    assert validate_semantic_rule_concurrency(1) == 1
    assert validate_semantic_rule_concurrency(3) == 3

    with pytest.raises(ReviewPipelineError):
        validate_semantic_rule_concurrency(0)
    with pytest.raises(ReviewPipelineError):
        validate_semantic_rule_concurrency(4)
    with pytest.raises(ReviewPipelineError):
        validate_semantic_rule_concurrency(True)


def test_application_wires_rule_concurrency_without_enabling_external_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((60, 80), "软件开发合同应保留可审计的交付条款。")
    pdf_bytes = document.tobytes()
    document.close()

    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY", 3)
    monkeypatch.setattr(settings, "CONTRACT_SEAL_DETECTION_ENABLED", False)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_CACHE_DIR",
        str(tmp_path / "review-cache"),
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_RESULT_STORE_DIR",
        str(tmp_path / "review-results"),
    )

    captured: dict[str, object] = {}

    class _NoopSemanticClient:
        def close(self) -> None:
            pass

    def fake_semantic_client():
        return _NoopSemanticClient()

    def fake_semantic_pipeline(paths, **kwargs):
        captured.update(kwargs)
        return run_review(
            paths,
            package_id=kwargs["package_id"],
            rule_bundle=kwargs["rule_bundle"],
            review_context=kwargs["review_context"],
            document_precedence=kwargs["document_precedence"],
            document_kinds=kwargs["document_kinds"],
            document_filenames=kwargs["document_filenames"],
            ocr_provider=kwargs["ocr_provider"],
            extra_evidence=kwargs["extra_evidence"],
            knowledge_index_factory=kwargs["knowledge_index_factory"],
            retrieval_top_k=kwargs["retrieval_top_k"],
            configuration=kwargs["configuration"],
        )

    monkeypatch.setattr(review_service, "_semantic_client", fake_semantic_client)
    monkeypatch.setattr(
        review_service,
        "run_review_with_semantic_client",
        fake_semantic_pipeline,
    )

    result = review_service.run_contract_review(
        [("contract.pdf", pdf_bytes)],
        package_id="pkg-semantic-concurrency-wiring",
        review_context=ReviewContext(contract_type="software"),
        ocr_provider=StaticOCRProvider({}),
    )

    assert captured["semantic_max_concurrency"] == 3
    assert result.run.configuration["semantic_rule_max_concurrency"] == 3
    assert audit_result(result).passed
