"""Read-only PDF parsing with page, block, token and evidence coordinates."""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

from .models import (
    BlockType,
    BoundingBox,
    Document,
    DocumentKind,
    DocumentNode,
    Evidence,
    EvidenceType,
    LayoutBlock,
    NormalizedBoundingBox,
    Page,
    PageGeometry,
    ParsedDocument,
    ParsedPage,
    SourceLocator,
    TextToken,
    utc_now,
)
from .ocr import OCRPageResult, OCRProvider, OCRProviderError

PARSER_VERSION = "pdf-text-0.1.0"
DOCX_PARSER_VERSION = "docx-xml-0.1.0"
XLSX_PARSER_VERSION = "xlsx-xml-0.1.0"

_XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "package_rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class ParseError(RuntimeError):
    """Raised when the document cannot be safely parsed."""


def sha256_file(path: str | Path) -> str:
    """Hash a file in bounded chunks without loading it into memory."""

    file_path = Path(path)
    if not file_path.is_file():
        raise ParseError(f"document does not exist: {file_path}")

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_text(text: str) -> str:
    normalized_lines = []
    for line in text.replace("\u00a0", " ").replace("\r\n", "\n").split("\n"):
        normalized_lines.append(re.sub(r"[ \t]+", " ", line).strip())
    return "\n".join(normalized_lines).strip()


def _bbox(values: Any) -> BoundingBox:
    if not values or len(values) < 4:
        raise ParseError("PDF object has no valid bounding box")
    return BoundingBox(x1=float(values[0]), y1=float(values[1]), x2=float(values[2]), y2=float(values[3]))


def _normalized_bbox(box: BoundingBox, geometry: PageGeometry) -> NormalizedBoundingBox:
    return NormalizedBoundingBox(
        x1=min(box.x1 / geometry.width, 1),
        y1=min(box.y1 / geometry.height, 1),
        x2=min(box.x2 / geometry.width, 1),
        y2=min(box.y2 / geometry.height, 1),
    )


def _classify_text_block(text: str) -> BlockType:
    compact = " ".join(text.split())
    if len(compact) <= 80 and (
        re.match(r"^第[一二三四五六七八九十百千万零〇0-9]+[章节条款]", compact)
        or re.match(r"^\d+(?:\.\d+)*[、. ]", compact)
    ):
        return BlockType.HEADING
    return BlockType.PARAGRAPH


def _extract_block_text(raw_block: dict[str, Any]) -> str:
    lines = []
    for line in raw_block.get("lines", []):
        spans = line.get("spans", [])
        lines.append("".join(str(span.get("text", "")) for span in spans))
    return "\n".join(lines).strip()


def _casefold_span(text: str, query: str) -> tuple[int, int] | None:
    """Find a folded query while returning offsets in the original string."""

    folded_text_parts: list[str] = []
    original_indices: list[int] = []
    for index, character in enumerate(text):
        folded_character = character.casefold()
        folded_text_parts.append(folded_character)
        original_indices.extend([index] * len(folded_character))
    folded_text = "".join(folded_text_parts)
    folded_query = query.casefold()
    folded_start = folded_text.find(folded_query)
    if folded_start < 0 or not folded_query:
        return None
    folded_end = folded_start + len(folded_query) - 1
    return original_indices[folded_start], original_indices[folded_end] + 1


