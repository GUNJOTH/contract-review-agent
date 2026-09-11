"""版本化合同审查 Playbook 的确定性评估。

评估器只使用明确配置的立场和条款证据做自动判断。无法识别的条款保持
``UNKNOWN``，不会因为模型或关键词没有报错就被误判为通过。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from .models import (
    ContractClause,
    FindingStatus,
    MissingClausePolicy,
    ModelBase,
    PlaybookAction,
    Rule,
)


PLAYBOOK_ENGINE_VERSION = "playbook-engine-0.1.0"


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
    default_evidence_ids: Sequence[str],
) -> PlaybookEvaluation | None:
    """评估带有确定性 Playbook 配置的规则。

    返回 ``None`` 表示规则仍使用原有语义，由现有确定性或模型检查器处理。
    返回评估结果后，该规则由 Playbook 结果负责，并可在优选、备选或禁止
    立场被原文证据直接命中时不依赖外部模型。
    """

    playbook = rule.playbook
    if playbook is None or not playbook.has_deterministic_positions:
        return None

    relevant = _matching_clauses(clauses, playbook.clause_types)
    evidence_ids = list(default_evidence_ids)
    evidence_ids.extend(
        evidence_id for clause in relevant for evidence_id in clause.evidence_ids
    )
    evidence_ids = list(dict.fromkeys(evidence_ids))
    if not evidence_ids:
        raise ValueError(f"Playbook 规则缺少证据边界: {rule.rule_id}")

    if not relevant:
        return _missing_evaluation(
            rule,
            policy=playbook.missing_clause_policy,
            evidence_ids=evidence_ids,
            reason=(
                f"未找到 Playbook 要求的条款类型："
                f"{', '.join(playbook.clause_types) or rule.title}。"
            ),
        )

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
