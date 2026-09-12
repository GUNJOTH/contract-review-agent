"""规则快照检查器注册、业务计算和边界校验。"""

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from contract_review.models import (
    AssessmentOutcome,
    EvidenceType,
    FindingStatus,
    ReviewContext,
    Rule,
    RuleBundle,
)
from contract_review.playbook import publish_playbook_bundle
from contract_review.pipeline import run_review
from contract_review.rule_checkers import checker_for_rule
from contract_review.revisions import build_revision_set
from contract_review.rules import RuleBundleError, validate_rule


class RuleCheckerTests(unittest.TestCase):
    def test_rule_checker_is_explicit_and_legacy_snapshot_has_no_fallback(self) -> None:
        explicit = Rule(
            rule_id="amount-case",
            version="v1",
            title="金额大小写",
            category="金额",
            applies_to=["software"],
            check_method="deterministic",
            checker="amount_case_consistency",
            source_snapshot="rules-v1",
        )
        legacy = explicit.model_copy(
            update={"rule_id": "tax-rate", "title": "税率", "checker": None, "legacy_id": 9}
        )
        invalid = explicit.model_copy(
            update={"rule_id": "invalid-checker", "checker": "not-registered"}
        )

        self.assertEqual(checker_for_rule(explicit), "amount_case_consistency")
        self.assertIsNone(checker_for_rule(legacy))
        with self.assertRaises(RuleBundleError):
            validate_rule(invalid)

    def test_attachment_checker_aggregates_missing_artifacts_into_one_finding(self) -> None:
        bundle = publish_playbook_bundle(RuleBundle(
            bundle_id="attachment-rules-v1",
            source_filename="attachment-rules.json",
            source_sha256="c" * 64,
            source_sheet="rules",
            source_range="A1:D2",
            rules=[
                Rule(
                    rule_id="technical-agreement",
                    version="v1",
                    title="技术协议",
                    category="合同主体",
                    applies_to=["software"],
                    check_method="semantic",
                    checker="attachment_completeness",
                    source_snapshot="attachment-rules#technical-agreement",
                )
            ],
        ))
        text = "本合同技术方案详见《技术协议》。"
        with tempfile.TemporaryDirectory(prefix="contract-attachment-checker-") as temp:
            docx_path = Path(temp) / "contract.docx"
            xml = f"""<?xml version='1.0' encoding='UTF-8'?>
            <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
              <w:body><w:p><w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
            </w:document>"""
            with ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", xml)

            result = run_review(
                [docx_path],
                package_id="pkg-attachment-checker",
                rule_bundle=bundle,
                review_context=ReviewContext(contract_type="software"),
                run_id="run-attachment-checker",
            )

        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.status, FindingStatus.UNKNOWN)
        self.assertEqual(finding.uncertainty_reason, "required_attachment_missing")
        self.assertEqual(
            sum(item.evidence_type == EvidenceType.MISSING_ARTIFACT for item in result.evidence),
            1,
        )
        self.assertEqual(
            result.question_assessments[0].outcome,
            AssessmentOutcome.NOT_MENTIONED,
        )

    def test_financial_rules_produce_auditable_deterministic_findings(self) -> None:
        bundle = publish_playbook_bundle(RuleBundle(
            bundle_id="financial-rules-v1",
            source_filename="financial-rules.json",
            source_sha256="b" * 64,
            source_sheet="rules",
            source_range="A1:D4",
            rules=[
                Rule(
                    rule_id="amount-case",
                    version="v1",
                    title="合同金额大小写一致",
                    category="金额",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="amount_case_consistency",
                    source_snapshot="financial-rules#amount-case",
                ),
                Rule(
                    rule_id="tax-rate",
                    version="v1",
                    title="合同税率",
                    category="金额",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="tax_rate",
                    applicability={
                        "software": {
                            "applicability": "expected_value",
                            "expected_value": 0.13,
                        }
                    },
                    source_snapshot="financial-rules#tax-rate",
                ),
                Rule(
                    rule_id="tax-amount",
                    version="v1",
                    title="合同税额",
                    category="金额",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="tax_amount",
                    source_snapshot="financial-rules#tax-amount",
                ),
                Rule(
                    rule_id="payment-total",
                    version="v1",
                    title="付款总额",
                    category="金额",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="payment_total",
                    source_snapshot="financial-rules#payment-total",
                ),
            ],
        ))
        text = (
            "合同金额：小写1000000元，大写：壹佰万元整。\n"
            "不含税金额：1000000元；税率：13%；税额：130000元。\n"
            "付款金额：1000000元。"
        )
        with tempfile.TemporaryDirectory(prefix="contract-rule-checker-") as temp:
            docx_path = Path(temp) / "contract.docx"
            xml = f"""<?xml version='1.0' encoding='UTF-8'?>
            <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
              <w:body><w:p><w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
            </w:document>"""
            with ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", xml)

            result = run_review(
                [docx_path],
                package_id="pkg-rule-checker",
                rule_bundle=bundle,
                review_context=ReviewContext(contract_type="software"),
                run_id="run-rule-checker",
            )

        findings = {finding.rule_id: finding for finding in result.findings}
        self.assertEqual(
            {rule_id: finding.status for rule_id, finding in findings.items()},
            {
                "amount-case": FindingStatus.PASS,
                "tax-rate": FindingStatus.PASS,
                "tax-amount": FindingStatus.PASS,
                "payment-total": FindingStatus.PASS,
            },
        )
        self.assertTrue(all(finding.fact_ids for finding in findings.values()))
        self.assertTrue(result.run.result_fingerprint)

    def test_term_checker_binds_finding_and_revision_to_source_clause(self) -> None:
        bundle = publish_playbook_bundle(RuleBundle(
            bundle_id="term-revision-rules-v1",
            source_filename="term-revision-rules.json",
            source_sha256="d" * 64,
            source_sheet="rules",
            source_range="A1:D2",
            rules=[
                Rule(
                    rule_id="payment-term",
                    version="v1",
                    title="付款条件",
                    category="付款",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="payment_terms",
                    source_snapshot="term-revision-rules#payment",
                )
            ],
        ))
        text = "付款条款：合同签订后100%预付，乙方按项目计划完成实施。"
        with tempfile.TemporaryDirectory(prefix="contract-term-revision-") as temp:
            docx_path = Path(temp) / "contract.docx"
            xml = f"""<?xml version='1.0' encoding='UTF-8'?>
            <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
              <w:body><w:p><w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
            </w:document>"""
            with ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", xml)

            result = run_review(
                [docx_path],
                package_id="pkg-term-revision",
                rule_bundle=bundle,
                review_context=ReviewContext(contract_type="software"),
                run_id="run-term-revision",
            )

        finding = result.findings[0]
        self.assertEqual(finding.status, FindingStatus.BLOCK)
        self.assertTrue(finding.clause_ids)

        revisions = build_revision_set(result)
        self.assertEqual(revisions.changes[0].clause_id, finding.clause_ids[0])


if __name__ == "__main__":
    unittest.main()
