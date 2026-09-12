"""规则检查器在统一候选证据边界下的行为测试。"""

import unittest

from contract_review.models import (
    AttachmentReference,
    CandidateEvidence,
    ContractFact,
    Document,
    DocumentKind,
    Evidence,
    EvidenceType,
    FindingStatus,
    KnowledgeSourceKind,
    RetrievalSource,
    ReviewContext,
    Rule,
    SourceLocator,
)
from contract_review.rule_checkers import (
    RuleCheckContext,
    execute_configured_rule_checker,
)


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.main_document = Document(
            document_id="main-1",
            package_id="package-1",
            filename="main-contract.pdf",
            mime_type="application/pdf",
            source_sha256="a" * 64,
            document_kind=DocumentKind.MAIN_CONTRACT,
            parser_version="test",
            parse_status="parsed",
        )
        self.package_evidence = Evidence(
            evidence_id="package-evidence",
            evidence_type=EvidenceType.COMPARISON,
            package_id="package-1",
            locator=SourceLocator(
                locator_type="external_uri",
                external_uri="urn:test:package",
            ),
            extraction_method="test",
            extraction_version="v1",
        )
        self.rule_evidence = Evidence(
            evidence_id="rule-evidence",
            evidence_type=EvidenceType.EXTERNAL_REFERENCE,
            source_sha256="b" * 64,
            locator=SourceLocator(
                locator_type="external_uri",
                external_uri="urn:test:rule",
            ),
            extraction_method="test",
            extraction_version="v1",
        )

    def _candidate(
        self,
        *,
        candidate_id: str = "candidate-1",
        evidence_ids: list[str] | None = None,
        content: str = "本合同技术方案详见《技术协议》。",
    ) -> CandidateEvidence:
        return CandidateEvidence(
            candidate_id=candidate_id,
            query_id="query-test",
            rule_id="test-rule",
            rule_version="v1",
            rank=1,
            chunk_id="chunk-test",
            document_id=self.main_document.document_id,
            source_name=self.main_document.filename,
            source_sha256=self.main_document.source_sha256,
            source_version="parser-v1",
            source_kind=KnowledgeSourceKind.CONTRACT,
            content=content,
            evidence_ids=evidence_ids or ["evidence-main-clause-1"],
            score=1.0,
            retrieval_sources=[RetrievalSource.LEXICAL],
        )

    def _context(
        self,
        rule: Rule,
        *,
        candidate: CandidateEvidence,
        facts_by_type: dict[str, list[ContractFact]] | None = None,
        attachment_references: list[AttachmentReference] | None = None,
        documents: list[Document] | None = None,
        evidence_by_id: dict[str, Evidence] | None = None,
    ) -> RuleCheckContext:
        return RuleCheckContext(
            rule=rule,
            rule_evidence=self.rule_evidence,
            package_evidence=self.package_evidence,
            facts_by_type=facts_by_type or {},
            attachment_references=attachment_references or [],
            documents=documents or [self.main_document],
            visual_evidence=[],
            clauses=[],
            effective_context=ReviewContext(
                contract_type="software",
                document_kinds=[DocumentKind.MAIN_CONTRACT],
            ),
            all_parsed=True,
            candidate_evidence=[candidate],
            evidence_by_id=evidence_by_id
            or {
                "evidence-main-clause-1": Evidence(
                    evidence_id="evidence-main-clause-1",
                    evidence_type=EvidenceType.TEXT,
                    package_id="package-1",
                    document_id=self.main_document.document_id,
                    source_sha256=self.main_document.source_sha256,
                    locator=SourceLocator(
                        locator_type="document_block",
                        paragraph_index=0,
                    ),
                    raw_excerpt=candidate.content,
                    extraction_method="test",
                    extraction_version="v1",
                ),
                self.package_evidence.evidence_id: self.package_evidence,
                self.rule_evidence.evidence_id: self.rule_evidence,
            },
        )

    def test_missing_attachment_is_unknown_with_missing_artifact_evidence(self) -> None:
        rule = Rule(
            rule_id="test-rule",
            version="v1",
            title="技术协议必须存在",
            category="附件完整性",
            applies_to=["software"],
            check_method="deterministic",
            checker="attachment_completeness",
            source_snapshot="rules-v1",
        )
        candidate = self._candidate()
        reference = AttachmentReference(
            reference_id="ref-1",
            referenced_name="技术协议",
            evidence_ids=list(candidate.evidence_ids),
            candidate_ids=[candidate.candidate_id],
        )
        context = self._context(
            rule,
            candidate=candidate,
            attachment_references=[reference],
        )

        result = execute_configured_rule_checker(rule, context)

        assert result is not None
        self.assertEqual(result.status, FindingStatus.UNKNOWN)
        self.assertEqual(result.uncertainty_reason, "required_attachment_missing")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].evidence_type, EvidenceType.MISSING_ARTIFACT)
        self.assertIn("evidence-main-clause-1", result.evidence_ids)

    def test_matching_attachment_does_not_create_finding(self) -> None:
        rule = Rule(
            rule_id="test-rule",
            version="v1",
            title="技术协议必须存在",
            category="附件完整性",
            applies_to=["software"],
            check_method="deterministic",
            checker="attachment_completeness",
            source_snapshot="rules-v1",
        )
        candidate = self._candidate()
        reference = AttachmentReference(
            reference_id="ref-1",
            referenced_name="技术协议",
            aliases=["technical-agreement"],
            evidence_ids=list(candidate.evidence_ids),
            candidate_ids=[candidate.candidate_id],
        )
        agreement = self.main_document.model_copy(
            update={"filename": "技术协议-最终版.pdf"}
        )
        context = self._context(
            rule,
            candidate=candidate,
            attachment_references=[reference],
            documents=[self.main_document, agreement],
        )

        result = execute_configured_rule_checker(rule, context)

        self.assertIsNone(result)

    def test_fact_comparison_keeps_both_fact_evidence_ids(self) -> None:
        rule = Rule(
            rule_id="test-rule",
            version="v1",
            title="金额大小写",
            category="金额",
            applies_to=["software"],
            check_method="deterministic",
            checker="amount_case_consistency",
            source_snapshot="rules-v1",
        )
        candidate = self._candidate(
            evidence_ids=["ev-left", "ev-right"],
            content="合同金额：小写100元，大写：壹佰元。",
        )
        left = ContractFact(
            fact_id="fact-left",
            fact_type="financial.contract_amount_numeric",
            value="100",
            normalized_value="100",
            source_document_ids=[self.main_document.document_id],
            evidence_ids=["ev-left"],
            candidate_ids=[candidate.candidate_id],
            extractor_version="test",
        )
        right = ContractFact(
            fact_id="fact-right",
            fact_type="financial.contract_amount_upper",
            value="90",
            normalized_value="90",
            source_document_ids=[self.main_document.document_id],
            evidence_ids=["ev-right"],
            candidate_ids=[candidate.candidate_id],
            extractor_version="test",
        )
        context = self._context(
            rule,
            candidate=candidate,
            facts_by_type={
                left.fact_type: [left],
                right.fact_type: [right],
            },
        )

        result = execute_configured_rule_checker(rule, context)

        assert result is not None
        self.assertEqual(result.status, FindingStatus.WARN)
        self.assertEqual(
            set(result.evidence_ids),
            {"rule-evidence", "package-evidence", "ev-left", "ev-right"},
        )
        self.assertEqual(result.comparison["numeric"], "100.00")
        self.assertEqual(result.comparison["uppercase"], "90.00")

    def test_fact_comparison_is_unknown_when_candidate_fact_is_incomplete(self) -> None:
        rule = Rule(
            rule_id="test-rule",
            version="v1",
            title="金额大小写",
            category="金额",
            applies_to=["software"],
            check_method="deterministic",
            checker="amount_case_consistency",
            source_snapshot="rules-v1",
        )
        candidate = self._candidate(content="合同金额：小写100元。")
        fact = ContractFact(
            fact_id="fact-left",
            fact_type="financial.contract_amount_numeric",
            value="100",
            normalized_value="100",
            source_document_ids=[self.main_document.document_id],
            evidence_ids=list(candidate.evidence_ids),
            candidate_ids=[candidate.candidate_id],
            extractor_version="test",
        )
        context = self._context(
            rule,
            candidate=candidate,
            facts_by_type={fact.fact_type: [fact]},
        )

        result = execute_configured_rule_checker(rule, context)

        assert result is not None
        self.assertEqual(result.status, FindingStatus.UNKNOWN)
        self.assertFalse(result.automatic)


if __name__ == "__main__":
    unittest.main()
