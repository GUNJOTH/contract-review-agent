"""Typed domain models for parsed documents and evidence-first review results."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .terminology import TERMINOLOGY_NORMALIZATION_VERSION


def utc_now() -> datetime:
    """Return an explicit UTC timestamp for reproducible audit records."""

    return datetime.now(timezone.utc)


class DocumentKind(StrEnum):
    MAIN_CONTRACT = "main_contract"
    ANNEX = "annex"
    QUOTATION = "quotation"
    ORDER = "order"
    TECHNICAL_AGREEMENT = "technical_agreement"
    ACCEPTANCE = "acceptance"
    INVOICE = "invoice"
    IP_EVIDENCE = "ip_evidence"
    AMENDMENT = "amendment"
    OTHER = "other"
    UNKNOWN = "unknown"


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    IMAGE = "image"
    SEAL = "seal"
    SIGNATURE = "signature"
    HEADER = "header"
    FOOTER = "footer"
    UNKNOWN = "unknown"


class EvidenceType(StrEnum):
    TEXT = "text"
    TABLE_CELL = "table_cell"
    VISUAL_REGION = "visual_region"
    MISSING_ARTIFACT = "missing_artifact"
    COMPARISON = "comparison"
    EXTERNAL_REFERENCE = "external_reference"


class KnowledgeSourceKind(StrEnum):
    """知识块的业务来源，区分合同事实与规则依据。"""

    CONTRACT = "contract"
    RULE = "rule"


class RuleBundleStatus(StrEnum):
    """规则包生命周期状态。"""

    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    RETIRED = "retired"


class RetrievalMode(StrEnum):
    """一次检索轨迹实际采用的召回方式。"""

    LEXICAL = "lexical"
    VECTOR = "vector"
    HYBRID = "hybrid"


class RetrievalFusion(StrEnum):
    """候选列表的融合算法。"""

    NONE = "none"
    RRF = "rrf"


class FindingStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceQuality(StrEnum):
    """一条结论的证据覆盖质量。"""

    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class RiskLevel(StrEnum):
    UNCLASSIFIED = "unclassified"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    QUALITY_GATED = "QUALITY_GATED"
    INDEXED = "INDEXED"
    EXTRACTED = "EXTRACTED"
    RULE_CHECKED = "RULE_CHECKED"
    SEMANTIC_REVIEWED = "SEMANTIC_REVIEWED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"


class ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BoundingBox(ModelBase):
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)
    x2: float = Field(ge=0)
    y2: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_geometry(self) -> "BoundingBox":
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError("bbox must satisfy x2 >= x1 and y2 >= y1")
        return self


class NormalizedBoundingBox(ModelBase):
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_geometry(self) -> "NormalizedBoundingBox":
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError("normalized bbox must satisfy x2 >= x1 and y2 >= y1")
        return self


class PageGeometry(ModelBase):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: int = Field(default=0, ge=0, lt=360)


class SourceLocator(ModelBase):
    locator_type: Literal[
        "page",
        "bbox",
        "text_span",
        "document_block",
        "table_cell",
        "missing_artifact",
        "external_uri",
    ]
    page_number: int | None = Field(default=None, ge=1)
    printed_page_label: str | None = None
    block_id: str | None = None
    token_ids: list[str] = Field(default_factory=list)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    paragraph_index: int | None = Field(default=None, ge=0)
    run_index: int | None = Field(default=None, ge=0)
    table_index: int | None = Field(default=None, ge=0)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    bbox: BoundingBox | None = None
    normalized_bbox: NormalizedBoundingBox | None = None
    sheet_name: str | None = None
    cell_reference: str | None = None
    missing_name: str | None = None
    external_uri: str | None = None

    @model_validator(mode="after")
    def validate_span(self) -> "SourceLocator":
        if self.char_start is not None and self.char_end is not None:
            if self.char_end < self.char_start:
                raise ValueError("char_end must be greater than or equal to char_start")
        if self.locator_type in {"bbox", "text_span"} and self.bbox is None:
            raise ValueError("bbox or text_span locator requires bbox")
        if (
            self.locator_type in {"page", "bbox", "text_span"}
            and self.page_number is None
        ):
            raise ValueError("page-based locator requires page_number")
        if self.locator_type == "document_block" and self.paragraph_index is None:
            raise ValueError("document_block locator requires paragraph_index")
        if self.locator_type == "table_cell" and not self.cell_reference:
            raise ValueError("table_cell locator requires cell_reference")
        if self.locator_type == "missing_artifact" and not self.missing_name:
            raise ValueError("missing_artifact locator requires missing_name")
        if self.locator_type == "external_uri" and not self.external_uri:
            raise ValueError("external_uri locator requires external_uri")
        return self


class Document(ModelBase):
    document_id: str
    package_id: str
    filename: str
    mime_type: str
    source_sha256: str = Field(min_length=64, max_length=64)
    document_kind: DocumentKind = DocumentKind.UNKNOWN
    page_count: int = Field(default=0, ge=0)
    parser_version: str
    parse_status: Literal["parsed", "needs_ocr", "failed"]
    quality_flags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class Page(ModelBase):
    page_id: str
    document_id: str
    page_number: int = Field(ge=1)
    printed_page_label: str | None = None
    geometry: PageGeometry
    quality_flags: list[str] = Field(default_factory=list)
    needs_ocr: bool = False


class TextToken(ModelBase):
    token_id: str
    page_id: str
    text: str
    bbox: BoundingBox
    source_block_index: int | None = Field(default=None, ge=0)
    source_line_index: int | None = Field(default=None, ge=0)
    source_word_index: int | None = Field(default=None, ge=0)


class LayoutBlock(ModelBase):
    block_id: str
    page_id: str
    order: int = Field(ge=0)
    block_type: BlockType
    text: str = ""
    bbox: BoundingBox
    confidence: float | None = Field(default=None, ge=0, le=1)
    token_ids: list[str] = Field(default_factory=list)
    source_block_index: int | None = Field(default=None, ge=0)


class ParsedPage(ModelBase):
    page: Page
    raw_text: str
    normalized_text: str
    blocks: list[LayoutBlock] = Field(default_factory=list)
    tokens: list[TextToken] = Field(default_factory=list)


class DocumentNode(ModelBase):
    node_id: str
    document_id: str
    order: int = Field(ge=0)
    block_type: BlockType
    text: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    locator: SourceLocator


class ParsedDocument(ModelBase):
    document: Document
    pages: list[ParsedPage] = Field(default_factory=list)
    nodes: list[DocumentNode] = Field(default_factory=list)


class ContractPackage(ModelBase):
    package_id: str
    document_ids: list[str] = Field(default_factory=list)
    document_precedence: list[str] = Field(
        default_factory=list,
        description=(
            "从高到低的合同文件优先顺序；为空表示没有可验证的优先效力，"
            "发生冲突时必须保留 UNKNOWN。"
        ),
    )
    source_snapshot: str
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_document_precedence(self) -> "ContractPackage":
        """确保文件优先顺序只引用合同包内的唯一文档。"""

        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("合同包 document_ids 必须唯一")
        if len(self.document_precedence) != len(set(self.document_precedence)):
            raise ValueError("合同包 document_precedence 必须唯一")
        if not set(self.document_precedence).issubset(self.document_ids):
            raise ValueError("合同包 document_precedence 只能引用 document_ids")
        return self


class PartyPosition(StrEnum):
    """合同审查发起方在交易中的立场。"""

    BUYER = "buyer"
    SELLER = "seller"
    BOTH = "both"
    UNKNOWN = "unknown"


class ReviewContext(ModelBase):
    """一次合同审查的业务上下文。

    上下文只描述本次审查的业务前提，不承载规则正文。规则正文、版本和
    企业可接受立场仍由 ``RuleBundle`` 与 ``PlaybookSpec`` 负责，避免把
    合同类型、交易立场等请求参数散落到各个检查器中。
    """

    context_version: Literal["1.0"] = "1.0"
    contract_type: str | None = Field(
        default=None,
        max_length=128,
        description="合同类型；必须与规则快照中的适用性键一致才会触发类型规则。",
    )
    party_position: PartyPosition = Field(
        default=PartyPosition.UNKNOWN,
        description="本方在交易中的立场：buyer、seller、both 或 unknown。",
    )
    jurisdiction: str | None = Field(
        default=None,
        max_length=128,
        description="适用法域或地区，当前作为可追溯上下文保留。",
    )
    transaction_context: str | None = Field(
        default=None,
        max_length=2000,
        description="交易背景和本次审查需要关注的业务前提。",
    )
    transaction_tags: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="结构化交易背景标签，用于规则适用性和上下文留痕，不作为正文词法词项。",
    )
    transaction_amount: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        description="本次交易金额；缺失时不能满足依赖金额区间的适用条件。",
    )
    document_kinds: list[DocumentKind] = Field(
        default_factory=list,
        max_length=32,
        description="合同包实际包含的文档角色，由解析后的合同包事实填充。",
    )
    review_scope: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="规则 ID 或规则 category 白名单；为空表示审查全部规则。",
    )

    @field_validator("contract_type", "jurisdiction", "transaction_context", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("审查上下文文本字段必须是字符串")
        normalized = value.strip()
        return normalized or None

    @field_validator("review_scope", mode="before")
    @classmethod
    def normalize_review_scope(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("review_scope 必须是规则 ID 或 category 字符串数组")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("review_scope 的每一项必须是字符串")
            scope_item = item.strip()
            if scope_item and scope_item not in normalized:
                normalized.append(scope_item)
        return normalized

    @field_validator("transaction_tags", mode="before")
    @classmethod
    def normalize_transaction_tags(cls, value: object) -> list[str]:
        """清理交易标签，保持同一业务条件的上下文快照稳定。"""

        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("transaction_tags 必须是字符串数组")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("transaction_tags 的每一项必须是字符串")
            tag = item.strip()
            if tag and tag not in normalized:
                normalized.append(tag)
        return normalized

    @field_validator("document_kinds", mode="before")
    @classmethod
    def normalize_document_kinds(cls, value: object) -> list[DocumentKind]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("document_kinds 必须是文档类型数组")
        return list(dict.fromkeys(DocumentKind(item) for item in value))


class AttachmentReference(ModelBase):
    reference_id: str
    referenced_name: str
    aliases: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(
        min_length=1,
        description="识别该附件引用的 CandidateEvidence 身份。",
    )
    required: bool = True


class Evidence(ModelBase):
    evidence_id: str
    evidence_type: EvidenceType
    package_id: str | None = None
    document_id: str | None = None
    source_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    source_document_ids: list[str] = Field(default_factory=list)
    source_document_sha256: dict[str, str] = Field(default_factory=dict)
    locator: SourceLocator
    raw_excerpt: str | None = None
    display_excerpt: str | None = None
    excerpt_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    extraction_method: str
    extraction_version: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_source_binding(self) -> "Evidence":
        if self.document_id and not self.source_sha256:
            raise ValueError("document evidence requires source_sha256")
        if self.source_document_ids and set(self.source_document_ids) != set(
            self.source_document_sha256
        ):
            raise ValueError(
                "source_document_ids and source_document_sha256 must match"
            )
        if (
            self.evidence_type
            in {
                EvidenceType.TEXT,
                EvidenceType.TABLE_CELL,
                EvidenceType.VISUAL_REGION,
            }
            and not self.document_id
        ):
            raise ValueError("source evidence requires document_id")
        if self.evidence_type == EvidenceType.COMPARISON and not (
            self.package_id or self.document_id or self.source_document_ids
        ):
            raise ValueError(
                "comparison evidence requires a package or source document"
            )
        return self


class KnowledgeChunk(ModelBase):
    chunk_id: str
    source_name: str
    source_sha256: str = Field(min_length=64, max_length=64)
    source_version: str
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    source_kind: KnowledgeSourceKind
    clause_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalSource(StrEnum):
    """一条召回命中来自哪类候选生成器。"""

    LEXICAL = "lexical"
    VECTOR = "vector"


class RetrievalFilter(ModelBase):
    """检索候选的结构化范围，不承载任何审核结论。

    文档、条款、来源和版本过滤作用于知识块本身；适用规则过滤只作用于
    ``source_kind=rule`` 的规则定义块，合同正文不会因为规则定义元数据缺失
    而被误删。所有非空字段均按白名单解释，空列表表示不限制该维度。
    """

    document_ids: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)
    source_names: list[str] = Field(default_factory=list)
    source_sha256s: list[str] = Field(default_factory=list)
    source_versions: list[str] = Field(default_factory=list)
    source_kinds: list[KnowledgeSourceKind] = Field(default_factory=list)
    document_kinds: list[DocumentKind] = Field(default_factory=list)
    rule_versions: list[str] = Field(default_factory=list)
    applicable_rule_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def deduplicate_values(self) -> "RetrievalFilter":
        """保持过滤条件的声明稳定，避免同一条件产生不同指纹。"""

        for field_name in (
            "document_ids",
            "clause_ids",
            "source_names",
            "source_sha256s",
            "source_versions",
            "source_kinds",
            "document_kinds",
            "rule_versions",
            "applicable_rule_ids",
            "evidence_ids",
        ):
            values = getattr(self, field_name)
            setattr(self, field_name, list(dict.fromkeys(values)))
        return self


class RetrievalQuery(ModelBase):
    """一次规则审查的唯一检索查询契约。"""

    query_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    terminology_version: str = Field(
        default=TERMINOLOGY_NORMALIZATION_VERSION,
        min_length=1,
    )
    purpose: Literal["rule_review", "playbook_position", "cross_document_consistency"]
    text: str = Field(min_length=1, max_length=4000)
    clause_types: list[str] = Field(default_factory=list)
    lexical_terms: list[str] = Field(default_factory=list)
    exact_anchors: list[str] = Field(default_factory=list)
    numeric_anchors: list[str] = Field(default_factory=list)
    negation_anchors: list[str] = Field(default_factory=list)
    required_fact_types: list[str] = Field(default_factory=list)
    # 由规则声明或已注册 checker 的事实依赖映射而来；它们只用于提升候选
    # 召回，不能直接生成事实或审核结论。
    required_fact_anchors: list[str] = Field(default_factory=list)
    document_kinds: list[DocumentKind] = Field(default_factory=list)
    # 查询必须在创建时绑定完整过滤范围；不允许先生成一个全库查询，
    # 再由下游猜测它属于哪个合同包或规则。
    retrieval_filter: RetrievalFilter

    @model_validator(mode="after")
    def validate_query_shape(self) -> "RetrievalQuery":
        """保证查询可回放且不会把空条件误当成业务查询。"""

        if not self.text.strip():
            raise ValueError("RetrievalQuery.text 不能为空")
        for field_name in (
            "clause_types",
            "lexical_terms",
            "exact_anchors",
            "numeric_anchors",
            "negation_anchors",
            "required_fact_types",
            "required_fact_anchors",
            "document_kinds",
        ):
            values = getattr(self, field_name)
            setattr(self, field_name, list(dict.fromkeys(values)))
        if not set(self.required_fact_anchors).issubset(self.exact_anchors):
            raise ValueError("RetrievalQuery 的事实锚点必须同时属于精确锚点")
        if not self.retrieval_filter.document_ids:
            raise ValueError("RetrievalQuery 必须绑定当前合同包文档范围")
        if self.rule_id not in self.retrieval_filter.applicable_rule_ids:
            raise ValueError("RetrievalQuery 必须绑定自身 rule_id 的过滤范围")
        if self.rule_version not in self.retrieval_filter.rule_versions:
            raise ValueError("RetrievalQuery 必须绑定自身 rule_version 的过滤范围")
        if KnowledgeSourceKind.CONTRACT not in self.retrieval_filter.source_kinds:
            raise ValueError("RetrievalQuery 必须允许检索合同正文候选")
        expected_document_kinds = (
            self.retrieval_filter.document_kinds or self.document_kinds
        )
        if set(self.document_kinds) != set(expected_document_kinds):
            raise ValueError("RetrievalQuery 的文档角色与过滤范围不一致")
        return self


class RetrievalHit(ModelBase):
    chunk_id: str
    score: float = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1)
    matched_terms: list[str] = Field(default_factory=list)
    retrieval_sources: list[RetrievalSource] = Field(
        default_factory=lambda: [RetrievalSource.LEXICAL], min_length=1
    )
    lexical_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    # score 保留初排适配器分数；精排分数单独保存，避免破坏 BM25/RRF 审计语义。
    rerank_score: float | None = Field(default=None, ge=0, le=1)
    rerank_features: dict[str, float] = Field(default_factory=dict)


class CandidateEvidence(ModelBase):
    """从检索轨迹规范化出的候选证据，不携带任何审核结论。"""

    candidate_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    rank: int = Field(ge=1)
    chunk_id: str = Field(min_length=1)
    document_id: str | None = None
    source_name: str = Field(min_length=1)
    source_sha256: str = Field(min_length=64, max_length=64)
    source_version: str = Field(min_length=1)
    source_kind: KnowledgeSourceKind
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    clause_ids: list[str] = Field(default_factory=list)
    score: float = Field(ge=0)
    retrieval_sources: list[RetrievalSource] = Field(min_length=1)
    matched_terms: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_candidate_scope(self) -> "CandidateEvidence":
        """限制候选只能指向真实知识块及其证据范围。"""

        if self.source_kind == KnowledgeSourceKind.CONTRACT and not self.document_id:
            raise ValueError("合同候选必须带 document_id")
        if self.source_kind == KnowledgeSourceKind.RULE and self.document_id:
            raise ValueError("规则候选不能带合同 document_id")
        self.evidence_ids = list(dict.fromkeys(self.evidence_ids))
        self.clause_ids = list(dict.fromkeys(self.clause_ids))
        self.retrieval_sources = list(dict.fromkeys(self.retrieval_sources))
        self.matched_terms = list(dict.fromkeys(self.matched_terms))
        return self


class EvidenceAssessmentOutcome(StrEnum):
    """检索候选是否具备进入事实或语义判断的证据资格。"""

    ACCEPT = "ACCEPT"
    INSUFFICIENT = "INSUFFICIENT"
    REJECT = "REJECT"


class EvidenceAssessment(ModelBase):
    """对单个检索候选执行的证据资格裁决，不等同于规则结论。"""

    assessment_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    source_kind: KnowledgeSourceKind
    outcome: EvidenceAssessmentOutcome
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    matched_exact_anchors: list[str] = Field(default_factory=list)
    matched_required_fact_anchors: list[str] = Field(default_factory=list)
    matched_numeric_anchors: list[str] = Field(default_factory=list)
    matched_negation_anchors: list[str] = Field(default_factory=list)
    assessed_by: Literal["deterministic_gate", "semantic_model"]
    assessment_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_anchor_matches(self) -> "EvidenceAssessment":
        """保证审计快照中的证据和锚点引用稳定且不重复。"""

        self.evidence_ids = list(dict.fromkeys(self.evidence_ids))
        for field_name in (
            "matched_exact_anchors",
            "matched_required_fact_anchors",
            "matched_numeric_anchors",
            "matched_negation_anchors",
        ):
            values = getattr(self, field_name)
            setattr(self, field_name, list(dict.fromkeys(values)))
        return self


class RetrievalTrace(ModelBase):
    trace_id: str
    retrieval_query: RetrievalQuery
    index_version: str
    retrieval_mode: RetrievalMode = RetrievalMode.LEXICAL
    fusion_method: RetrievalFusion = RetrievalFusion.NONE
    top_k: int = Field(gt=0)
    hits: list[RetrievalHit] = Field(default_factory=list)
    used_for_rule_ids: list[str] = Field(default_factory=list)
    reranker_version: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_trace_contract(self) -> "RetrievalTrace":
        """保证一条轨迹只服务声明过的查询规则且候选块不重复。"""

        if self.retrieval_query.rule_id not in self.used_for_rule_ids:
            raise ValueError("RetrievalTrace 必须声明查询规则的使用范围")
        if len(self.used_for_rule_ids) != len(set(self.used_for_rule_ids)):
            raise ValueError("RetrievalTrace 的 used_for_rule_ids 必须唯一")
        hit_ids = [hit.chunk_id for hit in self.hits]
        if len(hit_ids) != len(set(hit_ids)):
            raise ValueError("RetrievalTrace 的命中知识块不能重复")
        if len(hit_ids) > self.top_k:
            raise ValueError("RetrievalTrace 命中数不能超过 top_k")
        return self


class SemanticReviewItem(ModelBase):
    rule_id: str
    status: FindingStatus
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    recommended_action: str | None = None


class SemanticReviewResponse(ModelBase):
    response_id: str
    provider: str
    model_version: str
    prompt_version: str
    request_fingerprint: str = Field(min_length=64, max_length=64)
    items: list[SemanticReviewItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ContractFact(ModelBase):
    fact_id: str
    fact_type: str
    value: Any
    normalized_value: Any | None = None
    unit: str | None = None
    source_document_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(
        default_factory=list,
        description="形成该事实的 CandidateEvidence；空值仅适用于非检索事实。",
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    extractor_version: str
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_candidate_binding_for_derived_fact(self) -> "ContractFact":
        """要求由候选正文抽取的事实保留候选身份，阻断全文旁路。"""

        derived = (
            self.fact_type == "keyword_presence"
            or self.fact_type == "tax_rate"
            or self.fact_type.startswith("financial.")
            or self.fact_type.startswith("contract_element:")
            or self.fact_type.startswith("contract_term:")
        )
        if derived and not self.candidate_ids:
            raise ValueError(
                f"检索派生事实必须绑定 CandidateEvidence: {self.fact_type}"
            )
        return self


class ClauseKind(StrEnum):
    """合同文本片段的结构类型。"""

    NUMBERED = "numbered"
    UNNUMBERED = "unnumbered"
    TABLE = "table"


class ContractClause(ModelBase):
    """可回指原文的合同条款或最小审查片段。"""

    clause_id: str
    document_id: str
    clause_kind: ClauseKind
    clause_number: str | None = None
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    order: int = Field(ge=0)
    source_chunk_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    extractor_version: str


class ClauseRelationType(StrEnum):
    """条款之间或条款与定义项之间的可审计关系。"""

    PARENT_OF = "parent_of"
    DEFINES = "defines"
    REFERENCES = "references"


class ClauseRelationTargetType(StrEnum):
    CLAUSE = "clause"
    TERM = "term"


class ClauseRelationResolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ClauseRelation(ModelBase):
    """从条款文本和编号结构确定性构建的关系边。"""

    relation_id: str
    relation_type: ClauseRelationType
    source_clause_id: str
    target_clause_id: str | None = None
    target_label: str = Field(min_length=1)
    target_type: ClauseRelationTargetType
    resolution: ClauseRelationResolution
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    extractor_version: str

    @model_validator(mode="after")
    def validate_relation_shape(self) -> "ClauseRelation":
        """拒绝无法被下游按关系类型解释的边。"""

        if self.target_type == ClauseRelationTargetType.CLAUSE:
            if (
                self.resolution == ClauseRelationResolution.RESOLVED
                and not self.target_clause_id
            ):
                raise ValueError("resolved clause relation requires target_clause_id")
            if (
                self.resolution == ClauseRelationResolution.UNRESOLVED
                and self.target_clause_id is not None
            ):
                raise ValueError("unresolved clause relation cannot carry target_clause_id")
        elif self.target_clause_id is not None:
            raise ValueError("term relation cannot carry target_clause_id")

        if self.relation_type == ClauseRelationType.PARENT_OF:
            if (
                self.target_type != ClauseRelationTargetType.CLAUSE
                or self.resolution != ClauseRelationResolution.RESOLVED
                or not self.target_clause_id
            ):
                raise ValueError("parent relation must resolve to a clause")
        elif self.relation_type == ClauseRelationType.DEFINES:
            if (
                self.target_type != ClauseRelationTargetType.TERM
                or self.resolution != ClauseRelationResolution.RESOLVED
            ):
                raise ValueError("definition relation must resolve to a term")
        elif self.relation_type == ClauseRelationType.REFERENCES:
            if self.target_type != ClauseRelationTargetType.CLAUSE:
                raise ValueError("reference relation must target a clause")
        return self


class ObligationModality(StrEnum):
    REQUIRED = "required"
    PROHIBITED = "prohibited"


class ContractObligation(ModelBase):
    """从条款中保守识别的履约义务，未知字段保持空值。"""

    obligation_id: str
    clause_id: str
    obligor: str | None = None
    modality: ObligationModality
    action: str = Field(min_length=1)
    deadline: str | None = None
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    extractor_version: str


class ReviewQuestion(ModelBase):
    """由正式规则快照派生的、可独立回答的审查问题。"""

    question_id: str
    rule_id: str
    rule_version: str
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_value: Any | None = None
    risk_level: RiskLevel
    required_evidence: list[str] = Field(default_factory=list)
    source_snapshot: str


class AssessmentOutcome(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_MENTIONED = "NOT_MENTIONED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PlaybookAction(StrEnum):
    """Playbook 判断完成后可执行的审查动作。"""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REVISE = "REVISE"
    ESCALATE = "ESCALATE"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"


class MissingClausePolicy(StrEnum):
    """Playbook 找不到目标条款时采用的处置策略。"""

    WARN = "WARN"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PlaybookSpec(ModelBase):
    """企业审查立场与动作的版本化配置。

    Playbook 与自由文本规则条件分开保存：condition 说明为什么设置规则，
    本对象说明合同中哪些立场可接受、哪些需要修改以及下一步动作。
    """

    playbook_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    evaluation_mode: Literal["position", "checker"] = "position"
    clause_types: list[str] = Field(default_factory=list)
    preferred_position: str | None = None
    fallback_positions: list[str] = Field(default_factory=list)
    prohibited_positions: list[str] = Field(default_factory=list)
    missing_clause_policy: MissingClausePolicy = MissingClausePolicy.UNKNOWN
    action_on_preferred: PlaybookAction = PlaybookAction.ACCEPT
    action_on_fallback: PlaybookAction = PlaybookAction.REVISE
    action_on_prohibited: PlaybookAction = PlaybookAction.REJECT
    suggested_language: str | None = None
    escalation_condition: str | None = None
    escalation_thresholds: list["EscalationThreshold"] = Field(default_factory=list)

    @property
    def has_deterministic_positions(self) -> bool:
        """判断 Playbook 是否配置了可由原文证据直接判断的立场。"""

        return self.evaluation_mode == "position" and bool(
            self.clause_types
            or self.preferred_position
            or self.fallback_positions
            or self.prohibited_positions
        )


class QuestionAssessment(ModelBase):
    """审查问题的证据化结论，不把未知或未提及伪装成通过。"""

    assessment_id: str
    question_id: str
    finding_id: str
    outcome: AssessmentOutcome
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    assessed_by: str = Field(min_length=1)


class ApplicabilitySpec(ModelBase):
    applicability: Literal[
        "required", "not_applicable", "expected_value", "unspecified"
    ]
    expected_value: Any | None = None
    note: str | None = None
    party_positions: list[PartyPosition] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    transaction_tags: list[str] = Field(default_factory=list)
    transaction_amount_min: Decimal | None = Field(default=None, ge=Decimal("0"))
    transaction_amount_max: Decimal | None = Field(default=None, ge=Decimal("0"))
    document_kinds: list[DocumentKind] = Field(default_factory=list)
    document_kinds_any: list[DocumentKind] = Field(
        default_factory=list,
        description="至少存在一种角色时满足的文档角色条件。",
    )
    exceptions: list["ApplicabilityException"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context_ranges(self) -> "ApplicabilitySpec":
        """阻断无法解释的金额范围和重复结构化条件。"""

        if (
            self.transaction_amount_min is not None
            and self.transaction_amount_max is not None
            and self.transaction_amount_min > self.transaction_amount_max
        ):
            raise ValueError("规则适用金额下限不能大于上限")
        self.jurisdictions = list(dict.fromkeys(item.strip() for item in self.jurisdictions if item.strip()))
        self.transaction_tags = list(dict.fromkeys(item.strip() for item in self.transaction_tags if item.strip()))
        self.document_kinds = list(dict.fromkeys(self.document_kinds))
        self.document_kinds_any = list(dict.fromkeys(self.document_kinds_any))
        exception_ids = [item.exception_id for item in self.exceptions]
        if len(exception_ids) != len(set(exception_ids)):
            raise ValueError("规则适用例外 exception_id 必须唯一")
        return self


class ApplicabilityException(ModelBase):
    """规则适用条件的结构化例外，命中后覆盖基础适用结论。"""

    exception_id: str = Field(min_length=1)
    result: Literal["required", "not_applicable", "unknown"]
    party_positions: list[PartyPosition] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    transaction_tags: list[str] = Field(default_factory=list)
    transaction_amount_min: Decimal | None = Field(default=None, ge=Decimal("0"))
    transaction_amount_max: Decimal | None = Field(default=None, ge=Decimal("0"))
    document_kinds: list[DocumentKind] = Field(default_factory=list)
    document_kinds_any: list[DocumentKind] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_exception_range(self) -> "ApplicabilityException":
        if not any(
            (
                self.party_positions,
                self.jurisdictions,
                self.transaction_tags,
                self.transaction_amount_min is not None,
                self.transaction_amount_max is not None,
                self.document_kinds,
                self.document_kinds_any,
            )
        ):
            raise ValueError("规则适用例外必须至少声明一个结构化条件")
        if (
            self.transaction_amount_min is not None
            and self.transaction_amount_max is not None
            and self.transaction_amount_min > self.transaction_amount_max
        ):
            raise ValueError("规则适用例外金额下限不能大于上限")
        self.jurisdictions = list(dict.fromkeys(item.strip() for item in self.jurisdictions if item.strip()))
        self.transaction_tags = list(dict.fromkeys(item.strip() for item in self.transaction_tags if item.strip()))
        self.document_kinds = list(dict.fromkeys(self.document_kinds))
        self.document_kinds_any = list(dict.fromkeys(self.document_kinds_any))
        return self


class EscalationThreshold(ModelBase):
    """Playbook 的结构化升级阈值。"""

    threshold_id: str = Field(min_length=1)
    metric: Literal[
        "transaction_amount",
        "payment_ratio",
        "confidence",
        "risk_level",
    ]
    operator: Literal[">", ">=", "<", "<=", "=="]
    value: Decimal = Field(ge=Decimal("0"))
    action: PlaybookAction = PlaybookAction.ESCALATE
    reason: str = Field(min_length=1)


class Rule(ModelBase):
    rule_id: str
    legacy_id: int | None = Field(default=None, ge=1)
    version: str
    title: str
    category: str
    applies_to: list[str] = Field(default_factory=list)
    condition: str | None = None
    check_method: Literal[
        "classification", "deterministic", "keyword", "semantic", "visual", "human"
    ]
    # 检查器是规则快照到领域实现的明确绑定；未配置时由上层保守输出 UNKNOWN。
    checker: str | None = Field(default=None, min_length=1)
    expected_value: Any | None = None
    risk_level: RiskLevel | None = None
    applicability: dict[str, ApplicabilitySpec] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    human_review: bool = False
    source_snapshot: str
    source_locator: SourceLocator | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    playbook: PlaybookSpec | None = None


class RuleBundle(ModelBase):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str
    source_filename: str
    source_sha256: str = Field(min_length=64, max_length=64)
    source_sheet: str
    source_range: str
    source_notes: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(min_length=1)
    # 新构造的规则包只能是草稿；正式审查必须经过显式发布流程并携带
    # release_fingerprint/published_at，避免未验证对象被误当成正式快照。
    release_status: RuleBundleStatus = RuleBundleStatus.DRAFT
    compatible_review_schema: Literal["2.0"] = "2.0"
    playbook_schema_version: Literal["1.0"] = "1.0"
    parent_bundle_id: str | None = None
    release_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    published_at: datetime | None = None
    imported_at: datetime = Field(default_factory=utc_now)


class SemanticModelRequest(ModelBase):
    """发送给语义审查器的规则、上下文和候选证据快照。"""

    request_id: str
    provider: str
    model_version: str
    prompt_version: str
    request_fingerprint: str = Field(min_length=64, max_length=64)
    rule_ids: list[str] = Field(min_length=1)
    rule_definitions: list[Rule] = Field(
        min_length=1,
        description="与 rule_ids 完全对应的版本化规则定义，不允许模型自行补写规则。",
    )
    candidate_evidence_by_rule: dict[str, list[CandidateEvidence]]
    system_instruction: str = Field(min_length=1)
    configuration: dict[str, Any] = Field(default_factory=dict)
    review_context: ReviewContext
    retrieval_queries_by_rule: dict[str, RetrievalQuery]

    @model_validator(mode="after")
    def validate_candidate_context(self) -> "SemanticModelRequest":
        """保证规则定义、查询和候选证据逐条对齐。"""

        rule_ids = set(self.rule_ids)
        if len(self.rule_ids) != len(rule_ids):
            raise ValueError("SemanticModelRequest 的 rule_ids 必须唯一")
        rule_definitions_by_id = {
            rule.rule_id: rule for rule in self.rule_definitions
        }
        if len(rule_definitions_by_id) != len(self.rule_definitions):
            raise ValueError("语义请求的规则定义 rule_id 必须唯一")
        if set(rule_definitions_by_id) != rule_ids:
            raise ValueError("语义请求规则定义必须覆盖且仅覆盖 rule_ids")
        self.rule_definitions = [
            rule_definitions_by_id[rule_id] for rule_id in self.rule_ids
        ]
        if set(self.candidate_evidence_by_rule) != rule_ids:
            raise ValueError("语义请求候选证据必须覆盖且仅覆盖 rule_ids")
        if set(self.retrieval_queries_by_rule) != rule_ids:
            raise ValueError("语义请求 RetrievalQuery 必须覆盖且仅覆盖 rule_ids")
        for rule_id, candidates in self.candidate_evidence_by_rule.items():
            query = self.retrieval_queries_by_rule[rule_id]
            rule_definition = rule_definitions_by_id[rule_id]
            if (
                query.rule_id != rule_id
                or query.rule_version != rule_definition.version
            ):
                raise ValueError("语义请求规则定义与 RetrievalQuery 不一致")
            if any(
                candidate.rule_id != rule_id
                or candidate.query_id != query.query_id
                or candidate.rule_version != query.rule_version
                for candidate in candidates
            ):
                raise ValueError("语义请求候选证据与其 RetrievalQuery 不一致")
        return self


class Finding(ModelBase):
    finding_id: str
    rule_id: str
    rule_version: str
    status: FindingStatus
    risk_level: RiskLevel
    title: str
    reason: str
    evidence_ids: list[str] = Field(min_length=1)
    fact_ids: list[str] = Field(default_factory=list)
    comparison: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_quality: EvidenceQuality = EvidenceQuality.SUFFICIENT
    automatic: bool = False
    recommended_action: str | None = None
    action: PlaybookAction | None = None
    playbook_id: str | None = None
    clause_ids: list[str] = Field(default_factory=list)
    uncertainty_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class DecisionType(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    WAIVE = "WAIVE"
    DEFER = "DEFER"


class ReviewDecision(ModelBase):
    decision_id: str
    run_id: str
    finding_id: str
    decision: DecisionType
    actor_id: str
    actor_role: str
    comment: str
    evidence_ids: list[str] = Field(min_length=1)
    decided_at: datetime = Field(default_factory=utc_now)


class RevisionOperation(StrEnum):
    """人工复核修订提案中允许的有限操作。"""

    INSERT = "INSERT"
    DELETE = "DELETE"
    REPLACE = "REPLACE"
    COMMENT = "COMMENT"


class RevisionChange(ModelBase):
    """一条绑定原文证据的条款级修订提案。"""

    change_id: str
    finding_id: str
    clause_id: str | None = None
    operation: RevisionOperation
    original_text: str = ""
    proposed_text: str = ""
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ContractRevisionSet(ModelBase):
    """从一次审查结果确定性生成、可供人工复核的变更集合。"""

    revision_id: str
    run_id: str
    base_result_fingerprint: str
    source_version: str
    status: Literal["PROPOSED", "CONFIRMED", "REJECTED"] = "PROPOSED"
    changes: list[RevisionChange] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    revision_fingerprint: str | None = None


class VersionChangeKind(StrEnum):
    """合同版本差异类型。"""

    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"


class VersionChange(ModelBase):
    """一条可回指比对输入的版本差异。"""

    change_id: str
    kind: VersionChangeKind
    base_index: int | None = Field(default=None, ge=0)
    compare_index: int | None = Field(default=None, ge=0)
    base_text: str = ""
    compare_text: str = ""
    evidence_ids: list[str] = Field(min_length=1)
    clause_ids: list[str] = Field(default_factory=list)


class VersionImpactLevel(StrEnum):
    """版本变化对业务风险或绑定关系的影响级别。"""

    INCREASED = "increased"
    DECREASED = "decreased"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
    REQUIRES_REVIEW = "requires_review"


class ContractVersionImpact(ModelBase):
    """把文本变化映射到业务义务、风险和 Playbook 重审动作。"""

    impact_id: str = Field(min_length=1)
    rule_id: str | None = None
    change_ids: list[str] = Field(min_length=1)
    obligation_ids: list[str] = Field(
        default_factory=list,
        description="受版本差异直接影响的 ContractObligation 身份。",
    )
    changed_obligations: list[str] = Field(min_length=1)
    payment_risk: VersionImpactLevel = VersionImpactLevel.UNKNOWN
    liability_risk: VersionImpactLevel = VersionImpactLevel.UNKNOWN
    liability_cap_impact: VersionImpactLevel = Field(
        default=VersionImpactLevel.UNKNOWN,
        description=(
            "责任上限的风险方向：increased 表示上限扩大/放宽，"
            "decreased 表示上限收窄或新增；无法确定时为 requires_review。"
        ),
    )
    delivery_acceptance_binding: VersionImpactLevel = VersionImpactLevel.UNKNOWN
    precedence_resolution: Literal["base", "compare", "unresolved", "not_required"] = (
        "unresolved"
    )
    playbook_retrigger_required: bool = True
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ContractVersionComparison(ModelBase):
    """挂入 ``ReviewResult`` 的合同版本比对结果。"""

    comparison_id: str
    run_id: str
    base_filename: str
    compare_filename: str
    base_source_sha256: str = Field(min_length=64, max_length=64)
    compare_source_sha256: str = Field(min_length=64, max_length=64)
    similarity: float = Field(ge=0, le=1)
    added: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    modified: int = Field(default=0, ge=0)
    source_version: str = Field(min_length=1)
    options: dict[str, bool] = Field(default_factory=dict)
    changes: list[VersionChange] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    impacts: list[ContractVersionImpact] = Field(default_factory=list)
    retrigger_rule_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ReviewReport(ModelBase):
    report_id: str
    run_id: str
    overall_status: FindingStatus
    finding_counts: dict[str, int] = Field(default_factory=dict)
    finding_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    comparison_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)
    review_required: bool
    generated_by: str
    report_version: str
    generated_at: datetime = Field(default_factory=utc_now)


class StageEvent(ModelBase):
    """统一的阶段事件账本条目。

    审查运行与异步任务共用同一事件形状；事件只记录状态、操作者和证据
    引用，不携带合同正文或凭据，便于持久化、回放和脱敏导出。
    """

    event_id: str
    subject_type: Literal["review_run", "async_task"]
    subject_id: str
    from_stage: str | None = None
    to_stage: str
    action: str
    actor: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)


class ReviewRun(ModelBase):
    run_id: str
    package_id: str
    status: ReviewStatus
    input_document_sha256: dict[str, str] = Field(default_factory=dict)
    parser_version: str
    rule_version: str
    model_version: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    configuration_fingerprint: str
    finding_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    comparison_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)
    report_id: str | None = None
    result_fingerprint: str | None = None
    stage_events: list[StageEvent] = Field(min_length=1)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class ReviewResult(ModelBase):
    schema_version: Literal["2.0"]
    package: ContractPackage
    review_context: ReviewContext
    documents: list[Document] = Field(min_length=1)
    rule_bundle: RuleBundle
    parsed_documents: list[ParsedDocument] = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    knowledge_chunks: list[KnowledgeChunk] = Field(default_factory=list)
    retrieval_traces: list[RetrievalTrace] = Field(default_factory=list)
    candidate_evidence: list[CandidateEvidence] = Field(default_factory=list)
    evidence_assessments: list[EvidenceAssessment]
    semantic_request: SemanticModelRequest | None = None
    semantic_response: SemanticReviewResponse | None = None
    attachment_references: list[AttachmentReference] = Field(default_factory=list)
    facts: list[ContractFact] = Field(default_factory=list)
    clauses: list[ContractClause]
    clause_relations: list[ClauseRelation] = Field(default_factory=list)
    obligations: list[ContractObligation]
    review_questions: list[ReviewQuestion]
    question_assessments: list[QuestionAssessment]
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[ReviewDecision] = Field(default_factory=list)
    version_comparisons: list[ContractVersionComparison] = Field(default_factory=list)
    revision_sets: list[ContractRevisionSet] = Field(default_factory=list)
    post_review_sequence: list[str] = Field(default_factory=list)
    run: ReviewRun
    report: ReviewReport
