"""应用服务对越界语义证据的降级回归测试。"""

import pymupdf

from contract_review import (
    parse_contract_package,
    publish_playbook_bundle,
    run_review,
)
from contract_review.audit import audit_result
from contract_review.models import (
    Evidence,
    EvidenceType,
    KnowledgeSourceKind,
    ReviewContext,
    Rule,
    RuleBundle,
    SemanticReviewItem,
    SemanticReviewResponse,
    SourceLocator,
)
from contract_review.semantic import contract_evidence_ids_by_rule
from contract_review_app.config import settings
from contract_review_app.services import review_service
from contract_review_app.services.review_result_store import (
    load_authoritative_review_result,
)
from contract_review_app.services.review_service import ReviewExecution

OUT_OF_CONTEXT_EVIDENCE_ID = "known-but-outside-semantic-context"


def _make_contract_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "甲方与乙方签订软件开发合同。合同完整性（无空白等），合同范围明确；"
        "乙方负责程序交付，合同金额为一百万元。",
        fontname="china-s",
    )
    data = document.tobytes()
    document.close()
    return data


class _OutOfContextSemanticReviewer:
    """返回已知但不属于目标规则候选集的证据，模拟模型越界引用。"""

    def __init__(self) -> None:
        self.calls = 0

    def review(self, request):
        self.calls += 1
        evidence_by_rule = contract_evidence_ids_by_rule(
            request.candidate_evidence_by_rule
        )
        target_rule_id = request.rule_ids[0]
        assert OUT_OF_CONTEXT_EVIDENCE_ID not in set(
            evidence_by_rule[target_rule_id]
        )
        items = []
        for rule_id in request.rule_ids:
            evidence_ids = (
                [OUT_OF_CONTEXT_EVIDENCE_ID]
                if rule_id == target_rule_id
                else [evidence_by_rule[rule_id][0]]
            )
            items.append(
                SemanticReviewItem(
                    rule_id=rule_id,
                    status="UNKNOWN",
                    reason="模拟模型返回的结构化审查结果。",
                    evidence_ids=evidence_ids,
                    confidence=0.0,
                )
            )
        return SemanticReviewResponse(
            response_id=f"response-outside-context-{self.calls}",
            provider=request.provider,
            model_version=request.model_version,
            prompt_version=request.prompt_version,
            request_fingerprint=request.request_fingerprint,
            items=items,
        )


def test_upload_review_degrades_without_persisting_out_of_context_response(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        type("EmptyOCR", (), {"recognize_seal": lambda *_args, **_kwargs: None})(),
    )
    semantic_reviewer = _OutOfContextSemanticReviewer()
    monkeypatch.setattr(review_service, "_semantic_client", lambda: semantic_reviewer)
    content = _make_contract_pdf()
    source_path = tmp_path / "合同主文.pdf"
    source_path.write_bytes(content)
    _, parsed_documents = parse_contract_package(
        [source_path],
        package_id="pkg-semantic-context-fallback",
    )
    document = parsed_documents[0].document
    out_of_context_evidence = Evidence(
        evidence_id=OUT_OF_CONTEXT_EVIDENCE_ID,
        evidence_type=EvidenceType.TEXT,
        package_id="pkg-semantic-context-fallback",
        document_id=document.document_id,
        source_sha256=document.source_sha256,
        locator=SourceLocator(locator_type="document_block", paragraph_index=0),
        raw_excerpt="已知但不在本次语义规则候选上下文中的证据。",
        display_excerpt="已知但不在本次语义规则候选上下文中的证据。",
        extraction_method="test",
        extraction_version="test-v1",
    )
    monkeypatch.setattr(
        review_service,
        "_collect_seal_evidence",
        lambda *_args, **_kwargs: [out_of_context_evidence],
    )

    first = review_service.run_contract_review(
        [("合同主文.pdf", content)],
        package_id="pkg-semantic-context-fallback",
        review_context=ReviewContext(contract_type="软件开发/转让服务"),
        return_cache_status=True,
    )

    assert isinstance(first, ReviewExecution)
    assert first.cached is False
    assert first.result.semantic_request is None
    assert first.result.semantic_response is None
    assert first.result.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "evidence_outside_context",
    }
    assert audit_result(first.result).passed

    second = review_service.run_contract_review(
        [("合同主文.pdf", content)],
        package_id="pkg-semantic-context-fallback",
        review_context=ReviewContext(contract_type="软件开发/转让服务"),
        return_cache_status=True,
    )

    assert isinstance(second, ReviewExecution)
    assert second.cached is False
    assert semantic_reviewer.calls == 2

    replayed = review_service.replay_contract_review(
        first.result,
        [source_path],
        rule_bundle=first.result.rule_bundle,
    )

    assert replayed.run.result_fingerprint == first.result.run.result_fingerprint


