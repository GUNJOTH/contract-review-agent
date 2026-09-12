"""合同版本比对的领域构建和 ``ReviewResult`` 挂载能力。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from .models import (
    ContractVersionImpact,
    ContractVersionComparison,
    ContractClause,
    Evidence,
    EvidenceType,
    ReviewResult,
    VersionImpactLevel,
    SourceLocator,
    VersionChange,
    VersionChangeKind,
)
from .facts import parse_money_value
from .replay import build_result_fingerprint


COMPARISON_VERSION = "contract-document-compare-0.4.0"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_version_comparison(
    *,
    run_id: str,
    base_filename: str,
    compare_filename: str,
    base_source_sha256: str,
    compare_source_sha256: str,
    similarity: float,
    added: int,
    deleted: int,
    modified: int,
    changes: Sequence[Mapping[str, object]],
    options: Mapping[str, bool] | None = None,
    finding_ids: Sequence[str] = (),
) -> ContractVersionComparison:
    """把应用层差异 DTO 转为稳定的领域比对对象。

    该函数不接触文件系统，也不创建证据。证据只有在绑定到具体
    ``ReviewResult`` 后才生成，避免把脱离合同包的临时差异当成审查事实。
    """

    canonical_changes = [
        {
            "change_id": str(item.get("change_id") or ""),
            "kind": str(item.get("kind") or ""),
            "base_index": item.get("base_index"),
            "compare_index": item.get("compare_index"),
            "base_text": str(item.get("base_text") or ""),
            "compare_text": str(item.get("compare_text") or ""),
            "clause_ids": [str(value) for value in (item.get("clause_ids") or ())],
        }
        for item in changes
    ]
    if any(not item["change_id"] for item in canonical_changes):
        raise ValueError("version comparison changes require change_id")
    comparison_key = json.dumps(
        {
            "run_id": run_id,
            "base_filename": base_filename,
            "compare_filename": compare_filename,
            "base_source_sha256": base_source_sha256,
            "compare_source_sha256": compare_source_sha256,
            "options": dict(options or {}),
            "changes": canonical_changes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    comparison_id = "comparison-" + hashlib.sha256(
        comparison_key.encode("utf-8")
    ).hexdigest()[:20]
    version_changes = [
        VersionChange(
            change_id=item["change_id"],
            kind=VersionChangeKind(item["kind"]),
            base_index=item["base_index"],
            compare_index=item["compare_index"],
            base_text=item["base_text"],
            compare_text=item["compare_text"],
            evidence_ids=[f"comparison-pending-{item['change_id']}"],
            clause_ids=item["clause_ids"],
        )
        for item in canonical_changes
    ]
    return ContractVersionComparison(
        comparison_id=comparison_id,
        run_id=run_id,
        base_filename=base_filename,
        compare_filename=compare_filename,
        base_source_sha256=base_source_sha256,
        compare_source_sha256=compare_source_sha256,
        similarity=similarity,
        added=added,
        deleted=deleted,
        modified=modified,
        source_version=COMPARISON_VERSION,
        options=dict(options or {}),
        changes=version_changes,
        finding_ids=list(dict.fromkeys(finding_ids)),
    )


def build_comparison_from_files(
    *,
    run_id: str,
    base_filename: str,
    base_content: bytes,
    compare_filename: str,
    compare_content: bytes,
    similarity: float,
    added: int,
    deleted: int,
    modified: int,
    changes: Sequence[Mapping[str, object]],
    options: Mapping[str, bool] | None = None,
    finding_ids: Sequence[str] = (),
) -> ContractVersionComparison:
    """用文件原始字节计算哈希后创建领域比对对象。"""

    return build_version_comparison(
        run_id=run_id,
        base_filename=base_filename,
        compare_filename=compare_filename,
        base_source_sha256=_sha256(base_content),
        compare_source_sha256=_sha256(compare_content),
        similarity=similarity,
        added=added,
        deleted=deleted,
        modified=modified,
        changes=changes,
        options=options,
        finding_ids=finding_ids,
    )


def _comparison_excerpt(comparison: ContractVersionComparison, change: VersionChange) -> str:
    parts = [f"基准文档：{comparison.base_filename}", f"比对文档：{comparison.compare_filename}"]
    if change.base_text:
        parts.append(f"基准原文：{change.base_text}")
    if change.compare_text:
        parts.append(f"比对原文：{change.compare_text}")
    return "；".join(parts)


_IMPACT_OBLIGATIONS = {
    "payment_terms": "付款节点、付款条件与成果绑定义务",
    "delivery_terms": "交付标的、期限、地点和方式义务",
    "acceptance_terms": "验收标准、期限和不合格处理义务",
    "renewal_terms": "续期通知、期限和退出义务",
    "termination_terms": "解除事由、通知、交接和结算义务",
    "breach_liability_terms": "违约责任、损失计算和责任上限义务",
    "cross_document_consistency": "跨文档事实一致性与文件优先效力义务",
}

_IMPACT_MARKERS = {
    "payment_terms": (
        "付款",
        "支付",
        "预付款",
        "首付款",
        "尾款",
        "结算",
        "价款",
        "合同金额",
        "合同总价",
        "发票",
    ),
    "delivery_terms": ("交付", "交货", "到货", "工期", "履行期限"),
    "acceptance_terms": ("验收", "验收标准", "验收方法", "不合格"),
    "renewal_terms": ("续期", "续签", "自动续期", "自动续签"),
    "termination_terms": ("解除", "终止", "解约", "提前解除"),
    "breach_liability_terms": (
        "违约",
        "违约责任",
        "违约金",
        "赔偿",
        "责任上限",
        "免责",
        "损失",
        "质保",
        "保修",
        "保证",
        "支持",
        "维护",
    ),
    "cross_document_consistency": (
        "合同",
        "协议",
        "附件",
        "报价单",
        "订单",
        "优先",
        "冲突",
    ),
}


def _changed_text(change: VersionChange) -> tuple[str, str]:
    """返回版本差异两侧文本，删除/新增侧分别保持为空。"""

    return change.base_text.strip(), change.compare_text.strip()


def _payment_risk(before: str, after: str) -> VersionImpactLevel:
    dangerous = re.compile(r"(?:100\s*%|百分之百|一次性全额)\s*(?:预付|支付)")
    before_hit = bool(dangerous.search(before))
    after_hit = bool(dangerous.search(after))
    if after_hit and not before_hit:
        return VersionImpactLevel.INCREASED
    if before_hit and not after_hit:
        return VersionImpactLevel.DECREASED
    return VersionImpactLevel.REQUIRES_REVIEW


_LIABILITY_CAP_LABELS = (
    "赔偿责任上限",
    "违约责任上限",
    "责任上限",
    "累计赔偿责任",
    "累计赔偿金额",
    "赔偿责任",
    "违约责任",
    "赔偿金额",
)
_LIABILITY_CAP_VALUE_PATTERN = re.compile(
    r"(?P<ratio>\d+(?:\.\d+)?\s*[％%]|百分之[零〇一二两三四五六七八九十百千万亿兆壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+)"
    r"|(?P<money>(?:人民币\s*)?(?:\d[\d,，]*(?:\.\d+)?\s*(?:万元?|万|元)|"
    r"[零〇一二两三四五六七八九十百千万亿兆壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+(?:万元?|万|元|圆)))"
)
_LIABILITY_CAP_MARKERS = (
    "不超过",
    "不高于",
    "最高",
    "责任上限",
    "赔偿责任上限",
    "违约责任上限",
    "为限",
    "限于",
    "作为",
)


def _parse_cap_value(match: re.Match[str]) -> tuple[str, Decimal] | None:
    ratio = match.group("ratio")
    if ratio:
        value = ratio.strip().replace("％", "%")
        if value.startswith("百分之"):
            numeral = value.removeprefix("百分之")
            try:
                numeric = parse_money_value(numeral + "元")
            except (TypeError, ValueError):
                numeric = None
            if numeric is None:
                return None
            return "ratio", numeric / Decimal("100")
        try:
            return "ratio", Decimal(value.removesuffix("%")) / Decimal("100")
        except InvalidOperation:
            return None
    money = match.group("money")
    if not money:
        return None
    try:
        value = parse_money_value(money)
    except (TypeError, ValueError):
        value = None
    return ("amount", value) if value is not None else None


def _extract_liability_cap(text: str) -> tuple[str, Decimal] | None:
    """从明确的责任上限表达中提取可比较值；歧义时返回空值。"""

    extracted: list[tuple[str, Decimal]] = []
    for segment in re.split(r"[。；;\n]", text):
        if not any(label in segment for label in _LIABILITY_CAP_LABELS):
            continue
        values: list[tuple[tuple[str, Decimal], bool]] = []
        for match in _LIABILITY_CAP_VALUE_PATTERN.finditer(segment):
            value = _parse_cap_value(match)
            if value is None:
                continue
            preceding = segment[max(0, match.start() - 48) : match.start()]
            explicit = any(marker in preceding for marker in _LIABILITY_CAP_MARKERS)
            values.append((value, explicit))
        if not values:
            continue
        explicit_values = [value for value, explicit in values if explicit]
        if len(explicit_values) == 1:
            extracted.append(explicit_values[0])
        elif len(values) == 1:
            extracted.append(values[0][0])
        else:
            # 同一片段同时出现合同基数、责任上限或上下限区间时，不能凭
            # 相邻数字猜测业务含义，交给完整条款规则重新判断。
            return None
    unique = list(dict.fromkeys(extracted))
    return unique[0] if len(unique) == 1 else None


def _liability_cap_impact(before: str, after: str) -> VersionImpactLevel:
    """判断责任上限是否扩大；无法定位完整上限时保持待复核。"""

    before_cap = _extract_liability_cap(before)
    after_cap = _extract_liability_cap(after)
    if before_cap is None and after_cap is None:
        return VersionImpactLevel.REQUIRES_REVIEW
    if before_cap is None:
        # 新增明确上限通常是收窄责任，但必须在完整规则中确认适用主体。
        return VersionImpactLevel.DECREASED
    if after_cap is None:
        # 删除原有上限可能放大责任范围，也可能只是差异片段不完整，
        # 这里的证据边界已足以提示风险方向，但仍要求规则重审。
        return VersionImpactLevel.INCREASED
    if before_cap[0] != after_cap[0]:
        return VersionImpactLevel.REQUIRES_REVIEW
    if after_cap[1] > before_cap[1]:
        return VersionImpactLevel.INCREASED
    if after_cap[1] < before_cap[1]:
        return VersionImpactLevel.DECREASED
    return VersionImpactLevel.UNCHANGED


def _liability_risk(before: str, after: str) -> VersionImpactLevel:
    exemption = re.compile(r"不承担(?:任何)?(?:违约)?责任|全面免责|免责")
    before_hit = bool(exemption.search(before))
    after_hit = bool(exemption.search(after))
    if after_hit and not before_hit:
        return VersionImpactLevel.INCREASED
    if before_hit and not after_hit:
        return VersionImpactLevel.DECREASED
    cap_impact = _liability_cap_impact(before, after)
    if cap_impact in {
        VersionImpactLevel.INCREASED,
        VersionImpactLevel.DECREASED,
    }:
        return cap_impact
    # 责任上限的例外范围和适用主体需要重新跑规则，不能从差异片段臆断。
    return VersionImpactLevel.REQUIRES_REVIEW


def _delivery_acceptance_risk(before: str, after: str) -> VersionImpactLevel:
    def bound(text: str) -> bool:
        return "交付" in text and "验收" in text and any(
            marker in text for marker in ("付款", "支付", "结算")
        )

    before_bound = bound(before)
    after_bound = bound(after)
    if before_bound and not after_bound:
        return VersionImpactLevel.INCREASED
    if after_bound and not before_bound:
        return VersionImpactLevel.DECREASED
    return VersionImpactLevel.REQUIRES_REVIEW


def _precedence_resolution(
    result: ReviewResult,
    comparison: ContractVersionComparison,
) -> str:
    """根据合同包显式优先顺序确定版本覆盖方向；没有顺序就保持未决。"""

    documents_by_hash = {
        document.source_sha256: document for document in result.documents
    }
    base = documents_by_hash.get(comparison.base_source_sha256)
    compare = documents_by_hash.get(comparison.compare_source_sha256)
    precedence = result.package.document_precedence
    if comparison.base_source_sha256 == comparison.compare_source_sha256:
        return "not_required"
    # 只有两侧文档都属于当前合同包且都出现在完整优先顺序中，才能回答
    # “哪份文件优先”。外部比较文件或部分顺序都必须保留 unresolved，
    # 不能把“基准文档在顺序里”误当成“基准文档覆盖比较文档”。
    if (
        base is None
        or compare is None
        or base.document_id not in precedence
        or compare.document_id not in precedence
    ):
        return "unresolved"
    return (
        "base"
        if precedence.index(base.document_id) < precedence.index(compare.document_id)
        else "compare"
    )


def _normalise_change_text(value: str) -> str:
    """归一化版本差异文本，支持跨解析器的空白差异匹配条款。"""

    return " ".join(value.replace("\u00a0", " ").split()).strip()


def _clause_matches_change_text(clause: ContractClause, text: str) -> bool:
    """判断差异侧文本是否能在完整条款中被确定性定位。"""

    normalized_text = _normalise_change_text(text)
    if not normalized_text:
        return False
    normalized_clause = _normalise_change_text(clause.text)
    return normalized_text in normalized_clause


def _bind_change_clause_ids(
    result: ReviewResult,
    comparison: ContractVersionComparison,
    change: VersionChange,
) -> list[str]:
    """将版本差异绑定到已审查合同包中可定位的基准/比较条款。"""

    clauses_by_document: dict[str, list[ContractClause]] = {}
    for clause in result.clauses:
        clauses_by_document.setdefault(clause.document_id, []).append(clause)
    documents_by_hash = {
        document.source_sha256: document for document in result.documents
    }
    bound_ids = list(dict.fromkeys(change.clause_ids))
    sides = (
        (comparison.base_source_sha256, change.base_text),
        (comparison.compare_source_sha256, change.compare_text),
    )
    for source_sha256, text in sides:
        document = documents_by_hash.get(source_sha256)
        if document is None:
            continue
        for clause in clauses_by_document.get(document.document_id, ()):
            if _clause_matches_change_text(clause, text):
                bound_ids.append(clause.clause_id)
    return list(dict.fromkeys(bound_ids))


def _obligation_summary(obligation) -> str:
    """把履约义务转换成版本影响中可读且可回指的摘要。"""

    subject = obligation.obligor or "合同当事人"
    modality = "必须" if obligation.modality.value == "required" else "不得"
    return f"{subject}{modality}{obligation.action}"[:240]


def _build_version_impacts(
    result: ReviewResult,
    comparison: ContractVersionComparison,
    changes: Sequence[VersionChange],
) -> tuple[list[ContractVersionImpact], list[str]]:
    """把差异映射到规则义务，无法确定风险方向时显式要求重审。"""

    all_rules_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    configured_rule_ids = result.run.configuration.get("selected_rule_ids")
    active_rule_ids = {
        str(rule_id)
        for rule_id in (
            configured_rule_ids
            or [rule.rule_id for rule in result.rule_bundle.rules]
        )
        if str(rule_id) in all_rules_by_id
    }
    rules_by_id = {
        rule_id: rule
        for rule_id, rule in all_rules_by_id.items()
        if rule_id in active_rule_ids
    }
    obligations_by_clause: dict[str, list[str]] = {}
    obligations_by_id = {}
    for obligation in result.obligations:
        obligations_by_clause.setdefault(obligation.clause_id, []).append(
            obligation.obligation_id
        )
        obligations_by_id[obligation.obligation_id] = obligation
    findings_by_id = {finding.finding_id: finding for finding in result.findings}
    impacts_by_rule: dict[str, dict[str, object]] = {}
    for change in changes:
        before, after = _changed_text(change)
        changed = f"{before}\n{after}"
        linked_rule_ids = {
            finding.rule_id
            for finding in findings_by_id.values()
            if set(change.clause_ids).intersection(finding.clause_ids)
        }
        if not linked_rule_ids:
            linked_rule_ids = {
                rule.rule_id
                for rule in rules_by_id.values()
                if any(
                    marker
                    and marker in changed
                    for marker in (
                        rule.title,
                        rule.category,
                        rule.condition,
                        *(rule.playbook.clause_types if rule.playbook else []),
                        *_IMPACT_MARKERS.get(rule.checker or "", ()),
                    )
                )
            }
        if not linked_rule_ids:
            # 差异无法映射到具体规则时也必须形成显式影响项，不能让“没有命中
            # 规则”被误读成“没有业务影响”。该项要求重新执行全部已选规则。
            linked_rule_ids = {"__unmapped_version_impact__"}
        for rule_id in sorted(linked_rule_ids):
            rule = rules_by_id.get(rule_id)
            item = impacts_by_rule.setdefault(
                rule_id,
                {
                    "texts": [],
                    "evidence_ids": [],
                    "change_ids": [],
                    "obligation_ids": [],
                    "changed_obligations": [
                        (
                            _IMPACT_OBLIGATIONS.get(
                                rule.checker or "",
                                f"规则“{rule.title}”对应的业务义务",
                            )
                            if rule is not None
                            else "无法定位到具体规则的合同业务义务"
                        )
                    ],
                },
            )
            item["texts"].append((before, after, rule.checker if rule else ""))
            item["evidence_ids"].extend(change.evidence_ids)
            item["change_ids"].append(change.change_id)
            mapped_obligation_ids = [
                obligation_id
                for clause_id in change.clause_ids
                for obligation_id in obligations_by_clause.get(clause_id, [])
            ]
            item["obligation_ids"].extend(mapped_obligation_ids)
            item["changed_obligations"].extend(
                _obligation_summary(obligations_by_id[obligation_id])
                for obligation_id in mapped_obligation_ids
                if obligation_id in obligations_by_id
            )

    precedence_resolution = _precedence_resolution(result, comparison)
    impacts: list[ContractVersionImpact] = []
    for rule_id, item in sorted(impacts_by_rule.items()):
        text_pairs = item["texts"]
        before = "\n".join(pair[0] for pair in text_pairs)
        after = "\n".join(pair[1] for pair in text_pairs)
        checkers = {pair[2] for pair in text_pairs}
        payment = (
            _payment_risk(before, after)
            if "payment_terms" in checkers
            else (
                VersionImpactLevel.REQUIRES_REVIEW
                if "" in checkers
                else VersionImpactLevel.UNKNOWN
            )
        )
        liability = (
            _liability_risk(before, after)
            if "breach_liability_terms" in checkers
            else (
                VersionImpactLevel.REQUIRES_REVIEW
                if "" in checkers
                else VersionImpactLevel.UNKNOWN
            )
        )
        liability_cap = (
            _liability_cap_impact(before, after)
            if "breach_liability_terms" in checkers
            else (
                VersionImpactLevel.REQUIRES_REVIEW
                if "" in checkers
                else VersionImpactLevel.UNKNOWN
            )
        )
        delivery_acceptance = (
            _delivery_acceptance_risk(before, after)
            if checkers.intersection({"delivery_terms", "acceptance_terms"})
            else (
                VersionImpactLevel.REQUIRES_REVIEW
                if "" in checkers
                else VersionImpactLevel.UNKNOWN
            )
        )
        direction_notes = [
            f"付款风险：{payment.value}",
            f"责任风险：{liability.value}",
            (
                "责任上限变化（风险方向）："
                + {
                    VersionImpactLevel.INCREASED: "increased（上限扩大/放宽）",
                    VersionImpactLevel.DECREASED: "decreased（上限收窄或新增）",
                    VersionImpactLevel.UNCHANGED: "unchanged（明确数值未变）",
                    VersionImpactLevel.UNKNOWN: "unknown",
                    VersionImpactLevel.REQUIRES_REVIEW: "requires_review（需重审完整条款）",
                }[liability_cap]
            ),
            f"交付/验收绑定：{delivery_acceptance.value}",
        ]
        changed_obligation_summaries = list(
            dict.fromkeys(item["changed_obligations"])
        )
        if len(changed_obligation_summaries) > 1:
            direction_notes.append(
                "受影响业务义务："
                + "；".join(changed_obligation_summaries[1:])
            )
        if precedence_resolution == "unresolved":
            direction_notes.append("文件优先效力未配置，版本覆盖方向未决")
        impact_key = "\x1f".join(
            (comparison.comparison_id, rule_id, *sorted(item["evidence_ids"]))
        )
        impacts.append(
            ContractVersionImpact(
                impact_id="impact-" + hashlib.sha256(impact_key.encode("utf-8")).hexdigest()[:20],
                rule_id=None if rule_id == "__unmapped_version_impact__" else rule_id,
                change_ids=list(dict.fromkeys(item["change_ids"])),
                obligation_ids=list(dict.fromkeys(item["obligation_ids"])),
                changed_obligations=changed_obligation_summaries,
                payment_risk=payment,
                liability_risk=liability,
                liability_cap_impact=liability_cap,
                delivery_acceptance_binding=delivery_acceptance,
                precedence_resolution=precedence_resolution,
                playbook_retrigger_required=True,
                reason=(
                    "；".join(direction_notes)
                    + (
                        "；该规则必须基于比对后的完整合同包重新执行。"
                        if rule_id != "__unmapped_version_impact__"
                        else "；差异无法定位到具体规则，必须基于比对后的完整合同包重新执行全部已选规则。"
                    )
                ),
                evidence_ids=list(dict.fromkeys(item["evidence_ids"])),
            )
        )
    retrigger_rule_ids = [
        impact.rule_id for impact in impacts if impact.rule_id is not None
    ]
    if any(impact.rule_id is None for impact in impacts):
        selected_rule_ids = [
            str(rule_id)
            for rule_id in sorted(active_rule_ids)
            if str(rule_id) in all_rules_by_id
        ]
        retrigger_rule_ids.extend(selected_rule_ids)
    return impacts, list(dict.fromkeys(retrigger_rule_ids))


def attach_version_comparison(
    result: ReviewResult,
    comparison: ContractVersionComparison,
) -> ReviewResult:
    """将版本差异及其证据挂入核心结果，并更新运行/报告指针。"""

    from .audit import audit_result

    base_audit = audit_result(result)
    if not base_audit.passed:
        raise ValueError(
            "审查结果未通过完整性门禁，不能挂载版本比对："
            + "；".join(base_audit.issues[:3])
        )
    if comparison.run_id != result.run.run_id:
        raise ValueError("version comparison belongs to a different review run")
    reviewed_source_hashes = {
        document.source_sha256 for document in result.documents
    }
    if comparison.base_source_sha256 not in reviewed_source_hashes:
        raise ValueError(
            "version comparison base document is not part of the reviewed contract package"
        )
    base_document = next(
        document
        for document in result.documents
        if document.source_sha256 == comparison.base_source_sha256
    )
    if comparison.base_filename != base_document.filename:
        raise ValueError(
            "version comparison base filename does not match the reviewed document"
        )
    compare_document = next(
        (
            document
            for document in result.documents
            if document.source_sha256 == comparison.compare_source_sha256
        ),
        None,
    )
    if (
        compare_document is not None
        and comparison.compare_filename != compare_document.filename
    ):
        raise ValueError(
            "version comparison compare filename does not match the reviewed document"
        )
    if any(
        item.comparison_id == comparison.comparison_id
        for item in result.version_comparisons
    ):
        raise ValueError(f"version comparison already exists: {comparison.comparison_id}")
    finding_ids = {item.finding_id for item in result.findings}
    clause_ids = {item.clause_id for item in result.clauses}
    if not set(comparison.finding_ids).issubset(finding_ids):
        raise ValueError("version comparison references an unknown finding")
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    change_ids = [item.change_id for item in comparison.changes]
    if len(change_ids) != len(set(change_ids)):
        raise ValueError("version comparison contains duplicate change_id")
    summary_evidence_id = f"{comparison.comparison_id}-summary"
    if summary_evidence_id in evidence_by_id:
        raise ValueError("version comparison summary evidence ID already exists")
    new_evidence: list[Evidence] = []
    new_evidence.append(
        Evidence(
            evidence_id=summary_evidence_id,
            evidence_type=EvidenceType.COMPARISON,
            package_id=result.package.package_id,
            locator=SourceLocator(
                locator_type="external_uri",
                external_uri=(
                    f"urn:contract-review:comparison:{comparison.comparison_id}"
                ),
            ),
            raw_excerpt=(
                f"基准文档：{comparison.base_filename}；"
                f"比对文档：{comparison.compare_filename}；"
                f"新增 {comparison.added} 处、删除 {comparison.deleted} 处、"
                f"修改 {comparison.modified} 处。"
            ),
            display_excerpt="版本比对摘要",
            extraction_method="contract_version_compare",
            extraction_version=comparison.source_version,
            confidence=1.0,
        )
    )
    bound_changes: list[VersionChange] = []
    change_evidence_ids: list[str] = []
    for change in comparison.changes:
        bound_clause_ids = _bind_change_clause_ids(result, comparison, change)
        if not set(bound_clause_ids).issubset(clause_ids):
            raise ValueError(
                f"version comparison change references an unknown clause: {change.change_id}"
            )
        change_evidence_id = f"{comparison.comparison_id}-{change.change_id}"
        requested_ids = [
            evidence_id
            for evidence_id in change.evidence_ids
            if not evidence_id.startswith("comparison-pending-")
        ]
        if requested_ids:
            if not set(requested_ids).issubset(evidence_by_id):
                raise ValueError(
                    f"version comparison change references unknown evidence: {change.change_id}"
                )
            if any(
                evidence_by_id[evidence_id].evidence_type != EvidenceType.COMPARISON
                for evidence_id in requested_ids
            ):
                raise ValueError("version comparison evidence must use comparison evidence")
            bound_ids = list(dict.fromkeys(requested_ids))
        else:
            if change_evidence_id in evidence_by_id:
                raise ValueError(
                    f"version comparison evidence ID already exists: {change.change_id}"
                )
            new_evidence.append(
                Evidence(
                    evidence_id=change_evidence_id,
                    evidence_type=EvidenceType.COMPARISON,
                    package_id=result.package.package_id,
                    locator=SourceLocator(
                        locator_type="external_uri",
                        external_uri=(
                            f"urn:contract-review:comparison:{comparison.comparison_id}:"
                            f"change:{change.change_id}"
                        ),
                    ),
                    raw_excerpt=_comparison_excerpt(comparison, change),
                    display_excerpt=_comparison_excerpt(comparison, change),
                    extraction_method="contract_version_compare",
                    extraction_version=comparison.source_version,
                    confidence=1.0,
                )
            )
            bound_ids = [change_evidence_id]
        change_evidence_ids.extend(bound_ids)
        bound_changes.append(
            change.model_copy(
                update={
                    "evidence_ids": bound_ids,
                    "clause_ids": bound_clause_ids,
                }
            )
        )
    impacts, retrigger_rule_ids = _build_version_impacts(
        result,
        comparison,
        bound_changes,
    )
    evidence_by_id.update({item.evidence_id: item for item in new_evidence})
    bound_comparison = comparison.model_copy(
        update={
            "changes": bound_changes,
            "evidence_ids": list(
                dict.fromkeys([summary_evidence_id, *change_evidence_ids])
            ),
            "impacts": impacts,
            "retrigger_rule_ids": retrigger_rule_ids,
        }
    )
    comparisons = [*result.version_comparisons, bound_comparison]
    comparison_ids = [item.comparison_id for item in comparisons]
    sequence = [
        *result.post_review_sequence,
        f"comparison:{bound_comparison.comparison_id}",
    ]
    run = result.run.model_copy(update={"comparison_ids": comparison_ids})
    report = result.report.model_copy(update={"comparison_ids": comparison_ids})
    updated = result.model_copy(
        update={
            "evidence": [*result.evidence, *new_evidence],
            "version_comparisons": comparisons,
            "post_review_sequence": sequence,
            "run": run,
            "report": report,
        }
    )
    fingerprint = build_result_fingerprint(updated)
    updated = updated.model_copy(
        update={"run": run.model_copy(update={"result_fingerprint": fingerprint})}
    )
    final_audit = audit_result(updated)
    if not final_audit.passed:
        raise ValueError(
            "挂载版本比对后未通过完整性门禁："
            + "；".join(final_audit.issues[:3])
        )
    return updated
