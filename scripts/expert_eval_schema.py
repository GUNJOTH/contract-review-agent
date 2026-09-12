"""中文合同专家评测集的数据契约。

评测案例必须描述完整合同包和专家标注闭环。该模块只服务离线评测，
不向生产审查接口提供旧格式兼容或隐式默认值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExpertModel(BaseModel):
    """所有评测数据对象的严格基类，拒绝未声明字段。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


ExpertDocumentKind = Literal[
    "main_contract",
    "annex",
    "quotation",
    "technical_agreement",
    "amendment",
]


class ExpertDocument(ExpertModel):
    """合同包中的一份可解析文档。"""

    document_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    document_kind: ExpertDocumentKind
    version_label: str = Field(min_length=1)
    text: str = Field(min_length=1)


class BusinessBackground(ExpertModel):
    """专家判断所依赖的交易背景，而不是合同正文的替代品。"""

    scenario: str = Field(min_length=1)
    transaction_purpose: str = Field(min_length=1)
    review_focus: list[str] = Field(min_length=1)

    def as_transaction_context(self) -> str:
        """将结构化背景转换为领域层使用的可追溯上下文文本。"""

        return (
            f"场景：{self.scenario}；交易目的：{self.transaction_purpose}；"
            f"审查重点：{'、'.join(self.review_focus)}。"
        )


class EnterprisePosition(ExpertModel):
    """企业在本次交易中的立场、底线和升级触发条件。"""

    party_position: Literal["buyer", "seller", "both", "unknown"]
    position_summary: str = Field(min_length=1)
    non_negotiables: list[str] = Field(min_length=1)
    acceptable_fallbacks: list[str] = Field(default_factory=list)
    escalation_triggers: list[str] = Field(min_length=1)


class ReviewInputContext(ExpertModel):
    """传给合同审查领域层的合同类型、法域和规则范围。"""

    contract_type: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    transaction_tags: list[str] = Field(default_factory=list)
    transaction_amount: str | None = None
    review_scope: list[str] = Field(default_factory=list)


