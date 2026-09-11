"""从证据化审查结果生成可人工确认的条款修订提案。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from .models import (
    ContractRevisionSet,
    FindingStatus,
    PlaybookAction,
    RevisionChange,
    RevisionOperation,
    ReviewResult,
)


REVISION_BUILDER_VERSION = "revision-builder-0.1.0"


def _clause_for_finding(result: ReviewResult, finding) -> object | None:
    clauses = {clause.clause_id: clause for clause in result.clauses}
    for clause_id in finding.clause_ids:
        clause = clauses.get(clause_id)
        if clause is not None:
            return clause
    finding_evidence = set(finding.evidence_ids)
    return next(
        (
            clause
            for clause in result.clauses
            if finding_evidence.intersection(clause.evidence_ids)
        ),
        None,
    )


def _action_for_finding(finding) -> PlaybookAction | None:
    if finding.action is not None:
        return finding.action
    raw = str(finding.recommended_action or "").strip().upper()
    try:
        return PlaybookAction(raw)
    except ValueError:
        return None


def _operation_for_finding(finding, proposed_text: str) -> RevisionOperation:
    action = _action_for_finding(finding)
    if action == PlaybookAction.REVISE and proposed_text:
        return RevisionOperation.REPLACE
    if action == PlaybookAction.REQUEST_INFORMATION and not proposed_text:
        return RevisionOperation.COMMENT
    return RevisionOperation.COMMENT


def _fingerprint(changes: Iterable[RevisionChange], base: str) -> str:
    payload = {
        "builder_version": REVISION_BUILDER_VERSION,
        "base_result_fingerprint": base,
        "changes": [item.model_dump(mode="json") for item in changes],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_revision_set(
    result: ReviewResult,
    *,
    include_statuses: Iterable[FindingStatus] = (
        FindingStatus.BLOCK,
        FindingStatus.WARN,
        FindingStatus.UNKNOWN,
    ),
) -> ContractRevisionSet:
    """把需要人工确认的发现转换为条款级修订/评论提案。

    该函数不会修改合同文件，也不会把模型建议直接当成最终文本。只有
    Playbook 明确提供 ``suggested_language`` 且动作是 REVISE 时，才输出
    ``REPLACE``；其他风险统一输出 COMMENT，等待人工确认。
    """

    base_fingerprint = result.run.result_fingerprint
    if not base_fingerprint:
        raise ValueError("审查结果没有 result_fingerprint，不能生成修订提案")
    status_set = set(include_statuses)
    changes: list[RevisionChange] = []
    for finding in result.findings:
        if finding.status not in status_set:
            continue
        if not finding.evidence_ids:
            raise ValueError(f"发现 {finding.finding_id} 没有证据，不能生成修订提案")
        clause = _clause_for_finding(result, finding)
        comparison = finding.comparison or {}
        proposed_text = str(comparison.get("suggested_language") or "").strip()
        original_text = str(getattr(clause, "text", "") or "")
        action = _action_for_finding(finding)
        reason = finding.reason
        if action == PlaybookAction.ESCALATE and comparison.get("escalation_condition"):
            reason = f"{reason} 升级条件：{comparison['escalation_condition']}"
        changes.append(
            RevisionChange(
                change_id=f"change-{finding.finding_id}",
                finding_id=finding.finding_id,
                clause_id=getattr(clause, "clause_id", None),
                operation=_operation_for_finding(finding, proposed_text),
                original_text=original_text,
                proposed_text=proposed_text,
                reason=reason,
                evidence_ids=list(dict.fromkeys(finding.evidence_ids)),
            )
        )
    revision_fingerprint = _fingerprint(changes, base_fingerprint)
    return ContractRevisionSet(
        revision_id=f"revision-{result.run.run_id}-{revision_fingerprint[:16]}",
        run_id=result.run.run_id,
        base_result_fingerprint=base_fingerprint,
        source_version=REVISION_BUILDER_VERSION,
        changes=changes,
        revision_fingerprint=revision_fingerprint,
    )
