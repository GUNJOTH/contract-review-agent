"""向量检索索引测试（mock embeddings API，不打外网）。"""

import pymupdf
import httpx
import pytest

from contract_review.knowledge import LexicalKnowledgeIndex
from contract_review.models import (
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalQuery,
    RetrievalFilter,
    RetrievalFusion,
    RetrievalMode,
    ReviewContext,
)

from contract_review_app.config import settings
from contract_review_app.services.review_service import run_contract_review
from contract_review_app.services.model_transport import HttpxModelTransport
from contract_review_app.services.vector_knowledge_index import (
    HybridKnowledgeIndex,
    VectorKnowledgeIndex,
)
from contract_review_app.services import vector_knowledge_index as vector_module


def _chunks() -> list[KnowledgeChunk]:
    return [
        KnowledgeChunk(
            chunk_id="chunk-amount",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="合同金额为人民币一百万元整",
            evidence_ids=["ev-amount"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-payment",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="付款方式为银行转账",
            evidence_ids=["ev-payment"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-other",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="违约责任与争议解决",
            evidence_ids=["ev-other"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
    ]


def _rule_chunk(rule_id: str = "R1") -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=f"chunk-rule-{rule_id.lower()}",
        source_name="规则快照",
        source_sha256="r" * 64,
        source_version="rules-v0.14",
        content=f"{rule_id} | 类别 / 测试规则 / 条件",
        evidence_ids=["ev-rule"],
        source_kind=KnowledgeSourceKind.RULE,
        metadata={"rule_id": rule_id, "rule_version": "v1"},
    )


def _query(
    text: str,
    *,
    rule_id: str = "R1",
    rule_version: str = "v1",
    source_kinds: list[KnowledgeSourceKind] | None = None,
    retrieval_filter: RetrievalFilter | None = None,
) -> RetrievalQuery:
    """为索引测试构造完整的统一检索查询契约。"""

    effective_filter = retrieval_filter or RetrievalFilter(
        document_ids=["document-main"],
        source_kinds=source_kinds or [KnowledgeSourceKind.CONTRACT],
        applicable_rule_ids=[rule_id],
        rule_versions=[rule_version],
    )
    return RetrievalQuery(
        query_id=f"query-{rule_id}-{text}",
        rule_id=rule_id,
        rule_version=rule_version,
        purpose="rule_review",
        text=text,
        retrieval_filter=effective_filter,
    )


class FakeEmbedding:
    """按文本关键词返回确定性三维向量，便于断言相似度排序（替换 _call_embedding_api）。"""

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "金额" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "付款" in text:
                vectors.append([0.5, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class _SequencedEmbeddingClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _embedding_response() -> httpx.Response:
    request = httpx.Request("POST", "http://fake/v1/embeddings")
    return httpx.Response(
        200,
        request=request,
        json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]},
    )


def test_vector_index_ranks_by_similarity(monkeypatch):
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    index = VectorKnowledgeIndex(_chunks(), use_cache=False)

    trace = index.retrieve(_query("合同金额"), top_k=2, used_for_rule_ids=["R1"])

    assert trace.index_version == "vector-knowledge-0.5.0-qwen3-embedding-8b"
    assert trace.used_for_rule_ids == ["R1"]
    assert trace.hits[0].chunk_id == "chunk-amount"
    assert trace.hits[0].score == pytest.approx(1.0)
    assert trace.hits[0].evidence_ids == ["ev-amount"]
    # 余弦量化到 2 位小数：金额=1.0，付款≈0.45，其他=0
    assert trace.hits[1].chunk_id == "chunk-payment"
    assert trace.hits[1].score == pytest.approx(0.45)


def test_vector_query_embedding_includes_registered_terminology_alias(monkeypatch):
    captured_texts: list[str] = []

    class CapturingEmbedding:
        def __call__(self, texts: list[str]) -> list[list[float]]:
            captured_texts.extend(texts)
            return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api",
        CapturingEmbedding(),
    )
    index = VectorKnowledgeIndex(_chunks(), use_cache=False)

    index.retrieve(_query("合同金额"), top_k=1, used_for_rule_ids=["R1"])

    assert any("合同价款" in text for text in captured_texts)


def test_vector_index_falls_back_to_lexical(monkeypatch):
    class BrokenEmbedding:
        def __call__(self, *args, **kwargs):
            del args, kwargs
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api",
        BrokenEmbedding(),
    )
    index = VectorKnowledgeIndex(_chunks(), use_cache=False)

    trace = index.retrieve(_query("付款方式"), top_k=1, used_for_rule_ids=["R1"])

    assert trace.index_version.endswith("vector-fallback")
    assert trace.hits[0].chunk_id == "chunk-payment"
    assert trace.hits[0].evidence_ids == ["ev-payment"]


def test_embedding_transport_retries_transient_errors_then_returns_vector(monkeypatch):
    client = _SequencedEmbeddingClient(
        [httpx.ConnectTimeout("connect"), _embedding_response()]
    )
    transport = HttpxModelTransport(
        max_attempts=2,
        backoff_seconds=0,
        client=client,
    )
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT", transport)
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT_POLICY", (2, 0.0))
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "http://fake/v1/embeddings")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS", 0.0)

    assert vector_module.embed_texts(["普通合同正文"], use_cache=False) == [[1.0, 0.0, 0.0]]
    assert client.calls == 2