def parse_pdf(
    path: str | Path,
    *,
    package_id: str,
    document_kind: DocumentKind = DocumentKind.UNKNOWN,
    document_id: str | None = None,
    ocr_provider: OCRProvider | None = None,
) -> ParsedDocument:
    """Parse a PDF while retaining source coordinates and OCR quality boundaries.

    Digital text is extracted from the PDF text layer. A page with no text is
    explicitly marked ``needs_ocr``; this function never invents OCR output.
    """

    file_path = Path(path)
    source_sha256 = sha256_file(file_path)
    resolved_document_id = document_id or f"doc-{source_sha256[:16]}"

    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ParseError("PyMuPDF is required for PDF parsing") from exc

    parsed_pages: list[ParsedPage] = []
    document_quality_flags: list[str] = []

    try:
        with fitz.open(str(file_path)) as pdf:
            for page_index, pdf_page in enumerate(pdf):
                page_number = page_index + 1
                page_id = f"{resolved_document_id}-page-{page_number}"
                geometry = PageGeometry(
                    width=float(pdf_page.rect.width),
                    height=float(pdf_page.rect.height),
                    rotation=int(pdf_page.rotation),
                )
                raw_text = str(pdf_page.get_text("text", sort=True) or "")
                normalized_text = _normalize_text(raw_text)
                needs_ocr = not normalized_text
                quality_flags = ["needs_ocr"] if needs_ocr else []

                raw_words = pdf_page.get_text("words", sort=True) or []
                tokens: list[TextToken] = []
                for token_index, raw_word in enumerate(raw_words):
                    token_text = str(raw_word[4]).strip()
                    if not token_text:
                        continue
                    tokens.append(
                        TextToken(
                            token_id=f"{page_id}-token-{token_index}",
                            page_id=page_id,
                            text=token_text,
                            bbox=_bbox(raw_word[:4]),
                            source_block_index=int(raw_word[5]) if len(raw_word) > 5 else None,
                            source_line_index=int(raw_word[6]) if len(raw_word) > 6 else None,
                            source_word_index=int(raw_word[7]) if len(raw_word) > 7 else None,
                        )
                    )

                blocks: list[LayoutBlock] = []
                raw_dict = pdf_page.get_text("dict", sort=True) or {}
                for block_index, raw_block in enumerate(raw_dict.get("blocks", [])):
                    block_type = int(raw_block.get("type", 0))
                    block_id = f"{page_id}-block-{block_index}"
                    block_box = _bbox(raw_block.get("bbox"))
                    if block_type == 0:
                        block_text = _extract_block_text(raw_block)
                        if not block_text:
                            continue
                        semantic_type = _classify_text_block(block_text)
                    elif block_type == 1:
                        block_text = ""
                        semantic_type = BlockType.IMAGE
                    else:
                        block_text = ""
                        semantic_type = BlockType.UNKNOWN
                    block_token_ids = [
                        token.token_id
                        for token in tokens
                        if token.source_block_index == block_index
                    ]
                    blocks.append(
                        LayoutBlock(
                            block_id=block_id,
                            page_id=page_id,
                            order=len(blocks),
                            block_type=semantic_type,
                            text=block_text,
                            bbox=block_box,
                            confidence=1.0 if block_type == 0 else None,
                            token_ids=block_token_ids,
                            source_block_index=block_index,
                        )
                    )

                if needs_ocr and ocr_provider is not None:
                    try:
                        pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        ocr_result = OCRPageResult.model_validate(
                            ocr_provider.recognize(
                                pixmap.tobytes("png"),
                                page_number=page_number,
                                geometry=geometry,
                            )
                        )
                    except (OCRProviderError, OSError, ValueError) as exc:
                        ocr_result = OCRPageResult()
                        quality_flags.extend(["ocr_failed", f"ocr_error_{type(exc).__name__}"])
                    if ocr_result.blocks:
                        ocr_blocks: list[LayoutBlock] = []
                        for ocr_index, ocr_block in enumerate(ocr_result.blocks):
                            if (
                                ocr_block.bbox.x2 > geometry.width
                                or ocr_block.bbox.y2 > geometry.height
                            ):
                                quality_flags.append("ocr_bbox_out_of_bounds")
                                ocr_blocks = []
                                break
                            ocr_blocks.append(
                                LayoutBlock(
                                    block_id=f"{page_id}-ocr-block-{ocr_index}",
                                    page_id=page_id,
                                    order=ocr_index,
                                    block_type=ocr_block.block_type,
                                    text=ocr_block.text.strip(),
                                    bbox=ocr_block.bbox,
                                    confidence=ocr_block.confidence,
                                )
                            )
                        if ocr_blocks:
                            blocks = ocr_blocks
                            raw_text = "\n".join(block.text for block in ocr_blocks)
                            normalized_text = _normalize_text(raw_text)
                            needs_ocr = False
                            quality_flags = [
                                flag for flag in quality_flags if flag != "needs_ocr"
                            ]
                            quality_flags.append("ocr_applied")
                        else:
                            quality_flags.append("ocr_rejected")

                page = Page(
                    page_id=page_id,
                    document_id=resolved_document_id,
                    page_number=page_number,
                    geometry=geometry,
                    quality_flags=quality_flags,
                    needs_ocr=needs_ocr,
                )
                parsed_pages.append(
                    ParsedPage(
                        page=page,
                        raw_text=raw_text,
                        normalized_text=normalized_text,
                        blocks=blocks,
                        tokens=tokens,
                    )
                )
                if needs_ocr:
                    document_quality_flags.append(f"page_{page_number}_needs_ocr")
                elif "ocr_applied" in quality_flags:
                    document_quality_flags.append(f"page_{page_number}_ocr_applied")
                if "ocr_failed" in quality_flags or "ocr_rejected" in quality_flags:
                    document_quality_flags.append(f"page_{page_number}_ocr_quality_error")
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"failed to parse PDF {file_path.name}: {exc}") from exc

    if not parsed_pages:
        document_quality_flags.append("empty_document")
    parse_status = "failed" if "empty_document" in document_quality_flags else (
        "needs_ocr"
        if any(flag.endswith("_needs_ocr") for flag in document_quality_flags)
        else "parsed"
    )
    parser_version = PARSER_VERSION
    if ocr_provider is not None:
        parser_version = f"{PARSER_VERSION}+ocr-{ocr_provider.provider_version}"
    document = Document(
        document_id=resolved_document_id,
        package_id=package_id,
        filename=file_path.name,
        mime_type="application/pdf",
        source_sha256=source_sha256,
        document_kind=document_kind,
        page_count=len(parsed_pages),
        parser_version=parser_version,
        parse_status=parse_status,
        quality_flags=document_quality_flags,
        created_at=utc_now(),
    )
    return ParsedDocument(document=document, pages=parsed_pages)


