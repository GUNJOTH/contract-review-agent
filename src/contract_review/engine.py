"""带有明确适用性和未实现检查处置的合同规则执行器。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from pydantic import Field

from .models import (
    AttachmentReference,
    CandidateEvidence,
    ContractClause,
    ContractFact,
    Evidence,
    EvidenceQuality,
    EvidenceType,
    Document,
    Finding,
    FindingStatus,
    KnowledgeSourceKind,
    ModelBase,
    ParsedDocument,
    Rule,
    RuleBundle,
    RiskLevel,
    SourceLocator,
    PlaybookAction,
    ReviewContext,
)
from .playbook import evaluate_playbook_rule
from .rule_checkers import RuleCheckContext, execute_configured_rule_checker
from .rules import (
    assert_rule_bundle_compatible,
    resolve_rule_applicability,
    select_rules,
)

ENGINE_VERSION = "rule-engine-0.3.0"


class RuleExecutionResult(ModelBase):
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


def _rule_source_evidence(rule: Rule, source_sha256: str) -> Evidence:
    if rule.source_locator is not None:
        locator = rule.source_locator
    else:
        locator = SourceLocator(
            locator_type="external_uri",
            external_uri=f"urn:contract-review:rule:{rule.rule_id}",
        )
    digest = hashlib.sha256(rule.rule_id.encode("utf-8")).hexdigest()[:16]
    return Evidence(
        evidence_id=f"rule-source-{digest}",
        evidence_type=EvidenceType.EXTERNAL_REFERENCE,
        source_sha256=source_sha256,
        locator=locator,
        raw_excerpt=f"{rule.category} / {rule.title}",
        display_excerpt=f"规则来源：{rule.category} / {rule.title}",
        extraction_method="rule_snapshot",
        extraction_version=ENGINE_VERSION,
        confidence=1.0,
    )


def _finding(
    rule: Rule,
    *,
    status: FindingStatus,
    reason: str,
    evidence_ids: Sequence[str],
    recommended_action: str | None = None,
    confidence: float | None = None,
    fact_ids: Sequence[str] = (),
    comparison: dict[str, object] | None = None,
    action: PlaybookAction | None = None,
    playbook_id: str | None = None,
    clause_ids: Sequence[str] = (),
    uncertainty_reason: str | None = None,
    evidence_quality: EvidenceQuality | None = None,
    automatic: bool | None = None,
) -> Finding:
    unique_evidence_ids = list(dict.fromkeys(evidence_ids))
    if not unique_evidence_ids:
        raise ValueError("a rule finding must retain at least one evidence id")
    return Finding(
        finding_id=f"finding-rule-{rule.rule_id}",
        rule_id=rule.rule_id,
        rule_version=rule.version,
        status=status,
        risk_level=rule.risk_level or RiskLevel.UNCLASSIFIED,
        title=rule.title,
        reason=reason,
        evidence_ids=unique_evidence_ids,
        fact_ids=list(fact_ids),
        comparison=comparison,
        confidence=confidence,
        evidence_quality=(
            EvidenceQuality.INSUFFICIENT
            if evidence_quality is None and status == FindingStatus.UNKNOWN
            else evidence_quality or EvidenceQuality.SUFFICIENT
        ),
        automatic=(
            status in {FindingStatus.PASS, FindingStatus.WARN, FindingStatus.BLOCK}
            if automatic is None
            else automatic
        ),
        recommended_action=recommended_action,
        action=action,
        playbook_id=playbook_id,
        clause_ids=list(clause_ids),
        uncertainty_reason=uncertainty_reason,
    )


def execute_rule_bundle(
    rule_bundle: RuleBundle,
    *,
    package_id: str,
    parsed_documents: Sequence[ParsedDocument],
    package_evidence: Evidence,
    contract_type_fact: ContractFact | None = None,
    facts: Sequence[ContractFact] = (),
    candidate_evidence_by_rule: Mapping[str, Sequence[CandidateEvidence]],
    attachment_references: Sequence[AttachmentReference] = (),
    documents: Sequence[Document] = (),
    visual_evidence: Sequence[Evidence] = (),
    clauses: Sequence[ContractClause] = (),
    clause_evidence: Sequence[Evidence] = (),
    known_evidence: Sequence[Evidence] = (),
    review_context: ReviewContext,
    selected_rule_ids: Sequence[str] | None = None,
) -> RuleExecutionResult:
    """执行所有规则，未实现的检查显式输出 UNKNOWN。

    缺少实现的规则必须进入可见复核队列，不能隐式变成 PASS 后从报告中消失。
    """

    if package_evidence.package_id != package_id:
        raise ValueError("package evidence belongs to a different package")
    # 领域引擎也要独立执行发布门禁，避免绕过应用服务直接注入草稿规则。
    assert_rule_bundle_compatible(rule_bundle)
    effective_context = review_context
    effective_contract_type = effective_context.contract_type
    selected_ids = (
        set(selected_rule_ids)
        if selected_rule_ids is not None
        else {rule.rule_id for rule in select_rules(rule_bundle, effective_context)}
    )
    known_rule_ids = {rule.rule_id for rule in rule_bundle.rules}
    unknown_selected_ids = selected_ids - known_rule_ids
    if unknown_selected_ids:
        raise ValueError(f"selected_rule_ids 包含未知规则: {sorted(unknown_selected_ids)}")
    expected_candidate_rule_ids = {
        rule.rule_id
        for rule in rule_bundle.rules
        if rule.rule_id in selected_ids
        and resolve_rule_applicability(rule, review_context=effective_context)
        != "not_applicable"
    }
    actual_candidate_rule_ids = set(candidate_evidence_by_rule)
    if actual_candidate_rule_ids != expected_candidate_rule_ids:
        raise ValueError(
            "CandidateEvidence 规则覆盖与适用规则不一致: "
            f"expected={sorted(expected_candidate_rule_ids)} "
            f"actual={sorted(actual_candidate_rule_ids)}"
        )
    if any(
        candidate.rule_id != rule_id
        for rule_id, candidates in candidate_evidence_by_rule.items()
        for candidate in candidates
    ):
        raise ValueError("CandidateEvidence 不能挂到其他规则的候选分组")

    evidence: dict[str, Evidence] = {
        package_evidence.evidence_id: package_evidence,
        **{item.evidence_id: item for item in known_evidence},
        **{item.evidence_id: item for item in clause_evidence},
    }
    findings: list[Finding] = []
    all_parsed = all(
        parsed.document.parse_status == "parsed" for parsed in parsed_documents
    )
    contract_type_evidence_ids = contract_type_fact.evidence_ids if contract_type_fact else []
    facts_by_type: dict[str, list[ContractFact]] = {}
    for fact in facts:
        facts_by_type.setdefault(fact.fact_type, []).append(fact)

    for rule in rule_bundle.rules:
        if rule.rule_id not in selected_ids:
            continue
        rule_evidence = _rule_source_evidence(rule, rule_bundle.source_sha256)
        evidence[rule_evidence.evidence_id] = rule_evidence
        rule_candidates = tuple(
            candidate_evidence_by_rule.get(rule.rule_id, ())
        )
        rule_candidate_evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for candidate in rule_candidates
                for evidence_id in candidate.evidence_ids
            )
        )
        rule_candidate_ids = {candidate.candidate_id for candidate in rule_candidates}
        rule_facts_by_type = {
                fact_type: [
                    fact
                    for fact in typed_facts
                    if bool(rule_candidate_ids.intersection(fact.candidate_ids))
                ]
            for fact_type, typed_facts in facts_by_type.items()
        }
        rule_attachment_references = tuple(
            reference
            for reference in attachment_references
            if rule_candidate_ids.intersection(reference.candidate_ids)
        )
        applicability = resolve_rule_applicability(
            rule,
            review_context=effective_context,
        )

        if applicability == "not_applicable":
            findings.append(
                _finding(
                    rule,
                    status=FindingStatus.NOT_APPLICABLE,
                    reason="规则快照明确标注该规则不适用于当前合同类型。",
                    evidence_ids=[rule_evidence.evidence_id],
                    confidence=1.0,
                )
            )
            continue

        if applicability in {"unknown", "unspecified"}:
            findings.append(
                _finding(
                    rule,
                    status=FindingStatus.UNKNOWN,
                    reason="当前合同类型未能从来源规则中确定本规则的适用性。",
                    evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                    recommended_action="补充合同类型事实及其原文证据，或由审核人配置规则适用性。",
                    confidence=0.0,
                )
            )
            continue

        playbook_evaluation = evaluate_playbook_rule(
            rule,
            clauses,
            candidate_evidence=rule_candidates,
            facts=[
                fact
                for typed_facts in rule_facts_by_type.values()
                for fact in typed_facts
            ],
            review_context=effective_context,
            default_evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
        )
        if playbook_evaluation is not None:
            findings.append(
                _finding(
                    rule,
                    status=playbook_evaluation.status,
                    reason=playbook_evaluation.reason,
                    evidence_ids=playbook_evaluation.evidence_ids,
                    recommended_action=(
                        str(
                            (playbook_evaluation.comparison or {}).get(
                                "suggested_language"
                            )
                            or ""
                        ).strip()
                        or (
                            playbook_evaluation.action.value
                            if playbook_evaluation.action is not None
                            else None
                        )
                    ),
                    confidence=playbook_evaluation.confidence,
                    comparison=playbook_evaluation.comparison,
                    action=playbook_evaluation.action,
                    playbook_id=(
                        rule.playbook.playbook_id if rule.playbook is not None else None
                    ),
                    clause_ids=playbook_evaluation.clause_ids,
                    uncertainty_reason=playbook_evaluation.uncertainty_reason,
                    evidence_quality=(
                        EvidenceQuality.INSUFFICIENT
                        if playbook_evaluation.status == FindingStatus.UNKNOWN
                        else EvidenceQuality.SUFFICIENT
                    ),
                    automatic=playbook_evaluation.status
                    in {
                        FindingStatus.PASS,
                        FindingStatus.WARN,
                        FindingStatus.BLOCK,
                    },
                )
            )
            continue

        if rule.check_method == "classification":
            if not effective_contract_type or not contract_type_fact:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="合同类型尚未形成带原文证据的结构化事实。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="先确认合同类型，并保留对应条款证据。",
                        confidence=0.0,
                        evidence_quality=EvidenceQuality.INSUFFICIENT,
                        automatic=False,
                    )
                )
            elif applicability in {"required", "expected_value"}:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.PASS,
                        reason=f"已确认合同类型为“{effective_contract_type}”。",
                        evidence_ids=[rule_evidence.evidence_id, *contract_type_evidence_ids],
                        confidence=contract_type_fact.confidence,
                        fact_ids=[contract_type_fact.fact_id],
                        evidence_quality=EvidenceQuality.SUFFICIENT,
                        automatic=True,
                    )
                )
            else:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.NOT_APPLICABLE,
                        reason=f"当前合同类型为“{effective_contract_type}”，不是本规则对应类型。",
                        evidence_ids=[rule_evidence.evidence_id, *contract_type_evidence_ids],
                        confidence=contract_type_fact.confidence,
                        fact_ids=[contract_type_fact.fact_id],
                        evidence_quality=EvidenceQuality.SUFFICIENT,
                        automatic=True,
                    )
                )
            continue

        checker_result = execute_configured_rule_checker(
            rule,
            RuleCheckContext(
                rule=rule,
                rule_evidence=rule_evidence,
                package_evidence=package_evidence,
                facts_by_type=rule_facts_by_type,
                attachment_references=rule_attachment_references,
                documents=documents,
                visual_evidence=visual_evidence,
                clauses=clauses,
                effective_context=effective_context,
                all_parsed=all_parsed,
                evidence_by_id=evidence,
                candidate_evidence=rule_candidates,
            ),
        )
        if checker_result is not None:
            for item in checker_result.evidence:
                evidence[item.evidence_id] = item
            findings.append(
                _finding(
                    rule,
                    status=checker_result.status,
                    reason=checker_result.reason,
                    evidence_ids=checker_result.evidence_ids,
                    recommended_action=checker_result.recommended_action,
                    confidence=checker_result.confidence,
                    fact_ids=checker_result.fact_ids,
                    comparison=checker_result.comparison,
                    action=checker_result.action,
                    clause_ids=checker_result.clause_ids,
                    uncertainty_reason=checker_result.uncertainty_reason,
                    evidence_quality=checker_result.evidence_quality,
                    automatic=checker_result.automatic,
                )
            )
            continue

        if rule.check_method == "keyword":
            candidates = list(
                candidate_evidence_by_rule.get(rule.rule_id, ())
            )
            matched_evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for candidate in candidates
                    if candidate.source_kind == KnowledgeSourceKind.CONTRACT
                    and rule.title in candidate.content
                    for evidence_id in candidate.evidence_ids
                )
            )
            if not all_parsed:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="至少一个文档未完成文字解析，关键字未命中不能作为否定结论。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="先完成 OCR 或补充可检索版本，再复核关键字规则。",
                        confidence=0.0,
                        evidence_quality=EvidenceQuality.INSUFFICIENT,
                        automatic=False,
                    )
                )
            elif matched_evidence_ids:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.WARN,
                        reason=f"在合同包文字层发现关键字“{rule.title}”，需要核对其业务含义和交付责任。",
                        evidence_ids=[rule_evidence.evidence_id, *matched_evidence_ids],
                        recommended_action="人工核对命中条款，确认是否涉及源码、程序或相关交付义务。",
                        confidence=1.0,
                        evidence_quality=EvidenceQuality.SUFFICIENT,
                        automatic=True,
                    )
                )
            else:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason=(
                            f"检索候选未命中关键字“{rule.title}”，候选召回不能证明全文不存在该表达。"
                        ),
                        evidence_ids=[
                            rule_evidence.evidence_id,
                            package_evidence.evidence_id,
                            *rule_candidate_evidence_ids,
                        ],
                        recommended_action="补充更高召回的检索结果或由审核人核对全文，再确认关键字规则。",
                        confidence=0.0,
                        evidence_quality=EvidenceQuality.INSUFFICIENT,
                        automatic=False,
                    )
                )
            continue

        method_action = {
            "deterministic": "补充该确定性规则的事实字段和计算器后重跑。",
            "semantic": "提交语义模型或专业审核人判断，并保留条款证据。",
            "visual": "提交页面图像/视觉识别结果，并由人工核验印章或版式。",
            "human": "由法务、财税或技术审核人直接确认。",
        }.get(rule.check_method, "由审核人确认规则处理方式。")
        visual_evidence_ids = [
            item.evidence_id
            for item in visual_evidence
            if item.evidence_type == EvidenceType.VISUAL_REGION
        ]
        if rule.check_method == "visual" and visual_evidence_ids:
            findings.append(
                _finding(
                    rule,
                    status=FindingStatus.UNKNOWN,
                    reason=(
                        f"检测到 {len(visual_evidence_ids)} 项印章/视觉证据，"
                        "需人工核验是否符合规则要求（如骑缝章覆盖范围）。"
                    ),
                    evidence_ids=[
                        rule_evidence.evidence_id,
                        package_evidence.evidence_id,
                        *rule_candidate_evidence_ids,
                        *visual_evidence_ids,
                    ],
                    recommended_action="人工核验页面图像中的印章位置和覆盖范围。",
                    confidence=0.0,
                )
            )
            continue
        findings.append(
            _finding(
                rule,
                status=FindingStatus.UNKNOWN,
                reason=f"规则已纳入审查范围，但当前运行未配置“{rule.check_method}”检查器。",
                evidence_ids=[
                    rule_evidence.evidence_id,
                    package_evidence.evidence_id,
                    *rule_candidate_evidence_ids,
                ],
                recommended_action=method_action,
                confidence=0.0,
                evidence_quality=EvidenceQuality.INSUFFICIENT,
                automatic=False,
            )
        )

    return RuleExecutionResult(evidence=list(evidence.values()), findings=findings)
