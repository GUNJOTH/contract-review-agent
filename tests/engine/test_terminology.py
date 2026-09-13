"""中文合同术语归一和证据匹配测试。"""

from contract_review.evidence import assess_candidate_evidence
from contract_review.knowledge import LexicalKnowledgeIndex
from contract_review.models import (
    Document,
    DocumentKind,
    KnowledgeChunk,
    KnowledgeSourceKind,
    RetrievalFilter,
    RetrievalQuery,
    ReviewContext,
    Rule,
    RuleBundle,
)
from contract_review.retrieval import (
    build_candidate_evidence,
    build_retrieval_query,
    build_rule_retrieval_filter,
)
from contract_review.terminology import (
    TERMINOLOGY_NORMALIZATION_VERSION,
    expand_terminology_text,
    expand_terminology_terms,
    matched_terminology_terms,
    terminology_matches,
)


def _payment_query() -> RetrievalQuery:
    return RetrievalQuery(
        query_id="query-terminology-payment",
        rule_id="payment-rule",
        rule_version="v1",
        purpose="rule_review",
        text="付款条件",
        lexical_terms=["付款条件"],
        exact_anchors=["付款条件"],
        required_fact_anchors=["付款条件"],
        retrieval_filter=RetrievalFilter(
            document_ids=["document-main"],
            source_kinds=[KnowledgeSourceKind.CONTRACT],
            applicable_rule_ids=["payment-rule"],
            rule_versions=["v1"],
        ),
    )


def _payment_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id="chunk-payment-terminology",
        source_name="主合同.docx",
        source_sha256="a" * 64,
        source_version="docx-text-v1",
        content="付款条款：合同签订后支付30%，验收后支付70%。",
        evidence_ids=["evidence-payment-terminology"],
        source_kind=KnowledgeSourceKind.CONTRACT,
        metadata={"document_id": "document-main"},
    )


def test_terminology_expansion_is_stable_and_preserves_original_term() -> None:
    assert expand_terminology_terms(["付款条件"]) == [
        "付款条件",
        "付款条款",
        "支付条件",
        "支付条款",
    ]
    assert expand_terminology_text("付款条件与节点").startswith("付款条件与节点；")
    assert "付款条款与节点" in expand_terminology_text("付款条件与节点")
    assert TERMINOLOGY_NORMALIZATION_VERSION == "contract-terminology-0.1.0"


def test_terminology_matching_returns_original_query_term() -> None:
    content = "付款条款：合同签订后支付30%。"

    assert terminology_matches(content, "付款条件")
    assert matched_terminology_terms(["付款条件"], content) == ["付款条件"]
    assert not terminology_matches("合同终止后结算", "合同解除")


def test_lexical_retrieval_and_evidence_gate_accept_registered_alias() -> None:
    query = _payment_query()
    chunk = _payment_chunk()
    chunks_by_id = {chunk.chunk_id: chunk}

    trace = LexicalKnowledgeIndex([chunk]).retrieve(
        query,
        top_k=1,
        used_for_rule_ids=[query.rule_id],
    )
    candidates = build_candidate_evidence(trace, chunks_by_id)
    assessment = assess_candidate_evidence(candidates[0], query)

    assert [hit.chunk_id for hit in trace.hits] == [chunk.chunk_id]
    assert assessment.outcome.value == "ACCEPT"
    assert assessment.matched_exact_anchors == ["付款条件"]
    assert assessment.matched_required_fact_anchors == ["付款条件"]
    assert chunk.content == "付款条款：合同签订后支付30%，验收后支付70%。"


def test_built_query_records_terminology_version_and_aliases() -> None:
    rule = Rule(
        rule_id="payment-terminology-rule",
        version="v1",
        title="付款条件与节点",
        category="付款",
        applies_to=["software"],
        check_method="deterministic",
        required_evidence=["contract_term:payment"],
        source_snapshot="rules-v1",
    )
    document = Document(
        document_id="document-main",
        package_id="package-terminology",
        filename="主合同.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_sha256="b" * 64,
        document_kind=DocumentKind.MAIN_CONTRACT,
        parser_version="docx-text-v1",
        parse_status="parsed",
    )
    bundle = RuleBundle(
        bundle_id="bundle-terminology-v1",
        source_filename="rules.json",
        source_sha256="c" * 64,
        source_sheet="rules",
        source_range="A1:D2",
        rules=[rule],
    )
    context = ReviewContext(
        contract_type="software",
        document_kinds=[DocumentKind.MAIN_CONTRACT],
    )
    retrieval_filter = build_rule_retrieval_filter(
        rule,
        rule_bundle=bundle,
        documents=[document],
        clauses=[],
        review_context=context,
    )

    query = build_retrieval_query(
        rule,
        review_context=context,
        retrieval_filter=retrieval_filter,
    )

    assert query.terminology_version == TERMINOLOGY_NORMALIZATION_VERSION
    assert "付款条款与节点" in query.text
    assert "支付条件与节点" in query.text
    assert "支付条款与节点" in query.lexical_terms


def test_terminology_does_not_expand_unregistered_legal_concepts() -> None:
    assert expand_terminology_terms(["解除与终止"]) == ["解除与终止"]
    assert not terminology_matches("合同终止", "合同解除")
    assert not terminology_matches("履约保函", "保证金")
