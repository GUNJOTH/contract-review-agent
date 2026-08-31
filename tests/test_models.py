import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from contract_review.models import (
    BoundingBox,
    Evidence,
    EvidenceType,
    Finding,
    FindingStatus,
    RiskLevel,
    Rule,
    RuleBundle,
    SourceLocator,
)


class ModelTests(unittest.TestCase):
    def test_bbox_rejects_inverted_geometry(self) -> None:
        with self.assertRaises(ValidationError):
            BoundingBox(x1=20, y1=10, x2=5, y2=30)


    def test_evidence_requires_locator_specific_fields(self) -> None:
        with self.assertRaises(ValidationError):
            SourceLocator(locator_type="table_cell", page_number=1)

        with self.assertRaises(ValidationError):
            SourceLocator(locator_type="page")

        with self.assertRaises(ValidationError):
            SourceLocator(locator_type="external_uri")

        with self.assertRaises(ValidationError):
            Evidence(
                evidence_id="evidence-1",
                evidence_type=EvidenceType.TEXT,
                locator=SourceLocator(locator_type="page", page_number=1),
                extraction_method="test",
                extraction_version="test",
            )


    def test_finding_requires_at_least_one_evidence_id(self) -> None:
        with self.assertRaises(ValidationError):
            Finding(
                finding_id="finding-1",
                rule_id="rule-1",
                rule_version="v1",
                status=FindingStatus.WARN,
                risk_level=RiskLevel.MEDIUM,
                title="缺少依据",
                reason="测试",
                evidence_ids=[],
            )


    def test_evidence_and_rule_are_json_serializable(self) -> None:
        evidence = Evidence(
            evidence_id="evidence-1",
            evidence_type=EvidenceType.MISSING_ARTIFACT,
            locator=SourceLocator(locator_type="missing_artifact", missing_name="技术协议"),
            extraction_method="manifest_check",
            extraction_version="manifest-0.1.0",
            captured_at=datetime.now(timezone.utc),
        )
        rule = Rule(
            rule_id="attachment-001",
            version="v0.14",
            title="技术协议必须存在",
            category="attachment_completeness",
            applies_to=["software_development"],
            check_method="deterministic",
            risk_level=RiskLevel.HIGH,
            source_snapshot="contract-rules-v0.14",
        )

        self.assertEqual(evidence.model_dump(mode="json")["locator"]["missing_name"], "技术协议")
        self.assertEqual(rule.model_dump(mode="json")["rule_id"], "attachment-001")

    def test_rule_bundle_keeps_source_locator_and_applicability(self) -> None:
        bundle = RuleBundle(
            bundle_id="bundle-1",
            source_filename="rules.xlsx",
            source_sha256="a" * 64,
            source_sheet="Sheet1",
            source_range="A1:M54",
            rules=[
                Rule(
                    rule_id="rule-1",
                    version="v1",
                    title="金额大小写一致",
                    category="金额",
                    applies_to=["软件产品销售"],
                    check_method="deterministic",
                    risk_level=RiskLevel.HIGH,
                    applicability={"软件产品销售": {"applicability": "required"}},
                    source_snapshot="rules.xlsx#hash",
                    source_locator=SourceLocator(
                        locator_type="table_cell",
                        sheet_name="Sheet1",
                        cell_reference="C10",
                    ),
                )
            ],
        )

        self.assertEqual(bundle.rules[0].source_locator.cell_reference, "C10")
        self.assertEqual(
            bundle.rules[0].applicability["软件产品销售"].applicability, "required"
        )
