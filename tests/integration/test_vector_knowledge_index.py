"""向量检索索引测试（mock embeddings API，不打外网）。"""

import fitz
import httpx
import pytest

from contract_review.models import KnowledgeChunk

from contract_review_app.config import settings
from contract_review_app.services.review_service import run_contract_review
from contract_review_app.services.vector_knowledge_index import VectorKnowledgeIndex


def _chunks() -> list[KnowledgeChunk]:
    return [
        KnowledgeChunk(
            chunk_id="chunk-amount",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="合同金额为人民币一百万元整",
            evidence_ids=["ev-amount"],
        ),
        KnowledgeChunk(
            chunk_id="chunk-payment",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="付款方式为银行转账",
            evidence_ids=["ev-payment"],
        ),
        KnowledgeChunk(
            chunk_id="chunk-other",
            source_name="合同主文.pdf",
            source_sha256="s" * 64,
            source_version="pdf-text-0.1.0",
            content="违约责任与争议解决",
            evidence_ids=["ev-other"],
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
        metadata={"rule_id": rule_id},
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


def test_vector_index_ranks_by_similarity(monkeypatch):
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api", FakeEmbedding()
    )
    index = VectorKnowledgeIndex(_chunks(), use_cache=False)

    trace = index.retrieve("合同金额", top_k=2, used_for_rule_ids=["R1"])

    assert trace.index_version == "vector-knowledge-0.1.0-qwen3-embedding-8b"
    assert trace.used_for_rule_ids == ["R1"]
    assert trace.hits[0].chunk_id == "chunk-amount"
    assert trace.hits[0].score == pytest.approx(1.0)
    assert trace.hits[0].evidence_ids == ["ev-amount"]
    # 余弦量化到 2 位小数：金额=1.0，付款≈0.45，其他=0
    assert trace.hits[1].chunk_id == "chunk-payment"
    assert trace.hits[1].score == pytest.approx(0.45)


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

    trace = index.retrieve("付款方式", top_k=1, used_for_rule_ids=["R1"])

    assert trace.index_version.endswith("vector-fallback")
    assert trace.hits[0].chunk_id == "chunk-payment"
    assert trace.hits[0].evidence_ids == ["ev-payment"]


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
        metadata={"rule_id": "R2"},
    )
    index = VectorKnowledgeIndex([*_chunks(), r1, r2], use_cache=False)

    # 查询 R1：R2 相似度更高，但 R1 是当前规则，必须排在第一位
    trace = index.retrieve("合同金额", top_k=2, used_for_rule_ids=["R1"])
    assert trace.hits[0].chunk_id == "chunk-rule-r1", "被查询规则的定义块必须优先"

    # 查询 R2：同理 R2 定义块排在第一位
    trace_r2 = index.retrieve("合同金额", top_k=2, used_for_rule_ids=["R2"])
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
    )
    index = VectorKnowledgeIndex([*_chunks(), fragment], use_cache=False)

    trace = index.retrieve("合同金额", top_k=5, used_for_rule_ids=["R1"])
    hit_ids = [hit.chunk_id for hit in trace.hits]
    assert "chunk-document-frag" not in hit_ids, "短碎片不应参与检索"


def _make_contract_pdf() -> bytes:
    doc = fitz.open()
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
        contract_type="software",
    )

    vector_traces = [
        trace
        for trace in result.retrieval_traces
        if trace.index_version.startswith("vector-knowledge")
    ]
    assert vector_traces, "配置 embedding 后审查应使用向量检索"
    # 语义规则应检索到正文证据（不是只有规则片段）
    chunks_by_id = {chunk.chunk_id: chunk for chunk in result.knowledge_chunks}
    assert any(
        "合同主文.pdf" in chunks_by_id[hit.chunk_id].source_name
        for trace in vector_traces
        for hit in trace.hits
    )
