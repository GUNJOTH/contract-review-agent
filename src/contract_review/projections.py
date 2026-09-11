"""核心审查结果的应用兼容投影。

投影只读 ``ReviewResult``，不执行解析、规则、模型或数据库操作。旧客户端
仍需要的风险项、要素和规则列表形状在这里生成，避免应用层重新维护第二套
业务对象；新的调用方应直接使用 ``ReviewResult``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import (
    EvidenceType,
    ModelBase,
    ReviewResult,
    Rule,
    RuleBundle,
)


REVIEW_PROJECTION_VERSION = "review-result-projection-0.1.0"
REVIEW_MODULES = ("风险点", "合理性", "内控", "资信")


class ReviewItemProjection(ModelBase):
    """旧风险清单形状的只读投影。"""

    risk_id: str
    title: str
    risk_level: str
    reason: str
    quote: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_action: str | None = None
    source: Literal["ai", "rule", "merged"] = "rule"
    module: str = "内控"
    category: str | None = None
    metric: str | None = None
    value: str | None = None
    section: str | None = None


class ReviewAnalysisProjection(ModelBase):
    """历史 ``ai_analysis`` 字段的兼容投影，不代表第二次模型调用。"""

    projection_version: str = REVIEW_PROJECTION_VERSION
    analysis_id: str
    provider: str
    model_version: str
    prompt_version: str
    status: Literal["completed", "blocked"] = "completed"
    blocked_reason: str | None = None
    blocked_pii_types: list[str] = Field(default_factory=list)
    contract_type: dict[str, str] | None = None
    items: list[ReviewItemProjection] = Field(default_factory=list)
    panels: dict[str, list[ReviewItemProjection]] = Field(default_factory=dict)


class ElementCandidateProjection(ModelBase):
    """历史要素候选项的只读投影。"""

    value: str
    quote: str | None = None
    source: Literal["rule", "ai"] = "rule"


class ContractElementProjection(ModelBase):
    """从 ``ReviewResult.facts`` 投影出的合同要素。"""

    key: str
    label: str
    value: str = ""
    quote: str | None = None
    source: Literal["rule", "ai", "merged", "empty"] = "empty"
    confidence: float = 0.0
    candidates: list[ElementCandidateProjection] = Field(default_factory=list)


class ElementExtractionProjection(ModelBase):
    """历史 ``ElementExtractionResult`` 形状的只读投影。"""

    projection_version: str = REVIEW_PROJECTION_VERSION
    extraction_id: str
    package_id: str
    prompt_version: str
    documents: list[str] = Field(default_factory=list)
    fields: list[ContractElementProjection] = Field(default_factory=list)
    fillable: dict[str, str] = Field(default_factory=dict)
    suggestions: dict[str, list[str]] = Field(default_factory=dict)
    external_model_blocked: bool = False
    external_model_block_reason: str | None = None
    external_model_pii_types: list[str] = Field(default_factory=list)


class RuleProjection(ModelBase):
    """正式规则的历史列表投影。"""

    id: str
    rule_id: str
    code: str
    title: str
    condition: str
    category: str
    topic: str
    module: str
    check_method: str
    risk_level: str
    applies_to: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    human_review: bool = False
    source_snapshot: str
    playbook: dict[str, object] | None = None
    status: Literal["active"] = "active"
    enabled: bool = True
    read_only: bool = True


class RuleGroupProjection(ModelBase):
    """按历史规则主题分组的只读投影。"""

    name: str
    count: int
    rules: list[RuleProjection] = Field(default_factory=list)


class RulePackProjection(ModelBase):
    """兼容历史规则包分栏的只读投影。"""

    rules: list[RuleProjection] = Field(default_factory=list)
    groups: list[RuleGroupProjection] = Field(default_factory=list)


class RulePacksProjection(ModelBase):
    """兼容历史 approval/ai 分包结构。"""

    approval: RulePackProjection
    ai: RulePackProjection


class RuleBundleProjection(ModelBase):
    """正式规则包及其历史接口形状的明确契约。"""

    projection_version: str = REVIEW_PROJECTION_VERSION
    read_only: bool = True
    bundle: RuleBundle
    bundle_id: str
    source_filename: str
    source_sha256: str
    modules: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    rules: list[RuleProjection] = Field(default_factory=list)
    groups: list[RuleGroupProjection] = Field(default_factory=list)
    packs: RulePacksProjection


def project_review_analysis(result: ReviewResult) -> ReviewAnalysisProjection:
    """把核心发现投影为历史风险清单，绝不触发外部副作用。"""

    rules_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    facts_by_id = {item.fact_id: item for item in result.facts}
    clauses_by_id = {item.clause_id: item for item in result.clauses}
    semantic_rule_ids = {
        item.rule_id
        for item in (result.semantic_response.items if result.semantic_response else [])
    }
    items: list[ReviewItemProjection] = []
    for finding in result.findings:
        if finding.status.value in {"PASS", "NOT_APPLICABLE"}:
            continue
        rule = rules_by_id.get(finding.rule_id)
        quote = _finding_quote(finding.evidence_ids, evidence_by_id)
        clause_number = next(
            (
                clauses_by_id[clause_id].clause_number
                for clause_id in finding.clause_ids
                if clause_id in clauses_by_id and clauses_by_id[clause_id].clause_number
            ),
            None,
        )
        category = rule.category if rule is not None else None
        items.append(
            ReviewItemProjection(
                risk_id=finding.finding_id,
                title=finding.title,
                risk_level=finding.status.value,
                reason=finding.reason,
                quote=quote,
                evidence_ids=list(finding.evidence_ids),
                suggested_action=finding.recommended_action,
                source="ai" if finding.rule_id in semantic_rule_ids else "rule",
                module=_module_for_rule(rule, finding.title, finding.reason),
                category=category,
                metric=_metric_for_finding(finding.title, finding.reason),
                value=_value_for_finding(finding.fact_ids, facts_by_id),
                section=clause_number,
            )
        )
    items.sort(key=lambda item: -_severity(item.risk_level))
    panels = {name: [] for name in REVIEW_MODULES}
    for item in items:
        panels.setdefault(item.module, []).append(item)
    gate = _pii_gate_configuration(result)
    contract_type = _contract_type_projection(result)
    return ReviewAnalysisProjection(
        analysis_id=f"review-{result.run.run_id}",
        provider=(
            result.semantic_response.provider
            if result.semantic_response is not None
            else "deterministic-rule-engine"
        ),
        model_version=(
            result.semantic_response.model_version
            if result.semantic_response is not None
            else result.run.model_version or ""
        ),
        prompt_version=(
            result.semantic_response.prompt_version
            if result.semantic_response is not None
            else REVIEW_PROJECTION_VERSION
        ),
        status="blocked" if gate.get("decision") == "block" else "completed",
        blocked_reason=gate.get("reason") if gate.get("decision") == "block" else None,
        blocked_pii_types=(
            list(gate.get("finding_types") or [])
            if gate.get("decision") == "block"
            else []
        ),
        contract_type=contract_type,
        items=items,
        panels=panels,
    )


def project_element_extraction(result: ReviewResult) -> ElementExtractionProjection:
    """把核心合同事实投影为历史要素抽取响应。"""

    facts_by_key: dict[str, list] = {}
    for fact in result.facts:
        prefix, separator, key = fact.fact_type.partition(":")
        if prefix != "contract_element" or not separator or not key:
            continue
        facts_by_key.setdefault(key, []).append(fact)
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    fields: list[ContractElementProjection] = []
    # 目录定义由 core.elements 提供；延迟导入避免元素目录和投影互相依赖。
    from .elements import list_contract_element_definitions

    for definition in list_contract_element_definitions():
        facts = facts_by_key.get(str(definition["key"]), [])
        candidates = [
            ElementCandidateProjection(
                value=str(fact.value),
                quote=_finding_quote(fact.evidence_ids, evidence_by_id),
                source="rule",
            )
            for fact in facts[:5]
        ]
        first = candidates[0] if candidates else None
        fields.append(
            ContractElementProjection(
                key=str(definition["key"]),
                label=str(definition["label"]),
                value=first.value if first else "",
                quote=first.quote if first else None,
                source="rule" if first else "empty",
                confidence=max(
                    (float(fact.confidence or 0.0) for fact in facts),
                    default=0.0,
                ),
                candidates=candidates,
            )
        )
    fillable = {item.key: item.value for item in fields if item.value}
    suggestions = {
        item.key: [candidate.value for candidate in item.candidates]
        for item in fields
        if item.candidates
    }
    gate = _pii_gate_configuration(result)
    blocked = gate.get("decision") == "block"
    return ElementExtractionProjection(
        extraction_id=f"review-elements-{result.run.run_id}",
        package_id=result.package.package_id,
        prompt_version=REVIEW_PROJECTION_VERSION,
        documents=[item.filename for item in result.documents],
        fields=fields,
        fillable=fillable,
        suggestions=suggestions,
        external_model_blocked=blocked,
        external_model_block_reason=gate.get("reason") if blocked else None,
        external_model_pii_types=list(gate.get("finding_types") or []) if blocked else [],
    )


def project_rule_bundle(bundle: RuleBundle) -> RuleBundleProjection:
    """把正式 ``RuleBundle`` 投影为旧规则列表的只读形状。"""

    rules = [_project_rule(rule) for rule in bundle.rules]
    topics: list[str] = []
    for rule in rules:
        topic = rule.topic
        if topic not in topics:
            topics.append(topic)
    groups = _group_rules(rules, topics)
    return RuleBundleProjection(
        bundle=bundle,
        bundle_id=bundle.bundle_id,
        source_filename=bundle.source_filename,
        source_sha256=bundle.source_sha256,
        modules=list(REVIEW_MODULES),
        topics=topics,
        rules=rules,
        groups=groups,
        packs=RulePacksProjection(
            approval=RulePackProjection(rules=rules, groups=groups),
            ai=RulePackProjection(),
        ),
    )


def _group_rules(
    rules: list[RuleProjection], topics: list[str]
) -> list[RuleGroupProjection]:
    return [
        RuleGroupProjection(
            name=topic,
            count=sum(1 for rule in rules if rule.topic == topic),
            rules=[rule for rule in rules if rule.topic == topic],
        )
        for topic in topics
    ]


def _project_rule(rule: Rule) -> RuleProjection:
    risk_level = rule.risk_level.value.upper() if rule.risk_level else "UNKNOWN"
    return RuleProjection(
        id=rule.rule_id,
        rule_id=rule.rule_id,
        code=rule.rule_id,
        title=rule.title,
        condition=rule.condition or "",
        category=rule.category,
        topic=rule.category,
        module=_module_for_rule(rule, rule.title, rule.condition or ""),
        check_method=rule.check_method,
        risk_level=risk_level,
        applies_to=list(rule.applies_to),
        required_evidence=list(rule.required_evidence),
        human_review=rule.human_review,
        source_snapshot=rule.source_snapshot,
        playbook=rule.playbook.model_dump(mode="json") if rule.playbook else None,
    )


def _module_for_rule(rule: Rule | None, title: str, reason: str) -> str:
    category = rule.category if rule is not None else ""
    if category == "合同类型" or "合同类型" in title:
        return "风险点"
    if category.startswith("Qx") or any(
        key in f"{title}{reason}"
        for key in ("合理性", "信创", "国产化", "云架构", "数据治理")
    ):
        return "合理性"
    if category in {"资信", "客户资信风险"} or any(
        key in f"{title}{reason}" for key in ("资信", "征信", "注册资本", "诉讼")
    ):
        return "资信"
    return "内控"


def _metric_for_finding(title: str, reason: str) -> str | None:
    text = f"{title} {reason}"
    mapping = (
        ("利润率", ("利润",)),
        ("资金要求", ("资金",)),
        ("项目预算", ("预算",)),
        ("收款进度", ("收款", "进度")),
        ("履行期限", ("期限", "工期", "签订时间")),
        ("权属范围", ("权属", "知识产权", "源代码")),
        ("注册资本", ("注册资本",)),
        ("合作历史", ("合作历史",)),
        ("当前合同", ("当前合同",)),
        ("经营状况", ("经营状况", "经营")),
    )
    for metric, keywords in mapping:
        if any(keyword in text for keyword in keywords):
            return metric
    return None


def _value_for_finding(fact_ids: list[str], facts_by_id: dict[str, object]) -> str | None:
    for fact_id in fact_ids:
        fact = facts_by_id.get(fact_id)
        if fact is not None and fact.value not in (None, ""):
            return str(fact.value)
    return None


def _finding_quote(evidence_ids: list[str], evidence_by_id: dict[str, object]) -> str | None:
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None or evidence.evidence_type == EvidenceType.MISSING_ARTIFACT:
            continue
        excerpt = evidence.display_excerpt or evidence.raw_excerpt
        if excerpt:
            return excerpt[:100]
    return None


def _contract_type_projection(result: ReviewResult) -> dict[str, str] | None:
    if result.review_context is not None and result.review_context.contract_type:
        return {"name": result.review_context.contract_type, "basis": "审查上下文"}
    for fact in result.facts:
        if fact.fact_type == "contract_type" and fact.value:
            return {"name": str(fact.value), "basis": "核心合同事实"}
    return None


def _pii_gate_configuration(result: ReviewResult) -> dict[str, object]:
    value = result.run.configuration.get("external_model_pii_gate")
    return value if isinstance(value, dict) else {}


def _severity(value: str) -> int:
    return {"BLOCK": 5, "WARN": 4, "INFO": 3, "UNKNOWN": 2}.get(value, 0)
