"""向量检索索引：基于 OpenAI 兼容 /v1/embeddings 的合同条款召回。

与引擎的 ``KnowledgeIndex`` 契约一致，产出带证据 ID 的 RetrievalTrace。

确定性设计：embedding 服务在跨批次调用时存在 ~1e-3 的元素级噪声，会导致
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

from contract_review.knowledge import LexicalKnowledgeIndex
from contract_review.models import (
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalHit,
    RetrievalTrace,
)

from contract_review_app.config import settings
from contract_review_app.services.pii_gate import gate_external_model_input
from contract_review_app.telemetry.tracing import start_span

VECTOR_INDEX_VERSION = "vector-knowledge-0.1.0"

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
        self._retrievable = [
            chunk
            for chunk in self.chunks
            if chunk.source_kind == KnowledgeSourceKind.RULE
            or len(chunk.content.strip()) >= MIN_DOCUMENT_CHUNK_CHARS
        ]
        self._lexical = LexicalKnowledgeIndex(self._retrievable)
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
        query: str,
        *,
        top_k: int = 5,
        used_for_rule_ids: Sequence[str] = (),
    ) -> RetrievalTrace:
        if not query.strip():
            raise ValueError("retrieval query cannot be empty")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if self._fallback or self._vectors is None:
            trace = self._lexical.retrieve(
                query, top_k=top_k, used_for_rule_ids=used_for_rule_ids
            )
            return trace.model_copy(
                update={"index_version": f"{trace.index_version}-vector-fallback"}
            )
        query_vector = embed_texts([query], use_cache=self._use_cache)[0]
        rule_scored, doc_scored = [], []
        for chunk, vector in zip(self._retrievable, self._vectors):
            score = round(max(0.0, _cosine(query_vector, vector)), 2)
            item = (chunk, score)
            if chunk.source_kind == KnowledgeSourceKind.RULE:
                rule_scored.append(item)
            else:
                doc_scored.append(item)

        def _top(items, limit: int) -> list[RetrievalHit]:
            ranked = sorted(items, key=lambda item: (-item[1], item[0].chunk_id))
            return [
                RetrievalHit(
                    chunk_id=chunk.chunk_id,
                    score=score,
                    evidence_ids=chunk.evidence_ids,
                    matched_terms=[],
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
            # 正文块不足时用规则块补齐，保证 top_k 恒定
            fillers = [
                hit
                for hit in _top(rule_scored, len(rule_scored))
                if hit.chunk_id not in taken
            ][: doc_slots - len(doc_hits)]
            doc_hits = [*doc_hits, *fillers]
        hits = (rule_hits + doc_hits)[:top_k]
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return RetrievalTrace(
            trace_id=f"retrieval-{digest}",
            query=query,
            index_version=(
                f"{VECTOR_INDEX_VERSION}-{settings.CONTRACT_REVIEW_EMBEDDING_MODEL}"
            ),
            top_k=top_k,
            hits=hits,
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
