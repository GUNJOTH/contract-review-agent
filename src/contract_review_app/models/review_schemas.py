"""合同审查 API 的输入输出 DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from contract_review.models import ContractRevisionSet, DecisionType, ReviewResult


class RiskPanelsResponse(BaseModel):
    """v1 四栏风险视图的只读投影。

    ``panels`` 的键固定为 风险点/合理性/内控/资信；条目来自该次审查的
    findings（规则/语义判据）与通读风险分析 items（AI 补充判据），
    ``verdicts`` 按 v1 的三分类口径计数。
    """

    package_id: str
    panels: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    verdicts: dict[str, int] = Field(default_factory=dict)
    risk_analysis: dict[str, Any] = Field(default_factory=dict)


class RuleWriteRequest(BaseModel):
    """规则引擎库新增/编辑载荷：字段结构对齐域层 ``Rule``。"""

    payload: dict[str, Any]


class RuleWriteResponse(BaseModel):
    """规则写入结果摘要（保存即生效，无需发布步骤）。"""

    status: str
    rule_id: str


class RulesEngineViewResponse(BaseModel):
    """v1 ``/ai-rules`` 同构的规则引擎库视图。

    ``packs.approval`` = 合同检查标准（当前规则全集 + 启停状态），
    ``packs.ai`` = AI 自进化规则（模型提炼的候选检查点，确认后生效）；
    ``groups`` 按检查维度分组，直接供评分矩阵渲染。
    """

    topics: list[str] = Field(default_factory=list)
    packs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    editable: bool = True


class ContractReviewResponse(BaseModel):
    """同步合同审查用例的稳定响应契约。"""

    review_result: ReviewResult
    cached: bool = False


class ReviewResultResponse(BaseModel):
    """只返回核心 ReviewResult 的 HTTP 响应契约。"""

    review_result: ReviewResult


class ContractRevisionSetResponse(BaseModel):
    """从审查结果生成的修订提案响应契约。"""

    revision_set: ContractRevisionSet
    review_result: ReviewResult


class ReviewDecisionRequest(BaseModel):
    """记录一条人工决定所需的完整核心结果和定位信息。"""

    review_result: ReviewResult
    finding_id: str = Field(min_length=1)
    decision: DecisionType
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    evidence_ids: list[str] | None = None


class ReviewFinalizationRequest(BaseModel):
    """完成审查运行所需的人工确认信息。"""

    review_result: ReviewResult
    actor_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)


class ReviewResultRequest(BaseModel):
    """只需要客户端提交完整核心结果即可满足的后置只读请求。"""

    review_result: ReviewResult


class ContractElementFieldDefinitionResponse(BaseModel):
    """要素字段目录中的一条定义（只读口径，不含任何取值）。"""

    key: str
    label: str
    hint: str = ""
    aliases: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    required: bool = False
    enabled: bool = True
    sort_order: int = 0


class ContractElementFieldCatalogResponse(BaseModel):
    """标准要素字段目录快照。

    这是抽取口径本身，不是抽取结果：它决定"能抽哪些字段"，但不包含任何从
    合同里抽到的值。取值只能通过 ``/contract-review/element-form`` 从某次
    已完成审查的事实投影得到。
    """

    catalog_id: str
    schema_version: str
    extractor_version: str
    fingerprint: str
    editable: bool = False
    fields: list[ContractElementFieldDefinitionResponse] = Field(default_factory=list)


class CreditRiskCheckResponse(BaseModel):
    """客商风险的一项核验点。

    ``status`` 取 ``RISK`` / ``CLEAR`` / ``UNKNOWN`` / ``UNAVAILABLE``：前三个是
    已核验结论，``UNAVAILABLE`` 表示**需要外部企业征信数据源才能得出结论**。
    没有数据源时必须显式返回 ``UNAVAILABLE``，不能用 ``CLEAR`` 假装"核过了没问题"。
    """

    code: str
    label: str
    status: str
    detail: str = ""


class CreditRiskSubjectResponse(BaseModel):
    """一个合同主体（甲方/乙方）。"""

    role: str
    key: str
    value: str = ""
    source: str = "empty"
    confidence: float | None = None
    fact_ids: list[str] = Field(default_factory=list)


class CreditRiskFindingResponse(BaseModel):
    """审查结果里与合同主体相关的发现。"""

    finding_id: str
    rule_id: str
    title: str
    status: str
    risk_level: str | None = None
    reason: str = ""
    recommended_action: str | None = None
    evidence_count: int = 0


class CreditRiskViewResponse(BaseModel):
    """客商风险视图（从某次已完成审查投影，不重新解析合同）。"""

    data_source_connected: bool = False
    data_source_provider: str = "not_configured"
    data_source_message: str = ""
    subjects: list[CreditRiskSubjectResponse] = Field(default_factory=list)
    checks: list[CreditRiskCheckResponse] = Field(default_factory=list)
    subject_findings: list[CreditRiskFindingResponse] = Field(default_factory=list)
    summary: str = ""


class ContractElementFieldWriteRequest(BaseModel):
    """新增或修改一个要素字段定义。

    所有属性都可选：新增时只强制 ``label``（``key`` 留空则按名称自动生成），
    修改时只传需要变的属性即可，未传的保持原值。
    """

    label: str | None = None
    key: str | None = None
    hint: str | None = None
    aliases: list[str] | None = None
    patterns: list[str] | None = None
    required: bool | None = None
    enabled: bool | None = None
    sort_order: int | None = None

    def writable_values(self) -> dict[str, object]:
        """只返回显式给出的属性，避免用默认值覆盖目录里的既有配置。"""

        return {
            name: value
            for name, value in self.model_dump(exclude_unset=True).items()
            if value is not None
        }


class ContractElementFormFieldResponse(BaseModel):
    """要素回填表单中的一个字段。

    取值口径与 v1 的 ``ContractElement`` 同名同义，便于前端沿用同一套渲染
    分支：``source`` 取 ``rule`` / ``ai`` / ``merged`` / ``empty``，
    ``candidates`` 保留同一字段的其他候选值。
    """

    key: str
    label: str
    value: str = ""
    source: str = "empty"
    required: bool = False
    hint: str = ""
    confidence: float | None = None
    quote: str | None = None
    candidates: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class ContractElementFormResponse(BaseModel):
    """要素回填表单的响应契约。

    ``fillable`` / ``suggestions`` 沿用 v1 的字段结构；表单是核心审查结果的
    只读投影，重新解析合同不会产生第二套要素结论。
    """

    form_version: str
    package_id: str
    document_filenames: list[str] = Field(default_factory=list)
    catalog_id: str
    catalog_fingerprint: str
    extractor_version: str
    fields: list[ContractElementFormFieldResponse] = Field(default_factory=list)
    fillable: dict[str, str] = Field(default_factory=dict)
    suggestions: dict[str, list[str]] = Field(default_factory=dict)
    missing_required: list[str] = Field(default_factory=list)