_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    return "".join(
        str(text_node.text or "")
        for text_node in paragraph.findall(".//w:t", _DOCX_NS)
    ).strip()


def _docx_block_type(paragraph: ElementTree.Element) -> BlockType:
    style = paragraph.find("./w:pPr/w:pStyle", _DOCX_NS)
    style_name = style.attrib.get(f"{{{_DOCX_NS['w']}}}val", "") if style is not None else ""
    return BlockType.HEADING if style_name.lower().startswith("heading") else BlockType.PARAGRAPH


def parse_docx(
    path: str | Path,
    *,
    package_id: str,
    document_kind: DocumentKind = DocumentKind.UNKNOWN,
    document_id: str | None = None,
) -> ParsedDocument:
    """Parse DOCX XML without inventing page numbers unavailable in the source."""

    file_path = Path(path)
    source_sha256 = sha256_file(file_path)
    resolved_document_id = document_id or f"doc-{source_sha256[:16]}"
    nodes: list[DocumentNode] = []
    paragraph_index = 0
    try:
        with ZipFile(file_path) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            body = root.find("./w:body", _DOCX_NS)
            if body is None:
                raise ParseError("DOCX has no document body")
            table_index = 0
            for child in body:
                if child.tag == f"{{{_DOCX_NS['w']}}}p":
                    text = _docx_paragraph_text(child)
                    if text:
                        node_id = f"{resolved_document_id}-paragraph-{paragraph_index}"
                        nodes.append(
                            DocumentNode(
                                node_id=node_id,
                                document_id=resolved_document_id,
                                order=len(nodes),
                                block_type=_docx_block_type(child),
                                text=text,
                                confidence=1.0,
                                locator=SourceLocator(
                                    locator_type="document_block",
                                    block_id=node_id,
                                    paragraph_index=paragraph_index,
                                ),
                            )
                        )
                    paragraph_index += 1
                elif child.tag == f"{{{_DOCX_NS['w']}}}tbl":
                    rows = child.findall("./w:tr", _DOCX_NS)
                    for row_index, row in enumerate(rows):
                        cells = row.findall("./w:tc", _DOCX_NS)
                        for column_index, cell in enumerate(cells):
                            text = "\n".join(
                                value
                                for value in (
                                    _docx_paragraph_text(paragraph)
                                    for paragraph in cell.findall("./w:p", _DOCX_NS)
                                )
                                if value
                            ).strip()
                            if not text:
                                continue
                            node_id = (
                                f"{resolved_document_id}-table-{table_index}-"
                                f"row-{row_index}-cell-{column_index}"
                            )
                            nodes.append(
                                DocumentNode(
                                    node_id=node_id,
                                    document_id=resolved_document_id,
                                    order=len(nodes),
                                    block_type=BlockType.TABLE_CELL,
                                    text=text,
                                    confidence=1.0,
                                    locator=SourceLocator(
                                        locator_type="table_cell",
                                        block_id=node_id,
                                        table_index=table_index,
                                        row_index=row_index,
                                        column_index=column_index,
                                        cell_reference=(
                                            f"T{table_index + 1}R{row_index + 1}"
                                            f"C{column_index + 1}"
                                        ),
                                    ),
                                )
                            )
                    table_index += 1
    except ParseError:
        raise
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
        raise ParseError(f"failed to parse DOCX {file_path.name}: {exc}") from exc

    quality_flags = [] if nodes else ["empty_document"]
    document = Document(
        document_id=resolved_document_id,
        package_id=package_id,
        filename=file_path.name,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_sha256=source_sha256,
        document_kind=document_kind,
        page_count=0,
        parser_version=DOCX_PARSER_VERSION,
        parse_status="parsed" if nodes else "failed",
        quality_flags=quality_flags,
        created_at=utc_now(),
    )
    return ParsedDocument(document=document, nodes=nodes)


