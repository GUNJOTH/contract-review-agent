import unittest
from pathlib import Path

import fitz

from contract_review.models import BoundingBox, Rule, RuleBundle
from contract_review.ocr import OCRPageResult, OCRTextBlock, StaticOCRProvider
from contract_review.parser import find_text_evidence, parse_pdf
from contract_review.pipeline import replay_review, run_review


class OcrParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_path = Path.cwd() / ".test-work"
        self.work_path.mkdir(exist_ok=True)
        self.pdf_path = self.work_path / "scan-like.pdf"
        pdf = fitz.open()
        pdf.new_page(width=600, height=800)
        pdf.save(str(self.pdf_path))
        pdf.close()
        self.addCleanup(lambda: self.pdf_path.unlink(missing_ok=True))

    def test_ocr_provider_turns_scanned_page_into_coordinate_evidence(self) -> None:
        provider = StaticOCRProvider(
            {
                1: OCRPageResult(
                    blocks=[
                        OCRTextBlock(
                            text="附件技术协议",
                            bbox=BoundingBox(x1=40, y1=50, x2=240, y2=80),
                            confidence=0.91,
                        )
                    ]
                )
            }
        )
        parsed = parse_pdf(
            self.pdf_path,
            package_id="package-ocr",
            ocr_provider=provider,
        )

        self.assertEqual(parsed.document.parse_status, "parsed")
        self.assertIn("ocr-static-ocr-0.1.0", parsed.document.parser_version)
        self.assertFalse(parsed.pages[0].page.needs_ocr)
        self.assertEqual(parsed.pages[0].blocks[0].text, "附件技术协议")
        evidence = find_text_evidence(parsed, "技术协议")
        self.assertEqual(evidence[0].locator.page_number, 1)
        self.assertEqual(evidence[0].locator.bbox.x1, 40)
        self.assertEqual(evidence[0].confidence, 0.91)

    def test_ocr_provider_is_part_of_full_review_replay(self) -> None:
        provider = StaticOCRProvider(
            {
                1: OCRPageResult(
                    blocks=[
                        OCRTextBlock(
                            text="附件技术协议",
                            bbox=BoundingBox(x1=40, y1=50, x2=240, y2=80),
                            confidence=0.91,
                        )
                    ]
                )
            }
        )
        bundle = RuleBundle(
            bundle_id="ocr-rules-v1",
            source_filename="ocr-rules.xlsx",
            source_sha256="d" * 64,
            source_sheet="Sheet1",
            source_range="A1:C1",
            rules=[
                Rule(
                    rule_id="ocr-keyword",
                    version="v1",
                    title="技术协议",
                    category="附件",
                    applies_to=["software"],
                    check_method="keyword",
                    source_snapshot="ocr-rules#1",
                )
            ],
        )
        result = run_review(
            [self.pdf_path],
            package_id="package-ocr-review",
            rule_bundle=bundle,
            contract_type="software",
            ocr_provider=provider,
            run_id="run-ocr-review",
        )

        self.assertEqual(result.documents[0].parse_status, "parsed")
        self.assertEqual(result.findings[0].status, "UNKNOWN")
        replayed = replay_review(
            result,
            [self.pdf_path],
            rule_bundle=bundle,
            ocr_provider=provider,
        )
        self.assertEqual(replayed.run.result_fingerprint, result.run.result_fingerprint)
