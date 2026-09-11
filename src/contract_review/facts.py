"""Deterministic fact extraction helpers with evidence references."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from .models import AttachmentReference, ContractFact, Evidence, ParsedDocument
from .parser import find_text_evidence

FACT_EXTRACTOR_VERSION = "deterministic-facts-0.1.0"


def _text_blocks(parsed_document: ParsedDocument) -> Iterable[tuple[str, str]]:
    for page in parsed_document.pages:
        for block in page.blocks:
            if block.text:
                yield block.block_id, block.text
    for node in parsed_document.nodes:
        if node.text:
            yield node.node_id, node.text


def extract_keyword_facts(
    parsed_documents: Sequence[ParsedDocument],
    terms: Iterable[str],
) -> tuple[list[ContractFact], list[Evidence]]:
    """Extract presence facts; absence is intentionally left to the rule engine."""

    facts: list[ContractFact] = []
    evidence_by_id: dict[str, Evidence] = {}
    for term in sorted({term.strip() for term in terms if term.strip()}):
        for parsed_document in parsed_documents:
            matches = find_text_evidence(parsed_document, term, evidence_prefix="keyword")
            if not matches:
                continue
            for item in matches:
                evidence_by_id[item.evidence_id] = item
            digest = hashlib.sha256(
                f"{parsed_document.document.document_id}\x1f{term}".encode("utf-8")
            ).hexdigest()[:16]
            facts.append(
                ContractFact(
                    fact_id=f"fact-keyword-{digest}",
                    fact_type="keyword_presence",
                    value=term,
                    normalized_value=term.casefold(),
                    evidence_ids=[item.evidence_id for item in matches],
                    confidence=1.0
                    if parsed_document.document.parse_status == "parsed"
                    else 0.0,
                    extractor_version=FACT_EXTRACTOR_VERSION,
                )
            )
    return facts, list(evidence_by_id.values())


_TAX_RATE_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[％%]")

# 百分比只有当附近出现税务语境时才作为税率候选，避免把付款比例、
# 违约金比例等误提取为税率（0.9 的付款比例曾被当成 90% 税率）。
_TAX_CONTEXT_KEYWORDS = ("税率", "增值税", "含税", "不含税", "税额", "发票", "开票", "税")
_TAX_CONTEXT_WINDOW = 10


def extract_tax_rate_facts(
    parsed_documents: Sequence[ParsedDocument],
) -> tuple[list[ContractFact], list[Evidence]]:
    """Extract percentage literals as candidate tax-rate facts.

    A percentage literal is only a candidate fact; the rule engine still needs
    the surrounding clause and contract type before treating it as compliant.
    """

    facts: list[ContractFact] = []
    evidence_by_id: dict[str, Evidence] = {}
    for parsed_document in parsed_documents:
        for block_id, block_text in _text_blocks(parsed_document):
            for match in _TAX_RATE_PATTERN.finditer(block_text):
                window = block_text[
                    max(0, match.start() - _TAX_CONTEXT_WINDOW) :
                    match.end() + _TAX_CONTEXT_WINDOW
                ]
                if not any(keyword in window for keyword in _TAX_CONTEXT_KEYWORDS):
                    continue
                value = float(match.group(1)) / 100
                matches = [
                    item
                    for item in find_text_evidence(
                        parsed_document,
                        match.group(0),
                        evidence_prefix="tax-rate",
                    )
                    if item.locator.block_id == block_id
                ]
                for item in matches:
                    evidence_by_id[item.evidence_id] = item
                if not matches:
                    continue
                digest = hashlib.sha256(
                    f"{parsed_document.document.document_id}\x1f{block_id}\x1f{match.start()}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:16]
                facts.append(
                    ContractFact(
                        fact_id=f"fact-tax-rate-{digest}",
                        fact_type="tax_rate",
                        value=match.group(0),
                        normalized_value=value,
                        unit="ratio",
                        evidence_ids=[item.evidence_id for item in matches],
                        confidence=1.0
                        if parsed_document.document.parse_status == "parsed"
                        else 0.0,
                        extractor_version=FACT_EXTRACTOR_VERSION,
                    )
                )
    return facts, list(evidence_by_id.values())


_ATTACHMENT_PATTERN = re.compile(
    r"(?:详见|参见|随附|附件(?:为|：|:)?)[\s\"“”']*([^，。；;\n\"“”']{2,40})"
)

# 模板套话过滤：合同效力/不可抗力等格式条款中的"附件"表述不是真实附件引用。
_ATTACHMENT_BOILERPLATE_MARKERS = (
    "不能",
    "不可",
    "作为",
    "本合",
    "其",
    "补充协议",
    "具有",
    "以及",
    "未尽事宜",
    "同等效力",
)
_ATTACHMENT_TRAILING_NOISE = ("相关内容", "内容", "文件")


def _normalize_attachment_name(name: str) -> str | None:
    """清洗附件名：优先取《》内名称，剔除模板套话与过长片段。"""
    bracket = re.search(r"[《]([^》]{2,40})[》]", name)
    if bracket:
        name = bracket.group(1)
    for noise in _ATTACHMENT_TRAILING_NOISE:
        if name.endswith(noise):
            name = name[: -len(noise)]
            break
    name = name.strip(" ：:、（）()")
    if len(name) < 2 or len(name) > 30:
        return None
    if any(marker in name for marker in _ATTACHMENT_BOILERPLATE_MARKERS):
        return None
    return name


def extract_attachment_references(
    parsed_documents: Sequence[ParsedDocument],
) -> tuple[list[AttachmentReference], list[Evidence]]:
    """Find conservative attachment references for later human confirmation."""

    references: list[AttachmentReference] = []
    evidence_by_id: dict[str, Evidence] = {}
    seen: set[tuple[str, str]] = set()
    for parsed_document in parsed_documents:
        source_text = "\n".join(
            [page.normalized_text for page in parsed_document.pages]
            + [node.text for node in parsed_document.nodes]
        )
        for match in _ATTACHMENT_PATTERN.finditer(source_text):
            name = _normalize_attachment_name(match.group(1))
            if not name:
                continue
            matches = find_text_evidence(parsed_document, name, evidence_prefix="attachment")
            if not matches:
                continue
            key = (parsed_document.document.document_id, name)
            if key in seen:
                continue
            seen.add(key)
            evidence_by_id.update({item.evidence_id: item for item in matches})
            digest = hashlib.sha256(
                f"{parsed_document.document.document_id}\x1f{name}".encode("utf-8")
            ).hexdigest()[:16]
            references.append(
                AttachmentReference(
                    reference_id=f"attachment-ref-{digest}",
                    referenced_name=name,
                    evidence_ids=[item.evidence_id for item in matches],
                    required=True,
                )
            )
    return references, list(evidence_by_id.values())