def test_embedding_transport_exhaustion_keeps_lexical_fallback(monkeypatch):
    client = _SequencedEmbeddingClient(
        [httpx.ConnectTimeout("connect")] * 3
    )
    transport = HttpxModelTransport(
        max_attempts=3,
        backoff_seconds=0,
        client=client,
    )
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT", transport)
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT_POLICY", (3, 0.0))
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "http://fake/v1/embeddings")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL_RETRY_BACKOFF_SECONDS", 0.0)

    index = VectorKnowledgeIndex(_chunks(), use_cache=False)
    trace = index.retrieve(_query("付款方式"), top_k=1, used_for_rule_ids=["R1"])

    assert client.calls == 3
    assert trace.index_version.endswith("vector-fallback")
    assert trace.hits[0].chunk_id == "chunk-payment"


def test_embedding_cache_isolated_by_endpoint(monkeypatch, tmp_path):
    """更换 embedding 服务端点时不能复用旧服务生成的向量。"""

    responses = iter([[[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]]])
    monkeypatch.setattr(
        vector_module,
        "_call_embedding_api",
        lambda _texts: next(responses),
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_CACHE_DIR",
        str(tmp_path / "embedding-cache"),
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "same-model")
    vector_module._EMBEDDING_MEMORY_CACHE.clear()

    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://embedding-a.test/v1/embeddings",
    )
    first = vector_module.embed_texts(
        ["相同正文"], cache_identities=["chunk:stable"]
    )

    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://embedding-b.test/v1/embeddings",
    )
    second = vector_module.embed_texts(
        ["相同正文"], cache_identities=["chunk:stable"]
    )

    assert first == [[1.0, 0.0, 0.0]]
    assert second == [[0.0, 1.0, 0.0]]
    vector_module._EMBEDDING_MEMORY_CACHE.clear()


def test_embedding_transport_recreated_when_endpoint_or_model_changes(monkeypatch):
    """更换 embedding 实现身份时不能沿用旧的并发闸门或熔断器。"""

    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT", None)
    monkeypatch.setattr(vector_module, "_EMBEDDING_TRANSPORT_POLICY", None)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://embedding-a.test/v1/embeddings",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "model-a")
    first = vector_module._embedding_transport()

    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://embedding-b.test/v1/embeddings",
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "model-b")
    second = vector_module._embedding_transport()

    try:
        assert second is not first
        assert second._concurrency_gate is not first._concurrency_gate
        assert second._circuit_breaker is not first._circuit_breaker
    finally:
        second.close()


def test_hybrid_index_fuses_lexical_and_vector_candidates(monkeypatch):
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    index = HybridKnowledgeIndex(_chunks(), use_cache=False)

    trace = index.retrieve(_query("付款方式"), top_k=2, used_for_rule_ids=["R1"])

    assert trace.retrieval_mode == RetrievalMode.HYBRID
    assert trace.fusion_method == RetrievalFusion.RRF
    assert trace.index_version.endswith("+hybrid-knowledge-rrf-0.5.1")
    assert trace.hits[0].chunk_id == "chunk-payment"
    assert set(trace.hits[0].retrieval_sources) == {"lexical", "vector"}
    assert trace.hits[0].lexical_rank == 1
    assert trace.hits[0].vector_rank == 1
    assert trace.hits[0].score == pytest.approx(round(2 / 61, 12))
    assert trace.hits[1].chunk_id == "chunk-amount"
    assert trace.hits[1].retrieval_sources == ["vector"]
    assert trace.hits[1].lexical_rank is None
    assert trace.hits[1].vector_rank == 2