def _xlsx_column_number(cell_reference: str) -> int:
    match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", cell_reference)
    if match is None:
        raise ParseError(f"invalid XLSX cell reference: {cell_reference}")
    column_number = 0
    for character in match.group(1).upper():
        column_number = column_number * 26 + ord(character) - ord("A") + 1
    return column_number


def _xlsx_sheet_path(target: str) -> str:
    if target.startswith("/"):
        normalized = target.lstrip("/")
    else:
        normalized = posixpath.normpath(posixpath.join("xl", target))
    if not normalized.startswith("xl/") or ".." in normalized.split("/"):
        raise ParseError(f"unsafe XLSX worksheet path: {target}")
    return normalized


def _xlsx_shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(str(text_node.text or "") for text_node in item.findall(".//main:t", _XLSX_NS))
        for item in root.findall("./main:si", _XLSX_NS)
    ]


def _xlsx_cell_text(
    cell: ElementTree.Element,
    shared_strings: list[str],
) -> tuple[str, bool]:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("./main:v", _XLSX_NS)
    value = "" if value_node is None else str(value_node.text or "")
    if cell_type == "s":
        try:
            value = shared_strings[int(value)]
        except (IndexError, ValueError):
            raise ParseError("XLSX shared string index is invalid") from None
    elif cell_type == "inlineStr":
        value = "".join(
            str(text_node.text or "") for text_node in cell.findall(".//main:t", _XLSX_NS)
        )
    elif cell_type == "b":
        value = "TRUE" if value == "1" else "FALSE"
    formula_node = cell.find("./main:f", _XLSX_NS)
    has_uncached_formula = formula_node is not None and not value
    if has_uncached_formula:
        value = f"={formula_node.text or ''}"
    return value.strip(), has_uncached_formula


