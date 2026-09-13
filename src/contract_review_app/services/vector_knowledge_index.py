"""向量检索索引：基于 OpenAI 兼容 /v1/embeddings 的合同条款召回。

与引擎的 ``KnowledgeIndex`` 契约一致，产出带证据 ID 的 RetrievalTrace。

确定性设计：embedding 服务在跨批次调用时存在约 1e-3 的元素级噪声，会导致
排序在微小分数边界上翻转，破坏引擎"同一输入可重放"的指纹契约。因此向量
按 ``(模型, 内容哈希)`` 缓存到内存和磁盘（``runtime/embedding_cache``），
同一审查的两次检索运行和跨进程回放都使用完全相同的向量；相似度分数量化
到 2 位小数，排序并列时按 chunk_id 确定性打破。embedding 服务不可用时
自动降级到词法基线，保证审查不中断。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

import httpx
from loguru import logger

from contract_review.knowledge import (
    LexicalKnowledgeIndex,
    RRF_K,
    _retrieval_trace_id,
    chunk_matches_retrieval_filter,
)
from contract_review.models import (
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalHit,
    RetrievalFusion,
    RetrievalMode,
    RetrievalQuery,
    RetrievalSource,
    RetrievalTrace,
)
from contract_review.terminology import expand_terminology_text

from contract_review_app.config import settings
from contract_review_app.services.pii_gate import gate_external_model_input
from contract_review_app.telemetry.tracing import start_span

VECTOR_INDEX_VERSION = "vector-knowledge-0.5.0"
# 混合索引包含词法分支；词法候选门控或分词策略变化时必须生成新的
# 轨迹版本，避免旧的融合结果被误认为可直接回放。
HYBRID_INDEX_VERSION = "hybrid-knowledge-rrf-0.5.0"
MIN_VECTOR_DOCUMENT_SCORE = 0.1
HYBRID_CANDIDATE_MULTIPLIER = 3

# 每个规则查询的槽位分配：规则定义块（模型靠它知道规则要求什么）与
# 正文证据块（模型判断的依据）。查询是规则标题，规则块相似度天然最高，
# 若不分开排名会占满 top-k，模型将看不到合同正文。规则定义块已强制必达
# （见 retrieve 中的 used_rule_ids 匹配），因此规则槽只需 1 个。
RULE_DEFINITION_SLOTS = 1

# 检索时过滤过短的正文碎片（表头/标签如"帐 号""税费"）：embedding 对短
# 文本的相似度虚高，会挤掉真正的条款块。
MIN_DOCUMENT_CHUNK_CHARS = 8

# 进程内缓存：{(模型, 内容哈希): 向量}，保证一次审查内多次检索完全一致
_EMBEDDING_MEMORY_CACHE: dict[tuple[str, str], list[float]] = {}


def _retrievable_chunks(chunks: Sequence[KnowledgeChunk]) -> list[KnowledgeChunk]:
    """过滤不具备足够语义上下文的正文碎片。"""

    return [
        chunk
        for chunk in chunks
        if chunk.source_kind == KnowledgeSourceKind.RULE
        or len(chunk.content.strip()) >= MIN_DOCUMENT_CHUNK_CHARS
    ]


def embed_texts(texts: list[str], *, use_cache: bool = True) -> list[list[float]]:
    """批量 embedding（内存+磁盘缓存）；失败抛异常由调用方处理。"""
    if not texts:
        return []
    if not use_cache:
        return _call_embedding_api(texts)
    model = settings.CONTRACT_REVIEW_EMBEDDING_MODEL
    cache_dir = settings.resolve_path(settings.CONTRACT_REVIEW_EMBEDDING_CACHE_DIR)
    vectors: list[list[float] | None] = [None] * len(texts)
    missing: list[tuple[int, str]] = []
    for index, text in enumerate(texts):
        key = (model, _text_digest(text))
        cached = _EMBEDDING_MEMORY_CACHE.get(key)
        if cached is None:
            cached = _load_disk_cache(cache_dir, key)
        if cached is not None:
            vectors[index] = cached
        else:
            missing.append((index, text))
    if missing:
        results = _call_embedding_api([text for _, text in missing])
        if len(results) != len(missing):
            raise ValueError("embedding API returned a mismatched batch size")
        for (index, text), vector in zip(missing, results):
            key = (model, _text_digest(text))
            _EMBEDDING_MEMORY_CACHE[key] = vector
            _save_disk_cache(cache_dir, key, vector)
            vectors[index] = vector
    if any(vector is None for vector in vectors):
        raise ValueError("embedding 缓存或 API 返回了不完整的向量批次")
    # 保持输入顺序；缺失项不能被静默过滤，否则向量会和知识块错位。
    return [vector for vector in vectors if vector is not None]


class VectorKnowledgeIndex:
    """批量 embedding + 余弦相似度检索；失败自动降级词法。"""

    def __init__(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        use_cache: bool = True,
    ) -> None:
        self.chunks = tuple(chunks)
        self._use_cache = use_cache
        self._retrievable = _retrievable_chunks(self.chunks)
        # 短碎片只会干扰向量相似度，不能从 BM25 的精确词法候选中删除。
        self._lexical = LexicalKnowledgeIndex(self.chunks)
        self._vectors: list[list[float]] | None = None
        self._fallback = False
        try:
            self._vectors = embed_texts(
                [chunk.content for chunk in self._retrievable],
                use_cache=self._use_cache,
            )
        except Exception as exc:
            self._fallback = True
            logger.warning(f"向量检索不可用，降级到词法基线: {exc}")

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
        if self._fallback or self._vectors is None:
            trace = self._lexical.retrieve(
                query,
                top_k=top_k,
                used_for_rule_ids=used_for_rule_ids,
            )
            return trace.model_copy(
                update={"index_version": f"{trace.index_version}-vector-fallback"}
            )
        try:
            query_vector = embed_texts(
                [expand_terminology_text(query.text)],
                use_cache=self._use_cache,
            )[0]
        except Exception as exc:
            logger.warning(f"查询向量生成失败，降级到词法基线: {exc}")
            trace = self._lexical.retrieve(
                query,
                top_k=top_k,
                used_for_rule_ids=used_for_rule_ids,
            )
            return trace.model_copy(
                update={"index_version": f"{trace.index_version}-vector-fallback"}
            )
        rule_scored, doc_scored = [], []
        for chunk, vector in zip(self._retrievable, self._vectors):
            if not chunk_matches_retrieval_filter(chunk, effective_filter):
                continue
            score = round(max(0.0, _cosine(query_vector, vector)), 2)
            item = (chunk, score)
            if chunk.source_kind == KnowledgeSourceKind.RULE:
                rule_scored.append(item)
            elif score >= MIN_VECTOR_DOCUMENT_SCORE:
                doc_scored.append(item)

        def _top(items, limit: int) -> list[RetrievalHit]:
            ranked = sorted(items, key=lambda item: (-item[1], item[0].chunk_id))
            return [
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    score=score,
                    evidence_ids=chunk.evidence_ids,
                    matched_terms=[],
                    retrieval_sources=[RetrievalSource.VECTOR],
                )
                for chunk, score in ranked[:limit]
            ]

        # 强制包含当前规则自身的定义块（引擎把 rule_id 传入 used_for_rule_ids，
        # 规则块的业务来源和 metadata 都带规则身份），避免模型缺少规则定义。
        used_rule_ids = set(used_for_rule_ids)
        forced = [
            item
            for item in rule_scored
            if (item[0].metadata or {}).get("rule_id") in used_rule_ids
        ]
        rule_hits = _top(forced, RULE_DEFINITION_SLOTS)
        taken = {hit.chunk_id for hit in rule_hits}
        remaining = RULE_DEFINITION_SLOTS - len(rule_hits)
        if remaining > 0:
            extras = [
                hit
                for hit in _top(rule_scored, len(rule_scored))
                if hit.chunk_id not in taken
            ][:remaining]
            rule_hits = [*rule_hits, *extras]
            taken.update(hit.chunk_id for hit in extras)
        doc_slots = max(0, top_k - len(rule_hits))
        doc_hits = _top(doc_scored, doc_slots)
        if len(doc_hits) < doc_slots:
            # 正文块不足时只保留规则定义候选，不用低相似度正文补齐。
            fillers = [
                hit
                for hit in _top(rule_scored, len(rule_scored))
                if hit.chunk_id not in taken
            ][: doc_slots - len(doc_hits)]
            doc_hits = [*doc_hits, *fillers]
        hits = [
            hit.model_copy(update={"vector_rank": rank})
            for rank, hit in enumerate((rule_hits + doc_hits)[:top_k], start=1)
        ]
        return RetrievalTrace(
            trace_id=_retrieval_trace_id(
                query,
                used_for_rule_ids,
                top_k=top_k,
            ),
            retrieval_query=query,
            index_version=(
                f"{VECTOR_INDEX_VERSION}-{settings.CONTRACT_REVIEW_EMBEDDING_MODEL}"
            ),
            retrieval_mode=RetrievalMode.VECTOR,
            fusion_method=RetrievalFusion.NONE,
            top_k=top_k,
            hits=hits,
            used_for_rule_ids=list(used_for_rule_ids),
        )


class HybridKnowledgeIndex:
    """使用 RRF 融合词法与向量候选的确定性检索适配器。

    词法召回负责保留精确术语命中，向量召回负责补充同义或隐含表达；两者
    只负责生成候选证据，最终规则结论仍由领域引擎和 Playbook 决定。向量
    服务不可用时直接保留词法降级轨迹，不把降级伪装成混合结果。
    """

    def __init__(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        use_cache: bool = True,
    ) -> None:
        self.chunks = tuple(chunks)
        # 向量分支过滤短碎片，BM25 分支必须覆盖完整合同语料，避免漏掉短的
        # 否定词、期限和定义表达。
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        self._lexical = LexicalKnowledgeIndex(self.chunks)
        self._vector = VectorKnowledgeIndex(self.chunks, use_cache=use_cache)

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
        candidate_top_k = max(
            top_k * HYBRID_CANDIDATE_MULTIPLIER,
            top_k + 1,
        )
        vector_trace = self._vector.retrieve(
            query,
            top_k=candidate_top_k,
            used_for_rule_ids=used_for_rule_ids,
        )
        # VectorKnowledgeIndex 已经把初始化和查询阶段的失败降级为词法结果。
        # 此时不再二次融合，避免相同命中被错误放大。
        if vector_trace.retrieval_mode != RetrievalMode.VECTOR:
            return vector_trace.model_copy(
                update={
                    "trace_id": _retrieval_trace_id(
                        query,
                        used_for_rule_ids,
                        top_k=top_k,
                    ),
                    "index_version": (
                        f"{vector_trace.index_version}+{HYBRID_INDEX_VERSION}"
                    ),
                    "top_k": top_k,
                    "hits": vector_trace.hits[:top_k],
                    "retrieval_mode": RetrievalMode.LEXICAL,
                    "fusion_method": RetrievalFusion.NONE,
                }
            )

        lexical_trace = self._lexical.retrieve(
            query,
            top_k=candidate_top_k,
            used_for_rule_ids=used_for_rule_ids,
        )
        vector_scores = {
            hit.chunk_id: hit.score for hit in vector_trace.hits
        }
        lexical_scores = {
            hit.chunk_id: hit.score for hit in lexical_trace.hits
        }
        vector_hits = {hit.chunk_id: hit for hit in vector_trace.hits}
        lexical_hits = {hit.chunk_id: hit for hit in lexical_trace.hits}
        candidate_ids = set(vector_scores) | set(lexical_scores)
        fused: dict[str, RetrievalHit] = {}
        for chunk_id in candidate_ids:
            chunk = self._chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            vector_hit = vector_hits.get(chunk_id)
            lexical_hit = lexical_hits.get(chunk_id)
            vector_rank = vector_hit.vector_rank if vector_hit is not None else None
            lexical_rank = (
                lexical_hit.lexical_rank if lexical_hit is not None else None
            )
            matched_terms = sorted(set(lexical_hit.matched_terms if lexical_hit else []))
            sources = sorted(
                {
                    *(vector_hit.retrieval_sources if vector_hit else []),
                    *(lexical_hit.retrieval_sources if lexical_hit else []),
                },
                key=lambda item: item.value,
            )
            fused[chunk_id] = RetrievalHit(
                chunk_id=chunk_id,
                score=round(
                    (1 / (RRF_K + lexical_rank) if lexical_rank else 0.0)
                    + (1 / (RRF_K + vector_rank) if vector_rank else 0.0),
                    12,
                ),
                evidence_ids=chunk.evidence_ids,
                matched_terms=matched_terms,
                retrieval_sources=sources,
                lexical_rank=lexical_rank,
                vector_rank=vector_rank,
            )

        def ranked(items: Sequence[RetrievalHit]) -> list[RetrievalHit]:
            return sorted(items, key=lambda hit: (-hit.score, hit.chunk_id))

        forced_rule_ids = set(used_for_rule_ids)
        forced = ranked(
            [
                hit
                for hit in fused.values()
                if (
                    self._chunks_by_id[hit.chunk_id].source_kind
                    == KnowledgeSourceKind.RULE
                    and (self._chunks_by_id[hit.chunk_id].metadata or {}).get(
                        "rule_id"
                    )
                    in forced_rule_ids
                )
            ]
        )[:RULE_DEFINITION_SLOTS]
        taken = {hit.chunk_id for hit in forced}
        remaining = max(0, top_k - len(forced))
        document_hits = ranked(
            [
                hit
                for hit in fused.values()
                if hit.chunk_id not in taken
                and self._chunks_by_id[hit.chunk_id].source_kind
                == KnowledgeSourceKind.CONTRACT
            ]
        )[:remaining]
        taken.update(hit.chunk_id for hit in document_hits)
        remaining -= len(document_hits)
        rule_fillers = ranked(
            [hit for hit in fused.values() if hit.chunk_id not in taken]
        )[:remaining]
        hits = [*forced, *document_hits, *rule_fillers]
        return RetrievalTrace(
            trace_id=_retrieval_trace_id(
                query,
                used_for_rule_ids,
                top_k=top_k,
            ),
            retrieval_query=query,
            index_version=(
                f"{vector_trace.index_version}+{HYBRID_INDEX_VERSION}"
            ),
            retrieval_mode=RetrievalMode.HYBRID,
            fusion_method=RetrievalFusion.RRF,
            top_k=top_k,
            hits=hits[:top_k],
            used_for_rule_ids=list(used_for_rule_ids),
        )


def _call_embedding_api(texts: list[str]) -> list[list[float]]:
    gate = gate_external_model_input(texts)
    if gate.blocked:
        raise ValueError("embedding 输入被 PII 门禁阻止")
    headers = {"Content-Type": "application/json"}
    if settings.CONTRACT_REVIEW_EMBEDDING_API_KEY:
        headers["Authorization"] = (
            f"Bearer {settings.CONTRACT_REVIEW_EMBEDDING_API_KEY}"
        )
    with start_span(
        "external_model.embedding",
        attributes={
            "model_version": settings.CONTRACT_REVIEW_EMBEDDING_MODEL,
            "context_count": len(texts),
        },
    ):
        response = httpx.post(
            settings.CONTRACT_REVIEW_EMBEDDING_ENDPOINT,
            json={
                "model": settings.CONTRACT_REVIEW_EMBEDDING_MODEL,
                "input": texts,
            },
            headers=headers,
            timeout=120.0,
        )
    response.raise_for_status()
    payload = response.json()
    data = sorted(payload["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in data]


def _load_disk_cache(cache_dir: Path, key: tuple[str, str]) -> list[float] | None:
    path = cache_dir / f"{key[0]}-{key[1]}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_disk_cache(cache_dir: Path, key: tuple[str, str], vector: list[float]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{key[0]}-{key[1]}.json"
        path.write_text(json.dumps(vector), encoding="utf-8")
    except OSError as exc:
        logger.debug(f"embedding 缓存写入失败（忽略）: {exc}")


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)
