"""标准合同要素事实抽取测试。"""

import tempfile
import unittest
from pathlib import Path

import fitz

from contract_review.elements import extract_contract_element_facts
from contract_review.models import DocumentKind
from contract_review.parser import parse_pdf


class ContractElementFactTests(unittest.TestCase):
    def test_standard_elements_are_facts_with_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="contract-elements-test-") as temp_dir:
            pdf_path = Path(temp_dir) / "contract.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "合同名称：软件开发合同\n"
                "合同编号：HT-2026-001\n"
                "甲方：某某科技有限公司\n"
                "乙方：某某软件有限公司\n"
                "合同金额：人民币1000000元\n"
                "税率：13%\n"
                "付款方式：银行转账",
                fontname="china-s",
            )
            document.save(str(pdf_path))
            document.close()

            parsed = parse_pdf(
                pdf_path,
                package_id="pkg-elements-test",
                document_kind=DocumentKind.MAIN_CONTRACT,
            )
            facts, evidence = extract_contract_element_facts([parsed])

        by_type = {fact.fact_type: fact for fact in facts}
        self.assertEqual(by_type["contract_element:party_a"].value, "某某科技有限公司")
        self.assertEqual(by_type["contract_element:party_b"].value, "某某软件有限公司")
        self.assertEqual(by_type["contract_element:contract_no"].value, "HT-2026-001")
        self.assertIn("1000000", str(by_type["contract_element:amount"].value))
        self.assertEqual(by_type["contract_element:tax_rate"].value, "13%")
        self.assertEqual(by_type["contract_element:payment_method"].value, "银行转账")
        evidence_ids = {item.evidence_id for item in evidence}
        self.assertTrue(
            all(
                set(fact.evidence_ids).issubset(evidence_ids)
                for fact in facts
            )
        )
        self.assertTrue(all(fact.extractor_version for fact in facts))


if __name__ == "__main__":
    unittest.main()
