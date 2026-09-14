"""合同规则检查器。

本模块只负责把 ``RuleCheckContext`` 中已经形成的事实、原文证据和条款
结构转换成一个规则结论。规则执行器只负责适用性、Playbook 和结果编排，
不再按规则标题散落业务分支。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections.abc import Mapping, Sequence
from typing import Callable

from .models import (
    AttachmentReference,
    CandidateEvidence,
    ContractClause,
    ContractFact,
    Document,
    Evidence,
    EvidenceType,
    EvidenceQuality,
    FindingStatus,
    KnowledgeSourceKind,
    MissingClausePolicy,
    PlaybookAction,
    Rule,
    ReviewContext,
    SourceLocator,
)


RULE_CHECKER_VERSION = "rule-checkers-0.3.0"
MONEY_SCALE = Decimal("0.01")

_FULL_PAYMENT_PATTERN = re.compile(
    r"(?:"
    r"(?:100\s*%|百分之百)\s*(?:预付|预付款|支付|付款)"
    r"|(?:预付|预付款|支付|付款)\s*(?:100\s*%|百分之百)"
    r"|一次性\s*全额\s*(?:预付|预付款|支付|付款)"
    r")"
)
_PAYMENT_ACCEPTANCE_BINDING_PATTERN = re.compile(
    r"(?:"
    r"验收(?:合格|通过|完成|结果|节点)?[\s,，]*"
    r"(?:之后|以后|后|时)?[\s,，]*(?:方可|才可|即可|才能|再)?"
    r"(?:支付|付款|结算)"
    r"|(?:支付|付款|结算)(?:应|须|需|必须)?[\s,，]*"
    r"(?:以|按|依据|根据|在|于)[\s,，]*.{0,12}验收"
    r"|以[\s,，]*.{0,8}验收.{0,8}为.{0,8}(?:支付|付款|结算)"
    r"|验收.{0,12}(?:作为|为|决定).{0,8}(?:支付|付款|结算)"
    r"|(?:支付|付款|结算).{0,8}(?:绑定|挂钩|关联).{0,12}验收"
    r"|(?:支付|付款|结算).{0,8}(?:与|和).{0,8}验收"
    r".{0,8}(?:绑定|挂钩|关联|相关)"
    r"|验收.{0,8}(?:与|和).{0,8}(?:支付|付款|结算)"
    r".{0,8}(?:绑定|挂钩|关联|相关)"
    r")"
)


def _normalized_attachment_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def _document_matches(
    reference: AttachmentReference,
    document: Document,
) -> bool:
    document_name = _normalized_attachment_name(document.filename)
    candidates = [reference.referenced_name, *reference.aliases]
    return any(
        normalized_candidate
        and normalized_candidate in document_name
        for candidate in candidates
        for normalized_candidate in [_normalized_attachment_name(candidate)]
    )


@dataclass(frozen=True)
class RuleCheckContext:
    """单条规则检查所需的只读领域输入。"""

    rule: Rule
    rule_evidence: Evidence
    package_evidence: Evidence
    facts_by_type: Mapping[str, Sequence[ContractFact]]
    attachment_references: Sequence[AttachmentReference]
    documents: Sequence[Document]
    visual_evidence: Sequence[Evidence]
    clauses: Sequence[ContractClause]
    effective_context: ReviewContext
    all_parsed: bool
    candidate_evidence: Sequence[CandidateEvidence]
    evidence_by_id: Mapping[str, Evidence] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleCheckResult:
    """检查器返回的单一规则结论和新增证据。"""

    status: FindingStatus
    reason: str
    evidence_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...] = ()
    recommended_action: str | None = None
    confidence: float | None = None
    fact_ids: tuple[str, ...] = ()
    comparison: dict[str, object] | None = None
    action: PlaybookAction | None = None
    clause_ids: tuple[str, ...] = ()
    uncertainty_reason: str | None = None
    evidence_quality: EvidenceQuality = EvidenceQuality.SUFFICIENT
    automatic: bool = False


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _fact_values(
    facts: Sequence[ContractFact],
) -> list[tuple[ContractFact, Decimal]]:
    values: list[tuple[ContractFact, Decimal]] = []
    for fact in facts:
        raw = fact.normalized_value if fact.normalized_value is not None else fact.value
        try:
            value = Decimal(str(raw).replace(",", "").replace("，", "").strip())
        except (InvalidOperation, ValueError):
            continue
        values.append((fact, value))
    return values


def _fact_confidence(facts: Sequence[ContractFact]) -> float | None:
    values = [fact.confidence for fact in facts if fact.confidence is not None]
    return min(values) if values else None


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP), "f")


def _fact_evidence_ids(facts: Sequence[ContractFact]) -> list[str]:
    return [evidence_id for fact in facts for evidence_id in fact.evidence_ids]


def _fact_clause_ids(
    context: RuleCheckContext,
    facts: Sequence[ContractFact],
) -> tuple[str, ...]:
    """从事实证据反查其所属条款，保证规则结果可生成条款级修订。"""

    fact_evidence_ids = set(_fact_evidence_ids(facts))
    if not fact_evidence_ids:
        return ()
    clause_ids: list[str] = []
    for clause in context.clauses:
        if fact_evidence_ids.intersection(clause.evidence_ids):
            clause_ids.append(clause.clause_id)
            continue
        for evidence_id in fact_evidence_ids:
            evidence = context.evidence_by_id.get(evidence_id)
            if evidence is None or evidence.document_id != clause.document_id:
                continue
            block_id = evidence.locator.block_id
            block_matches = bool(
                block_id
                and any(
                    chunk_id == block_id
                    or chunk_id.endswith(f"-{block_id}")
                    for chunk_id in clause.source_chunk_ids
                )
            )
            excerpt_matches = bool(
                evidence.raw_excerpt
                and evidence.raw_excerpt.strip() in clause.text
            )
            if block_matches or excerpt_matches:
                clause_ids.append(clause.clause_id)
                break
    return _unique(clause_ids)


def _base_result(
    context: RuleCheckContext,
    *,
    status: FindingStatus,
    reason: str,
    facts: Sequence[ContractFact] = (),
    extra_evidence_ids: Sequence[str] = (),
    evidence: Sequence[Evidence] = (),
    recommended_action: str | None = None,
    confidence: float | None = None,
    comparison: dict[str, object] | None = None,
    action: PlaybookAction | None = None,
    clause_ids: Sequence[str] = (),
    uncertainty_reason: str | None = None,
    evidence_quality: EvidenceQuality = EvidenceQuality.SUFFICIENT,
    automatic: bool | None = None,
) -> RuleCheckResult:
    fact_ids = tuple(fact.fact_id for fact in facts)
    evidence_ids = _unique(
        [
            context.rule_evidence.evidence_id,
            context.package_evidence.evidence_id,
            *_fact_evidence_ids(facts),
            *extra_evidence_ids,
            *[item.evidence_id for item in evidence],
        ]
    )
    resolved_confidence = (
        confidence if confidence is not None else _fact_confidence(facts)
    )
    effective_status = status
    effective_reason = reason
    effective_recommended_action = recommended_action
    effective_uncertainty_reason = uncertainty_reason
    effective_evidence_quality = evidence_quality
    effective_automatic = (
        status in {FindingStatus.PASS, FindingStatus.WARN, FindingStatus.BLOCK}
        if automatic is None
        else automatic
    )
    # OCR 未完成时仍可能抽到低置信度事实；任何基于这类事实的确定性
    # 结论都必须退回 UNKNOWN，不能仅靠 automatic=False 掩盖 PASS。
    fact_confidence = _fact_confidence(facts)
    if (
        facts
        and fact_confidence is not None
        and fact_confidence < 0.5
        and status in {FindingStatus.PASS, FindingStatus.WARN, FindingStatus.BLOCK}
    ):
        effective_status = FindingStatus.UNKNOWN
        effective_reason = f"事实置信度不足，原结论未自动采纳：{reason}"
        effective_recommended_action = (
            recommended_action or "先完成文字解析，再由审核人核对原文证据。"
        )
        effective_uncertainty_reason = "low_confidence_extracted_fact"
        effective_evidence_quality = EvidenceQuality.INSUFFICIENT
        effective_automatic = False
    resolved_clause_ids = _unique(
        [*clause_ids, *_fact_clause_ids(context, facts)]
    )
    return RuleCheckResult(
        status=effective_status,
        reason=effective_reason,
        evidence_ids=evidence_ids,
        evidence=tuple(evidence),
        recommended_action=effective_recommended_action,
        confidence=resolved_confidence,
        fact_ids=fact_ids,
        comparison=comparison,
        action=action,
        clause_ids=resolved_clause_ids,
        uncertainty_reason=effective_uncertainty_reason,
        evidence_quality=effective_evidence_quality,
        automatic=effective_automatic,
    )


def _unknown(
    context: RuleCheckContext,
    reason: str,
    *,
    facts: Sequence[ContractFact] = (),
    extra_evidence_ids: Sequence[str] = (),
    evidence: Sequence[Evidence] = (),
    recommended_action: str,
    uncertainty_reason: str | None = "insufficient_structured_evidence",
) -> RuleCheckResult:
    return _base_result(
        context,
        status=FindingStatus.UNKNOWN,
        reason=reason,
        facts=facts,
        extra_evidence_ids=extra_evidence_ids,
        evidence=evidence,
        recommended_action=recommended_action,
        confidence=0.0,
        uncertainty_reason=uncertainty_reason,
        evidence_quality=EvidenceQuality.INSUFFICIENT,
        automatic=False,
    )


def _find_text_matches(
    context: RuleCheckContext,
    query: str,
    *,
    evidence_prefix: str,
) -> list[Evidence]:
    """只在当前规则的合同候选中查找文本，并回指已有证据对象。"""

    del evidence_prefix
    evidence_items: list[Evidence] = []
    seen: set[str] = set()
    for candidate in context.candidate_evidence:
        if (
            candidate.source_kind != KnowledgeSourceKind.CONTRACT
            or query not in candidate.content
        ):
            continue
        for evidence_id in candidate.evidence_ids:
            if evidence_id in seen:
                continue
            evidence = context.evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            seen.add(evidence_id)
            evidence_items.append(evidence)
    return evidence_items


def _single_value(
    facts: Sequence[ContractFact],
) -> tuple[ContractFact, Decimal] | None:
    values = _fact_values(facts)
    distinct = {value for _, value in values}
    if len(distinct) != 1:
        return None
    return values[0] if values else None


def check_contract_amount_case(context: RuleCheckContext) -> RuleCheckResult:
    numeric_facts = context.facts_by_type.get("financial.contract_amount_numeric", ())
    uppercase_facts = context.facts_by_type.get("financial.contract_amount_upper", ())
    numeric = _single_value(numeric_facts)
    uppercase = _single_value(uppercase_facts)
    if numeric is None or uppercase is None:
        return _unknown(
            context,
            "未同时提取到合同金额的数字和大写金额，不能自动判断大小写是否一致。",
            facts=[*numeric_facts, *uppercase_facts],
            recommended_action="补充金额数字/大写原文，或由财务审核人核对金额口径。",
        )
    left_fact, left_value = numeric
    right_fact, right_value = uppercase
    same = left_value == right_value
    facts = [left_fact, right_fact]
    return _base_result(
        context,
        status=FindingStatus.PASS if same else FindingStatus.WARN,
        reason=(
            f"合同金额数字 {_money(left_value)} 与大写金额 {_money(right_value)} 一致。"
            if same
            else f"合同金额数字 {_money(left_value)} 与大写金额 {_money(right_value)} 不一致。"
        ),
        facts=facts,
        comparison={
            "numeric": _money(left_value),
            "uppercase": _money(right_value),
            "consistent": same,
        },
        recommended_action=None if same else "人工核对金额数字、大写金额及含税口径。",
    )


def check_detail_total(context: RuleCheckContext) -> RuleCheckResult:
    detail_facts = context.facts_by_type.get("financial.detail_amount", ())
    total_facts = context.facts_by_type.get("financial.detail_total", ())
    details = _fact_values(detail_facts)
    total = _single_value(total_facts)
    if not details or total is None:
        return _unknown(
            context,
            "未同时提取到至少一项金额明细和唯一合计值，不能自动核对明细汇总。",
            facts=[*detail_facts, *total_facts],
            recommended_action="补充结构化金额明细和合计，或由财务审核人核对表格。",
        )
    total_fact, total_value = total
    detail_sum = sum(value for _, value in details)
    same = detail_sum == total_value
    facts = [fact for fact, _ in details] + [total_fact]
    return _base_result(
        context,
        status=FindingStatus.PASS if same else FindingStatus.WARN,
        reason=(
            f"金额明细合计 {_money(detail_sum)} 与总计 {_money(total_value)} 一致。"
            if same
            else f"金额明细合计 {_money(detail_sum)} 与总计 {_money(total_value)} 不一致。"
        ),
        facts=facts,
        comparison={
            "detail_sum": _money(detail_sum),
            "declared_total": _money(total_value),
            "detail_count": len(details),
        },
        recommended_action=None if same else "人工核对金额明细、单位和合计行。",
    )


def check_tax_rate(context: RuleCheckContext) -> RuleCheckResult:
    from .rules import expected_rule_value

    facts = list(context.facts_by_type.get("tax_rate", ()))
    expected = expected_rule_value(context.rule, review_context=context.effective_context)
    if not facts:
        return _unknown(
            context,
            "合同包中没有提取到带原文证据的税率百分比。",
            recommended_action="补充税率条款或由财税审核人确认适用税率。",
        )
    if not isinstance(expected, (int, float)):
        return _unknown(
            context,
            "当前合同类型的税率预期值为混合或非数值策略，不能自动比较。",
            facts=facts,
            recommended_action="由财税审核人确认混合税率的分项和适用依据。",
        )
    values = [float(value) for _, value in _fact_values(facts)]
    matches = all(abs(value - float(expected)) < 1e-9 for value in values)
    return _base_result(
        context,
        status=FindingStatus.PASS if matches else FindingStatus.WARN,
        reason=(
            f"提取税率与规则预期值 {float(expected):.4g} 一致。"
            if matches
            else f"提取税率 {values} 与规则预期值 {float(expected):.4g} 不一致。"
        ),
        facts=facts,
        comparison={"actual": values, "expected": float(expected)},
        recommended_action=None if matches else "由财税审核人核对合同类型、税率和开票依据。",
    )


def check_untaxed(context: RuleCheckContext) -> RuleCheckResult:
    untaxed = _find_text_matches(context, "不含税", evidence_prefix="untaxed")
    taxed = [
        item
        for item in _find_text_matches(context, "含税", evidence_prefix="taxed")
        if "不含税" not in (item.raw_excerpt or "")
    ]
    if untaxed and not taxed:
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="合同包明确出现“不含税”表述。",
            extra_evidence_ids=[item.evidence_id for item in untaxed],
            evidence=untaxed,
            confidence=1.0,
        )
    if taxed:
        return _base_result(
            context,
            status=FindingStatus.WARN,
            reason="合同包出现“含税”表述，需确认是否满足本规则对不含税金额的要求。",
            extra_evidence_ids=[item.evidence_id for item in taxed],
            evidence=taxed,
            recommended_action="人工核对含税/不含税口径及金额计算。",
            confidence=1.0,
        )
    return _unknown(
        context,
        "未发现“含税/不含税”表述，不能据此确认金额口径。",
        recommended_action="补充金额口径或由财税审核人确认。",
    )


def check_tax_amount(context: RuleCheckContext) -> RuleCheckResult:
    base_facts = list(context.facts_by_type.get("financial.tax_base_amount", ()))
    if not base_facts:
        # 只有正文明确出现不含税口径时，合同总额才可作为税额计算基数。
        untaxed = _find_text_matches(context, "不含税", evidence_prefix="tax-base")
        if untaxed:
            base_facts = list(
                context.facts_by_type.get("financial.contract_amount_numeric", ())
            )
        else:
            untaxed = []
    else:
        untaxed = []
    tax_facts = list(context.facts_by_type.get("financial.tax_amount", ()))
    rate_facts = list(context.facts_by_type.get("tax_rate", ()))
    base = _single_value(base_facts)
    tax = _fact_values(tax_facts)
    rate = _single_value(rate_facts)
    if base is None or not tax or rate is None:
        return _unknown(
            context,
            "未同时提取到明确的不含税基数、税率和税额，不能自动计算税额。",
            facts=[*base_facts, *tax_facts, *rate_facts],
            extra_evidence_ids=[item.evidence_id for item in untaxed],
            evidence=untaxed,
            recommended_action="补充不含税金额、税率和税额，或由财税审核人确认。",
        )
    base_fact, base_value = base
    rate_fact, rate_value = rate
    tax_value = sum(value for _, value in tax)
    expected = (base_value * rate_value).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    actual = tax_value.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    same = actual == expected
    facts = [base_fact, rate_fact, *[fact for fact, _ in tax]]
    return _base_result(
        context,
        status=FindingStatus.PASS if same else FindingStatus.WARN,
        reason=(
            f"按不含税基数 {_money(base_value)} 和税率 {float(rate_value):.4g} 计算，税额 {_money(actual)} 一致。"
            if same
            else f"按不含税基数 {_money(base_value)} 和税率 {float(rate_value):.4g} 计算应为 {_money(expected)}，合同税额为 {_money(actual)}。"
        ),
        facts=facts,
        extra_evidence_ids=[item.evidence_id for item in untaxed],
        evidence=untaxed,
        comparison={
            "tax_base": _money(base_value),
            "tax_rate": float(rate_value),
            "actual_tax": _money(actual),
            "expected_tax": _money(expected),
        },
        recommended_action=None if same else "人工核对含税口径、税率和税额四舍五入规则。",
    )


def _check_sum_against_contract(
    context: RuleCheckContext,
    *,
    fact_type: str,
    label: str,
) -> RuleCheckResult:
    amount_facts = list(context.facts_by_type.get(fact_type, ()))
    contract_facts = list(
        context.facts_by_type.get("financial.contract_amount_numeric", ())
    )
    amounts = _fact_values(amount_facts)
    contract = _single_value(contract_facts)
    if not amounts or contract is None:
        return _unknown(
            context,
            f"未同时提取到{label}和合同金额，不能自动核对总额。",
            facts=[*amount_facts, *contract_facts],
            recommended_action=f"补充{label}和合同金额的结构化事实，或由财务审核人确认。",
        )
    contract_fact, contract_value = contract
    actual = sum(value for _, value in amounts)
    same = actual == contract_value
    facts = [*[fact for fact, _ in amounts], contract_fact]
    return _base_result(
        context,
        status=FindingStatus.PASS if same else FindingStatus.WARN,
        reason=(
            f"{label}合计 {_money(actual)} 与合同金额 {_money(contract_value)} 一致。"
            if same
            else f"{label}合计 {_money(actual)} 与合同金额 {_money(contract_value)} 不一致。"
        ),
        facts=facts,
        comparison={"actual_total": _money(actual), "contract_amount": _money(contract_value)},
        recommended_action=None if same else f"人工核对{label}明细、单位和合同总额。",
    )


def check_payment_total(context: RuleCheckContext) -> RuleCheckResult:
    return _check_sum_against_contract(
        context,
        fact_type="financial.payment_amount",
        label="付款金额",
    )


def check_invoice_total(context: RuleCheckContext) -> RuleCheckResult:
    return _check_sum_against_contract(
        context,
        fact_type="financial.invoice_amount",
        label="发票金额",
    )


def check_payment_ratio(context: RuleCheckContext) -> RuleCheckResult:
    facts = list(context.facts_by_type.get("financial.payment_ratio", ()))
    values = _fact_values(facts)
    if not values:
        return _unknown(
            context,
            "未提取到带付款语境的付款比例。",
            recommended_action="补充付款节点和比例，或由财务审核人确认。",
        )
    total = sum(value for _, value in values)
    same = total <= Decimal("1.000001")
    return _base_result(
        context,
        status=FindingStatus.PASS if same else FindingStatus.WARN,
        reason=(
            f"已提取付款比例合计 {float(total):.4g}，未超过 100%。"
            if same
            else f"已提取付款比例合计 {float(total):.4g}，超过 100%。"
        ),
        facts=facts,
        comparison={"ratio_total": float(total), "ratio_limit": 1.0},
        recommended_action=None if same else "人工核对付款比例是否重复计算或存在超付风险。",
    )


def check_guarantee(context: RuleCheckContext) -> RuleCheckResult:
    matches = _find_text_matches(context, "保函", evidence_prefix="guarantee")
    if matches:
        return _base_result(
            context,
            status=FindingStatus.WARN,
            reason="合同包明确出现“保函”要求，需核对保函类型、金额、期限和出具条件。",
            extra_evidence_ids=[item.evidence_id for item in matches],
            evidence=matches,
            recommended_action="由财务和法务确认保函要求及授权边界。",
            confidence=1.0,
        )
    return _unknown(
        context,
        "未发现“保函”文字证据，不能据此确认业务上不需要保函。",
        recommended_action="由业务和财务确认是否存在保函要求。",
    )


def check_invoice_type(context: RuleCheckContext) -> RuleCheckResult:
    facts = list(context.facts_by_type.get("contract_element:invoice_type", ()))
    if not facts:
        return _unknown(
            context,
            "合同包中没有提取到带原文证据的发票类型。",
            recommended_action="补充发票类型条款，或由财税审核人确认。",
        )
    values = list(dict.fromkeys(str(fact.value) for fact in facts))
    return _base_result(
        context,
        status=FindingStatus.PASS,
        reason=f"已提取发票类型：{'、'.join(values)}。",
        facts=facts,
        comparison={"values": values},
    )


def check_invoice_amount(context: RuleCheckContext) -> RuleCheckResult:
    facts = list(context.facts_by_type.get("financial.invoice_amount", ()))
    if not facts:
        return _unknown(
            context,
            "合同包中没有提取到带原文证据的发票金额。",
            recommended_action="补充发票金额条款，或由财税审核人确认。",
        )
    values = [_money(value) for _, value in _fact_values(facts)]
    return _base_result(
        context,
        status=FindingStatus.PASS,
        reason=f"已提取发票金额：{'、'.join(values)}。",
        facts=facts,
        comparison={"values": values},
    )


def check_attachment_completeness_rule(
    context: RuleCheckContext,
) -> RuleCheckResult | None:
    """把多个缺失附件聚合为一条规则发现，避免同一规则重复出结论。"""

    if not context.attachment_references:
        return None
    missing_references = [
        reference
        for reference in context.attachment_references
        if reference.required
        and not any(
            _document_matches(reference, document)
            for document in context.documents
        )
    ]
    if not missing_references:
        return None
    missing_evidence: list[Evidence] = []
    for reference in missing_references:
        digest = hashlib.sha256(
            f"{reference.reference_id}\x1f{reference.referenced_name}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        missing_evidence.append(
            Evidence(
                evidence_id=f"missing-attachment-{digest}",
                evidence_type=EvidenceType.MISSING_ARTIFACT,
                locator=SourceLocator(
                    locator_type="missing_artifact",
                    missing_name=reference.referenced_name,
                ),
                extraction_method="attachment_manifest",
                extraction_version=RULE_CHECKER_VERSION,
                confidence=1.0,
            )
        )
    missing_names = [
        item.locator.missing_name
        for item in missing_evidence
        if item.locator.missing_name
    ]
    reference_evidence_ids = [
        evidence_id
        for reference in context.attachment_references
        for evidence_id in reference.evidence_ids
    ]
    clause_ids = [
        clause.clause_id
        for clause in context.clauses
        if set(clause.evidence_ids).intersection(reference_evidence_ids)
    ]
    reason = (
        "合同包缺少引用附件："
        + "、".join(dict.fromkeys(missing_names))
        + "；未发现匹配文件。"
    )
    return _base_result(
        context,
        status=FindingStatus.UNKNOWN,
        reason=reason,
        extra_evidence_ids=reference_evidence_ids,
        evidence=missing_evidence,
        recommended_action="补充附件，或由业务审核人确认该引用是否应当保留。",
        confidence=0.0,
        clause_ids=clause_ids,
        uncertainty_reason="required_attachment_missing",
    )


def _term_facts(
    context: RuleCheckContext,
    *fact_types: str,
) -> list[ContractFact]:
    """按事实类型读取合同履约条款事实。"""

    return [
        fact
        for fact_type in fact_types
        for fact in context.facts_by_type.get(fact_type, ())
    ]


def _term_text(facts: Sequence[ContractFact]) -> str:
    return "\n".join(
        str(fact.normalized_value if fact.normalized_value is not None else fact.value)
        for fact in facts
    )


def _term_segments(facts: Sequence[ContractFact]) -> tuple[str, ...]:
    """把合同事实切成可判断关系的正文片段，避免跨条款拼接关键词。"""

    segments: list[str] = []
    for fact in facts:
        value = str(
            fact.normalized_value if fact.normalized_value is not None else fact.value
        )
        for segment in re.split(r"[\r\n。！？!?；;]+", value):
            compact = re.sub(r"\s+", "", segment)
            if compact:
                segments.append(compact)
    return tuple(dict.fromkeys(segments))


def _missing_term_evidence(context: RuleCheckContext, label: str) -> Evidence:
    digest = hashlib.sha256(
        f"{context.package_evidence.package_id}\x1f{context.rule.rule_id}\x1f{label}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return Evidence(
        evidence_id=f"missing-term-{digest}",
        evidence_type=EvidenceType.MISSING_ARTIFACT,
        package_id=context.package_evidence.package_id,
        locator=SourceLocator(locator_type="missing_artifact", missing_name=label),
        raw_excerpt=None,
        display_excerpt=f"未在合同包中定位到：{label}",
        extraction_method="required_contract_term_gate",
        extraction_version=RULE_CHECKER_VERSION,
        confidence=1.0 if context.all_parsed else 0.0,
    )


def _missing_contract_term(
    context: RuleCheckContext,
    *,
    label: str,
    fact_types: Sequence[str],
    recommended_action: str,
) -> RuleCheckResult:
    """对关键条款缺失执行统一 fail-closed 策略。"""

    policy = (
        context.rule.playbook.missing_clause_policy
        if context.rule.playbook is not None
        else MissingClausePolicy.UNKNOWN
    )
    status_by_policy = {
        MissingClausePolicy.WARN: FindingStatus.WARN,
        MissingClausePolicy.BLOCK: FindingStatus.BLOCK,
        MissingClausePolicy.UNKNOWN: FindingStatus.UNKNOWN,
        MissingClausePolicy.NOT_APPLICABLE: FindingStatus.NOT_APPLICABLE,
    }
    status = status_by_policy[policy]
    facts = _term_facts(context, *fact_types)
    if facts:
        return _base_result(
            context,
            status=status,
            reason=f"已定位{label}相关文字，但结构化内容不完整，不能自动确认满足要求。",
            facts=facts,
            recommended_action=(
                recommended_action
                if status != FindingStatus.NOT_APPLICABLE
                else None
            ),
            confidence=0.0 if status in {FindingStatus.UNKNOWN, FindingStatus.BLOCK} else 1.0,
            evidence_quality=EvidenceQuality.INSUFFICIENT,
            automatic=False,
            uncertainty_reason=(
                "incomplete_contract_term"
                if status != FindingStatus.NOT_APPLICABLE
                else None
            ),
        )
    if not context.all_parsed:
        return _unknown(
            context,
            f"合同包存在未完成解析的文档，不能确认是否缺少{label}。",
            recommended_action=f"先完成全部文档解析，再由审核人确认{label}。",
            uncertainty_reason="incomplete_document_parse",
        )
    missing = _missing_term_evidence(context, label)
    return _base_result(
        context,
        status=status,
        reason=f"合同包未定位到{label}，不能自动视为满足审核要求。",
        evidence=[missing],
        recommended_action=(
            recommended_action
            if status != FindingStatus.NOT_APPLICABLE
            else None
        ),
        confidence=0.0 if status in {FindingStatus.UNKNOWN, FindingStatus.BLOCK} else 1.0,
        evidence_quality=EvidenceQuality.SUFFICIENT,
        automatic=status != FindingStatus.UNKNOWN,
        uncertainty_reason=(
            "required_contract_term_missing"
            if status != FindingStatus.NOT_APPLICABLE
            else None
        ),
        comparison={
            "match_kind": "missing",
            "missing_term": label,
            "missing_clause_policy": policy.value,
            "suggested_language": (
                context.rule.playbook.suggested_language
                if context.rule.playbook is not None
                else None
            ),
        },
    )


def _playbook_language(context: RuleCheckContext) -> str | None:
    if context.rule.playbook is None:
        return None
    return context.rule.playbook.suggested_language


def check_payment_terms(context: RuleCheckContext) -> RuleCheckResult:
    facts = _term_facts(context, "contract_term:payment")
    if not facts:
        return _missing_contract_term(
            context,
            label="付款条件",
            fact_types=("contract_term:payment",),
            recommended_action="补充付款节点、付款条件和付款比例，并由财务审核。",
        )
    segments = _term_segments(facts)
    if any(_FULL_PAYMENT_PATTERN.search(segment) for segment in segments):
        return _base_result(
            context,
            status=FindingStatus.BLOCK,
            reason="付款条款命中一次性全额预付表述，触及企业付款底线。",
            facts=facts,
            recommended_action="拒绝一次性全额预付，改为与交付/验收节点绑定的付款安排。",
            action=PlaybookAction.REJECT,
            comparison={
                "match_kind": "prohibited",
                "suggested_language": _playbook_language(context),
            },
        )
    if any(
        _PAYMENT_ACCEPTANCE_BINDING_PATTERN.search(segment)
        for segment in segments
    ):
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="付款条件明确绑定验收节点。",
            facts=facts,
            action=PlaybookAction.ACCEPT,
            comparison={"match_kind": "acceptance_bound"},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位付款条款，但未能确认付款与交付/验收节点充分绑定。",
        facts=facts,
        recommended_action="补充付款节点、付款条件和付款比例，并由财务审核。",
        action=PlaybookAction.REVISE,
        comparison={
            "match_kind": "incomplete_condition",
            "suggested_language": _playbook_language(context),
        },
    )


def check_delivery_terms(context: RuleCheckContext) -> RuleCheckResult:
    term_facts = _term_facts(context, "contract_term:delivery")
    date_facts = _term_facts(context, "contract_element:delivery_date")
    if not term_facts:
        return _missing_contract_term(
            context,
            label="交付/履行条款",
            fact_types=("contract_term:delivery", "contract_element:delivery_date"),
            recommended_action="补充交付标的、时间、地点、方式和逾期责任。",
        )
    if not date_facts:
        return _missing_contract_term(
            context,
            label="交付日期或履行期限",
            fact_types=("contract_term:delivery", "contract_element:delivery_date"),
            recommended_action="补充可执行的交付日期或履行期限，并明确逾期责任。",
        )
    term_document_ids = {
        document_id for fact in term_facts for document_id in fact.source_document_ids
    }
    date_document_ids = {
        document_id for fact in date_facts for document_id in fact.source_document_ids
    }
    if term_document_ids and date_document_ids and not term_document_ids.intersection(
        date_document_ids
    ):
        return _unknown(
            context,
            "交付条款与交付期限来自不同文档，无法确认两者属于同一履约口径。",
            facts=[*term_facts, *date_facts],
            recommended_action="人工核对主合同、技术协议和交付计划的文件优先效力。",
            uncertainty_reason="delivery_term_document_conflict",
        )
    facts = [*term_facts, *date_facts]
    text = _term_text(facts)
    has_location_or_method = any(
        keyword in text for keyword in ("地点", "地址", "现场", "交付方式", "交货方式", "线上", "现场")
    )
    if has_location_or_method:
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="已定位交付/履行条款、期限以及地点或方式。",
            facts=facts,
            comparison={"has_deadline": True, "has_location_or_method": True},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位交付期限，但未同时确认交付地点或方式。",
        facts=facts,
        recommended_action="补充交付地点、交付方式和交付材料。",
        comparison={"has_deadline": True, "has_location_or_method": False},
    )


def check_acceptance_terms(context: RuleCheckContext) -> RuleCheckResult:
    facts = _term_facts(context, "contract_term:acceptance")
    if not facts:
        return _missing_contract_term(
            context,
            label="验收标准和方法",
            fact_types=("contract_term:acceptance",),
            recommended_action="补充可验证的验收指标、验收材料、验收期限和不合格处理。",
        )
    text = _term_text(facts)
    if re.search(r"视为验收合格|自动验收合格|默认验收合格", text):
        return _base_result(
            context,
            status=FindingStatus.BLOCK,
            reason="验收条款包含自动视为合格表述，可能绕过实际验收。",
            facts=facts,
            recommended_action="删除自动验收合格表述，补充客观验收标准和不合格处理方式。",
            action=PlaybookAction.REJECT,
        )
    has_standard = any(
        keyword in text for keyword in ("标准", "指标", "功能", "性能", "测试")
    )
    has_method_or_deadline = any(
        keyword in text for keyword in ("方法", "材料", "报告", "日内", "工作日", "期限")
    )
    if has_standard and has_method_or_deadline:
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="已定位可验证的验收标准及验收方法/期限。",
            facts=facts,
            comparison={"has_standard": True, "has_method_or_deadline": True},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位验收相关条款，但验收标准、方法或期限不完整。",
        facts=facts,
        recommended_action="补充可验证的验收指标、验收材料、验收期限和不合格处理。",
        comparison={
            "has_standard": has_standard,
            "has_method_or_deadline": has_method_or_deadline,
        },
    )


def check_renewal_terms(context: RuleCheckContext) -> RuleCheckResult:
    facts = _term_facts(context, "contract_term:renewal")
    if not facts:
        return _missing_contract_term(
            context,
            label="续期/续签条款",
            fact_types=("contract_term:renewal",),
            recommended_action="确认是否续期、续签以及到期通知期限，并保留业务确认。",
        )
    text = _term_text(facts)
    if re.search(r"不自动续期|不自动续签|到期终止", text):
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="合同明确约定不自动续期或到期终止。",
            facts=facts,
            comparison={"automatic_renewal": False},
        )
    if re.search(r"自动续期|自动续签|期满自动", text):
        return _base_result(
            context,
            status=FindingStatus.WARN,
            reason="合同包含自动续期/续签安排，需要确认期限、通知和退出条件。",
            facts=facts,
            recommended_action="明确续期期限、提前通知期限及任一方退出条件。",
            comparison={"automatic_renewal": True},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位续期/续签条款，但其触发条件或通知期限需要人工确认。",
        facts=facts,
        recommended_action="确认续期期限、通知期限和续签条件。",
        comparison={"automatic_renewal": None},
    )


def check_termination_terms(context: RuleCheckContext) -> RuleCheckResult:
    facts = _term_facts(context, "contract_term:termination")
    if not facts:
        return _missing_contract_term(
            context,
            label="解除/终止条款",
            fact_types=("contract_term:termination",),
            recommended_action="补充解除/终止事由、通知期限、交接和费用结算规则。",
        )
    text = _term_text(facts)
    has_trigger = any(
        keyword in text for keyword in ("违约", "未履行", "逾期", "破产", "不可抗力", "事由")
    )
    has_notice_or_settlement = any(
        keyword in text for keyword in ("通知", "提前", "结算", "交接", "日内", "工作日")
    )
    if has_trigger and has_notice_or_settlement:
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="已定位解除/终止事由以及通知、交接或结算安排。",
            facts=facts,
            comparison={"has_trigger": True, "has_notice_or_settlement": True},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位解除/终止条款，但事由、通知或结算安排不完整。",
        facts=facts,
        recommended_action="补充解除/终止事由、通知期限、交接和费用结算规则。",
        comparison={
            "has_trigger": has_trigger,
            "has_notice_or_settlement": has_notice_or_settlement,
        },
    )


def check_breach_liability_terms(context: RuleCheckContext) -> RuleCheckResult:
    facts = _term_facts(context, "contract_term:breach")
    if not facts:
        return _missing_contract_term(
            context,
            label="违约责任条款",
            fact_types=("contract_term:breach",),
            recommended_action="补充违约情形、责任承担、违约金/损失计算和责任上限。",
        )
    text = _term_text(facts)
    if re.search(r"不承担任何违约责任|不承担违约责任|免责", text):
        return _base_result(
            context,
            status=FindingStatus.BLOCK,
            reason="违约责任条款包含全面免责或不承担违约责任表述。",
            facts=facts,
            recommended_action="删除全面免责表述，补充对等且可执行的责任承担规则。",
            action=PlaybookAction.REJECT,
            comparison={"liability_defined": False, "exemption_detected": True},
        )
    has_liability = any(
        keyword in text for keyword in ("承担", "赔偿", "违约金", "损失", "责任")
    )
    has_measure = any(
        keyword in text for keyword in ("比例", "%", "金额", "上限", "计算", "标准")
    )
    if has_liability and has_measure:
        return _base_result(
            context,
            status=FindingStatus.PASS,
            reason="已定位违约责任及责任计算/金额口径。",
            facts=facts,
            comparison={"liability_defined": True, "measure_defined": True},
        )
    return _base_result(
        context,
        status=FindingStatus.WARN,
        reason="已定位违约责任条款，但责任承担或计算口径不完整。",
        facts=facts,
        recommended_action="补充违约情形、责任承担、违约金/损失计算和责任上限。",
        comparison={"liability_defined": has_liability, "measure_defined": has_measure},
    )


_CROSS_DOCUMENT_FACT_TYPES = (
    "contract_element:contract_name",
    "contract_element:project_name",
    "contract_element:party_a",
    "contract_element:party_b",
    "financial.contract_amount_numeric",
    "financial.contract_amount_upper",
    "tax_rate",
    "contract_element:delivery_date",
)


def _fact_document_ids(
    fact: ContractFact,
    evidence_by_id: Mapping[str, Evidence],
) -> set[str]:
    document_ids = set(fact.source_document_ids)
    if document_ids:
        return document_ids
    return {
        evidence.document_id
        for evidence_id in fact.evidence_ids
        if (evidence := evidence_by_id.get(evidence_id)) is not None
        and evidence.document_id
    }


def check_cross_document_consistency(context: RuleCheckContext) -> RuleCheckResult:
    """比较多份合同材料中的同类事实，区分不适用与合同包不完整。

    单文档例外由规则适用性层（Playbook）先行裁决；能够进入本检查器的
    单文档请求，说明规则已经被判定为需要执行，此时缺少可比较文档只能
    输出 ``UNKNOWN``，不能把输入缺失折叠成 ``NOT_APPLICABLE``。
    """

    if len(context.documents) < 2:
        return _base_result(
            context,
            status=FindingStatus.UNKNOWN,
            reason=(
                "跨文档一致性检查至少需要两份已解析文档；"
                f"当前合同包只有 {len(context.documents)} 份，不能据此判定事实一致。"
            ),
            recommended_action=(
                "补充主合同、技术协议或附件后重新审查；若本次确为单文档审查，"
                "请在业务上下文中声明 single_document_review。"
            ),
            confidence=0.0,
            comparison={
                "document_count": len(context.documents),
                "required_document_count": 2,
            },
            uncertainty_reason="insufficient_documents_for_cross_document_check",
            evidence_quality=EvidenceQuality.INSUFFICIENT,
            automatic=False,
        )
    comparable: list[ContractFact] = []
    values_by_type: dict[str, dict[str, set[str]]] = {}
    for fact_type in _CROSS_DOCUMENT_FACT_TYPES:
        facts = list(context.facts_by_type.get(fact_type, ()))
        by_document: dict[str, list[ContractFact]] = {}
        for fact in facts:
            for document_id in _fact_document_ids(fact, context.evidence_by_id):
                by_document.setdefault(document_id, []).append(fact)
        if len(by_document) < 2:
            continue
        for document_id, document_facts in by_document.items():
            for fact in document_facts:
                normalized = str(
                    fact.normalized_value if fact.normalized_value is not None else fact.value
                ).strip().casefold()
                values_by_type.setdefault(fact_type, {}).setdefault(normalized, set()).add(
                    document_id
                )
                comparable.append(fact)
    required_fact_types = set(context.rule.required_evidence)
    if not values_by_type or not required_fact_types.issubset(values_by_type):
        return _unknown(
            context,
            "多文档中没有覆盖规则要求的全部同类结构化事实，不能自动判断一致性。",
            facts=comparable,
            recommended_action="补充主合同、技术协议或附件中的关键事实，并由审核人核对。",
            uncertainty_reason="no_cross_document_comparable_fact",
        )
    conflicts = {
        fact_type: {
            value: sorted(document_ids)
            for value, document_ids in values.items()
        }
        for fact_type, values in values_by_type.items()
        if len(values) > 1
    }
    if conflicts:
        return _base_result(
            context,
            status=FindingStatus.WARN,
            reason="多份合同材料中的关键事实不一致，需要确认优先效力和修订范围。",
            facts=comparable,
            recommended_action="人工核对主合同、附件和技术协议的金额、主体、项目或交付事实。",
            comparison={"conflicts": conflicts},
        )
    return _base_result(
        context,
        status=FindingStatus.PASS,
        reason="多份合同材料中可比较的关键事实一致。",
        facts=comparable,
        comparison={
            "consistent_fact_types": sorted(values_by_type),
            "document_count": len(context.documents),
        },
    )


_CHECKERS: dict[str, Callable[[RuleCheckContext], RuleCheckResult | None]] = {
    "amount_case_consistency": check_contract_amount_case,
    "amount_detail_total": check_detail_total,
    "untaxed_amount": check_untaxed,
    "tax_rate": check_tax_rate,
    "tax_amount": check_tax_amount,
    "payment_ratio": check_payment_ratio,
    "guarantee_requirement": check_guarantee,
    "payment_total": check_payment_total,
    "invoice_type": check_invoice_type,
    "invoice_amount": check_invoice_amount,
    "invoice_total": check_invoice_total,
    "attachment_completeness": check_attachment_completeness_rule,
    "payment_terms": check_payment_terms,
    "delivery_terms": check_delivery_terms,
    "acceptance_terms": check_acceptance_terms,
    "renewal_terms": check_renewal_terms,
    "termination_terms": check_termination_terms,
    "breach_liability_terms": check_breach_liability_terms,
    "cross_document_consistency": check_cross_document_consistency,
}

def checker_for_rule(rule: Rule) -> str | None:
    """读取规则快照显式声明的检查器。"""

    return rule.checker


def is_supported_checker(checker: str) -> bool:
    """判断规则快照中的 checker 是否有明确实现。"""

    return checker in _CHECKERS


def is_rule_checker_configured(rule: Rule) -> bool:
    """判断规则是否已绑定可执行的确定性检查器。"""

    checker = checker_for_rule(rule)
    return checker is not None and is_supported_checker(checker)


def execute_configured_rule_checker(
    rule: Rule,
    context: RuleCheckContext,
) -> RuleCheckResult | None:
    """执行规则声明的检查器；未配置时返回 ``None``。"""

    checker = checker_for_rule(rule)
    if checker is None:
        return None
    implementation = _CHECKERS.get(checker)
    if implementation is None:
        raise ValueError(f"规则声明了未注册的 checker: {checker}")
    return implementation(context)