class ExpertContractPackage(ExpertModel):
    """每个评测案例的完整合同包及业务前提。"""

    package_id: str = Field(min_length=1)
    main_contract: ExpertDocument
    supporting_documents: list[ExpertDocument] = Field(min_length=1)
    version_or_amendment: list[ExpertDocument] = Field(min_length=1)
    document_precedence: list[str] = Field(default_factory=list)
    business_background: BusinessBackground
    enterprise_position: EnterprisePosition
    review_context: ReviewInputContext

    @property
    def documents(self) -> tuple[ExpertDocument, ...]:
        """按业务分组顺序返回全部文档，供评测器构造临时文件。"""

        return (
            self.main_contract,
            *self.supporting_documents,
            *self.version_or_amendment,
        )

    @model_validator(mode="after")
    def validate_document_groups(self) -> "ExpertContractPackage":
        """确保合同包的三类必要文档都真实存在且身份唯一。"""

        if self.main_contract.document_kind != "main_contract":
            raise ValueError("main_contract 必须使用 main_contract 文档类型")
        supported_kinds = {
            document.document_kind for document in self.supporting_documents
        }
        if not supported_kinds.intersection({"annex", "quotation", "technical_agreement"}):
            raise ValueError(
                "supporting_documents 至少需要 annex、quotation 或 technical_agreement"
            )
        if any(
            document.document_kind != "amendment"
            for document in self.version_or_amendment
        ):
            raise ValueError("version_or_amendment 只能包含 amendment 文档")
        document_ids = [document.document_id for document in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("合同包内 document_id 必须唯一")
        if len(self.document_precedence) != len(set(self.document_precedence)):
            raise ValueError("合同包 document_precedence 必须唯一")
        if not set(self.document_precedence).issubset(document_ids):
            raise ValueError("合同包 document_precedence 只能引用合同包内文档")
        filenames = [document.filename for document in self.documents]
        if len(filenames) != len(set(filenames)):
            raise ValueError("合同包内 filename 必须唯一")
        return self


class AnnotationMetadata(ExpertModel):
    """专家标注的来源、版本和当前签署状态。"""

    annotation_id: str = Field(min_length=1)
    annotator_id: str = Field(min_length=1)
    expert_role: str = Field(min_length=1)
    annotated_at: str = Field(min_length=1)
    annotation_state: Literal["seed", "adjudicated"]
    adjudication_note: str = Field(min_length=1)


class ClauseLocalization(ExpertModel):
    """条款定位标注，明确目标文档和期望是否存在。"""

    target_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    phrase: str = Field(min_length=1)
    expected_present: bool
    rationale: str = Field(min_length=1)


class EvidenceScope(ExpertModel):
    """规则结论允许引用的证据范围。"""

    scope_id: str = Field(min_length=1)
    checker: str = Field(min_length=1)
    document_ids: list[str] = Field(min_length=1)
    required_phrases: list[str] = Field(min_length=1)
    expected_evidence: bool
    rationale: str = Field(min_length=1)


RetrievalSliceType = Literal[
    "negation",
    "numeric",
    "definition",
    "exception",
    "cross_document_conflict",
    "version_override",
]


class RetrievalSlice(ExpertModel):
    """检索专项切片，固定法律文本中最容易被漏召回的表达。"""

    slice_type: RetrievalSliceType
    document_ids: list[str] = Field(min_length=1)
    phrases: list[str] = Field(min_length=1)
    expected_recall: bool
    rationale: str = Field(min_length=1)


class RetrievalAnnotation(ExpertModel):
    """一条规则的检索金标准、可接受候选和错误候选。"""

    retrieval_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    checker: str = Field(min_length=1)
    search_document_ids: list[str] = Field(min_length=1)
    expected_present: bool
    gold_document_ids: list[str] = Field(default_factory=list)
    gold_phrases: list[str] = Field(default_factory=list)
    acceptable_phrases: list[str] = Field(default_factory=list)
    error_candidate_phrases: list[str] = Field(min_length=1)
    slices: list[RetrievalSlice] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_retrieval_gold(self) -> "RetrievalAnnotation":
        """缺失条款必须明确标为无金标准，存在条款必须给出定位短语。"""

        if self.expected_present and not (
            self.gold_document_ids and self.gold_phrases
        ):
            raise ValueError("存在条款的检索标注必须提供金标准文档和短语")
        if not self.expected_present and (
            self.gold_document_ids or self.gold_phrases
        ):
            raise ValueError("缺失条款的检索标注不能伪造金标准候选")
        return self


FindingStatusValue = Literal[
    "PASS",
    "WARN",
    "BLOCK",
    "UNKNOWN",
    "NOT_APPLICABLE",
]


class RuleConclusion(ExpertModel):
    """专家对某个检查器的期望结论。"""

    checker: str = Field(min_length=1)
    expected_status: FindingStatusValue
    conclusion_basis: str = Field(min_length=1)


class UnknownReason(ExpertModel):
    """UNKNOWN 不是空值，而是带证据缺口原因的专家标注。"""

    checker: str = Field(min_length=1)
    expected_status: Literal["UNKNOWN"]
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)
    required_document_ids: list[str] = Field(min_length=1)


class FinancialFact(ExpertModel):
    """金额事实的规范值、来源文档和证据短语。"""

    fact_type: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    document_ids: list[str] = Field(min_length=1)
    evidence_phrases: list[str] = Field(min_length=1)
    fact_basis: str = Field(min_length=1)


class FinancialCalculation(ExpertModel):
    """专家期望的结构化金额计算结果，不从自然语言原因反解析。"""

    checker: str = Field(min_length=1)
    input_fact_types: list[str] = Field(min_length=1)
    expected_status: FindingStatusValue
    expected_comparison: dict[str, Any] = Field(default_factory=dict)
    calculation_basis: str = Field(min_length=1)


VersionChangeKind = Literal["added", "deleted", "modified"]


class VersionChange(ExpertModel):
    """专家标注的一条版本差异。"""

    kind: VersionChangeKind
    base_contains: str | None = None
    compare_contains: str | None = None

    @model_validator(mode="after")
    def validate_change_anchor(self) -> "VersionChange":
        """每条版本差异至少要能在一侧文档中被定位。"""

        if not self.base_contains and not self.compare_contains:
            raise ValueError("版本变化必须提供 base_contains 或 compare_contains")
        if self.kind == "added" and not self.compare_contains:
            raise ValueError("added 版本变化必须提供 compare_contains")
        if self.kind == "deleted" and not self.base_contains:
            raise ValueError("deleted 版本变化必须提供 base_contains")
        if self.kind == "modified" and not (
            self.base_contains and self.compare_contains
        ):
            raise ValueError("modified 版本变化必须同时提供两侧定位短语")
        return self


