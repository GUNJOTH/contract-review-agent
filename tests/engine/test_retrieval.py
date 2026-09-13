"""统一检索查询的事实类型锚点测试。"""

from contract_review.knowledge import LexicalKnowledgeIndex
from contract_review.models import (
    Document,
    DocumentKind,
    KnowledgeChunk,
    KnowledgeSourceKind,
    Rule,
    RuleBundle,
    RetrievalFilter,
    RetrievalQuery,
    ReviewContext,
)
from contract_review.retrieval import (
    build_retrieval_query,
    build_rule_retrieval_filter,
)


def _document(document_id: str, filename: str) -> Document:
    return Document(
        document_id=document_id,
        package_id="package-retrieval",
        filename=filename,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_sha256=(document_id[0] * 64),
        document_kind=DocumentKind.MAIN_CONTRACT,
        parser_version="docx-text-v1",
        parse_status="parsed",
    )


def test_required_fact_types_become_query_anchors_and_improve_recall() -> None:
    documents = [
        _document("document-main", "主合同.docx"),
        _document("document-annex", "技术协议.docx"),
    ]
    rule = Rule(
        rule_id="cross-document-amount",
        version="v1",
        title="跨文档关键事实一致性",
        category="跨文档",
        applies_to=["software"],
        check_method="deterministic",
        required_evidence=[
            "contract_element:project_name",
            "financial.contract_amount_numeric",
        ],
        source_snapshot="rules-v1",
    )
    bundle = RuleBundle(
        bundle_id="bundle-v1",
        source_filename="rules.xlsx",
        source_sha256="r" * 64,
        source_sheet="Sheet1",
        source_range="A1:C2",
        rules=[rule],
    )
    context = ReviewContext(
        contract_type="software",
        transaction_context="软件开发项目采购，关注付款与金额口径",
        transaction_amount=1000000,
        transaction_tags=["内部背景", "项目"],
        document_kinds=[DocumentKind.MAIN_CONTRACT],
    )
    retrieval_filter = build_rule_retrieval_filter(
        rule,
        rule_bundle=bundle,
        documents=documents,
        clauses=[],
        review_context=context,
    )
    query = build_retrieval_query(
        rule,
        review_context=context,
        retrieval_filter=retrieval_filter,
    )

    assert "项目名称" in query.exact_anchors
    assert "合同金额" in query.exact_anchors
    assert query.required_fact_anchors == [
        "项目名称",
        "项目",
        "合同金额",
        "金额",
        "价款",
        "小写",
    ]
    assert "项目名称" in query.lexical_terms
    assert "合同金额" in query.lexical_terms
    assert "软件开发项目采购" not in query.text
    assert "关注付款与金额口径" not in query.text
    assert "内部背景" not in query.lexical_terms
    assert "1000000" not in query.text

    chunks = [
        KnowledgeChunk(
            chunk_id="chunk-main-amount",
            source_name="主合同.docx",
            source_sha256="d" * 64,
            source_version="docx-text-v1",
            content="项目名称：仓储系统；合同金额：1000000元。",
            evidence_ids=["e-main-amount"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={
                "document_id": "document-main",
                "document_kind": DocumentKind.MAIN_CONTRACT.value,
            },
        ),
        KnowledgeChunk(
            chunk_id="chunk-annex-amount",
            source_name="技术协议.docx",
            source_sha256="d" * 64,
            source_version="docx-text-v1",
            content="项目名称：仓储系统；合同金额：1200000元。",
            evidence_ids=["e-annex-amount"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={
                "document_id": "document-annex",
                "document_kind": DocumentKind.MAIN_CONTRACT.value,
            },
        ),
        KnowledgeChunk(
            chunk_id="chunk-unrelated",
            source_name="主合同.docx",
            source_sha256="d" * 64,
            source_version="docx-text-v1",
            content="服务范围包括部署支持和操作培训。",
            evidence_ids=["e-unrelated"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={
                "document_id": "document-main",
                "document_kind": DocumentKind.MAIN_CONTRACT.value,
            },
        ),
    ]
    trace = LexicalKnowledgeIndex(chunks).retrieve(
        query,
        top_k=2,
        used_for_rule_ids=[rule.rule_id],
    )

    assert [hit.chunk_id for hit in trace.hits] == [
        "chunk-annex-amount",
        "chunk-main-amount",
    ]


def test_low_signal_contract_chunks_are_not_used_to_fill_top_k() -> None:
    retrieval_filter = RetrievalFilter(
        document_ids=["document-main"],
        source_kinds=[KnowledgeSourceKind.CONTRACT],
        applicable_rule_ids=["delivery-rule"],
        rule_versions=["v1"],
    )
    query = RetrievalQuery(
        query_id="query-delivery-test",
        rule_id="delivery-rule",
        rule_version="v1",
        purpose="rule_review",
        text="乙方应在交付期限前完成交付",
        lexical_terms=["乙方应在交付期限前完成交付"],
        exact_anchors=["交付期限"],
        retrieval_filter=retrieval_filter,
    )

    def chunk(chunk_id: str, content: str) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=chunk_id,
            source_name="主合同.docx",
            source_sha256="d" * 64,
            source_version="docx-text-v1",
            content=content,
            evidence_ids=[f"evidence-{chunk_id}"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        )

    trace = LexicalKnowledgeIndex(
        [
            chunk("chunk-low-signal", "交付"),
            chunk("chunk-high-signal", "乙方应在交付期限前完成交付"),
        ]
    ).retrieve(query, top_k=5, used_for_rule_ids=["delivery-rule"])

    assert [hit.chunk_id for hit in trace.hits] == ["chunk-high-signal"]


def test_relative_score_gate_removes_weak_contract_candidates() -> None:
    query = RetrievalQuery(
        query_id="query-payment-relative-score",
        rule_id="payment-rule",
        rule_version="v1",
        purpose="rule_review",
        text=(
            "付款条件与节点；付款应有节点、条件和比例，并与交付或验收结果形成可执行绑定。；"
            "contract_term:payment；付款；支付；价款；结算"
        ),
        lexical_terms=["付款条件与节点", "contract_term:payment", "付款", "支付", "价款", "结算"],
        exact_anchors=["付款条件与节点", "付款", "支付", "价款", "结算"],
        required_fact_anchors=["付款", "支付", "价款", "结算"],
        retrieval_filter=RetrievalFilter(
            document_ids=["document-main"],
            source_kinds=[KnowledgeSourceKind.CONTRACT],
            applicable_rule_ids=["payment-rule"],
            rule_versions=["v1"],
        ),
    )

    chunks = [
        KnowledgeChunk(
            chunk_id="chunk-payment-strong",
            source_name="主合同.docx",
            source_sha256="e" * 64,
            source_version="docx-text-v1",
            content="付款条款：合同签订后支付30%，验收后支付70%。",
            evidence_ids=["evidence-payment-strong"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
        KnowledgeChunk(
            chunk_id="chunk-payment-weak",
            source_name="主合同.docx",
            source_sha256="e" * 64,
            source_version="docx-text-v1",
            content="发生重大违约时，守约方有权提前通知解除合同并完成费用结算。",
            evidence_ids=["evidence-payment-weak"],
            source_kind=KnowledgeSourceKind.CONTRACT,
            metadata={"document_id": "document-main"},
        ),
    ]

    trace = LexicalKnowledgeIndex(chunks).retrieve(
        query,
        top_k=5,
        used_for_rule_ids=["payment-rule"],
    )

    assert [hit.chunk_id for hit in trace.hits] == ["chunk-payment-strong"]
