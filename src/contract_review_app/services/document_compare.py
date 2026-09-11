"""合同文档对比：支持 Word / PDF 文本级差异和相似度。"""

from __future__ import annotations

import hashlib
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, Field

from contract_review import parse_contract_package
from contract_review.models import BlockType
from contract_review.parser import parse_document

from contract_review_app.services.document_preview import render_docx_preview, render_xlsx_preview
from contract_review_app.services.triton_ocr_provider import TritonOCRProvider

IGNORE_KEYS = (
    "ignore_symbols",
    "ignore_watermark",
    "ignore_seals",
    "ignore_images",
    "ignore_header_footer",
    "ignore_tables",
    "ignore_handwriting",
)

_SYMBOLS = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_SKIP_TYPES = {
    "ignore_tables": {BlockType.TABLE, BlockType.TABLE_CELL},
    "ignore_images": {BlockType.IMAGE},
    "ignore_header_footer": {BlockType.HEADER, BlockType.FOOTER},
    "ignore_seals": {BlockType.SEAL},
    "ignore_watermark": {BlockType.IMAGE},
}


class CompareUnit(BaseModel):
    index: int
    text: str
    kind: str = "paragraph"


class CompareChange(BaseModel):
    change_id: str
    kind: str
    base_index: int | None = None
    compare_index: int | None = None
    base_text: str = ""
    compare_text: str = ""


class CompareDocumentView(BaseModel):
    filename: str
    html: str
    unit_count: int


class DocumentCompareResult(BaseModel):
    similarity: float
    similarity_label: str
    base_filename: str
    compare_filename: str
    added: int = 0
    deleted: int = 0
    modified: int = 0
    changes: list[CompareChange] = Field(default_factory=list)
    base: CompareDocumentView
    compare: CompareDocumentView
    report: str = ""
    options: dict[str, bool] = Field(default_factory=dict)