class VersionChanges(ExpertModel):
    """版本或补充协议的完整比较标注。"""

    base_document_id: str = Field(min_length=1)
    compare_document_id: str = Field(min_length=1)
    expected_added: int = Field(ge=0)
    expected_deleted: int = Field(ge=0)
    expected_modified: int = Field(ge=0)
    expected_changes: list[VersionChange] = Field(min_length=1)
    change_summary: str = Field(min_length=1)


class RedlineRecommendation(ExpertModel):
    """红线/修订建议的专家期望，支持明确标注“无需红线”。"""

    recommendation_id: str = Field(min_length=1)
    checker: str = Field(min_length=1)
    expectation: Literal["REQUIRED", "NONE"]
    target_document_ids: list[str] = Field(min_length=1)
    operation: Literal["REPLACE", "COMMENT"] | None = None
    anchor_phrase: str | None = None
    proposed_text_contains: str | None = None
    evidence_scope_id: str | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_redline_shape(self) -> "RedlineRecommendation":
        """要求红线建议具备可执行锚点，明确无红线时不伪造文本。"""

        if self.expectation == "REQUIRED":
            if not self.operation or not self.anchor_phrase:
                raise ValueError(
                    "REQUIRED 红线建议必须同时提供 operation 和 anchor_phrase"
                )
            if not self.evidence_scope_id:
                raise ValueError("REQUIRED 红线建议必须绑定 evidence_scope_id")
        elif any(
            value is not None
            for value in (
                self.operation,
                self.anchor_phrase,
                self.proposed_text_contains,
                self.evidence_scope_id,
            )
        ):
            raise ValueError("NONE 红线建议不能伪造操作、锚点或证据引用")
        return self


