"""统一检索查询的事实类型锚点测试。"""

from contract_review.knowledge import LexicalKnowledgeIndex
from contract_review.models import (
    Document,
    DocumentKind,
    KnowledgeChunk,
    KnowledgeSourceKind,
    Rule,
    RuleBundle,
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
