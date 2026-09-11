"""Typed domain models for parsed documents and evidence-first review results."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class FindingStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


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
    source_snapshot: str
    created_at: datetime = Field(default_factory=utc_now)


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


class AttachmentReference(ModelBase):
    reference_id: str
    referenced_name: str
    aliases: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
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
    # 默认合同正文，兼容尚未带来源字段的结果；规则导入会显式写入 RULE。
    source_kind: KnowledgeSourceKind = KnowledgeSourceKind.CONTRACT
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_rule_source(cls, value: Any) -> Any:
        """为旧规则知识块从 rule_id 元数据补齐来源类型。"""

        if not isinstance(value, dict) or "source_kind" in value:
            return value
        metadata = value.get("metadata")
        if isinstance(metadata, dict) and metadata.get("rule_id"):
            return {**value, "source_kind": KnowledgeSourceKind.RULE}
        return value


class RetrievalHit(ModelBase):
    chunk_id: str
    score: float = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1)
    matched_terms: list[str] = Field(default_factory=list)


class RetrievalTrace(ModelBase):
    trace_id: str
    query: str = Field(min_length=1)
    index_version: str
    top_k: int = Field(gt=0)
    hits: list[RetrievalHit] = Field(default_factory=list)
    used_for_rule_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class SemanticModelRequest(ModelBase):
    request_id: str
    provider: str
    model_version: str
    prompt_version: str
    request_fingerprint: str = Field(min_length=64, max_length=64)
    rule_ids: list[str] = Field(min_length=1)
    context_chunks: list[KnowledgeChunk] = Field(default_factory=list)
    system_instruction: str = Field(min_length=1)
    configuration: dict[str, Any] = Field(default_factory=dict)
    review_context: ReviewContext | None = None


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
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    extractor_version: str
    created_at: datetime = Field(default_factory=utc_now)


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

    @property
    def has_deterministic_positions(self) -> bool:
        """判断 Playbook 是否配置了可由原文证据直接判断的立场。"""

        return bool(
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
    bundle_id: str
    source_filename: str
    source_sha256: str = Field(min_length=64, max_length=64)
    source_sheet: str
    source_range: str
    source_notes: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(min_length=1)
    imported_at: datetime = Field(default_factory=utc_now)


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


class ReviewReport(ModelBase):
    report_id: str
    run_id: str
    overall_status: FindingStatus
    finding_counts: dict[str, int] = Field(default_factory=dict)
    finding_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
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
    report_id: str | None = None
    result_fingerprint: str | None = None
    stage_events: list[StageEvent] = Field(min_length=1)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class ReviewResult(ModelBase):
    schema_version: Literal["2.0"]
    package: ContractPackage
    review_context: ReviewContext | None = None
    documents: list[Document] = Field(min_length=1)
    rule_bundle: RuleBundle
    parsed_documents: list[ParsedDocument] = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    knowledge_chunks: list[KnowledgeChunk] = Field(default_factory=list)
    retrieval_traces: list[RetrievalTrace] = Field(default_factory=list)
    semantic_request: SemanticModelRequest | None = None
    semantic_response: SemanticReviewResponse | None = None
    attachment_references: list[AttachmentReference] = Field(default_factory=list)
    facts: list[ContractFact] = Field(default_factory=list)
    clauses: list[ContractClause]
    obligations: list[ContractObligation]
    review_questions: list[ReviewQuestion]
    question_assessments: list[QuestionAssessment]
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[ReviewDecision] = Field(default_factory=list)
    run: ReviewRun
    report: ReviewReport
