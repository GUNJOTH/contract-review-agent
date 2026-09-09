"""合同文档对比：Word 差异列表与相似度。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi.testclient import TestClient

from contract_review_app.config import settings
from contract_review_app.main import app
from contract_review_app.services.document_compare import compare_contract_documents

client = TestClient(app)
SAMPLE_DOCX = Path(__file__).resolve().parents[2] / "xx集团有限公司生产项目管理系统建设项目合同.docx"


def _auth_headers() -> dict:
    if settings.API_TOKEN:
        return {settings.AUTH_HEADER_NAME: settings.API_TOKEN}
    return {}


def _docx_bytes(*paragraphs: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = Element(f"{{{ns}}}document")
    body = SubElement(document, f"{{{ns}}}body")
    for text in paragraphs:
        paragraph = SubElement(body, f"{{{ns}}}p")
        run = SubElement(paragraph, f"{{{ns}}}r")
        node = SubElement(run, f"{{{ns}}}t")
        node.text = text
    xml = tostring(document, encoding="utf-8", xml_declaration=True)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""")
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_compare_identical_sample_docx():
    content = SAMPLE_DOCX.read_bytes()
    result = compare_contract_documents((SAMPLE_DOCX.name, content), ("副本.docx", content))
    assert result.similarity == 1.0
    assert result.added == 0
    assert result.deleted == 0
    assert result.modified == 0
    assert "高度相似" in result.similarity_label


def test_compare_detects_added_and_modified_paragraphs():
    base = _docx_bytes("甲方：xx集团有限公司", "合同金额人民币一百万元整")
    other = _docx_bytes("甲方：xx集团有限公司", "合同金额人民币一百二十万元整", "新增质保期两年")
    result = compare_contract_documents(("基准.docx", base), ("比对.docx", other))
    kinds = {item.kind for item in result.changes}
    assert "modified" in kinds
    assert "added" in kinds
    assert result.similarity < 1
    assert "一百二十万元" in result.compare.html


def test_contract_compare_api_returns_diff_list():
    base = _docx_bytes("付款方式：银行转账")
    other = _docx_bytes("付款方式：支票支付")
    response = client.post(
        "/api/v1/contract-compare",
        headers=_auth_headers(),
        files={
            "base_file": ("基准.docx", base, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "compare_file": ("比对.docx", other, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["modified"] >= 1
    assert payload["changes"]
    assert "差异" in payload["report"] or "修改" in payload["report"]
