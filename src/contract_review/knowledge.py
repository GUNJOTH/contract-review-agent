"""确定性知识块和保留来源的词法候选检索器。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Protocol

from .index import index_text_evidence
from .models import (
    BlockType,
    Evidence,
    EvidenceType,
    KnowledgeChunk,
    KnowledgeSourceKind,
    ParsedDocument,
    RetrievalHit,
    RetrievalFilter,
    RetrievalFusion,
    RetrievalMode,
    RetrievalQuery,
    RetrievalSource,
    RetrievalTrace,
    RuleBundle,
    SourceLocator,
)
from .terminology import expand_terminology_terms, matched_terminology_terms

KNOWLEDGE_INDEX_VERSION = "lexical-knowledge-index-0.9.0"
BM25_K1 = 1.2
BM25_B = 0.75
EXACT_PHRASE_BOOST = 1.0
# 规则声明的事实锚点是合同候选层的高区分度信号：合同正文先通过锚点资格
# 门禁，再参与候选排序；事实仍必须由后续抽取器从 CandidateEvidence 中重新确认。
REQUIRED_FACT_ANCHOR_BOOST = 10.0
# 合同正文的中文单字重合度过高，容易把项目、合同等泛词扩散到无关片段。
# 2-3 gram 保留法律短语的局部精确性；单字仍可通过 exact/required anchor
# 的原文子串检查命中，不依赖 BM25 单字累积分数。
PRECISION_NGRAM_MAX_LENGTH = 3
# 该门槛只约束合同正文候选；规则定义候选仍由其规则过滤范围单独保留。
# 低于门槛的合同片段不会被强行补齐到 top_k，后续按证据不足进入 UNKNOWN。
MIN_CONTRACT_LEXICAL_SCORE = 4.0
# 在同一查询的合同候选中，明显低于最佳候选的弱相关片段不应继续占用
# 证据窗口；精确锚点和完整查询短语由独立保护条件保留。
MIN_CONTRACT_RELATIVE_SCORE_RATIO = 0.55
# 没有 required_fact_anchors 的规则仍可能依靠标题短语取得证据资格；保留
# 足够长的精确锚点，避免中文短片段因 BM25 累积分不足而被误删。
MIN_EXACT_ANCHOR_LENGTH = 3
# 直接调用索引时，短的否定词等法律表达也属于有效精确查询；它们不应
# 因为正文片段短、BM25 分值低而丢失。
MIN_EXACT_QUERY_LENGTH = 2
RRF_K = 60

_TOKEN_PATTERN = re.compile(
    r"\d[\d,]*(?:\.\d+)?%?|"
    r"[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*|"
    r"[\u4e00-\u9fff]+",
    flags=re.IGNORECASE,
)


def _normalized_text(text: str) -> str:
    """统一全角字符和大小写，保证数字及法律术语可稳定比较。"""

    return unicodedata.normalize("NFKC", text).casefold()


def _tokenize(text: str) -> list[str]:
    """生成保留数字、否定词和中文法律短语的确定性词元。

    中文不依赖外部分词器：生成有限长度的 2-3 gram，避免法律正文中大量
    泛化单字造成词频污染，同时使“不得”“除非”“不超过30日”等表达保持
    局部精确性。数字额外保留去千分位形式，兼顾“1,000”与“1000”的书写差异。
    单字锚点由检索器的原文子串匹配保留，不参与 BM25 的单字累积。
    """

    normalized = _normalized_text(text)
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        if token[0].isdigit():
            tokens.append(token)
            compact_number = token.replace(",", "")
            if compact_number != token:
                tokens.append(compact_number)
            continue
        if token[0].isascii():
            tokens.append(token)
            continue
        for ngram_length in range(2, PRECISION_NGRAM_MAX_LENGTH + 1):
            tokens.extend(
                token[index : index + ngram_length]
                for index in range(len(token) - ngram_length + 1)
            )
    return tokens


class KnowledgeIndex(Protocol):
    """可替换检索器契约：词法/向量检索都产出同样的 RetrievalTrace。

    检索只产生候选证据，不产生 Finding 或其它审核结论；替换实现必须保留
    证据 ID、过滤条件、融合方式和版本信息。
    """

    def retrieve(
        self,
        query: RetrievalQuery,
        *,
        top_k: int = 5,
        used_for_rule_ids: Sequence[str] = (),
    ) -> RetrievalTrace: ...


def _terms(text: str) -> set[str]:
    return set(_tokenize(text))


def _query_phrases(text: str) -> list[str]:
    """提取较长中文查询短语，供命中元数据复用。"""

    phrases: list[str] = []
    for match in re.finditer(r"[\u4e00-\u9fff]+", _normalized_text(text)):
        token = match.group(0)
        for ngram_length in range(4, min(6, len(token)) + 1):
            phrases.extend(
                token[index : index + ngram_length]
                for index in range(len(token) - ngram_length + 1)
            )
    return list(dict.fromkeys(phrases))


def _matched_query_phrases(
    phrases: Sequence[str],
    normalized_content: str,
) -> list[str]:
    """提取命中的较长中文查询短语，仅用于可解释的命中元数据。

    长 n-gram 不进入 BM25 词频统计，避免改变小语料上的分数分布；保留在
    ``matched_terms`` 中是为了让“不得超过”等精确法律表达仍可被审计查看。
    """

    return [phrase for phrase in phrases if phrase in normalized_content]


def chunk_matches_retrieval_filter(
    chunk: KnowledgeChunk,
    retrieval_filter: RetrievalFilter,
) -> bool:
    """判断知识块是否属于调用方声明的证据范围。"""

    if retrieval_filter.source_names and chunk.source_name not in retrieval_filter.source_names:
        return False
    if (
        retrieval_filter.source_sha256s
        and chunk.source_sha256 not in retrieval_filter.source_sha256s
    ):
        return False
    if (
        retrieval_filter.source_versions
        and chunk.source_version not in retrieval_filter.source_versions
    ):
        return False
    if (
        retrieval_filter.source_kinds
        and chunk.source_kind not in retrieval_filter.source_kinds
    ):
        return False
    if (
        retrieval_filter.document_kinds
        and chunk.source_kind == KnowledgeSourceKind.CONTRACT
        and chunk.metadata.get("document_kind")
        not in {kind.value for kind in retrieval_filter.document_kinds}
    ):
        return False
    if retrieval_filter.evidence_ids and not set(chunk.evidence_ids).issubset(
        retrieval_filter.evidence_ids
    ):
        return False

    if chunk.source_kind == KnowledgeSourceKind.CONTRACT:
        document_id = chunk.metadata.get("document_id")
        if retrieval_filter.document_ids and document_id not in retrieval_filter.document_ids:
            return False
        if retrieval_filter.clause_ids and not set(chunk.clause_ids).intersection(
            retrieval_filter.clause_ids
        ):
            return False
        return True

    rule_id = chunk.metadata.get("rule_id")
    rule_version = chunk.metadata.get("rule_version")
    if (
        retrieval_filter.applicable_rule_ids
        and rule_id not in retrieval_filter.applicable_rule_ids
    ):
        return False
    if retrieval_filter.rule_versions and rule_version not in retrieval_filter.rule_versions:
        return False
    return True


def contract_chunk_has_required_fact_anchor(
    chunk: KnowledgeChunk,
    query: RetrievalQuery,
    *,
    matched_anchors: Sequence[str] | None = None,
) -> bool:
    """判断合同正文块是否命中查询声明的必要事实锚点。

    查询已经声明必要事实时，只有语义相近但不含任何登记表达的正文块不能
    占用确定性词法候选名额。未登记的同义表达应先进入术语目录并经过回归
    标注，不能在这里用无条件兜底放宽事实边界；规则定义块不受此门禁影响。
    """

    if chunk.source_kind != KnowledgeSourceKind.CONTRACT:
        return True
    if not query.required_fact_anchors:
        return True
    if matched_anchors is None:
        matched_anchors = matched_terminology_terms(
            query.required_fact_anchors,
            chunk.content,
        )
    return bool(matched_anchors)


def _retrieval_trace_id(
    query: RetrievalQuery,
    used_for_rule_ids: Sequence[str],
    *,
    top_k: int | None = None,
) -> str:
    payload = {
        "query": query.model_dump(mode="json"),
        "used_for_rule_ids": list(used_for_rule_ids),
        "top_k": top_k,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"retrieval-{digest}"


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


def _document_chunk_metadata(
    parsed_document: ParsedDocument,
    item: Evidence,
) -> dict[str, object]:
    """把解析器的版面顺序和块类型带入知识块，供条款分段使用。"""

    metadata: dict[str, object] = {
        "document_id": parsed_document.document.document_id,
        "document_kind": parsed_document.document.document_kind.value,
        "page_number": item.locator.page_number,
        "block_id": item.locator.block_id,
        "source_order": 0,
        "block_type": BlockType.UNKNOWN.value,
        "is_heading": False,
    }
    block_id = item.locator.block_id
    for page in parsed_document.pages:
        for block in page.blocks:
            if block.block_id != block_id:
                continue
            metadata.update(
                {
                    "source_order": block.order,
                    "block_type": block.block_type.value,
                    "is_heading": block.block_type == BlockType.HEADING,
                }
            )
            return metadata
    for node in parsed_document.nodes:
        if node.node_id != block_id:
            continue
        metadata.update(
            {
                "source_order": node.order,
                "block_type": node.block_type.value,
                "is_heading": node.block_type == BlockType.HEADING,
                "table_index": node.locator.table_index,
                "row_index": node.locator.row_index,
                "column_index": node.locator.column_index,
            }
        )
        return metadata
    return metadata


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
                    metadata=_document_chunk_metadata(parsed_document, item),
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
                    metadata={
                        "rule_id": rule.rule_id,
                        "rule_version": rule.version,
                        "legacy_id": rule.legacy_id,
                        "category": rule.category,
                        "applies_to": list(rule.applies_to),
                        "playbook_id": (
                            rule.playbook.playbook_id if rule.playbook is not None else None
                        ),
                    },
                )
            )
    return chunks, list(evidence.values())


class LexicalKnowledgeIndex:
    """确定性的 BM25 词法候选检索器，不承担最终审核判断。"""

    def __init__(self, chunks: Sequence[KnowledgeChunk]) -> None:
        self.chunks = tuple(chunks)
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        self._token_counts = {
            chunk.chunk_id: Counter(_tokenize(chunk.content)) for chunk in self.chunks
        }
        self._document_lengths = {
            chunk_id: sum(counts.values())
            for chunk_id, counts in self._token_counts.items()
        }

    def retrieve(
        self,
        query: RetrievalQuery,
        *,
        top_k: int = 5,
        used_for_rule_ids: Sequence[str] = (),
    ) -> RetrievalTrace:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if query.rule_id not in set(used_for_rule_ids):
            raise ValueError("检索查询的 rule_id 必须出现在 used_for_rule_ids 中")
        effective_filter = query.retrieval_filter
        # 结构化字段与可读查询文本共同进入词法候选生成器。这样数字、否定词、
        # 定义词和精确锚点不会只停留在审计元数据里，而是真正影响 BM25 召回。
        query_parts = expand_terminology_terms(
            [
                query.text,
                *query.lexical_terms,
                *query.exact_anchors,
                *query.numeric_anchors,
                *query.negation_anchors,
            ]
        )
        query_terms = _terms("\x1f".join(query_parts))
        query_phrases = _query_phrases(query.text)
        normalized_numeric_anchors = [
            _normalized_text(anchor).strip()
            for anchor in query.numeric_anchors
            if _normalized_text(anchor).strip()
        ]
        normalized_negation_anchors = [
            _normalized_text(anchor).strip()
            for anchor in query.negation_anchors
            if _normalized_text(anchor).strip()
        ]
        candidate_chunks = [
            chunk
            for chunk in self.chunks
            if chunk_matches_retrieval_filter(chunk, effective_filter)
        ]
        document_count = len(candidate_chunks)
        average_length = (
            sum(self._document_lengths[chunk.chunk_id] for chunk in candidate_chunks)
            / document_count
            if document_count
            else 0.0
        )
        document_frequency = Counter(
            term
            for chunk in candidate_chunks
            for term in self._token_counts[chunk.chunk_id]
        )
        normalized_query = _normalized_text(query.text).strip()
        compact_query = "".join(normalized_query.split())
        scored: list[RetrievalHit] = []
        protected_contract_chunk_ids: set[str] = set()
        for chunk in candidate_chunks:
            token_counts = self._token_counts[chunk.chunk_id]
            matched = sorted(query_terms.intersection(token_counts))
            normalized_content = _normalized_text(chunk.content)
            exact_anchor_matches = matched_terminology_terms(
                query.exact_anchors,
                chunk.content,
            )
            matched_query_phrases = _matched_query_phrases(
                query_phrases,
                normalized_content,
            )
            required_fact_anchor_matches = matched_terminology_terms(
                query.required_fact_anchors,
                chunk.content,
            )
            if not contract_chunk_has_required_fact_anchor(
                chunk,
                query,
                matched_anchors=required_fact_anchor_matches,
            ):
                continue
            if not matched and not exact_anchor_matches:
                continue
            document_length = self._document_lengths[chunk.chunk_id]
            score = 0.0
            for term in matched:
                frequency = token_counts[term]
                inverse_document_frequency = (
                    0.0
                    if document_count == 0
                    else math.log(
                        1
                        + (document_count - document_frequency[term] + 0.5)
                        / (document_frequency[term] + 0.5)
                    )
                )
                normalization = (
                    frequency
                    + BM25_K1
                    * (
                        1
                        - BM25_B
                        + BM25_B * document_length / max(average_length, 1.0)
                    )
                )
                score += (
                    inverse_document_frequency
                    * frequency
                    * (BM25_K1 + 1)
                    / max(normalization, 1e-12)
                )
            if normalized_query and normalized_query in normalized_content:
                score += EXACT_PHRASE_BOOST
            score += EXACT_PHRASE_BOOST * len(exact_anchor_matches)
            score += REQUIRED_FACT_ANCHOR_BOOST * len(
                required_fact_anchor_matches
            )
            # 否定和数字锚点是法律风险的高区分度信号，命中时提高候选排序，
            # 但仍然只改变候选顺序，不在检索层生成规则结论。
            score += 0.5 * sum(
                anchor in normalized_content
                for anchor in normalized_numeric_anchors
            )
            score += 0.5 * sum(
                anchor in normalized_content
                for anchor in normalized_negation_anchors
            )
            has_precise_exact_anchor = any(
                len(anchor) >= MIN_EXACT_ANCHOR_LENGTH
                for anchor in exact_anchor_matches
            )
            has_exact_query_phrase = (
                len(compact_query) >= MIN_EXACT_QUERY_LENGTH
                and compact_query in normalized_content
            )
            if chunk.source_kind == KnowledgeSourceKind.CONTRACT and (
                has_precise_exact_anchor or has_exact_query_phrase
            ):
                protected_contract_chunk_ids.add(chunk.chunk_id)
            if (
                chunk.source_kind == KnowledgeSourceKind.CONTRACT
                and score < MIN_CONTRACT_LEXICAL_SCORE
                and not has_precise_exact_anchor
                and not has_exact_query_phrase
            ):
                continue
            matched_terms = list(
                dict.fromkeys(
                    [
                        *matched,
                        *exact_anchor_matches,
                        *required_fact_anchor_matches,
                        *matched_query_phrases,
                    ]
                )
            )
            scored.append(
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    score=round(score, 6),
                    evidence_ids=chunk.evidence_ids,
                    matched_terms=matched_terms,
                    retrieval_sources=[RetrievalSource.LEXICAL],
                )
            )
        contract_scores = [
            hit.score
            for hit in scored
            if self._chunk_by_id[hit.chunk_id].source_kind
            == KnowledgeSourceKind.CONTRACT
        ]
        best_contract_score = max(contract_scores, default=0.0)
        if best_contract_score > 0:
            scored = [
                hit
                for hit in scored
                if self._chunk_by_id[hit.chunk_id].source_kind
                != KnowledgeSourceKind.CONTRACT
                or hit.chunk_id in protected_contract_chunk_ids
                or hit.score
                >= best_contract_score * MIN_CONTRACT_RELATIVE_SCORE_RATIO
            ]
        scored.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        hits = [
            hit.model_copy(update={"lexical_rank": rank})
            for rank, hit in enumerate(scored[:top_k], start=1)
        ]
        return RetrievalTrace(
            trace_id=_retrieval_trace_id(
                query,
                used_for_rule_ids,
                top_k=top_k,
            ),
            retrieval_query=query,
            index_version=KNOWLEDGE_INDEX_VERSION,
            retrieval_mode=RetrievalMode.LEXICAL,
            fusion_method=RetrievalFusion.NONE,
            top_k=top_k,
            hits=hits,
            used_for_rule_ids=list(used_for_rule_ids),
        )
