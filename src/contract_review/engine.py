"""Rule execution with explicit applicability and unsupported-check handling."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from pydantic import Field

from .models import (
    AttachmentReference,
    ContractFact,
    Evidence,
    EvidenceType,
    Document,
    Finding,
    FindingStatus,
    ModelBase,
    ParsedDocument,
    Rule,
    RuleBundle,
    RiskLevel,
    SourceLocator,
)
from .parser import find_text_evidence
from .review import check_attachment_completeness

ENGINE_VERSION = "rule-engine-0.1.0"


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
        recommended_action=recommended_action,
    )


def _applicability(rule: Rule, contract_type: str | None) -> str:
    if not contract_type:
        return "unknown"
    spec = rule.applicability.get(contract_type)
    if spec is not None:
        return spec.applicability
    if contract_type in rule.applies_to:
        return "required"
    return "unknown"


def execute_rule_bundle(
    rule_bundle: RuleBundle,
    *,
    package_id: str,
    parsed_documents: Sequence[ParsedDocument],
    package_evidence: Evidence,
    contract_type: str | None = None,
    contract_type_fact: ContractFact | None = None,
    facts: Sequence[ContractFact] = (),
    retrieved_evidence_ids: Mapping[str, Sequence[str]] | None = None,
    attachment_references: Sequence[AttachmentReference] = (),
    documents: Sequence[Document] = (),
) -> RuleExecutionResult:
    """Evaluate every rule and emit UNKNOWN for checks not yet implemented.

    A missing implementation is a visible review queue item. It never becomes
    an implicit PASS and therefore cannot disappear from a report.
    """

    if package_evidence.package_id != package_id:
        raise ValueError("package evidence belongs to a different package")

    evidence: dict[str, Evidence] = {package_evidence.evidence_id: package_evidence}
    findings: list[Finding] = []
    all_parsed = all(
        parsed.document.parse_status == "parsed" for parsed in parsed_documents
    )
    contract_type_evidence_ids = contract_type_fact.evidence_ids if contract_type_fact else []
    facts_by_type: dict[str, list[ContractFact]] = {}
    for fact in facts:
        facts_by_type.setdefault(fact.fact_type, []).append(fact)

    for rule in rule_bundle.rules:
        rule_evidence = _rule_source_evidence(rule, rule_bundle.source_sha256)
        evidence[rule_evidence.evidence_id] = rule_evidence
        applicability = _applicability(rule, contract_type)

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

        if rule.check_method == "classification":
            if not contract_type or not contract_type_fact:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="合同类型尚未形成带原文证据的结构化事实。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="先确认合同类型，并保留对应条款证据。",
                        confidence=0.0,
                    )
                )
            elif rule.title == contract_type:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.PASS,
                        reason=f"已确认合同类型为“{contract_type}”。",
                        evidence_ids=[rule_evidence.evidence_id, *contract_type_evidence_ids],
                        confidence=contract_type_fact.confidence,
                        fact_ids=[contract_type_fact.fact_id],
                    )
                )
            else:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.NOT_APPLICABLE,
                        reason=f"当前合同类型为“{contract_type}”，不是本规则对应类型。",
                        evidence_ids=[rule_evidence.evidence_id, *contract_type_evidence_ids],
                        confidence=contract_type_fact.confidence,
                        fact_ids=[contract_type_fact.fact_id],
                    )
                )
            continue

        if rule.title == "技术协议" and attachment_references:
            missing_evidence, attachment_findings = check_attachment_completeness(
                rule,
                attachment_references,
                documents,
            )
            for item in missing_evidence:
                evidence[item.evidence_id] = item
            if attachment_findings:
                findings.extend(
                    finding.model_copy(
                        update={
                            "evidence_ids": [
                                rule_evidence.evidence_id,
                                *finding.evidence_ids,
                            ]
                        }
                    )
                    for finding in attachment_findings
                )
                continue

        if rule.check_method == "keyword":
            matches: list[Evidence] = []
            for parsed_document in parsed_documents:
                matches.extend(find_text_evidence(parsed_document, rule.title, evidence_prefix="keyword"))
            for item in matches:
                evidence[item.evidence_id] = item
            if not all_parsed:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="至少一个文档未完成文字解析，关键字未命中不能作为否定结论。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="先完成 OCR 或补充可检索版本，再复核关键字规则。",
                        confidence=0.0,
                    )
                )
            elif matches:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.WARN,
                        reason=f"在合同包文字层发现关键字“{rule.title}”，需要核对其业务含义和交付责任。",
                        evidence_ids=[rule_evidence.evidence_id, *[item.evidence_id for item in matches]],
                        recommended_action="人工核对命中条款，确认是否涉及源码、程序或相关交付义务。",
                        confidence=1.0,
                    )
                )
            else:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.PASS,
                        reason=f"已对合同包的可检索文字层执行关键字搜索，未发现“{rule.title}”。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        confidence=1.0,
                    )
                )
            continue

        if rule.check_method == "deterministic" and rule.title == "税率":
            rate_facts = facts_by_type.get("tax_rate", [])
            expected = rule.applicability.get(contract_type).expected_value if contract_type and rule.applicability.get(contract_type) else None
            if not rate_facts:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="合同包中没有提取到带原文证据的税率百分比。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="补充税率条款或由财税审核人确认适用税率。",
                        confidence=0.0,
                    )
                )
            elif not isinstance(expected, (int, float)):
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="当前合同类型的税率预期值为混合或非数值策略，不能自动比较。",
                        evidence_ids=[
                            rule_evidence.evidence_id,
                            package_evidence.evidence_id,
                            *[evidence_id for fact in rate_facts for evidence_id in fact.evidence_ids],
                        ],
                        fact_ids=[fact.fact_id for fact in rate_facts],
                        recommended_action="由财税审核人确认混合税率的分项和适用依据。",
                        confidence=0.0,
                    )
                )
            else:
                values = [float(fact.normalized_value) for fact in rate_facts]
                matches = all(abs(value - float(expected)) < 1e-9 for value in values)
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.PASS if matches else FindingStatus.WARN,
                        reason=(
                            f"提取税率与规则预期值 {float(expected):.4g} 一致。"
                            if matches
                            else f"提取税率 {values} 与规则预期值 {float(expected):.4g} 不一致。"
                        ),
                        evidence_ids=[
                            rule_evidence.evidence_id,
                            package_evidence.evidence_id,
                            *[evidence_id for fact in rate_facts for evidence_id in fact.evidence_ids],
                        ],
                        fact_ids=[fact.fact_id for fact in rate_facts],
                        comparison={"actual": values, "expected": float(expected)},
                        recommended_action=None if matches else "由财税审核人核对合同类型、税率和开票依据。",
                        confidence=min(
                            fact.confidence for fact in rate_facts if fact.confidence is not None
                        )
                        if any(fact.confidence is not None for fact in rate_facts)
                        else None,
                    )
                )
            continue

        if rule.check_method == "deterministic" and rule.title == "不含税":
            untaxed_matches = [
                item
                for parsed_document in parsed_documents
                for item in find_text_evidence(parsed_document, "不含税", evidence_prefix="untaxed")
            ]
            taxed_matches = [
                item
                for parsed_document in parsed_documents
                for item in find_text_evidence(parsed_document, "含税", evidence_prefix="taxed")
                if "不含税" not in (item.raw_excerpt or "")
            ]
            for item in [*untaxed_matches, *taxed_matches]:
                evidence[item.evidence_id] = item
            if untaxed_matches and not taxed_matches:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.PASS,
                        reason="合同包明确出现“不含税”表述。",
                        evidence_ids=[rule_evidence.evidence_id, *[item.evidence_id for item in untaxed_matches]],
                        confidence=1.0,
                    )
                )
            elif taxed_matches:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.WARN,
                        reason="合同包出现“含税”表述，需确认是否满足本规则对不含税金额的要求。",
                        evidence_ids=[rule_evidence.evidence_id, *[item.evidence_id for item in taxed_matches]],
                        recommended_action="人工核对含税/不含税口径及金额计算。",
                        confidence=1.0,
                    )
                )
            else:
                findings.append(
                    _finding(
                        rule,
                        status=FindingStatus.UNKNOWN,
                        reason="未发现“含税/不含税”表述，不能据此确认金额口径。",
                        evidence_ids=[rule_evidence.evidence_id, package_evidence.evidence_id],
                        recommended_action="补充金额口径或由财税审核人确认。",
                        confidence=0.0,
                    )
                )
            continue

        method_action = {
            "deterministic": "补充该确定性规则的事实字段和计算器后重跑。",
            "semantic": "提交语义模型或专业审核人判断，并保留条款证据。",
            "visual": "提交页面图像/视觉识别结果，并由人工核验印章或版式。",
            "human": "由法务、财税或技术审核人直接确认。",
        }.get(rule.check_method, "由审核人确认规则处理方式。")
        retrieval_evidence_ids = list((retrieved_evidence_ids or {}).get(rule.rule_id, ()))
        findings.append(
            _finding(
                rule,
                status=FindingStatus.UNKNOWN,
                reason=f"规则已纳入审查范围，但当前运行未配置“{rule.check_method}”检查器。",
                evidence_ids=[
                    rule_evidence.evidence_id,
                    package_evidence.evidence_id,
                    *retrieval_evidence_ids,
                ],
                recommended_action=method_action,
                confidence=0.0,
            )
        )

    return RuleExecutionResult(evidence=list(evidence.values()), findings=findings)
