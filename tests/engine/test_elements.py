"""标准合同要素事实抽取测试。"""

import tempfile
import unittest
from pathlib import Path

import fitz

from contract_review.elements import extract_contract_element_facts_from_candidates
from contract_review.knowledge import build_knowledge_corpus
from contract_review.models import (
    CandidateEvidence,
    DocumentKind,
    KnowledgeSourceKind,
    RetrievalSource,
)
from contract_review.parser import parse_pdf


def _candidates(parsed):
    chunks, evidence = build_knowledge_corpus([parsed])
    candidates = [
        CandidateEvidence(
            candidate_id=f"candidate-element-{index}",
            query_id="query-element-test",
            rule_id="element-test",
            rule_version="v1",
            rank=index,
            chunk_id=chunk.chunk_id,
            document_id=parsed.document.document_id,
            source_name=chunk.source_name,
            source_sha256=chunk.source_sha256,
            source_version=chunk.source_version,
            source_kind=KnowledgeSourceKind.CONTRACT,
            content=chunk.content,
            evidence_ids=chunk.evidence_ids,
            clause_ids=chunk.clause_ids,
            score=1.0,
            retrieval_sources=[RetrievalSource.LEXICAL],
            metadata=chunk.metadata,
        )
        for index, chunk in enumerate(
            (chunk for chunk in chunks if chunk.source_kind == KnowledgeSourceKind.CONTRACT),
            start=1,
        )
    ]
    return candidates, evidence


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
                "付款方式：银行转账\n"
                "交付条款：乙方应于2026年12月31日前将系统交付至甲方指定地点，交付方式为现场部署。",
                fontname="china-s",
            )
            document.save(str(pdf_path))
            document.close()

            parsed = parse_pdf(
                pdf_path,
                package_id="pkg-elements-test",
                document_kind=DocumentKind.MAIN_CONTRACT,
            )
            candidates, evidence = _candidates(parsed)
            facts = extract_contract_element_facts_from_candidates(candidates)

        by_type = {fact.fact_type: fact for fact in facts}
        self.assertEqual(by_type["contract_element:party_a"].value, "某某科技有限公司")
        self.assertEqual(by_type["contract_element:party_b"].value, "某某软件有限公司")
        self.assertEqual(by_type["contract_element:contract_no"].value, "HT-2026-001")
        self.assertIn("1000000", str(by_type["contract_element:amount"].value))
        self.assertEqual(by_type["contract_element:tax_rate"].value, "13%")
        self.assertEqual(by_type["contract_element:payment_method"].value, "银行转账")
        self.assertEqual(
            by_type["contract_element:delivery_date"].value,
            "2026年12月31日",
        )
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
