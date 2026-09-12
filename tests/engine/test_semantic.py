"""语义响应按规则绑定合同证据的门禁测试。"""

import pytest

from contract_review.models import (
    Evidence,
    EvidenceType,
    KnowledgeChunk,
    KnowledgeSourceKind,
    Rule,
    RiskLevel,
    SemanticReviewItem,
    SemanticReviewResponse,
    SourceLocator,
)
from contract_review.semantic import findings_from_semantic_response


def _evidence(evidence_id: str, text: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TEXT,
        document_id="document-1",
        source_sha256="a" * 64,
        locator=SourceLocator(locator_type="document_block", paragraph_index=0),
        raw_excerpt=text,
        display_excerpt=text,
        extraction_method="test",
        extraction_version="test-v1",
    )


def _rule(rule_id: str) -> Rule:
    return Rule(
        rule_id=rule_id,
        version="v1",
        title=f"规则 {rule_id}",
        category="测试",
        applies_to=["software"],
        check_method="semantic",
        risk_level=RiskLevel.MEDIUM,
        source_snapshot="test-rules-v1",
    )


def test_semantic_response_cannot_use_another_rules_contract_evidence() -> None:
    response = SemanticReviewResponse(
        response_id="response-1",
        provider="test-provider",
        model_version="test-model",
        prompt_version="prompt-v1",
        request_fingerprint="f" * 64,
        items=[
            SemanticReviewItem(
                rule_id="R1",
                status="PASS",
                reason="模型试图引用另一条规则的证据。",
                evidence_ids=["evidence-r2"],
                confidence=0.95,
            )
        ],
    )

    with pytest.raises(ValueError, match="outside its context"):
        findings_from_semantic_response(
            response,
            rules={"R1": _rule("R1"), "R2": _rule("R2")},
            known_evidence={
                "evidence-r1": _evidence("evidence-r1", "R1 合同事实"),
                "evidence-r2": _evidence("evidence-r2", "R2 合同事实"),
            },
            allowed_evidence_ids_by_rule={
                "R1": {"evidence-r1"},
                "R2": {"evidence-r2"},
            },
            expected_rule_ids=["R1"],
        )


def test_semantic_context_keeps_rule_mapping_as_a_typed_snapshot() -> None:
    chunk = KnowledgeChunk(
        chunk_id="chunk-r1",
        source_name="主合同.pdf",
        source_sha256="a" * 64,
        source_version="parser-v1",
        content="R1 对应的合同正文。",
        evidence_ids=["evidence-r1"],
        source_kind=KnowledgeSourceKind.CONTRACT,
        clause_ids=["clause-r1"],
        metadata={"document_id": "document-1"},
    )

    assert chunk.source_kind == KnowledgeSourceKind.CONTRACT
    assert chunk.clause_ids == ["clause-r1"]