def _make_two_rule_bundle() -> RuleBundle:
    """构造两条正文检索范围不同的已发布语义规则。"""

    return publish_playbook_bundle(
        RuleBundle(
            bundle_id="semantic-evidence-context-regression-v1",
            source_filename="semantic-evidence-context-regression.json",
            source_sha256="b" * 64,
            source_sheet="Sheet1",
            source_range="A1:C3",
            rules=[
                Rule(
                    rule_id="semantic-source-code",
                    version="v1",
                    title="source code",
                    category="software-source",
                    applies_to=["software"],
                    check_method="semantic",
                    source_snapshot="source-code-rule-v1",
                ),
                Rule(
                    rule_id="semantic-breach-responsibility",
                    version="v1",
                    title="breach responsibility",
                    category="software-breach",
                    applies_to=["software"],
                    check_method="semantic",
                    source_snapshot="breach-responsibility-rule-v1",
                ),
            ],
        )
    )


def _make_two_rule_contract_pdf() -> bytes:
    """构造能让两条规则分别命中不同正文块的最小合同。"""

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "source code delivery is required.", fontname="helv")
    page.insert_text(
        (72, 120),
        "breach responsibility is defined separately.",
        fontname="helv",
    )
    data = document.tobytes()
    document.close()
    return data


class _InvalidEvidenceSemanticReviewer:
    """按目标规则返回一个不属于当前规则候选集的证据 ID。"""

    def __init__(self, invalid_evidence_id_by_rule: dict[str, str]) -> None:
        self.invalid_evidence_id_by_rule = invalid_evidence_id_by_rule
        self.calls = 0

    def review(self, request):
        self.calls += 1
        rule_id = request.rule_ids[0]
        evidence_by_rule = contract_evidence_ids_by_rule(
            request.candidate_evidence_by_rule
        )
        invalid_evidence_id = self.invalid_evidence_id_by_rule[rule_id]
        assert invalid_evidence_id not in set(evidence_by_rule.get(rule_id, ()))
        return SemanticReviewResponse(
            response_id=f"response-invalid-evidence-{self.calls}",
            provider=request.provider,
            model_version=request.model_version,
            prompt_version=request.prompt_version,
            request_fingerprint=request.request_fingerprint,
            items=[
                SemanticReviewItem(
                    rule_id=rule_id,
                    status="UNKNOWN",
                    reason="模拟模型返回的结构化审查结果。",
                    evidence_ids=[invalid_evidence_id],
                    confidence=0.0,
                )
            ],
        )


