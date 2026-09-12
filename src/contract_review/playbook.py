"""版本化合同审查 Playbook 的确定性评估。

评估器只使用明确配置的立场和条款证据做自动判断。无法识别的条款保持
``UNKNOWN``，不会因为模型或关键词没有报错就被误判为通过。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
import hashlib
import json

from pydantic import Field

from .models import (
    CandidateEvidence,
    ContractClause,
    ContractFact,
    FindingStatus,
    MissingClausePolicy,
    ModelBase,
    PlaybookAction,
    PlaybookSpec,
    ReviewContext,
    RuleBundle,
    RuleBundleStatus,
    Rule,
    RiskLevel,
    utc_now,
)


PLAYBOOK_ENGINE_VERSION = "playbook-engine-0.1.0"


class PlaybookValidationIssue(ModelBase):
    """Playbook 或规则包发布门禁中的一条问题。"""

    code: str
    message: str = Field(min_length=1)
    severity: str = "ERROR"
    rule_id: str | None = None
    playbook_id: str | None = None


class PlaybookValidationReport(ModelBase):
    """规则包发布前的确定性校验报告。"""

    bundle_id: str
    review_schema_version: str
    compatible: bool
    valid: bool
    playbook_count: int = 0
    issues: list[PlaybookValidationIssue] = Field(default_factory=list)


class PlaybookReleaseError(ValueError):
    """Playbook 未通过发布或版本兼容门禁时抛出。"""


class PlaybookEvaluation(ModelBase):
    """单条 Playbook 评估的证据化输出。"""

    status: FindingStatus
    reason: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    clause_ids: list[str] = Field(default_factory=list)
    action: PlaybookAction | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    comparison: dict[str, object] | None = None
    uncertainty_reason: str | None = None


def _release_fingerprint(bundle: RuleBundle) -> str:
    """计算不包含运行时发布时间的规则包发布指纹。"""

    payload = bundle.model_dump(
        mode="json",
        exclude={"imported_at", "published_at", "release_fingerprint", "release_status"},
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def validate_playbook_spec(playbook: PlaybookSpec) -> list[PlaybookValidationIssue]:
    """校验单个 Playbook 的立场、动作和评估模式。"""

    issues: list[PlaybookValidationIssue] = []
    normalized_positions: dict[str, set[str]] = {
        "preferred": {
            _normalise(playbook.preferred_position)
        }
        if playbook.preferred_position
        else set(),
        "fallback": {
            _normalise(item)
            for item in playbook.fallback_positions
            if _normalise(item)
        },
        "prohibited": {
            _normalise(item)
            for item in playbook.prohibited_positions
            if _normalise(item)
        },
    }
    if not playbook.playbook_id.strip() or not playbook.version.strip():
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_ID_VERSION_REQUIRED",
                message="Playbook 必须同时提供非空 playbook_id 和 version。",
                playbook_id=playbook.playbook_id or None,
            )
        )
    if playbook.evaluation_mode == "position":
        if not playbook.has_deterministic_positions:
            issues.append(
                PlaybookValidationIssue(
                    code="PLAYBOOK_POSITION_REQUIRED",
                    message="position 模式至少需要条款类型或一种可识别立场。",
                    playbook_id=playbook.playbook_id,
                )
            )
    elif not playbook.clause_types:
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_CHECKER_CLAUSE_TYPES_REQUIRED",
                message="checker 模式必须声明用于事实和证据筛选的 clause_types。",
                playbook_id=playbook.playbook_id,
            )
        )
    if normalized_positions["preferred"] & normalized_positions["prohibited"]:
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_POSITION_CONFLICT",
                message="preferred_position 不能同时出现在 prohibited_positions。",
                playbook_id=playbook.playbook_id,
            )
        )
    if normalized_positions["fallback"] & normalized_positions["prohibited"]:
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_FALLBACK_PROHIBITED_CONFLICT",
                message="fallback_positions 不能与 prohibited_positions 重叠。",
                playbook_id=playbook.playbook_id,
            )
        )
    if (
        playbook.action_on_fallback == PlaybookAction.REVISE
        and not (playbook.suggested_language or "").strip()
    ):
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_SUGGESTION_REQUIRED",
                message="fallback 动作为 REVISE 时必须提供 suggested_language。",
                playbook_id=playbook.playbook_id,
            )
        )
    threshold_ids = [item.threshold_id for item in playbook.escalation_thresholds]
    if len(threshold_ids) != len(set(threshold_ids)):
        issues.append(
            PlaybookValidationIssue(
                code="PLAYBOOK_THRESHOLD_ID_DUPLICATE",
                message="Playbook escalation_thresholds 的 threshold_id 必须唯一。",
                playbook_id=playbook.playbook_id,
            )
        )
    return issues


def validate_playbook_bundle(
    bundle: RuleBundle,
    *,
    review_schema_version: str = "2.0",
    require_published: bool = False,
) -> PlaybookValidationReport:
    """校验规则包、Playbook 版本和当前审核 Schema 的兼容性。"""

    issues: list[PlaybookValidationIssue] = []
    compatible = bundle.compatible_review_schema == review_schema_version
    if not compatible:
        issues.append(
            PlaybookValidationIssue(
                code="REVIEW_SCHEMA_INCOMPATIBLE",
                message=(
                    f"规则包兼容 {bundle.compatible_review_schema}，"
                    f"当前审核 Schema 为 {review_schema_version}。"
                ),
            )
        )
    if require_published and bundle.release_status != RuleBundleStatus.PUBLISHED:
        issues.append(
            PlaybookValidationIssue(
                code="RULE_BUNDLE_NOT_PUBLISHED",
                message=f"规则包当前状态为 {bundle.release_status.value}，不能用于正式审查。",
            )
        )
    if bundle.release_status == RuleBundleStatus.PUBLISHED:
        expected_fingerprint = _release_fingerprint(bundle)
        if not bundle.release_fingerprint or not bundle.published_at:
            issues.append(
                PlaybookValidationIssue(
                    code="PUBLISHED_METADATA_REQUIRED",
                    message="正式规则包必须同时提供 release_fingerprint 和 published_at。",
                )
            )
        elif bundle.release_fingerprint != expected_fingerprint:
            issues.append(
                PlaybookValidationIssue(
                    code="RELEASE_FINGERPRINT_MISMATCH",
                    message="规则包 release_fingerprint 与当前规则内容不一致。",
                )
            )
    elif bundle.release_status != RuleBundleStatus.PUBLISHED and (
        bundle.release_fingerprint is not None or bundle.published_at is not None
    ):
        issues.append(
            PlaybookValidationIssue(
                code="UNPUBLISHED_METADATA_INVALID",
                message="非 published 规则包不能携带正式发布指纹或发布时间。",
            )
        )
    rule_ids: set[str] = set()
    playbooks: dict[tuple[str, str], PlaybookSpec] = {}
    for rule in bundle.rules:
        if rule.rule_id in rule_ids:
            issues.append(
                PlaybookValidationIssue(
                    code="DUPLICATE_RULE_ID",
                    message=f"规则包包含重复 rule_id：{rule.rule_id}。",
                    rule_id=rule.rule_id,
                )
            )
        rule_ids.add(rule.rule_id)
        playbook = rule.playbook
        if playbook is None:
            if rule.checker is not None:
                from .rule_checkers import is_supported_checker

                if not is_supported_checker(rule.checker):
                    issues.append(
                        PlaybookValidationIssue(
                            code="UNSUPPORTED_CHECKER",
                            message=f"规则声明了未注册的 checker：{rule.checker}。",
                            rule_id=rule.rule_id,
                        )
                    )
            continue
        if playbook.evaluation_mode == "checker" and rule.checker is None:
            issues.append(
                PlaybookValidationIssue(
                    code="PLAYBOOK_CHECKER_BINDING_REQUIRED",
                    message="checker 模式 Playbook 必须绑定规则 checker。",
                    rule_id=rule.rule_id,
                    playbook_id=playbook.playbook_id,
                )
            )
        if rule.checker is not None:
            from .rule_checkers import is_supported_checker

            if not is_supported_checker(rule.checker):
                issues.append(
                    PlaybookValidationIssue(
                        code="UNSUPPORTED_CHECKER",
                        message=f"规则声明了未注册的 checker：{rule.checker}。",
                        rule_id=rule.rule_id,
                    )
                )
        for issue in validate_playbook_spec(playbook):
            issues.append(issue.model_copy(update={"rule_id": rule.rule_id}))
        key = (playbook.playbook_id, playbook.version)
        previous = playbooks.get(key)
        if previous is not None and previous != playbook:
            issues.append(
                PlaybookValidationIssue(
                    code="PLAYBOOK_VERSION_CONFLICT",
                    message=(
                        f"同一 Playbook 版本存在不同定义："
                        f"{playbook.playbook_id}/{playbook.version}。"
                    ),
                    rule_id=rule.rule_id,
                    playbook_id=playbook.playbook_id,
                )
            )
        playbooks[key] = playbook
    return PlaybookValidationReport(
        bundle_id=bundle.bundle_id,
        review_schema_version=review_schema_version,
        compatible=compatible,
        valid=not issues,
        playbook_count=len(playbooks),
        issues=issues,
    )


def assert_playbook_bundle_compatible(
    bundle: RuleBundle,
    *,
    review_schema_version: str = "2.0",
    require_published: bool = True,
) -> None:
    """在审查或发布入口阻断未验证、未发布或不兼容规则包。"""

    report = validate_playbook_bundle(
        bundle,
        review_schema_version=review_schema_version,
        require_published=require_published,
    )
    if not report.valid:
        messages = "；".join(issue.message for issue in report.issues[:5])
        raise PlaybookReleaseError(
            f"规则包 {bundle.bundle_id} 未通过 Playbook 门禁：{messages}"
        )


def publish_playbook_bundle(
    bundle: RuleBundle,
    *,
    review_schema_version: str = "2.0",
) -> RuleBundle:
    """为通过校验的规则包生成发布指纹并返回不可变发布快照。

    函数只返回新对象，不改写源文件。源文件的提交、评审和发布由版本化
    规则仓库负责，运行时不能把草稿直接覆盖正式规则。
    """

    assert_playbook_bundle_compatible(
        bundle,
        review_schema_version=review_schema_version,
        require_published=False,
    )
    fingerprint = _release_fingerprint(bundle)
    return bundle.model_copy(
        update={
            "release_status": RuleBundleStatus.PUBLISHED,
            "release_fingerprint": fingerprint,
            "published_at": utc_now(),
        }
    )


def _normalise(value: str | None) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains(text: str, position: str | None) -> bool:
    needle = _normalise(position)
    return bool(needle and needle in text)


def _matching_clauses(
    clauses: Sequence[ContractClause], clause_types: Sequence[str]
) -> list[ContractClause]:
    if not clause_types:
        return list(clauses)
    wanted = [_normalise(item) for item in clause_types if _normalise(item)]
    if not wanted:
        return list(clauses)
    return [
        clause
        for clause in clauses
        if any(
            term in _normalise(f"{clause.title} {clause.text}")
            for term in wanted
        )
    ]


def _position_match(
    clauses: Sequence[ContractClause], positions: Sequence[str]
) -> tuple[str, ContractClause] | None:
    for position in positions:
        for clause in clauses:
            if _contains(clause.text, position):
                return position, clause
    return None


_RISK_LEVEL_VALUES = {
    RiskLevel.UNCLASSIFIED: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


def _decimal_fact_value(fact: ContractFact) -> object:
    """返回可用于阈值比较的事实值，非法值交给调用方视为不可用。"""

    return fact.normalized_value if fact.normalized_value is not None else fact.value


def _threshold_actual(
    threshold,
    *,
    rule: Rule,
    review_context: ReviewContext,
    facts: Sequence[ContractFact],
) -> object:
    """从结构化上下文或候选事实取得阈值左值，不从自然语言原因反解析。"""

    if threshold.metric == "transaction_amount":
        return review_context.transaction_amount
    if threshold.metric == "payment_ratio":
        values = []
        for fact in facts:
            if fact.fact_type != "financial.payment_ratio":
                continue
            try:
                values.append(Decimal(str(_decimal_fact_value(fact))))
            except (InvalidOperation, TypeError, ValueError):
                continue
        return sum(values, Decimal("0")) if values else None
    if threshold.metric == "confidence":
        values = [
            Decimal(str(confidence))
            for fact in facts
            if (confidence := fact.confidence) is not None
        ]
        return min(values) if values else None
    if threshold.metric == "risk_level":
        return _RISK_LEVEL_VALUES.get(rule.risk_level or RiskLevel.UNCLASSIFIED)
    return None


def _threshold_matches(actual: object, threshold) -> bool:
    """按结构化数值执行升级阈值；未形成左值时返回未命中。"""

    if actual is None:
        return False
    operators = {
        ">": lambda left, right: left > right,
        ">=": lambda left, right: left >= right,
        "<": lambda left, right: left < right,
        "<=": lambda left, right: left <= right,
        "==": lambda left, right: left == right,
    }
    return operators[threshold.operator](actual, threshold.value)


def _missing_evaluation(
    rule: Rule,
    *,
    policy: MissingClausePolicy,
    evidence_ids: Sequence[str],
    reason: str,
) -> PlaybookEvaluation:
    status = {
        MissingClausePolicy.WARN: FindingStatus.WARN,
        MissingClausePolicy.BLOCK: FindingStatus.BLOCK,
        MissingClausePolicy.UNKNOWN: FindingStatus.UNKNOWN,
        MissingClausePolicy.NOT_APPLICABLE: FindingStatus.NOT_APPLICABLE,
    }[policy]
    action = {
        MissingClausePolicy.WARN: PlaybookAction.REQUEST_INFORMATION,
        MissingClausePolicy.BLOCK: PlaybookAction.ESCALATE,
        MissingClausePolicy.UNKNOWN: PlaybookAction.REQUEST_INFORMATION,
        MissingClausePolicy.NOT_APPLICABLE: None,
    }[policy]
    return PlaybookEvaluation(
        status=status,
        reason=reason,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        action=action,
        confidence=0.0 if status == FindingStatus.UNKNOWN else 1.0,
        uncertainty_reason=(
            "未找到可验证的目标条款或必备条款证据。"
            if status in {FindingStatus.UNKNOWN, FindingStatus.WARN, FindingStatus.BLOCK}
            else None
        ),
        comparison={
            "playbook_id": rule.playbook.playbook_id if rule.playbook else None,
            "match_kind": "missing",
            "suggested_language": (
                rule.playbook.suggested_language if rule.playbook else None
            ),
            "escalation_condition": (
                rule.playbook.escalation_condition if rule.playbook else None
            ),
        },
    )


def evaluate_playbook_rule(
    rule: Rule,
    clauses: Sequence[ContractClause],
    *,
    candidate_evidence: Sequence[CandidateEvidence],
    facts: Sequence[ContractFact] = (),
    review_context: ReviewContext,
    default_evidence_ids: Sequence[str],
) -> PlaybookEvaluation | None:
    """评估带有确定性 Playbook 配置的规则。

    返回 ``None`` 表示规则仍使用原有语义，由现有确定性或模型检查器处理。
    返回评估结果后，该规则由 Playbook 结果负责，并可在优选、备选或禁止
    立场被原文证据直接命中时不依赖外部模型。
    """

    playbook = rule.playbook
    if playbook is None:
        return None

    evidence_ids = list(default_evidence_ids)
    evidence_ids.extend(
        evidence_id
        for candidate in candidate_evidence or ()
        for evidence_id in candidate.evidence_ids
    )
    evidence_ids = list(dict.fromkeys(evidence_ids))
    if not evidence_ids:
        raise ValueError(f"Playbook 规则缺少证据边界: {rule.rule_id}")

    # 升级阈值属于 Playbook 的结构化门禁，即使最终由 checker 执行，
    # 也必须先检查，不能因评价模式不同而静默跳过。
    relevant = _matching_clauses(clauses, playbook.clause_types)
    candidate_clause_id_set = {
        clause_id
        for candidate in candidate_evidence
        for clause_id in candidate.clause_ids
    }
    relevant = [
        clause for clause in relevant if clause.clause_id in candidate_clause_id_set
    ]
    evidence_ids.extend(
        evidence_id for clause in relevant for evidence_id in clause.evidence_ids
    )
    evidence_ids = list(dict.fromkeys(evidence_ids))
    if not evidence_ids:
        raise ValueError(f"Playbook 规则缺少证据边界: {rule.rule_id}")

    if not relevant:
        if not playbook.has_deterministic_positions:
            # checker 自己负责缺失条款的处置；不能让阈值缺少上下文把
            # 更具体的 BLOCK/UNKNOWN 缺失条款结论遮住。
            return None
        return _missing_evaluation(
            rule,
            policy=playbook.missing_clause_policy,
            evidence_ids=evidence_ids,
            reason=(
                f"未找到 Playbook 要求的条款类型："
                f"{', '.join(playbook.clause_types) or rule.title}。"
            ),
        )

    triggered_thresholds = []
    missing_threshold_context: list[tuple[object, object]] = []
    for threshold in playbook.escalation_thresholds:
        actual = _threshold_actual(
            threshold,
            rule=rule,
            review_context=review_context,
            facts=facts,
        )
        if actual is None:
            missing_threshold_context.append((threshold, actual))
        elif _threshold_matches(actual, threshold):
            triggered_thresholds.append((threshold, actual))
    if triggered_thresholds:
        threshold, actual = triggered_thresholds[0]
        return PlaybookEvaluation(
            status=FindingStatus.UNKNOWN,
            reason=f"命中 Playbook 升级阈值“{threshold.threshold_id}”：{threshold.reason}",
            evidence_ids=evidence_ids,
            clause_ids=[clause.clause_id for clause in relevant],
            action=threshold.action,
            confidence=0.0,
            uncertainty_reason="playbook_escalation_threshold",
            comparison={
                "playbook_id": playbook.playbook_id,
                "match_kind": "escalation_threshold",
                "threshold_id": threshold.threshold_id,
                "metric": threshold.metric,
                "operator": threshold.operator,
                "threshold": str(threshold.value),
                "actual": str(actual),
                "retrigger_required": True,
            },
        )
    if missing_threshold_context:
        threshold, _ = missing_threshold_context[0]
        return PlaybookEvaluation(
            status=FindingStatus.UNKNOWN,
            reason=(
                f"Playbook 升级阈值“{threshold.threshold_id}”缺少可计算的结构化字段，"
                "不能自动放行。"
            ),
            evidence_ids=evidence_ids,
            clause_ids=[clause.clause_id for clause in relevant],
            action=threshold.action,
            confidence=0.0,
            uncertainty_reason="playbook_threshold_context_missing",
            comparison={
                "playbook_id": playbook.playbook_id,
                "match_kind": "threshold_context_missing",
                "threshold_id": threshold.threshold_id,
                "metric": threshold.metric,
                "operator": threshold.operator,
                "threshold": str(threshold.value),
                "actual": None,
                "retrigger_required": True,
                "suggested_language": playbook.suggested_language,
                "escalation_condition": playbook.escalation_condition,
            },
        )

    if not playbook.has_deterministic_positions:
        return None

    prohibited = _position_match(relevant, playbook.prohibited_positions)
    if prohibited is not None:
        position, clause = prohibited
        return PlaybookEvaluation(
            status=FindingStatus.BLOCK,
            reason=f"条款命中禁止立场“{position}”，不符合企业 Playbook 底线。",
            evidence_ids=list(dict.fromkeys([*evidence_ids, *clause.evidence_ids])),
            clause_ids=[clause.clause_id],
            action=playbook.action_on_prohibited,
            confidence=1.0,
            comparison={
                "playbook_id": playbook.playbook_id,
                "match_kind": "prohibited",
                "matched_position": position,
                "suggested_language": playbook.suggested_language,
                "escalation_condition": playbook.escalation_condition,
            },
        )

    preferred = _position_match(
        relevant,
        [playbook.preferred_position] if playbook.preferred_position else [],
    )
    if preferred is not None:
        position, clause = preferred
        return PlaybookEvaluation(
            status=FindingStatus.PASS,
            reason=f"条款命中 Playbook 优选立场“{position}”。",
            evidence_ids=list(dict.fromkeys([*evidence_ids, *clause.evidence_ids])),
            clause_ids=[clause.clause_id],
            action=playbook.action_on_preferred,
            confidence=1.0,
            comparison={
                "playbook_id": playbook.playbook_id,
                "match_kind": "preferred",
                "matched_position": position,
                "suggested_language": playbook.suggested_language,
                "escalation_condition": playbook.escalation_condition,
            },
        )

    fallback = _position_match(relevant, playbook.fallback_positions)
    if fallback is not None:
        position, clause = fallback
        return PlaybookEvaluation(
            status=FindingStatus.WARN,
            reason=f"条款命中 Playbook 备选立场“{position}”，需要确认是否接受或改回优选文本。",
            evidence_ids=list(dict.fromkeys([*evidence_ids, *clause.evidence_ids])),
            clause_ids=[clause.clause_id],
            action=playbook.action_on_fallback,
            confidence=1.0,
            comparison={
                "playbook_id": playbook.playbook_id,
                "match_kind": "fallback",
                "matched_position": position,
                "suggested_language": playbook.suggested_language,
                "escalation_condition": playbook.escalation_condition,
            },
        )

    return PlaybookEvaluation(
        status=FindingStatus.UNKNOWN,
        reason="已定位相关条款，但其文本不属于 Playbook 已配置的优选、备选或禁止立场。",
        evidence_ids=evidence_ids,
        clause_ids=[clause.clause_id for clause in relevant],
        action=PlaybookAction.ESCALATE,
        confidence=0.0,
        uncertainty_reason="条款存在但未命中已配置立场，不能自动视为可接受。",
        comparison={
            "playbook_id": playbook.playbook_id,
            "match_kind": "unrecognised_position",
            "suggested_language": playbook.suggested_language,
            "escalation_condition": playbook.escalation_condition,
        },
    )