def test_cached_embeddings_require_stable_identities():
    with pytest.raises(ValueError, match="cache identities are required"):
        vector_module.embed_texts(["正文内容"])


def test_cached_embeddings_reject_duplicate_identities():
    with pytest.raises(ValueError, match="must be unique within a batch"):
        vector_module.embed_texts(
            ["正文内容一", "正文内容二"],
            cache_identities=["chunk:same", "chunk:same"],
        )


def test_duplicate_chunk_embeddings_are_stable_across_initial_and_replay_retrieval(
    monkeypatch,
    tmp_path,
):
    """重复正文块在首次检索和缓存回放中必须保留同一条候选轨迹。"""

    duplicate_text = "重复正文条款内容"

    class DuplicateTextEmbedding:
        def __call__(self, texts: list[str]) -> list[list[float]]:
            duplicate_occurrence = 0
            vectors = []
            for text in texts:
                if text == duplicate_text:
                    vectors.append(
                        [1.0, 0.0, 0.0]
                        if duplicate_occurrence == 0
                        else [0.0, 1.0, 0.0]
                    )
                    duplicate_occurrence += 1
                elif "重复正文" in text:
                    vectors.append([1.0, 0.0, 0.0])
                else:
                    vectors.append([0.0, 0.0, 1.0])
            return vectors

    duplicate_chunks = [
        KnowledgeChunk(
            chunk_id="chunk-duplicate-a",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content=duplicate_text,
            evidence_ids=["ev-duplicate-a"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-duplicate-b",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content=duplicate_text,
            evidence_ids=["ev-duplicate-b"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-unrelated",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="其他正文条款内容",
            evidence_ids=["ev-unrelated"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
    ]
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_MODEL",
        "duplicate-cache-test-model",
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_CACHE_DIR",
        str(tmp_path / "embedding_cache"),
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api",
        DuplicateTextEmbedding(),
    )
    vector_module._EMBEDDING_MEMORY_CACHE.clear()

    try:
        query = _query("重复正文")
        first_trace = HybridKnowledgeIndex(
            duplicate_chunks,
            use_cache=True,
        ).retrieve(query, top_k=2, used_for_rule_ids=["R1"])
        # 清空进程内缓存，强制二次检索走磁盘回放，验证持久化缓存也不碰撞。
        vector_module._EMBEDDING_MEMORY_CACHE.clear()
        replay_trace = HybridKnowledgeIndex(
            duplicate_chunks,
            use_cache=True,
        ).retrieve(query, top_k=2, used_for_rule_ids=["R1"])

        assert replay_trace.trace_id == first_trace.trace_id
        assert replay_trace.hits == first_trace.hits
        assert len(list((tmp_path / "embedding_cache").glob("*.json"))) == 4
    finally:
        vector_module._EMBEDDING_MEMORY_CACHE.clear()


def test_lexical_bm25_preserves_negation_and_numeric_constraints():
    chunks = [
        KnowledgeChunk(
            chunk_id="chunk-exact-deadline",
            source_name="主合同.pdf",
            source_sha256="a" * 64,
            source_version="parser-v1",
            content="乙方不得超过30日支付全部价款。",
            evidence_ids=["ev-exact-deadline"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-payment"],
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-different-deadline",
            source_name="主合同.pdf",
            source_sha256="a" * 64,
            source_version="parser-v1",
            content="乙方应在60日内支付全部价款。",
            evidence_ids=["ev-different-deadline"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-payment-alt"],
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-permission",
            source_name="主合同.pdf",
            source_sha256="a" * 64,
            source_version="parser-v1",
            content="经甲方书面同意，乙方可以延后付款。",
            evidence_ids=["ev-permission"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-exception"],
            metadata={"document_id": "document-main"},
        ),
    ]

    trace = LexicalKnowledgeIndex(chunks).retrieve(
        _query("不得超过 30 日"), top_k=3, used_for_rule_ids=["R1"]
    )

    assert trace.hits[0].chunk_id == "chunk-exact-deadline"
    assert "不得超过" in trace.hits[0].matched_terms
    assert "30" in trace.hits[0].matched_terms
    assert trace.hits[0].lexical_rank == 1
    assert trace.fusion_method == RetrievalFusion.NONE


def test_retrieval_filter_applies_document_clause_source_version_and_evidence():
    chunks = [
        KnowledgeChunk(
            chunk_id="chunk-in-scope",
            source_name="附件-报价单.pdf",
            source_sha256="b" * 64,
            source_version="parser-v2",
            content="付款期限不得超过30日。",
            evidence_ids=["ev-in-scope"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-payment"],
            metadata={"document_id": "document-annex"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-wrong-document",
            source_name="主合同.pdf",
            source_sha256="b" * 64,
            source_version="parser-v2",
            content="付款期限不得超过30日。",
            evidence_ids=["ev-wrong-document"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-payment"],
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-wrong-version",
            source_name="附件-报价单.pdf",
            source_sha256="b" * 64,
            source_version="parser-v1",
            content="付款期限不得超过30日。",
            evidence_ids=["ev-wrong-version"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            clause_ids=["clause-payment"],
            metadata={"document_id": "document-annex"},
        ),
    ]
    retrieval_filter = RetrievalFilter(
        applicable_rule_ids=["R1"],
        rule_versions=["v1"],
        document_ids=["document-annex"],
        clause_ids=["clause-payment"],
        source_names=["附件-报价单.pdf"],
        source_sha256s=["b" * 64],
        source_versions=["parser-v2"],
        source_kinds=[KnowledgeSourceKind.CONTRACT],
        evidence_ids=["ev-in-scope"],
    )

    query = _query(
        "付款期限不得超过30日",
        retrieval_filter=retrieval_filter,
    )
    trace = LexicalKnowledgeIndex(chunks).retrieve(
        query,
        top_k=5,
        used_for_rule_ids=["R1"],
    )

    assert [hit.chunk_id for hit in trace.hits] == ["chunk-in-scope"]
    assert trace.retrieval_query.retrieval_filter == retrieval_filter


def test_retrieval_filter_limits_applicable_rule_definition(monkeypatch):
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    r1 = _rule_chunk("R1").model_copy(
        update={"metadata": {"rule_id": "R1", "rule_version": "v1"}}
    )
    r2 = _rule_chunk("R2").model_copy(
        update={"metadata": {"rule_id": "R2", "rule_version": "v1"}}
    )
    retrieval_filter = RetrievalFilter(
        applicable_rule_ids=["R1"],
        rule_versions=["v1"],
        document_ids=["document-main"],
        source_kinds=[KnowledgeSourceKind.RULE, KnowledgeSourceKind.CONTRACT],
    )

    trace = HybridKnowledgeIndex([*_chunks(), r1, r2], use_cache=False).retrieve(
        _query(
            "合同金额",
            retrieval_filter=retrieval_filter,
        ),
        top_k=5,
        used_for_rule_ids=["R1"],
    )

    assert "chunk-rule-r2" not in {hit.chunk_id for hit in trace.hits}
    assert trace.retrieval_query.retrieval_filter.applicable_rule_ids == ["R1"]


def test_rule_definition_chunk_forced_into_hits(monkeypatch):
    """当前规则的定义块即使相似度低也必须优先进入命中（避免'未找到定义块'）。"""
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    r1 = _rule_chunk("R1")  # 向量 [0,0,1]，与查询"合同金额"([1,0,0]) 相似度为 0
    r2 = KnowledgeChunk(
        chunk_id="chunk-rule-r2",
        source_name="规则快照",
        source_sha256="r" * 64,
        source_version="rules-v0.14",
        content="R2 | 类别 / 金额核对规则 / 条件",  # 含"金额"→向量 [1,0,0]，高相似
        evidence_ids=["ev-rule-2"],
        source_kind=KnowledgeSourceKind.RULE,
        metadata={"rule_id": "R2", "rule_version": "v1"},
    )
    index = VectorKnowledgeIndex([*_chunks(), r1, r2], use_cache=False)

    # 查询 R1：R2 相似度更高，但 R1 是当前规则，必须排在第一位
    trace = index.retrieve(
        _query(
            "合同金额",
            source_kinds=[KnowledgeSourceKind.RULE, KnowledgeSourceKind.CONTRACT],
        ),
        top_k=2,
        used_for_rule_ids=["R1"],
    )
    assert trace.hits[0].chunk_id == "chunk-rule-r1", "被查询规则的定义块必须优先"

    # 查询 R2：同理 R2 定义块排在第一位
    trace_r2 = index.retrieve(
        _query(
            "合同金额",
            rule_id="R2",
            source_kinds=[KnowledgeSourceKind.RULE, KnowledgeSourceKind.CONTRACT],
        ),
        top_k=2,
        used_for_rule_ids=["R2"],
    )
    assert trace_r2.hits[0].chunk_id == "chunk-rule-r2"


def test_short_fragment_chunks_excluded_from_retrieval(monkeypatch):
    """过短的正文碎片（表头/标签）不参与检索，避免相似度虚高挤掉条款块。"""
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    fragment = KnowledgeChunk(
        chunk_id="chunk-document-frag",
        source_name="合同主文.pdf",
        source_sha256="s" * 64,
        source_version="pdf-text-0.1.0",
        content="帐 号",
        evidence_ids=["ev-frag"],
        source_kind=KnowledgeSourceKind.CONTRACT,
        metadata={"document_id": "document-main"},
    )
    index = VectorKnowledgeIndex([*_chunks(), fragment], use_cache=False)

    trace = index.retrieve(_query("合同金额"), top_k=5, used_for_rule_ids=["R1"])
    hit_ids = [hit.chunk_id for hit in trace.hits]
    assert "chunk-document-frag" not in hit_ids, "短碎片不应参与检索"


def test_hybrid_lexical_branch_keeps_short_exact_terms(monkeypatch):
    """混合检索的 BM25 分支仍能召回短的精确法律表达。"""
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    short_term = KnowledgeChunk(
        chunk_id="chunk-document-negation",
        source_name="合同主文.pdf",
        source_sha256="s" * 64,
        source_version="pdf-text-0.1.0",
        content="不得",
        evidence_ids=["ev-negation"],
        source_kind=KnowledgeSourceKind.CONTRACT,
        metadata={"document_id": "document-main"},
    )

    trace = HybridKnowledgeIndex([*_chunks(), short_term], use_cache=False).retrieve(
        _query("不得"), top_k=5, used_for_rule_ids=["R1"]
    )

    hit = next(hit for hit in trace.hits if hit.chunk_id == "chunk-document-negation")
    assert hit.lexical_rank is not None
    assert hit.vector_rank is None
    assert hit.retrieval_sources == ["lexical"]


def _make_contract_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "甲方与乙方签订软件开发合同，合同金额为人民币一百万元整，付款方式为银行转账。",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def test_contract_review_uses_vector_retrieval(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_ENDPOINT",
        "http://fake/v1/embeddings",
    )
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_CACHE_DIR",
        str(tmp_path / "embedding_cache"),
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )

    result = run_contract_review(
        [("合同主文.pdf", _make_contract_pdf())],
        package_id="pkg-vec-001",
        review_context=ReviewContext(contract_type="software"),
    )

    vector_traces = [
        trace
        for trace in result.retrieval_traces
        if trace.index_version.startswith("vector-knowledge")
    ]
    assert vector_traces, "配置 embedding 后审查应使用向量检索"
    assert all(trace.fusion_method == RetrievalFusion.RRF for trace in vector_traces)
    assert all(
        trace.retrieval_query.retrieval_filter.applicable_rule_ids
        == trace.used_for_rule_ids
        for trace in vector_traces
    )
    assert all(
        trace.retrieval_query.retrieval_filter.clause_ids
        and trace.retrieval_query.retrieval_filter.source_names
        and trace.retrieval_query.retrieval_filter.source_versions
        and trace.retrieval_query.retrieval_filter.rule_versions
        for trace in vector_traces
    )
    # 语义规则应检索到正文证据（不是只有规则片段）
    chunks_by_id = {chunk.chunk_id: chunk for chunk in result.knowledge_chunks}
    assert any(
        "合同主文.pdf" in chunks_by_id[hit.chunk_id].source_name
        for trace in vector_traces
        for hit in trace.hits
    )
