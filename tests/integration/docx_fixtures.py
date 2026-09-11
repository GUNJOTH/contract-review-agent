"""Small, deterministic DOCX fixtures for integration tests."""

from __future__ import annotations

from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph(parent: Element, text: str, *, style: str | None = None) -> None:
    paragraph = SubElement(parent, f"{{{W_NS}}}p")
    if style:
        properties = SubElement(paragraph, f"{{{W_NS}}}pPr")
        SubElement(properties, f"{{{W_NS}}}pStyle", {f"{{{W_NS}}}val": style})
    run = SubElement(paragraph, f"{{{W_NS}}}r")
    text_node = SubElement(run, f"{{{W_NS}}}t")
    text_node.text = text


def _table(parent: Element, rows: list[tuple[str, ...]]) -> None:
    table = SubElement(parent, f"{{{W_NS}}}tbl")
    for values in rows:
        row = SubElement(table, f"{{{W_NS}}}tr")
        for value in values:
            cell = SubElement(row, f"{{{W_NS}}}tc")
            _paragraph(cell, value)


def sample_contract_docx() -> bytes:
    """Return a valid minimal OOXML document containing paragraphs and a table."""
    document = Element(f"{{{W_NS}}}document")
    body = SubElement(document, f"{{{W_NS}}}body")
    _paragraph(body, "技术开发与服务合同", style="Heading1")
    _paragraph(body, "甲方：xx集团有限公司")
    _paragraph(body, "乙方：珠海市同海科技股份有限公司")
    _table(
        body,
        [
            ("项目", "内容"),
            ("合同名称", "技术开发与服务"),
            ("签约主体", "xx集团有限公司 / 珠海市同海科技股份有限公司"),
        ],
    )
    SubElement(body, f"{{{W_NS}}}sectPr")
    xml = tostring(document, encoding="utf-8", xml_declaration=True)

    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        )
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()