def _assert_invalid_evidence_degrades_to_baseline(
    monkeypatch,
    tmp_path,
    *,
    violation: str,
) -> None:
    """验证单种证据上下文错误从模型边界降级，并贯穿持久化与回放。"""

    bundle = _make_two_rule_bundle()
    content = _make_two_rule_contract_pdf()
    source_path = tmp_path / "合同主文.pdf"
    source_path.write_bytes(content)
    package_id = f"pkg-semantic-context-{violation}"
    review_context = ReviewContext(contract_type="software")
    baseline = run_review(
        [source_path],
        package_id=package_id,
        rule_bundle=bundle,
        review_context=review_context,
        document_filenames=["合同主文.pdf"],
    )
    candidate_evidence_by_rule = {
        rule_id: [
            candidate
            for candidate in baseline.candidate_evidence
            if candidate.rule_id == rule_id
            and candidate.source_kind == KnowledgeSourceKind.CONTRACT
        ]
        for rule_id in (rule.rule_id for rule in bundle.rules)
    }
    contract_ids_by_rule = contract_evidence_ids_by_rule(candidate_evidence_by_rule)
    assert all(contract_ids_by_rule.values())

    rule_ids = [rule.rule_id for rule in bundle.rules]
    if violation == "cross-rule-known":
        invalid_evidence_id_by_rule = {}
        for index, rule_id in enumerate(rule_ids):
            other_rule_id = rule_ids[(index + 1) % len(rule_ids)]
            target_ids = set(contract_ids_by_rule[rule_id])
            outside_ids = [
                evidence_id
                for evidence_id in contract_ids_by_rule[other_rule_id]
                if evidence_id not in target_ids
            ]
            assert outside_ids
            invalid_evidence_id_by_rule[rule_id] = outside_ids[0]
    elif violation == "fabricated-id":
        invalid_evidence_id_by_rule = {
            rule_id: "model-invented-evidence-id"
            for rule_id in rule_ids
        }
    elif violation == "rule-source":
        rule_source_ids = [
            evidence.evidence_id
            for evidence in baseline.evidence
            if evidence.evidence_type == EvidenceType.EXTERNAL_REFERENCE
        ]
        assert rule_source_ids
        invalid_evidence_id_by_rule = {
            rule_id: rule_source_ids[0] for rule_id in rule_ids
        }
    else:
        raise AssertionError(f"unsupported regression violation: {violation}")

    semantic_reviewer = _InvalidEvidenceSemanticReviewer(
        invalid_evidence_id_by_rule
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(
        review_service,
        "load_active_rule_bundle",
        lambda *_args: bundle,
    )
    monkeypatch.setattr(review_service, "_semantic_client", lambda: semantic_reviewer)
    monkeypatch.setattr(
        review_service,
        "_collect_seal_evidence",
        lambda *_args, **_kwargs: [],
    )

    first = review_service.run_contract_review(
        [("合同主文.pdf", content)],
        package_id=package_id,
        review_context=review_context,
        return_cache_status=True,
    )

    assert isinstance(first, ReviewExecution)
    assert first.cached is False
    assert first.result.semantic_request is None
    assert first.result.semantic_response is None
    assert first.result.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "evidence_outside_context",
    }
    assert first.result.run.status.value == "HUMAN_REVIEW"
    assert audit_result(first.result).passed
    assert not list((tmp_path / "review_cache").glob("*.json"))

    authoritative = load_authoritative_review_result(first.result)
    assert authoritative.run.result_fingerprint == first.result.run.result_fingerprint
    assert authoritative.semantic_request is None
    assert authoritative.semantic_response is None
    assert authoritative.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "evidence_outside_context",
    }

    replayed = review_service.replay_contract_review(
        first.result,
        [source_path],
        rule_bundle=bundle,
    )
    assert replayed.run.result_fingerprint == first.result.run.result_fingerprint
    assert replayed.semantic_request is None
    assert replayed.semantic_response is None
    assert audit_result(replayed).passed

    second = review_service.run_contract_review(
        [("合同主文.pdf", content)],
        package_id=package_id,
        review_context=review_context,
        return_cache_status=True,
    )
    assert isinstance(second, ReviewExecution)
    assert second.cached is False
    assert second.result.semantic_request is None
    assert second.result.semantic_response is None
    assert not list((tmp_path / "review_cache").glob("*.json"))
    assert semantic_reviewer.calls == 2


def test_known_cross_rule_evidence_degrades_to_deterministic_baseline(
    monkeypatch, tmp_path
):
    _assert_invalid_evidence_degrades_to_baseline(
        monkeypatch,
        tmp_path,
        violation="cross-rule-known",
    )


def test_fabricated_evidence_id_degrades_to_deterministic_baseline(
    monkeypatch, tmp_path
):
    _assert_invalid_evidence_degrades_to_baseline(
        monkeypatch,
        tmp_path,
        violation="fabricated-id",
    )


def test_rule_source_evidence_degrades_to_deterministic_baseline(
    monkeypatch, tmp_path
):
    _assert_invalid_evidence_degrades_to_baseline(
        monkeypatch,
        tmp_path,
        violation="rule-source",
    )
