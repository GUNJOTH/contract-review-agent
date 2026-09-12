"""财务事实抽取和金额规范化的领域契约测试。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

from contract_review.facts import extract_financial_facts_from_candidates, parse_money_value
from contract_review.knowledge import build_knowledge_corpus
from contract_review.models import (
    CandidateEvidence,
    DocumentKind,
    KnowledgeSourceKind,
    RetrievalSource,
)
from contract_review.parser import parse_docx


class FinancialFactTests(unittest.TestCase):
    def test_parse_money_value_supports_arabic_and_chinese_amounts(self) -> None:
        cases = {
            "人民币1000000元": Decimal("1000000.00"),
            "1.2万元": Decimal("12000.00"),
            "人民币壹佰万元整": Decimal("1000000.00"),
            "壹佰贰拾叁万肆仟伍佰陆拾柒元捌角": Decimal("1234567.80"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_money_value(value), expected)

        self.assertIsNone(parse_money_value("合同编号2026"))

    def test_extract_financial_facts_binds_values_to_source_evidence(self) -> None:
        text = (
            "合同金额：小写1000000元，大写：壹佰万元整。\n"
            "不含税金额：1000000元；税额：130000元；税率：13%。\n"
            "付款金额：500000元；第二期付款金额：500000元；付款比例合计50%。\n"
            "发票金额：1000000元。"
        )
        with tempfile.TemporaryDirectory(prefix="contract-financial-facts-") as temp:
            docx_path = Path(temp) / "contract.docx"
            xml = f"""<?xml version='1.0' encoding='UTF-8'?>
            <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
              <w:body><w:p><w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
            </w:document>"""
            with ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", xml)

            parsed = parse_docx(
                docx_path,
                package_id="pkg-financial-facts",
                document_kind=DocumentKind.MAIN_CONTRACT,
            )
            chunks, evidence = build_knowledge_corpus([parsed])
            candidates = [
                CandidateEvidence(
                    candidate_id=f"candidate-financial-{index}",
                    query_id="query-financial-test",
                    rule_id="financial-test",
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
                    (
                        chunk
                        for chunk in chunks
                        if chunk.source_kind == KnowledgeSourceKind.CONTRACT
                    ),
                    start=1,
                )
            ]
            facts = extract_financial_facts_from_candidates(candidates)

        by_type: dict[str, list] = {}
        for fact in facts:
            by_type.setdefault(fact.fact_type, []).append(fact)
        self.assertEqual(
            [fact.normalized_value for fact in by_type["financial.contract_amount_numeric"]],
            ["1000000.00"],
        )
        self.assertEqual(
            [fact.normalized_value for fact in by_type["financial.contract_amount_upper"]],
            ["1000000.00"],
        )
        self.assertEqual(
            by_type["financial.tax_base_amount"][0].normalized_value,
            "1000000.00",
        )
        self.assertEqual(
            by_type["financial.tax_amount"][0].normalized_value,
            "130000.00",
        )
        self.assertEqual(
            [fact.normalized_value for fact in by_type["financial.payment_amount"]],
            ["500000.00", "500000.00"],
        )
        self.assertEqual(
            by_type["financial.payment_ratio"][0].normalized_value,
            "0.500000",
        )
        self.assertEqual(
            by_type["financial.invoice_amount"][0].normalized_value,
            "1000000.00",
        )

        evidence_ids = {item.evidence_id for item in evidence}
        self.assertTrue(facts)
        self.assertTrue(
            all(set(fact.evidence_ids).issubset(evidence_ids) for fact in facts)
        )
        self.assertTrue(all(fact.extractor_version for fact in facts))


if __name__ == "__main__":
    unittest.main()