def _xlsx_workbook_sheets(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    targets = {
        relationship.attrib.get("Id", ""): relationship.attrib.get("Target", "")
        for relationship in relationships.findall("./package_rel:Relationship", _XLSX_NS)
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall("./main:sheets/main:sheet", _XLSX_NS):
        name = sheet.attrib.get("name", "")
        relationship_id = sheet.attrib.get(f"{{{_XLSX_NS['rel']}}}id", "")
        target = targets.get(relationship_id)
        if not name or not target:
            raise ParseError("XLSX worksheet relationship is incomplete")
        sheets.append((name, _xlsx_sheet_path(target)))
    return sheets


def parse_xlsx(
    path: str | Path,
    *,
    package_id: str,
    document_kind: DocumentKind = DocumentKind.UNKNOWN,
    document_id: str | None = None,
) -> ParsedDocument:
    """Parse XLSX cell values without evaluating formulas or inventing display values."""

    file_path = Path(path)
    source_sha256 = sha256_file(file_path)
    resolved_document_id = document_id or f"doc-{source_sha256[:16]}"
    nodes: list[DocumentNode] = []
    quality_flags: list[str] = []
    try:
        with ZipFile(file_path) as archive:
            shared_strings = _xlsx_shared_strings(archive)
            for sheet_name, sheet_path in _xlsx_workbook_sheets(archive):
                root = ElementTree.fromstring(archive.read(sheet_path))
                for cell in root.findall(".//main:sheetData/main:row/main:c", _XLSX_NS):
                    cell_reference = cell.attrib.get("r", "")
                    if not cell_reference:
                        raise ParseError("XLSX cell has no reference")
                    text, has_uncached_formula = _xlsx_cell_text(cell, shared_strings)
                    if not text:
                        continue
                    column_number = _xlsx_column_number(cell_reference)
                    row_match = re.search(r"[1-9][0-9]*$", cell_reference)
                    if row_match is None:
                        raise ParseError(f"invalid XLSX row reference: {cell_reference}")
                    row_number = int(row_match.group(0))
                    node_id = (
                        f"{resolved_document_id}-sheet-{len(nodes)}-"
                        f"{sheet_name}-{cell_reference}"
                    )
                    nodes.append(
                        DocumentNode(
                            node_id=node_id,
                            document_id=resolved_document_id,
                            order=len(nodes),
                            block_type=BlockType.TABLE_CELL,
                            text=text,
                            confidence=0.9 if has_uncached_formula else 1.0,
                            locator=SourceLocator(
                                locator_type="table_cell",
                                block_id=node_id,
                                sheet_name=sheet_name,
                                cell_reference=cell_reference,
                                row_index=row_number - 1,
                                column_index=column_number - 1,
                            ),
                        )
                    )
                    if has_uncached_formula and "formula_without_cached_value" not in quality_flags:
                        quality_flags.append("formula_without_cached_value")
    except ParseError:
        raise
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError, AttributeError, ValueError) as exc:
        raise ParseError(f"failed to parse XLSX {file_path.name}: {exc}") from exc

    if not nodes:
        quality_flags.append("empty_workbook")
    document = Document(
        document_id=resolved_document_id,
        package_id=package_id,
        filename=file_path.name,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source_sha256=source_sha256,
        document_kind=document_kind,
        page_count=0,
        parser_version=XLSX_PARSER_VERSION,
        parse_status="parsed" if nodes else "failed",
        quality_flags=quality_flags,
        created_at=utc_now(),
    )
    return ParsedDocument(document=document, nodes=nodes)


def parse_document(
    path: str | Path,
    *,
    package_id: str,
    document_kind: DocumentKind = DocumentKind.UNKNOWN,
    document_id: str | None = None,
    ocr_provider: OCRProvider | None = None,
) -> ParsedDocument:
    """Dispatch to a format adapter while keeping one parsed-document contract."""

    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return parse_pdf(
            path,
            package_id=package_id,
            document_kind=document_kind,
            document_id=document_id,
            ocr_provider=ocr_provider,
        )
    if suffix == ".docx":
        return parse_docx(
            path,
            package_id=package_id,
            document_kind=document_kind,
            document_id=document_id,
        )
    if suffix == ".xlsx":
        return parse_xlsx(
            path,
            package_id=package_id,
            document_kind=document_kind,
            document_id=document_id,
        )
    raise ParseError(f"unsupported document format: {Path(path).suffix or '<none>'}")


def find_text_evidence(
    parsed_document: ParsedDocument,
    query: str,
    *,
    evidence_prefix: str = "evidence",
) -> list[Evidence]:
    """Find block-level text evidence with page and bounding-box anchors."""

    if not query.strip():
        return []

    normalized_query = query.casefold()
    evidence: list[Evidence] = []
    for parsed_page in parsed_document.pages:
        for block in parsed_page.blocks:
            if not block.text or normalized_query not in block.text.casefold():
                continue
            span = _casefold_span(block.text, query)
            if span is None:
                continue
            page = parsed_page.page
            evidence_id = f"{evidence_prefix}-{page.page_number}-{block.order}"
            excerpt = block.text.strip()
            excerpt_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            evidence.append(
                Evidence(
                    evidence_id=evidence_id,
                    evidence_type=EvidenceType.TEXT,
                    document_id=parsed_document.document.document_id,
                    source_sha256=parsed_document.document.source_sha256,
                    locator=SourceLocator(
                        locator_type="text_span",
                        page_number=page.page_number,
                        block_id=block.block_id,
                        token_ids=block.token_ids,
                        bbox=block.bbox,
                        normalized_bbox=_normalized_bbox(block.bbox, page.geometry),
                        char_start=span[0],
                        char_end=span[1],
                    ),
                    raw_excerpt=excerpt,
                    display_excerpt=excerpt,
                    excerpt_sha256=excerpt_sha256,
                    extraction_method="pdf_text_block_match",
                    extraction_version=parsed_document.document.parser_version,
                    confidence=block.confidence,
                )
            )
    for node in parsed_document.nodes:
        if not node.text or normalized_query not in node.text.casefold():
            continue
        span = _casefold_span(node.text, query)
        if span is None:
            continue
        excerpt = node.text.strip()
        evidence_id = f"{evidence_prefix}-{parsed_document.document.document_id}-node-{node.order}"
        locator = node.locator.model_copy(update={"char_start": span[0], "char_end": span[1]})
        excerpt_sha256 = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        evidence.append(
            Evidence(
                evidence_id=evidence_id,
                evidence_type=(
                    EvidenceType.TABLE_CELL
                    if node.block_type == BlockType.TABLE_CELL
                    else EvidenceType.TEXT
                ),
                document_id=parsed_document.document.document_id,
                source_sha256=parsed_document.document.source_sha256,
                locator=locator,
                raw_excerpt=excerpt,
                display_excerpt=excerpt,
                excerpt_sha256=excerpt_sha256,
                extraction_method="document_block_match",
                extraction_version=parsed_document.document.parser_version,
                confidence=node.confidence,
            )
        )
    return evidence
