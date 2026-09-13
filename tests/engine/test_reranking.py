"""合同证据候选精排的独立回归测试。"""

import pytest

from contract_review.models import (
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalFilter,
    RetrievalHit,
    RetrievalQuery,
    RetrievalTrace,
    RetrievalSource,
)
from contract_review.reranking import (
    LEGAL_RELEVANCE_RERANKER_VERSION,
    rerank_candidate_pool_size,
    rerank_retrieval_trace,
)


def _query() -> RetrievalQuery:
    return RetrievalQuery(
        query_id="query-rerank-regression",
        rule_id="payment-deadline",
        rule_version="v1",
        purpose="rule_review",
        text="付款期限不得超过30日",
        lexical_terms=["付款期限", "不得超过30日"],
        exact_anchors=["付款期限", "不得超过", "30日"],
        numeric_anchors=["30日"],
        negation_anchors=["不得超过"],
        required_fact_anchors=["付款期限"],
        retrieval_filter=RetrievalFilter(
            document_ids=["document-main"],
            source_kinds=[KnowledgeSourceKind.CONTRACT],
            applicable_rule_ids=["payment-deadline"],
            rule_versions=["v1"],
        ),
    )


def _chunk(chunk_id: str, content: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        source_name="主合同.pdf",
        source_sha256="a" * 64,
        source_version="pdf-text-v1",
        content=content,
        evidence_ids=[f"evidence-{chunk_id}"],
        source_kind=KnowledgeSourceKind.CONTRACT,
        metadata={"document_id": "document-main"},
    )


def _trace() -> tuple[RetrievalTrace, dict[str, KnowledgeChunk]]:
    chunks = {
        "chunk-weak": _chunk("chunk-weak", "付款方式为银行转账。"),
        "chunk-context": _chunk("chunk-context", "付款安排应结合项目进度执行。"),
        "chunk-direct": _chunk("chunk-direct", "付款期限不得超过30日。"),
    }
    trace = RetrievalTrace(
        trace_id="retrieval-rerank-before",
        retrieval_query=_query(),
        index_version="lexical-knowledge-index-test",
        top_k=3,
        used_for_rule_ids=["payment-deadline"],
        hits=[
            RetrievalHit(
                chunk_id="chunk-weak",
                score=3.0,
                evidence_ids=["evidence-chunk-weak"],
                retrieval_sources=[RetrievalSource.LEXICAL],
                lexical_rank=1,
            ),
            RetrievalHit(
                chunk_id="chunk-context",
                score=2.0,
                evidence_ids=["evidence-chunk-context"],
                retrieval_sources=[RetrievalSource.LEXICAL],
                lexical_rank=2,
            ),
            RetrievalHit(
                chunk_id="chunk-direct",
                score=1.0,
                evidence_ids=["evidence-chunk-direct"],
                retrieval_sources=[RetrievalSource.LEXICAL],
                lexical_rank=3,
            ),
        ],
    )
    return trace, chunks


def test_reranker_promotes_direct_evidence_from_initial_pool() -> None:
    trace, chunks = _trace()

    reranked = rerank_retrieval_trace(
        trace,
        chunks,
        top_k=2,
    )

    assert [hit.chunk_id for hit in reranked.hits] == [
        "chunk-direct",
        "chunk-weak",
    ]
    direct_hit = reranked.hits[0]
    assert direct_hit.score == 1.0
    assert direct_hit.rerank_score is not None
    assert direct_hit.rerank_features["required_fact_coverage"] == pytest.approx(1.0)
    assert direct_hit.rerank_features["constraint_coverage"] == pytest.approx(1.0)
    assert reranked.reranker_version == LEGAL_RELEVANCE_RERANKER_VERSION
    assert reranked.top_k == 2


@pytest.mark.parametrize(
    ("top_k", "expected"),
    [
        (1, 3),
        (10, 30),
        (20, 50),
        (50, 50),
        (51, 51),
    ],
)
def test_rerank_candidate_pool_size_is_bounded(top_k: int, expected: int) -> None:
    assert rerank_candidate_pool_size(top_k) == expected


@pytest.mark.parametrize("top_k", [0, -1, True])
def test_rerank_candidate_pool_size_rejects_invalid_top_k(top_k: object) -> None:
    with pytest.raises(ValueError, match="正整数"):
        rerank_candidate_pool_size(top_k)  # type: ignore[arg-type]


def test_reranker_rejects_unknown_chunk_instead_of_dropping_evidence() -> None:
    trace, _ = _trace()

    with pytest.raises(ValueError, match="不存在的知识块"):
        rerank_retrieval_trace(trace, {})
