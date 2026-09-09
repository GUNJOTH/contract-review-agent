import unittest

from contract_review.models import (
    AttachmentReference,
    ContractFact,
    Document,
    DocumentKind,
    EvidenceType,
    Rule,
    SourceLocator,
)
from contract_review.review import check_attachment_completeness, compare_fact_values


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = Rule(
            rule_id="attachment-001",
            version="v0.14",
            title="技术协议必须存在",
            category="附件完整性",
            applies_to=["软件开发/转让服务"],
            check_method="deterministic",
            source_snapshot="rules-v0.14",
        )
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

    def test_missing_attachment_is_unknown_with_missing_artifact_evidence(self) -> None:
        reference = AttachmentReference(
            reference_id="ref-1",
            referenced_name="技术协议",
            evidence_ids=["evidence-main-clause-1"],
        )

        evidence, findings = check_attachment_completeness(
            self.rule, [reference], [self.main_document]
        )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].evidence_type, EvidenceType.MISSING_ARTIFACT)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, "UNKNOWN")
        self.assertEqual(findings[0].risk_level, "unclassified")
        self.assertEqual(findings[0].evidence_ids, ["evidence-main-clause-1", evidence[0].evidence_id])

    def test_matching_attachment_does_not_create_finding(self) -> None:
        reference = AttachmentReference(
            reference_id="ref-1",
            referenced_name="技术协议",
            aliases=["technical-agreement"],
            evidence_ids=["evidence-main-clause-1"],
        )
        agreement = self.main_document.model_copy(update={"filename": "技术协议-最终版.pdf"})

        evidence, findings = check_attachment_completeness(
            self.rule, [reference], [self.main_document, agreement]
        )

        self.assertEqual(evidence, [])
        self.assertEqual(findings, [])

    def test_fact_comparison_keeps_both_fact_evidence_ids(self) -> None:
        left = ContractFact(
            fact_id="fact-left",
            fact_type="amount",
            value="100",
            normalized_value=100,
            evidence_ids=["ev-left"],
            extractor_version="test",
        )
        right = ContractFact(
            fact_id="fact-right",
            fact_type="amount",
            value="90",
            normalized_value=90,
            evidence_ids=["ev-right"],
            extractor_version="test",
        )

        finding = compare_fact_values(self.rule, left, right)

        self.assertEqual(finding.status, "WARN")
        self.assertEqual(finding.evidence_ids, ["ev-left", "ev-right"])
        self.assertEqual(finding.comparison["left"], 100)
        self.assertEqual(finding.comparison["right"], 90)

    def test_fact_comparison_blocks_incompatible_units(self) -> None:
        left = ContractFact(
            fact_id="fact-left",
            fact_type="amount",
            value=100,
            unit="CNY",
            evidence_ids=["ev-left"],
            extractor_version="test",
        )
        right = ContractFact(
            fact_id="fact-right",
            fact_type="amount",
            value=100,
            unit="USD",
            evidence_ids=["ev-right"],
            extractor_version="test",
        )

        finding = compare_fact_values(self.rule, left, right)

        self.assertEqual(finding.status, "UNKNOWN")
        self.assertEqual(finding.evidence_ids, ["ev-left", "ev-right"])
