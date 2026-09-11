"""把合同原文转成浏览器可内嵌的预览。

PDF / 图片可直接在 iframe 或 img 中打开；DOCX / XLSX 不能被浏览器内嵌，
因此在服务端转成带基础排版的 HTML。
"""

from __future__ import annotations

import html as html_lib
import posixpath
import re
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
X_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "package_rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_PREVIEW_CSS = """
:root { color-scheme: light; }
html, body { margin: 0; padding: 0; background: #f8fafc; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  color: #0f172a; line-height: 1.75; padding: 28px 32px 40px;
}
.doc { max-width: 860px; margin: 0 auto; background: #fff; padding: 36px 40px 48px;
  border: 1px solid #e2e8f0; box-shadow: 0 8px 24px rgba(15,23,42,.06); }
p { margin: 0 0 .55em; min-height: 1em; word-break: break-word; }
p.center { text-align: center; }
p.right { text-align: right; }
p.both { text-align: justify; }
h1, h2 { text-align: center; font-weight: 700; margin: .4em 0 .8em; }
h1 { font-size: 22px; letter-spacing: .08em; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 18px; font-size: 13px; }
td, th { border: 1px solid #cbd5e1; padding: 8px 10px; vertical-align: top; }
td p { margin: 0 0 .3em; }
.sheet { margin: 0 0 28px; }
.sheet h2 { font-size: 16px; text-align: left; margin: 0 0 10px; }
.placeholder { color: #94a3b8; font-size: 12px; }
"""


def preview_contract_document(filename: str, content: bytes) -> dict:
    """打开合同原文：PDF/图片可内嵌，Word/Excel 转成 HTML。"""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _result(filename, kind="pdf")
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
        return _result(filename, kind="image")
    if suffix == ".docx":
        body, text = render_docx_preview(content)
        return _html_result(filename, body, text, title="合同原文")
    if suffix == ".xlsx":
        body, text = render_xlsx_preview(content)
        return _html_result(filename, body, text, title="表格原文")
    if suffix == ".doc":
        return _result(
            filename,
            kind="empty",
            message="旧版 .doc 无法在浏览器内嵌预览，请另存为 .docx 后重新上传。",
        )
    return _result(
        filename,
        kind="empty",
        message=f"当前格式不便内嵌预览（{suffix or '未知格式'}）。",
    )


def render_docx_preview(content: bytes) -> tuple[str, str]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"无法解析 DOCX: {exc}") from exc
    body = root.find(f"./{{{W_NS}}}body")
    if body is None:
        raise ValueError("DOCX 没有正文")
    parts: list[str] = []
    text_parts: list[str] = []
    for child in list(body):
        tag = _local(child.tag)
        if tag == "tbl":
            table_html, table_text = _render_table(child)
            parts.append(table_html)
            if table_text:
                text_parts.append(table_text)
        elif tag == "p":
            paragraph_html, paragraph_text = _render_paragraph(child)
            parts.append(paragraph_html)
            if paragraph_text:
                text_parts.append(paragraph_text)
    return "".join(parts), "\n".join(text_parts)


def render_xlsx_preview(content: bytes) -> tuple[str, str]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            sheets = _xlsx_sheets(archive)
            shared = _xlsx_shared_strings(archive)
            blocks: list[str] = []
            text_parts: list[str] = []
            for sheet_name, sheet_path in sheets:
                root = ElementTree.fromstring(archive.read(sheet_path))
                row_map: dict[int, dict[int, str]] = {}
                max_col = 1
                for cell in root.findall(".//main:sheetData/main:row/main:c", X_NS):
                    ref = cell.attrib.get("r", "")
                    if not ref:
                        continue
                    value, _ = _xlsx_cell_text(cell, shared)
                    col, row = _xlsx_cell_ref(ref)
                    row_map.setdefault(row, {})[col] = value
                    max_col = max(max_col, col)
                if not row_map:
                    continue
                rows_html: list[str] = []
                for row_no in range(min(row_map), max(row_map) + 1):
                    cells = row_map.get(row_no, {})
                    tds = []
                    texts = []
                    for col_no in range(1, max_col + 1):
                        value = cells.get(col_no, "")
                        tds.append(f"<td>{html_lib.escape(value) if value else '&nbsp;'}</td>")
                        if value:
                            texts.append(value)
                    rows_html.append("<tr>" + "".join(tds) + "</tr>")
                    if texts:
                        text_parts.append("\t".join(texts))
                blocks.append(
                    "<section class='sheet'>"
                    f"<h2>{html_lib.escape(sheet_name)}</h2>"
                    f"<table>{''.join(rows_html)}</table>"
                    "</section>"
                )
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError, ValueError) as exc:
        raise ValueError(f"无法解析 XLSX: {exc}") from exc
    if not blocks:
        return "<p class='placeholder'>工作簿没有可显示的单元格。</p>", ""
    return "".join(blocks), "\n".join(text_parts)


def _html_result(filename: str, body: str, text: str, *, title: str) -> dict:
    if not body.strip() and not text.strip():
        return _result(filename, kind="empty", message="已打开文件，但没有解析出可读文本。")
    document = (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{html_lib.escape(title)}</title><style>{_PREVIEW_CSS}</style></head>"
        f"<body><article class='doc'>{body}</article></body></html>"
    )
    return _result(filename, kind="html", markup=document, text=text)


def _result(
    filename: str,
    *,
    kind: str,
    markup: str = "",
    text: str = "",
    message: str = "",
) -> dict:
    return {
        "filename": filename,
        "kind": kind,
        "html": markup,
        "text": text,
        "message": message,
    }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _wval(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    return node.attrib.get(f"{{{W_NS}}}val") or node.attrib.get("val") or ""


def _render_paragraph(paragraph: ElementTree.Element) -> tuple[str, str]:
    chunks: list[str] = []
    texts: list[str] = []
    for run in paragraph.findall(f".//{{{W_NS}}}r"):
        run_html, run_text = _render_run(run)
        chunks.append(run_html)
        texts.append(run_text)
    inner = "".join(chunks) or "&nbsp;"
    text = "".join(texts).strip()
    align = _wval(paragraph.find(f"./{{{W_NS}}}pPr/{{{W_NS}}}jc"))
    style = _wval(paragraph.find(f"./{{{W_NS}}}pPr/{{{W_NS}}}pStyle"))
    css = {"center": "center", "right": "right", "both": "both"}.get(align, "")
    if style.lower().startswith("heading") or (
        align == "center" and text and len(text) <= 40 and "合同" in text
    ):
        heading = "h1" if align == "center" else "h2"
        return f"<{heading}>{html_lib.escape(text) if text else inner}</{heading}>", text
    class_attr = f" class='{css}'" if css else ""
    return f"<p{class_attr}>{inner}</p>", text


def _render_run(run: ElementTree.Element) -> tuple[str, str]:
    chunks: list[str] = []
    texts: list[str] = []
    for child in list(run):
        tag = _local(child.tag)
        if tag == "t":
            value = child.text or ""
            chunks.append(html_lib.escape(value))
            texts.append(value)
        elif tag == "tab":
            chunks.append("&emsp;")
            texts.append("\t")
        elif tag == "br":
            chunks.append("<br>")
            texts.append("\n")
        elif tag in {"drawing", "pict", "object"}:
            chunks.append("<span class='placeholder'>[图片]</span>")
    markup = "".join(chunks)
    text = "".join(texts)
    rpr = run.find(f"{{{W_NS}}}rPr")
    if rpr is not None and markup:
        if rpr.find(f"{{{W_NS}}}b") is not None or rpr.find(f"{{{W_NS}}}bCs") is not None:
            markup = f"<strong>{markup}</strong>"
        if rpr.find(f"{{{W_NS}}}i") is not None:
            markup = f"<em>{markup}</em>"
        if rpr.find(f"{{{W_NS}}}u") is not None:
            markup = f"<u>{markup}</u>"
    return markup, text


def _render_table(table: ElementTree.Element) -> tuple[str, str]:
    rows_html: list[str] = []
    text_rows: list[str] = []
    for row in table.findall(f"./{{{W_NS}}}tr"):
        cells_html: list[str] = []
        cell_texts: list[str] = []
        for cell in row.findall(f"./{{{W_NS}}}tc"):
            paragraphs = []
            texts = []
            for paragraph in cell.findall(f"./{{{W_NS}}}p"):
                paragraph_html, paragraph_text = _render_paragraph(paragraph)
                paragraphs.append(paragraph_html)
                if paragraph_text:
                    texts.append(paragraph_text)
            span = _wval(cell.find(f"./{{{W_NS}}}tcPr/{{{W_NS}}}gridSpan"))
            colspan = f" colspan='{html_lib.escape(span)}'" if span.isdigit() and int(span) > 1 else ""
            cells_html.append(f"<td{colspan}>{''.join(paragraphs) or '&nbsp;'}</td>")
            cell_texts.append(" ".join(texts))
        rows_html.append("<tr>" + "".join(cells_html) + "</tr>")
        if any(cell_texts):
            text_rows.append("\t".join(part for part in cell_texts if part))
    return f"<table>{''.join(rows_html)}</table>", "\n".join(text_rows)


def _xlsx_sheets(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib.get("Id", ""): item.attrib.get("Target", "")
        for item in relationships.findall("./package_rel:Relationship", X_NS)
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall("./main:sheets/main:sheet", X_NS):
        name = sheet.attrib.get("name", "")
        rel_id = sheet.attrib.get(f"{{{X_NS['rel']}}}id", "")
        target = targets.get(rel_id, "")
        if not name or not target:
            continue
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = posixpath.normpath(posixpath.join("xl", target))
        if not path.startswith("xl/") or ".." in path.split("/"):
            continue
        sheets.append((name, path))
    return sheets


def _xlsx_shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(str(node.text or "") for node in item.findall(".//main:t", X_NS))
        for item in root.findall("./main:si", X_NS)
    ]


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> tuple[str, bool]:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("./main:v", X_NS)
    value = "" if value_node is None else str(value_node.text or "")
    if cell_type == "s":
        try:
            value = shared_strings[int(value)]
        except (IndexError, ValueError):
            value = ""
    elif cell_type == "inlineStr":
        value = "".join(str(node.text or "") for node in cell.findall(".//main:t", X_NS))
    elif cell_type == "b":
        value = "TRUE" if value == "1" else "FALSE"
    formula = cell.find("./main:f", X_NS)
    uncached = formula is not None and not value
    if uncached:
        value = f"={formula.text or ''}"
    return value.strip(), uncached


def _xlsx_cell_ref(cell_reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", cell_reference)
    if match is None:
        return 1, 1
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - ord("A") + 1
    return column, int(match.group(2))
