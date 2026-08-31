import unittest
from pathlib import Path
from zipfile import ZipFile

from contract_review.models import EvidenceType
from contract_review.parser import find_text_evidence, parse_xlsx


_WORKBOOK_XML = """<?xml version='1.0' encoding='UTF-8'?>
<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
          xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
  <sheets><sheet name='报价单' sheetId='1' r:id='rId1'/></sheets>
</workbook>"""
_RELATIONSHIPS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
  <Relationship Id='rId1' Type='worksheet' Target='worksheets/sheet1.xml'/>
</Relationships>"""
_SHARED_STRINGS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' count='1' uniqueCount='1'>
  <si><t>技术协议</t></si>
</sst>"""
_SHEET_XML = """<?xml version='1.0' encoding='UTF-8'?>
<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>
  <sheetData>
    <row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1'><v>13</v></c></row>
    <row r='2'><c r='C2'><f>SUM(B1:B1)</f></c></row>
  </sheetData>
</worksheet>"""


class XlsxParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_path = Path.cwd() / ".test-work"
        self.work_path.mkdir(exist_ok=True)
        self.xlsx_path = self.work_path / "quotation.xlsx"
        with ZipFile(self.xlsx_path, "w") as archive:
            archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
            archive.writestr("xl/_rels/workbook.xml.rels", _RELATIONSHIPS_XML)
            archive.writestr("xl/sharedStrings.xml", _SHARED_STRINGS_XML)
            archive.writestr("xl/worksheets/sheet1.xml", _SHEET_XML)
        self.addCleanup(lambda: self.xlsx_path.unlink(missing_ok=True))

    def test_xlsx_preserves_sheet_cell_and_formula_quality(self) -> None:
        parsed = parse_xlsx(self.xlsx_path, package_id="package-xlsx")

        self.assertEqual(parsed.document.parse_status, "parsed")
        self.assertIn("formula_without_cached_value", parsed.document.quality_flags)
        self.assertEqual(len(parsed.nodes), 3)
        self.assertEqual(parsed.nodes[0].text, "技术协议")
        self.assertEqual(parsed.nodes[0].locator.sheet_name, "报价单")
        self.assertEqual(parsed.nodes[0].locator.cell_reference, "A1")
        self.assertEqual(parsed.nodes[2].text, "=SUM(B1:B1)")

        evidence = find_text_evidence(parsed, "技术协议")
        self.assertEqual(evidence[0].evidence_type, EvidenceType.TABLE_CELL)
        self.assertEqual(evidence[0].locator.cell_reference, "A1")
