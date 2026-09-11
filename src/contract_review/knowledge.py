"""Deterministic knowledge chunks and a provenance-preserving lexical retriever."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Protocol

from .index import index_text_evidence
from .models import (
    Evidence,
    EvidenceType,
    KnowledgeChunk,
    KnowledgeSourceKind,
    ParsedDocument,
    RetrievalHit,
    RetrievalTrace,
    RuleBundle,
    SourceLocator,
)

KNOWLEDGE_INDEX_VERSION = "lexical-knowledge-index-0.1.0"


class KnowledgeIndex(Protocol):
    """可替换检索器契约：词法/向量检索都产出同样的 RetrievalTrace。

    生产环境可替换为向量检索，但必须保留证据 ID 和版本信息。
    """

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        used_for_rule_ids: Sequence[str] = (),
    ) -> RetrievalTrace: ...


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold()))


def _rule_evidence(
    rule_id: str,
    source_locator: SourceLocator | None,
    text: str,
    source_sha256: str,
) -> Evidence:
    locator = source_locator or SourceLocator(
        locator_type="external_uri",
        external_uri=f"urn:contract-review:knowledge-rule:{rule_id}",
    )
    digest = hashlib.sha256(rule_id.encode("utf-8")).hexdigest()[:16]
    return Evidence(
        evidence_id=f"knowledge-rule-source-{digest}",
        evidence_type=EvidenceType.EXTERNAL_REFERENCE,
        source_sha256=source_sha256,
        locator=locator,
        raw_excerpt=text,
        display_excerpt=f"知识库规则来源：{text}",
        extraction_method="rule_snapshot_knowledge_ingest",
        extraction_version=KNOWLEDGE_INDEX_VERSION,
        confidence=1.0,
    )


def build_knowledge_corpus(
    parsed_documents: Sequence[ParsedDocument],
    *,
    rule_bundle: RuleBundle | None = None,
) -> tuple[list[KnowledgeChunk], list[Evidence]]:
    """从合同文档和可选规则快照构建带来源绑定的知识块。"""

    chunks: list[KnowledgeChunk] = []
    evidence: dict[str, Evidence] = {}
    for parsed_document in parsed_documents:
        block_evidence = index_text_evidence(parsed_document)
        for item in block_evidence:
            evidence[item.evidence_id] = item
            digest = hashlib.sha256(item.evidence_id.encode("utf-8")).hexdigest()[:16]
            chunks.append(
                KnowledgeChunk(
                    chunk_id=f"chunk-document-{digest}",
                    source_name=parsed_document.document.filename,
                    source_sha256=parsed_document.document.source_sha256,
                    source_version=parsed_document.document.parser_version,
                    content=item.raw_excerpt or "",
                    evidence_ids=[item.evidence_id],
                    source_kind=KnowledgeSourceKind.CONTRACT,
                    metadata={
                        "document_id": parsed_document.document.document_id,
                        "page_number": item.locator.page_number,
                        "block_id": item.locator.block_id,
                    },
                )
            )
    if rule_bundle is not None:
        for rule in rule_bundle.rules:
            # 规则块内容带 rule_id 前缀：语义模型收到的 context 是扁平列表，
            # 只有把规则 ID 写进块内容，模型才能把"规则 ID ↔ 规则定义"对应起来；
            # Playbook 也随规则快照进入上下文，避免模型在应用层复制企业立场。
            text = (
                f"{rule.rule_id} | "
                + " / ".join(
                    value
                    for value in (
                        f"version={rule.version}",
                        rule.category,
                        rule.title,
                        rule.condition,
                    )
                    if value
                )
            )
            if rule.playbook is not None:
                text += " | playbook=" + json.dumps(
                    rule.playbook.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            item = _rule_evidence(
                rule.rule_id,
                rule.source_locator,
                text,
                rule_bundle.source_sha256,
            )
            evidence[item.evidence_id] = item
            digest = hashlib.sha256(item.evidence_id.encode("utf-8")).hexdigest()[:16]
            chunks.append(
                KnowledgeChunk(
                    chunk_id=f"chunk-rule-{digest}",
                    source_name=rule_bundle.source_filename,
                    source_sha256=rule_bundle.source_sha256,
                    source_version=rule_bundle.bundle_id,
                    content=text,
                    evidence_ids=[item.evidence_id],
                    source_kind=KnowledgeSourceKind.RULE,
                    metadata={"rule_id": rule.rule_id, "legacy_id": rule.legacy_id},
                )
            )
    return chunks, list(evidence.values())


class LexicalKnowledgeIndex:
    """A deterministic baseline retriever; it is not the final evidence store."""

    def __init__(self, chunks: Sequence[KnowledgeChunk]) -> None:
        self.chunks = tuple(chunks)
        self._terms = {chunk.chunk_id: _terms(chunk.content) for chunk in self.chunks}

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        used_for_rule_ids: Sequence[str] = (),
    ) -> RetrievalTrace:
        if not query.strip():
            raise ValueError("retrieval query cannot be empty")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_terms = _terms(query)
        scored: list[RetrievalHit] = []
        for chunk in self.chunks:
            matched = sorted(query_terms & self._terms[chunk.chunk_id])
            if not matched:
                continue
            score = len(matched) / max(len(query_terms), 1)
            if query.casefold() in chunk.content.casefold():
                score += 0.5
            scored.append(
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    score=round(score, 6),
                    evidence_ids=chunk.evidence_ids,
                    matched_terms=matched,
                )
            )
        scored.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        hits = scored[:top_k]
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return RetrievalTrace(
            trace_id=f"retrieval-{digest}",
            query=query,
            index_version=KNOWLEDGE_INDEX_VERSION,
            top_k=top_k,
            hits=hits,
            used_for_rule_ids=list(used_for_rule_ids),
        )
