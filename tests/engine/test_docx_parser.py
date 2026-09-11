import unittest
from pathlib import Path
from zipfile import ZipFile

from contract_review.models import BlockType
from contract_review.parser import find_text_evidence, parse_docx


_DOCX_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
  <w:body>
    <w:p><w:pPr><w:pStyle w:val='Heading1'/></w:pPr><w:r><w:t>合同附件</w:t></w:r></w:p>
    <w:p><w:r><w:t>双方应对技术资料保密。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>验收标准</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>"""


class DocxParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_path = Path.cwd() / ".test-work"
        self.work_path.mkdir(exist_ok=True)
        self.docx_path = self.work_path / "contract.docx"
        with ZipFile(self.docx_path, "w") as archive:
            archive.writestr("word/document.xml", _DOCX_XML)
        self.addCleanup(lambda: self.docx_path.unlink(missing_ok=True))

    def test_docx_preserves_paragraph_and_table_cell_locators(self) -> None:
        parsed = parse_docx(self.docx_path, package_id="package-docx")

        self.assertEqual(parsed.document.parse_status, "parsed")
        self.assertEqual(parsed.document.page_count, 0)
        self.assertEqual(len(parsed.nodes), 4)
        self.assertEqual(parsed.nodes[0].block_type, BlockType.HEADING)
        self.assertEqual(parsed.nodes[-1].locator.cell_reference, "T1R1C2")

        evidence = find_text_evidence(parsed, "保密")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].locator.locator_type, "document_block")
        self.assertIsNone(evidence[0].locator.page_number)
        self.assertEqual(evidence[0].locator.char_start, 8)

        cell_evidence = find_text_evidence(parsed, "验收标准")
        self.assertEqual(cell_evidence[0].evidence_type.value, "table_cell")
        self.assertEqual(cell_evidence[0].locator.locator_type, "table_cell")
        self.assertEqual(cell_evidence[0].locator.cell_reference, "T1R1C2")
