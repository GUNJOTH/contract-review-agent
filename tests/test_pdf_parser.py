import unittest
from pathlib import Path

import fitz

from contract_review.models import DocumentKind
from contract_review.parser import ParseError, find_text_evidence, parse_pdf, sha256_file


def _write_text_pdf(path: Path) -> None:
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 80), "Section 8 Payment Terms")
    page.insert_text((60, 130), "Contract total amount is CNY 1000000; numeric amount is 900000.")
    page.insert_text((60, 180), "If source code is delivered, record the delivery reason.")
    pdf.save(str(path))
    pdf.close()


class PdfParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path.cwd() / ".test-work"
        self.tmp_path.mkdir(exist_ok=True)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self.tmp_path.glob("*.pdf"):
            path.unlink(missing_ok=True)
        try:
            self.tmp_path.rmdir()
        except OSError:
            pass

    def test_parse_pdf_retains_page_blocks_tokens_and_hash(self) -> None:
        pdf_path = self.tmp_path / "main-contract.pdf"
        _write_text_pdf(pdf_path)

        parsed = parse_pdf(
            pdf_path,
            package_id="package-1",
            document_kind=DocumentKind.MAIN_CONTRACT,
        )

        self.assertEqual(parsed.document.page_count, 1)
        self.assertEqual(parsed.document.source_sha256, sha256_file(pdf_path))
        self.assertEqual(parsed.document.parse_status, "parsed")
        self.assertEqual(parsed.pages[0].page.page_number, 1)
        self.assertTrue(parsed.pages[0].tokens)
        self.assertTrue(parsed.pages[0].blocks)
        self.assertTrue(any(block.text.startswith("Section 8") for block in parsed.pages[0].blocks))


    def test_find_text_evidence_can_jump_back_to_original_page(self) -> None:
        pdf_path = self.tmp_path / "main-contract.pdf"
        _write_text_pdf(pdf_path)
        parsed = parse_pdf(pdf_path, package_id="package-1")

        evidence = find_text_evidence(parsed, "source code")

        self.assertEqual(len(evidence), 1)
        item = evidence[0]
        self.assertEqual(item.document_id, parsed.document.document_id)
        self.assertEqual(item.source_sha256, parsed.document.source_sha256)
        self.assertEqual(item.locator.page_number, 1)
        self.assertIsNotNone(item.locator.bbox)
        self.assertIsNotNone(item.locator.normalized_bbox)
        self.assertIn("source code", item.display_excerpt or "")


    def test_blank_text_page_is_explicitly_marked_for_ocr(self) -> None:
        pdf_path = self.tmp_path / "scanned-contract.pdf"
        pdf = fitz.open()
        pdf.new_page(width=600, height=800)
        pdf.save(str(pdf_path))
        pdf.close()

        parsed = parse_pdf(pdf_path, package_id="package-1")

        self.assertEqual(parsed.document.parse_status, "needs_ocr")
        self.assertTrue(parsed.pages[0].page.needs_ocr)
        self.assertIn("page_1_needs_ocr", parsed.document.quality_flags)

    def test_invalid_pdf_is_rejected_without_inventing_content(self) -> None:
        pdf_path = self.tmp_path / "invalid.pdf"
        pdf_path.write_bytes(b"not-a-pdf")

        with self.assertRaises(ParseError):
            parse_pdf(pdf_path, package_id="package-1")
