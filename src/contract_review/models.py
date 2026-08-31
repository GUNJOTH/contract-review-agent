"""Typed domain models for parsed documents and evidence-first review results."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        if self.locator_type in {"page", "bbox", "text_span"} and self.page_number is None:
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
            raise ValueError("source_document_ids and source_document_sha256 must match")
        if self.evidence_type in {
            EvidenceType.TEXT,
            EvidenceType.TABLE_CELL,
            EvidenceType.VISUAL_REGION,
        } and not self.document_id:
            raise ValueError("source evidence requires document_id")
        if self.evidence_type == EvidenceType.COMPARISON and not (
            self.package_id or self.document_id or self.source_document_ids
        ):
            raise ValueError("comparison evidence requires a package or source document")
        return self


class KnowledgeChunk(ModelBase):
    chunk_id: str
    source_name: str
    source_sha256: str = Field(min_length=64, max_length=64)
    source_version: str
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class ApplicabilitySpec(ModelBase):
    applicability: Literal["required", "not_applicable", "expected_value", "unspecified"]
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


class ReviewTransition(ModelBase):
    from_status: ReviewStatus | None = None
    to_status: ReviewStatus
    action: str
    actor: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
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
    transitions: list[ReviewTransition] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


class ReviewResult(ModelBase):
    package: ContractPackage
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
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[ReviewDecision] = Field(default_factory=list)
    run: ReviewRun
    report: ReviewReport
