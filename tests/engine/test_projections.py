"""核心 ReviewResult 兼容投影测试。"""

import tempfile
import unittest
from pathlib import Path

import fitz

from contract_review import (
    project_element_extraction,
    project_review_analysis,
    project_rule_bundle,
    run_review,
)
from contract_review.models import Rule, RuleBundle


class ReviewProjectionTests(unittest.TestCase):
    def test_legacy_shapes_are_projected_from_one_core_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="contract-projection-test-") as temp_dir:
            pdf_path = Path(temp_dir) / "contract.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "合同名称：软件开发合同\n"
                "甲方：某某科技有限公司\n"
                "乙方：某某软件有限公司\n"
                "源代码交付应明确范围。",
                fontname="china-s",
            )
            document.save(str(pdf_path))
            document.close()

            bundle = RuleBundle(
                bundle_id="projection-rules-v1",
                source_filename="projection-rules.xlsx",
                source_sha256="b" * 64,
                source_sheet="Sheet1",
                source_range="A1:C2",
                rules=[
                    Rule(
                        rule_id="source-code-001",
                        version="v1",
                        title="源代码",
                        category="知识产权",
                        applies_to=["software"],
                        check_method="keyword",
                        source_snapshot="projection-rules#1",
                    )
                ],
            )
            result = run_review(
                [pdf_path],
                package_id="pkg-projection-test",
                rule_bundle=bundle,
                contract_type="software",
                run_id="run-projection-test",
            )

        analysis = project_review_analysis(result)
        elements = project_element_extraction(result)
        rules = project_rule_bundle(result.rule_bundle)

        self.assertEqual(analysis.analysis_id, "review-run-projection-test")
        self.assertEqual(analysis.provider, "deterministic-rule-engine")
        self.assertTrue(analysis.items)
        self.assertEqual(analysis.items[0].risk_id, result.findings[0].finding_id)
        self.assertNotIn("ai_analysis", analysis.model_dump(mode="json"))

        fillable = elements.fillable
        self.assertEqual(fillable["party_a"], "某某科技有限公司")
        self.assertEqual(fillable["party_b"], "某某软件有限公司")
        self.assertTrue(elements.extraction_id.startswith("review-elements-"))
        self.assertTrue(any(item.key == "party_a" for item in elements.fields))

        self.assertTrue(rules.read_only)
        self.assertEqual(rules.bundle_id, result.rule_bundle.bundle_id)
        self.assertEqual(rules.rules[0].rule_id, "source-code-001")
        self.assertEqual(rules.packs.ai.rules, [])


if __name__ == "__main__":
    unittest.main()