class ExpertAnnotation(ExpertModel):
    """一份完整的专家标注记录。"""

    metadata: AnnotationMetadata
    retrieval_annotations: list[RetrievalAnnotation] = Field(min_length=1)
    clause_localization: list[ClauseLocalization] = Field(min_length=1)
    evidence_scope: list[EvidenceScope] = Field(min_length=1)
    rule_conclusions: list[RuleConclusion] = Field(min_length=1)
    unknown_reasons: list[UnknownReason] = Field(min_length=1)
    financial_facts: list[FinancialFact] = Field(min_length=1)
    financial_calculations: list[FinancialCalculation] = Field(min_length=1)
    version_changes: VersionChanges
    redline_recommendations: list[RedlineRecommendation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_annotation_identity(self) -> "ExpertAnnotation":
        """拒绝会让评测结果无法唯一对齐的重复标识。"""

        identity_groups = {
            "条款定位 target_id": [
                item.target_id for item in self.clause_localization
            ],
            "检索专项 retrieval_id": [
                item.retrieval_id for item in self.retrieval_annotations
            ],
            "证据范围 scope_id": [item.scope_id for item in self.evidence_scope],
            "规则结论 checker": [
                item.checker for item in self.rule_conclusions
            ],
            "UNKNOWN 原因 checker": [item.checker for item in self.unknown_reasons],
            "金额计算 checker": [
                item.checker for item in self.financial_calculations
            ],
            "红线建议 recommendation_id": [
                item.recommendation_id for item in self.redline_recommendations
            ],
        }
        for label, values in identity_groups.items():
            if len(values) != len(set(values)):
                raise ValueError(f"{label} 必须唯一")
        conclusion_checkers = {
            conclusion.checker for conclusion in self.rule_conclusions
        }
        retrieval_checkers = {
            annotation.checker for annotation in self.retrieval_annotations
        }
        if retrieval_checkers != conclusion_checkers:
            raise ValueError("检索专项必须覆盖每条规则结论 checker，且不能多标")
        unknown_checkers = {
            conclusion.checker
            for conclusion in self.rule_conclusions
            if conclusion.expected_status == "UNKNOWN"
        }
        annotated_unknown_checkers = {
            reason.checker for reason in self.unknown_reasons
        }
        if annotated_unknown_checkers != unknown_checkers:
            raise ValueError(
                "UNKNOWN 原因必须逐条覆盖且仅覆盖期望结论为 UNKNOWN 的 checker"
            )
        return self


class ExpertCase(ExpertModel):
    """一个完整合同包案例，不允许使用旧的扁平 text 字段。"""

    case_id: str = Field(min_length=1)
    package: ExpertContractPackage
    expert_annotation: ExpertAnnotation

    @model_validator(mode="after")
    def validate_annotation_references(self) -> "ExpertCase":
        """确保所有标注都能回指合同包中的文档和证据范围。"""

        document_ids = {document.document_id for document in self.package.documents}
        documents_by_id = {
            document.document_id: document for document in self.package.documents
        }

        def phrase_in_documents(phrase: str, candidate_ids: set[str]) -> bool:
            """用空白归一化后的原文判断标注锚点是否真实存在。"""

            normalized_phrase = " ".join(phrase.split())
            return any(
                normalized_phrase in " ".join(
                    documents_by_id[document_id].text.split()
                )
                for document_id in candidate_ids
            )

        def validate_phrase_presence(
            label: str,
            phrases: list[str],
            candidate_ids: set[str],
            expected_present: bool = True,
        ) -> None:
            """阻断脱离合同原文的金标准、错误候选和金额事实标注。"""

            for phrase in phrases:
                present = phrase_in_documents(phrase, candidate_ids)
                if present != expected_present:
                    expectation = "存在" if expected_present else "不存在"
                    raise ValueError(
                        f"{label} 短语“{phrase}”在指定文档中应{expectation}"
                    )

        for target in self.expert_annotation.clause_localization:
            if target.document_id not in document_ids:
                raise ValueError(f"条款定位 {target.target_id} 引用了未知文档")
            validate_phrase_presence(
                f"条款定位 {target.target_id}",
                [target.phrase],
                {target.document_id},
                target.expected_present,
            )
        evidence_scope_ids = {
            scope.scope_id for scope in self.expert_annotation.evidence_scope
        }
        rule_checkers = {
            conclusion.checker
            for conclusion in self.expert_annotation.rule_conclusions
        }
        for scope in self.expert_annotation.evidence_scope:
            if scope.checker not in rule_checkers:
                raise ValueError(
                    f"证据范围 {scope.scope_id} 没有对应的规则结论标注"
                )
            if not set(scope.document_ids).issubset(document_ids):
                raise ValueError(f"证据范围 {scope.scope_id} 引用了未知文档")
            validate_phrase_presence(
                f"证据范围 {scope.scope_id}",
                scope.required_phrases,
                set(scope.document_ids),
                scope.expected_evidence,
            )
        for annotation in self.expert_annotation.retrieval_annotations:
            if not set(annotation.search_document_ids).issubset(document_ids):
                raise ValueError(
                    f"检索专项 {annotation.retrieval_id} 引用了未知搜索文档"
                )
            if not set(annotation.gold_document_ids).issubset(document_ids):
                raise ValueError(
                    f"检索专项 {annotation.retrieval_id} 引用了未知金标准文档"
                )
            if not set(annotation.gold_document_ids).issubset(
                annotation.search_document_ids
            ):
                raise ValueError(
                    f"检索专项 {annotation.retrieval_id} 的金标准文档必须在搜索范围内"
                )
            if annotation.expected_present:
                validate_phrase_presence(
                    f"检索专项 {annotation.retrieval_id} 金标准",
                    annotation.gold_phrases,
                    set(annotation.gold_document_ids),
                )
            search_document_ids = set(annotation.search_document_ids)
            validate_phrase_presence(
                f"检索专项 {annotation.retrieval_id} 可接受候选",
                annotation.acceptable_phrases,
                search_document_ids,
            )
            validate_phrase_presence(
                f"检索专项 {annotation.retrieval_id} 错误候选",
                annotation.error_candidate_phrases,
                search_document_ids,
            )
            for slice_item in annotation.slices:
                if not set(slice_item.document_ids).issubset(document_ids):
                    raise ValueError(
                        f"检索切片 {annotation.retrieval_id} 引用了未知文档"
                    )
                validate_phrase_presence(
                    f"检索切片 {annotation.retrieval_id}/{slice_item.slice_type}",
                    slice_item.phrases,
                    set(slice_item.document_ids),
                )
        for unknown in self.expert_annotation.unknown_reasons:
            if unknown.checker not in rule_checkers:
                raise ValueError(
                    f"UNKNOWN 原因 {unknown.checker} 没有对应的规则结论标注"
                )
            if not set(unknown.required_document_ids).issubset(document_ids):
                raise ValueError(f"UNKNOWN 原因 {unknown.checker} 引用了未知文档")
        for fact in self.expert_annotation.financial_facts:
            if not set(fact.document_ids).issubset(document_ids):
                raise ValueError(f"金额事实 {fact.fact_type} 引用了未知文档")
            validate_phrase_presence(
                f"金额事实 {fact.fact_type}",
                fact.evidence_phrases,
                set(fact.document_ids),
            )
        for calculation in self.expert_annotation.financial_calculations:
            if calculation.checker not in rule_checkers:
                raise ValueError(
                    f"金额计算 {calculation.checker} 没有对应的规则结论标注"
                )
        version = self.expert_annotation.version_changes
        if version.base_document_id not in document_ids:
            raise ValueError("版本比较 base_document_id 不在合同包中")
        if version.compare_document_id not in document_ids:
            raise ValueError("版本比较 compare_document_id 不在合同包中")
        if version.base_document_id == version.compare_document_id:
            raise ValueError("版本比较的基础文档和比较文档必须不同")
        for change in version.expected_changes:
            if change.base_contains:
                validate_phrase_presence(
                    "版本变化基准侧",
                    [change.base_contains],
                    {version.base_document_id},
                )
            if change.compare_contains:
                validate_phrase_presence(
                    "版本变化比较侧",
                    [change.compare_contains],
                    {version.compare_document_id},
                )
        for recommendation in self.expert_annotation.redline_recommendations:
            if recommendation.checker not in rule_checkers:
                raise ValueError(
                    f"红线建议 {recommendation.recommendation_id} 没有对应的规则结论标注"
                )
            if not set(recommendation.target_document_ids).issubset(document_ids):
                raise ValueError(
                    f"红线建议 {recommendation.recommendation_id} 引用了未知文档"
                )
            if (
                recommendation.evidence_scope_id
                and recommendation.evidence_scope_id not in evidence_scope_ids
            ):
                raise ValueError(
                    f"红线建议 {recommendation.recommendation_id} 引用了未知证据范围"
                )
        return self


class ExpertDataset(ExpertModel):
    """专家评测集的顶层契约。"""

    dataset_id: str = Field(min_length=1)
    schema_version: Literal["2.0"]
    dataset_kind: Literal["expert_contract_package"]
    language: Literal["zh-CN"]
    annotation_status: Literal["seed_requires_legal_signoff", "adjudicated"]
    annotation_note: str = Field(min_length=1)
    privacy_note: str = Field(min_length=1)
    required_package_sections: list[
        Literal[
            "main_contract",
            "supporting_documents",
            "version_or_amendment",
            "business_background",
            "enterprise_position",
            "expert_annotation",
        ]
    ] = Field(min_length=6, max_length=6)
    cases: list[ExpertCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dataset_identity(self) -> "ExpertDataset":
        """确保案例和合同包身份不会在评测中发生碰撞。"""

        required = {
            "main_contract",
            "supporting_documents",
            "version_or_amendment",
            "business_background",
            "enterprise_position",
            "expert_annotation",
        }
        if set(self.required_package_sections) != required:
            raise ValueError("required_package_sections 必须完整声明六个合同包区段")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("评测集内 case_id 必须唯一")
        package_ids = [case.package.package_id for case in self.cases]
        if len(package_ids) != len(set(package_ids)):
            raise ValueError("评测集内 package_id 必须唯一")
        return self


def load_expert_dataset(path: Path) -> ExpertDataset:
    """读取并严格校验专家评测集，不接受旧格式回退。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return ExpertDataset.model_validate(payload)