def compare_contract_documents(
    base_file: tuple[str, bytes],
    compare_file: tuple[str, bytes],
    *,
    options: dict[str, bool] | None = None,
) -> DocumentCompareResult:
    flags = _normalize_options(options)
    base_name, base_bytes = base_file
    compare_name, compare_bytes = compare_file
    base_units = _extract_units(base_name, base_bytes, flags)
    compare_units = _extract_units(compare_name, compare_bytes, flags)
    base_norm = [_normalize(unit.text, flags) for unit in base_units]
    compare_norm = [_normalize(unit.text, flags) for unit in compare_units]
    matcher = SequenceMatcher(a=base_norm, b=compare_norm, autojunk=False)
    changes: list[CompareChange] = []
    added = deleted = modified = 0
    base_marks: dict[int, str] = {}
    compare_marks: dict[int, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for index in range(j1, j2):
                added += 1
                compare_marks[index] = "added"
                changes.append(
                    CompareChange(
                        change_id=f"add-{index}",
                        kind="added",
                        compare_index=index,
                        compare_text=compare_units[index].text,
                    )
                )
        elif tag == "delete":
            for index in range(i1, i2):
                deleted += 1
                base_marks[index] = "deleted"
                changes.append(
                    CompareChange(
                        change_id=f"del-{index}",
                        kind="deleted",
                        base_index=index,
                        base_text=base_units[index].text,
                    )
                )
        else:
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                base_index = i1 + offset
                compare_index = j1 + offset
                base_marks[base_index] = "modified"
                compare_marks[compare_index] = "modified"
                modified += 1
                changes.append(
                    CompareChange(
                        change_id=f"mod-{base_index}-{compare_index}",
                        kind="modified",
                        base_index=base_index,
                        compare_index=compare_index,
                        base_text=base_units[base_index].text,
                        compare_text=compare_units[compare_index].text,
                    )
                )
            for index in range(i1 + paired, i2):
                deleted += 1
                base_marks[index] = "deleted"
                changes.append(
                    CompareChange(
                        change_id=f"del-{index}",
                        kind="deleted",
                        base_index=index,
                        base_text=base_units[index].text,
                    )
                )
            for index in range(j1 + paired, j2):
                added += 1
                compare_marks[index] = "added"
                changes.append(
                    CompareChange(
                        change_id=f"add-{index}",
                        kind="added",
                        compare_index=index,
                        compare_text=compare_units[index].text,
                    )
                )
    joined_a = "\n".join(part for part in base_norm if part)
    joined_b = "\n".join(part for part in compare_norm if part)
    similarity = (
        SequenceMatcher(a=joined_a, b=joined_b, autojunk=False).ratio()
        if (joined_a or joined_b)
        else 1.0
    )
    result = DocumentCompareResult(
        similarity=round(similarity, 4),
        similarity_label=_similarity_label(similarity),
        base_filename=base_name,
        compare_filename=compare_name,
        added=added,
        deleted=deleted,
        modified=modified,
        changes=changes,
        base=CompareDocumentView(
            filename=base_name,
            html=_render_units(base_units, base_marks),
            unit_count=len(base_units),
        ),
        compare=CompareDocumentView(
            filename=compare_name,
            html=_render_units(compare_units, compare_marks),
            unit_count=len(compare_units),
        ),
        options=flags,
    )
    result.report = _render_report(result)
    return result


def _normalize_options(options: dict[str, bool] | None) -> dict[str, bool]:
    flags = {key: False for key in IGNORE_KEYS}
    for key, value in (options or {}).items():
        if key in flags:
            flags[key] = bool(value)
    return flags


def _extract_units(filename: str, content: bytes, flags: dict[str, bool]) -> list[CompareUnit]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        _, text = render_docx_preview(content)
        if flags.get("ignore_tables"):
            text = "\n".join(
                line for line in text.splitlines() if "\t" not in line
            )
        return _units_from_text(text)
    if suffix == ".xlsx":
        if flags.get("ignore_tables"):
            return []
        _, text = render_xlsx_preview(content)
        return _units_from_text(text)
    if suffix == ".pdf":
        return _units_from_parsed(_parse_pdf_or_ocr(filename, content), flags)
    if suffix == ".doc":
        raise ValueError("旧版 .doc 暂不支持对比，请另存为 .docx 或 PDF")
    raise ValueError(f"不支持的对比格式：{suffix or '未知格式'}")


def _units_from_text(text: str) -> list[CompareUnit]:
    units: list[CompareUnit] = []
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        units.append(CompareUnit(index=len(units), text=value, kind="paragraph"))
    return units


def _units_from_parsed(parsed, flags: dict[str, bool]) -> list[CompareUnit]:
    skip_types: set[BlockType] = set()
    for key, types in _SKIP_TYPES.items():
        if flags.get(key):
            skip_types.update(types)
    units: list[CompareUnit] = []
    if parsed.nodes:
        for node in parsed.nodes:
            if node.block_type in skip_types:
                continue
            text = (node.text or "").strip()
            if not text:
                continue
            units.append(CompareUnit(index=len(units), text=text, kind=str(node.block_type)))
        return units
    for page in parsed.pages:
        for block in page.blocks:
            if block.block_type in skip_types:
                continue
            text = (block.text or "").strip()
            if not text:
                continue
            units.append(CompareUnit(index=len(units), text=text, kind=str(block.block_type)))
        if not page.blocks:
            for line in (page.normalized_text or "").splitlines():
                text = line.strip()
                if text:
                    units.append(CompareUnit(index=len(units), text=text, kind="paragraph"))
    return units


def _parse_pdf_or_ocr(filename: str, content: bytes):
    with tempfile.TemporaryDirectory(prefix="contract-compare-") as tmp:
        path = Path(tmp) / (Path(filename).name or "document.pdf")
        path.write_bytes(content)
        parsed = parse_document(path, package_id="compare-pdf")
        has_text = any(page.normalized_text for page in parsed.pages) or any(
            node.text for node in parsed.nodes
        )
        if has_text:
            return parsed
        provider = TritonOCRProvider()
        _, items = parse_contract_package(
            [path],
            package_id=f"compare-{hashlib.sha256(content).hexdigest()[:12]}",
            ocr_provider=provider,
        )
    if not items:
        raise ValueError(f"无法解析文件：{filename}")
    return items[0]


def _normalize(text: str, flags: dict[str, bool]) -> str:
    value = text.replace("\u00a0", " ").strip()
    if flags.get("ignore_symbols"):
        return _SYMBOLS.sub("", value)
    return re.sub(r"\s+", " ", value)


def _similarity_label(score: float) -> str:
    percent = round(score * 100)
    if percent >= 95:
        return f"高度相似（{percent}%）"
    if percent >= 80:
        return f"较为相似（{percent}%）"
    if percent >= 50:
        return f"存在明显差异（{percent}%）"
    return f"差异较大（{percent}%）"


def _render_units(units: list[CompareUnit], marks: dict[int, str]) -> str:
    if not units:
        return "<p class='placeholder'>没有可对比的文本。</p>"
    parts = []
    for unit in units:
        css = marks.get(unit.index, "")
        class_attr = f" class='diff-{css}'" if css else ""
        parts.append(f"<p{class_attr}>{_escape(unit.text)}</p>")
    return "".join(parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_report(result: DocumentCompareResult) -> str:
    lines = [
        f"基准文档：{result.base_filename}",
        f"比对文档：{result.compare_filename}",
        f"相似度：{result.similarity_label}",
        f"新增 {result.added} 处 / 删除 {result.deleted} 处 / 修改 {result.modified} 处",
        "",
    ]
    labels = {"added": "新增", "deleted": "删除", "modified": "修改"}
    for index, change in enumerate(result.changes, 1):
        lines.append(f"{index}. [{labels.get(change.kind, change.kind)}]")
        if change.base_text:
            lines.append(f"   基准：{change.base_text}")
        if change.compare_text:
            lines.append(f"   比对：{change.compare_text}")
    if not result.changes:
        lines.append("未发现差异。")
    return "\n".join(lines)
