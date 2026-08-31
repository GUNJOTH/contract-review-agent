"""Evidence indexing over parsed documents.

The index is deliberately source-first: it stores stable locators and excerpts,
not embeddings. A vector index can later reference these evidence IDs without
becoming the authoritative source of a review conclusion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from .models import (
    BlockType,
    Evidence,
    EvidenceType,
    ParsedDocument,
    SourceLocator,
)
from .parser import _normalized_bbox

INDEX_VERSION = "evidence-index-0.1.0"


def _block_evidence_id(document_id: str, page_number: int, block_order: int) -> str:
    return f"text-{document_id}-p{page_number}-b{block_order}"


def index_text_evidence(parsed_document: ParsedDocument) -> list[Evidence]:
    """Create one stable evidence anchor for each extracted text block."""

    evidence: list[Evidence] = []
    for parsed_page in parsed_document.pages:
        page = parsed_page.page
        for block in parsed_page.blocks:
            if not block.text.strip():
                continue
            excerpt = block.text.strip()
            evidence.append(
                Evidence(
                    evidence_id=_block_evidence_id(
                        parsed_document.document.document_id,
                        page.page_number,
                        block.order,
                    ),
                    evidence_type=EvidenceType.TEXT,
                    document_id=parsed_document.document.document_id,
                    source_sha256=parsed_document.document.source_sha256,
                    locator=SourceLocator(
                        locator_type="text_span",
                        page_number=page.page_number,
                        printed_page_label=page.printed_page_label,
                        block_id=block.block_id,
                        token_ids=block.token_ids,
                        char_start=0,
                        char_end=len(block.text),
                        bbox=block.bbox,
                        normalized_bbox=_normalized_bbox(block.bbox, page.geometry),
                    ),
                    raw_excerpt=excerpt,
                    display_excerpt=excerpt,
                    excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    extraction_method="pdf_text_block_index",
                    extraction_version=(
                        f"{parsed_document.document.parser_version}+{INDEX_VERSION}"
                    ),
                    confidence=block.confidence,
                )
            )
    for node in parsed_document.nodes:
        if not node.text.strip():
            continue
        excerpt = node.text.strip()
        locator = node.locator.model_copy(
            update={"char_start": 0, "char_end": len(node.text)}
        )
        evidence.append(
            Evidence(
                evidence_id=f"text-{parsed_document.document.document_id}-node-{node.order}",
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
                excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                extraction_method="document_block_index",
                extraction_version=(
                    f"{parsed_document.document.parser_version}+{INDEX_VERSION}"
                ),
                confidence=node.confidence,
            )
        )
    return evidence


def index_package_snapshot(
    *,
    package_id: str,
    documents: Sequence[ParsedDocument],
    package_snapshot: str,
) -> Evidence:
    """Record the exact document set used by package-wide checks."""

    hashes = {
        parsed.document.document_id: parsed.document.source_sha256
        for parsed in documents
    }
    digest = hashlib.sha256(
        (package_id + "\x1f" + package_snapshot).encode("utf-8")
    ).hexdigest()[:16]
    return Evidence(
        evidence_id=f"package-snapshot-{digest}",
        evidence_type=EvidenceType.COMPARISON,
        package_id=package_id,
        source_document_ids=sorted(hashes),
        source_document_sha256=hashes,
        locator=SourceLocator(
            locator_type="external_uri",
            external_uri=f"urn:contract-review:package:{package_id}:{package_snapshot}",
        ),
        raw_excerpt=f"package_id={package_id}; source_snapshot={package_snapshot}",
        display_excerpt="本次审查使用的合同包快照。",
        extraction_method="contract_package_manifest",
        extraction_version=INDEX_VERSION,
        confidence=1.0,
    )


def evidence_by_id(evidence: Iterable[Evidence]) -> dict[str, Evidence]:
    """Build an index, tolerating the same anchor emitted by multiple extractors."""

    result: dict[str, Evidence] = {}
    for item in evidence:
        if item.evidence_id in result:
            existing = result[item.evidence_id]
            if existing.model_dump(mode="json", exclude={"captured_at"}) != item.model_dump(
                mode="json", exclude={"captured_at"}
            ):
                raise ValueError(f"conflicting evidence_id: {item.evidence_id}")
            continue
        result[item.evidence_id] = item
    return result
