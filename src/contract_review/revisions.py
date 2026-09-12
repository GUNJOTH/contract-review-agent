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
from .replay import build_result_fingerprint


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

    from .audit import audit_result

    audit = audit_result(result)
    if not audit.passed:
        raise ValueError(
            "审查结果未通过完整性门禁，不能生成修订提案："
            + "；".join(audit.issues[:3])
        )
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


def attach_revision_set(
    result: ReviewResult,
    revision: ContractRevisionSet,
) -> ReviewResult:
    """把修订提案作为核心结果附件保存，不直接修改合同正文。"""

    from .audit import audit_result

    audit = audit_result(result)
    if not audit.passed:
        raise ValueError(
            "审查结果未通过完整性门禁，不能挂载修订提案："
            + "；".join(audit.issues[:3])
        )
    if revision.run_id != result.run.run_id:
        raise ValueError("revision set belongs to a different review run")
    if revision.base_result_fingerprint != result.run.result_fingerprint:
        raise ValueError(
            "revision set base_result_fingerprint does not match current ReviewResult"
        )
    if any(item.revision_id == revision.revision_id for item in result.revision_sets):
        raise ValueError(f"revision set already exists: {revision.revision_id}")
    revisions = [*result.revision_sets, revision]
    revision_ids = [item.revision_id for item in revisions]
    sequence = [*result.post_review_sequence, f"revision:{revision.revision_id}"]
    run = result.run.model_copy(update={"revision_ids": revision_ids})
    report = result.report.model_copy(update={"revision_ids": revision_ids})
    updated = result.model_copy(
        update={
            "revision_sets": revisions,
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
            "挂载修订提案后未通过完整性门禁："
            + "；".join(final_audit.issues[:3])
        )
    return updated
