from contract_review.contract_domain import (
    build_contract_clauses,
    build_question_assessments,
    build_review_questions,
    extract_contract_obligations,
)
from contract_review.models import (
    AssessmentOutcome,
    ClauseKind,
    Evidence,
    EvidenceType,
    Finding,
    FindingStatus,
    KnowledgeChunk,
    ObligationModality,
    RiskLevel,
    Rule,
    RuleBundle,
    SourceLocator,
)


def _text_evidence(text: str) -> Evidence:
    return Evidence(
        evidence_id="evidence-contract-1",
        evidence_type=EvidenceType.TEXT,
        document_id="document-1",
        source_sha256="a" * 64,
        locator=SourceLocator(
            locator_type="document_block",
            paragraph_index=0,
        ),
        raw_excerpt=text,
        display_excerpt=text,
        extraction_method="test",
        extraction_version="test-v1",
        confidence=1.0,
    )


def test_build_contract_clauses_and_extract_obligations() -> None:
    text = (
        "第十条 付款义务。乙方应在验收后十个工作日内支付合同价款；"
        "甲方不得向无关第三方泄露源代码。"
    )
    evidence = _text_evidence(text)
    chunk = KnowledgeChunk(
        chunk_id="chunk-contract-1",
        source_name="合同.docx",
        source_sha256="a" * 64,
        source_version="test-v1",
        content=text,
        evidence_ids=[evidence.evidence_id],
        metadata={"document_id": "document-1", "block_id": "paragraph-0"},
    )

    clauses = build_contract_clauses([chunk], [evidence])
    obligations = extract_contract_obligations(clauses)

    assert len(clauses) == 1
    assert clauses[0].clause_kind == ClauseKind.NUMBERED
    assert clauses[0].clause_number == "第十条"
    assert {item.obligor for item in obligations} == {"甲方", "乙方"}
    assert {item.modality for item in obligations} == {
        ObligationModality.REQUIRED,
        ObligationModality.PROHIBITED,
    }
    payment = next(item for item in obligations if item.obligor == "乙方")
    assert payment.deadline == "在验收后十个工作日内"
    assert payment.evidence_ids == [evidence.evidence_id]


def test_missing_artifact_becomes_not_mentioned_assessment() -> None:
    missing = Evidence(
        evidence_id="missing-technical-agreement",
        evidence_type=EvidenceType.MISSING_ARTIFACT,
        locator=SourceLocator(
            locator_type="missing_artifact",
            missing_name="技术协议",
        ),
        extraction_method="manifest_check",
        extraction_version="test-v1",
    )
    bundle = RuleBundle(
        bundle_id="bundle-v2",
        source_filename="rules.xlsx",
        source_sha256="b" * 64,
        source_sheet="Sheet1",
        source_range="A1:M2",
        rules=[
            Rule(
                rule_id="attachment-technical",
                version="v2",
                title="技术协议必须存在",
                category="附件完整性",
                check_method="deterministic",
                risk_level=RiskLevel.HIGH,
                source_snapshot="rules.xlsx#v2",
            )
        ],
    )
    finding = Finding(
        finding_id="finding-technical",
        rule_id="attachment-technical",
        rule_version="v2",
        status=FindingStatus.UNKNOWN,
        risk_level=RiskLevel.HIGH,
        title="技术协议必须存在",
        reason="合同引用了技术协议，但合同包未包含该文件。",
        evidence_ids=[missing.evidence_id],
    )

    questions = build_review_questions(bundle)
    assessments = build_question_assessments(questions, [finding], [missing])

    assert len(questions) == 1
    assert assessments[0].outcome == AssessmentOutcome.NOT_MENTIONED
    assert assessments[0].question_id == questions[0].question_id
    assert assessments[0].evidence_ids == [missing.evidence_id]
