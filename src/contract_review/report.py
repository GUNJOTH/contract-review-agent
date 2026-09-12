"""Human-readable report rendering without losing the evidence graph."""

from __future__ import annotations

from pathlib import Path

from .audit import audit_result
from .models import Evidence, Finding, ReviewResult

REPORT_RENDERER_VERSION = "markdown-review-report-0.2.0"


def _markdown_text(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("\n", "  \n")


def _evidence_location(evidence: Evidence, filenames: dict[str, str]) -> str:
    locator = evidence.locator
    if evidence.document_id:
        filename = filenames.get(evidence.document_id, evidence.document_id)
        if locator.page_number is not None:
            location = f"{filename} 第 {locator.page_number} 页"
            if locator.block_id:
                location += f"，块 {locator.block_id}"
            if locator.char_start is not None and locator.char_end is not None:
                location += f"，字符 {locator.char_start}-{locator.char_end}"
            return location
        if locator.cell_reference:
            sheet = f"工作表 {locator.sheet_name} " if locator.sheet_name else ""
            return f"{filename} {sheet}表格单元格 {locator.cell_reference}"
        if locator.paragraph_index is not None:
            return f"{filename} 段落 {locator.paragraph_index}"
        return filename
    if locator.missing_name:
        return f"缺失附件：{locator.missing_name}"
    if locator.external_uri:
        return locator.external_uri
    return "跨文件/包级证据"


def _render_evidence(evidence: Evidence, filenames: dict[str, str]) -> list[str]:
    lines = [
        f"- `{evidence.evidence_id}` · `{evidence.evidence_type.value}` · "
        f"{_markdown_text(_evidence_location(evidence, filenames))}"
    ]
    if evidence.raw_excerpt:
        lines.append(f"  - 原文：{_markdown_text(evidence.raw_excerpt)}")
    if evidence.display_excerpt and evidence.display_excerpt != evidence.raw_excerpt:
        lines.append(f"  - 展示：{_markdown_text(evidence.display_excerpt)}")
    lines.append(
        f"  - 提取：`{evidence.extraction_method}` / `{evidence.extraction_version}`；"
        f"置信度：{evidence.confidence if evidence.confidence is not None else '未提供'}"
    )
    return lines


def _finding_evidence(
    finding: Finding, evidence_by_id: dict[str, Evidence], filenames: dict[str, str]
) -> list[str]:
    lines = ["证据："]
    for evidence_id in finding.evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            lines.append(f"- `{evidence_id}`（证据缺失，审计应失败）")
            continue
        lines.extend(_render_evidence(evidence, filenames))
    return lines


def render_markdown_report(result: ReviewResult) -> str:
    """Render a review report for human review while preserving evidence IDs."""

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    filenames = {item.document_id: item.filename for item in result.documents}
    audit = audit_result(result)
    lines = [
        f"# 合同审查报告（{result.report.report_id}）",
        "",
        f"- 审查运行：`{result.run.run_id}`",
        f"- 合同包：`{result.package.package_id}`",
        f"- 运行状态：`{result.run.status.value}`",
        f"- 总体结论：`{result.report.overall_status.value}`",
        f"- 是否需要人工确认：`{'是' if result.report.review_required else '否'}`",
        f"- 报告渲染器：`{REPORT_RENDERER_VERSION}`",
        f"- 审计门禁：`{'PASS' if audit.passed else 'FAIL'}`",
        "",
        "## 结果统计",
        "",
    ]
    for status, count in sorted(result.report.finding_counts.items()):
        lines.append(f"- `{status}`：{count}")
    lines.extend(
        [
            "",
            "## 合同结构与履约义务",
            "",
            f"- 条款片段：{len(result.clauses)}",
            f"- 履约义务：{len(result.obligations)}",
            "",
        ]
    )
    for obligation in result.obligations:
        subject = obligation.obligor or "主体待确认"
        deadline = obligation.deadline or "期限待确认"
        lines.append(
            f"- `{obligation.obligation_id}` · `{obligation.modality.value}` · "
            f"{_markdown_text(subject)}：{_markdown_text(obligation.action)}；"
            f"{_markdown_text(deadline)}；证据 "
            f"{', '.join(f'`{item}`' for item in obligation.evidence_ids)}"
        )
    if not result.obligations:
        lines.append("- 未识别到可由确定性规则确认的中文履约义务，需人工或模型复核。")
    lines.extend(["", "## 条款关系", ""])
    clause_labels = {
        clause.clause_id: clause.clause_number or clause.title
        for clause in result.clauses
    }
    if result.clause_relations:
        for relation in result.clause_relations:
            source_label = clause_labels.get(
                relation.source_clause_id, relation.source_clause_id
            )
            target_label = (
                clause_labels.get(relation.target_clause_id, relation.target_clause_id)
                if relation.target_clause_id
                else relation.target_label
            )
            lines.append(
                f"- `{relation.relation_type.value}`：{_markdown_text(source_label)}"
                f" → {_markdown_text(target_label)}；"
                f"解析：`{relation.resolution.value}`；证据 "
                f"{', '.join(f'`{item}`' for item in relation.evidence_ids)}"
            )
    else:
        lines.append("- 未识别到可由确定性规则确认的条款关系。")
    lines.extend(["", "## 逐条发现", ""])

    decisions_by_finding = {
        decision.finding_id: decision for decision in result.decisions
    }
    assessments_by_finding = {
        assessment.finding_id: assessment for assessment in result.question_assessments
    }
    for index, finding in enumerate(result.findings, start=1):
        assessment = assessments_by_finding.get(finding.finding_id)
        lines.extend(
            [
                f"### {index}. {_markdown_text(finding.title)}",
                "",
                f"- 规则：`{finding.rule_id}` / `{finding.rule_version}`",
                f"- 状态：`{finding.status.value}`",
                "- 问题结论："
                + (
                    f"`{assessment.outcome.value}`"
                    if assessment is not None
                    else "缺失（审计应失败）"
                ),
                f"- 风险级别：`{finding.risk_level.value}`",
                f"- 置信度：`{finding.confidence if finding.confidence is not None else '未提供'}`",
                f"- 证据质量：`{finding.evidence_quality.value}`；自动采纳：`{'是' if finding.automatic else '否'}`",
                f"- 原因：{_markdown_text(finding.reason)}",
            ]
        )
        if finding.recommended_action:
            lines.append(f"- 建议动作：{_markdown_text(finding.recommended_action)}")
        if finding.fact_ids:
            lines.append(
                f"- 事实：{', '.join(f'`{item}`' for item in finding.fact_ids)}"
            )
        lines.extend(_finding_evidence(finding, evidence_by_id, filenames))
        decision = decisions_by_finding.get(finding.finding_id)
        if decision is not None:
            lines.extend(
                [
                    "人工决定：",
                    f"- `{decision.decision.value}`，审核人 `{decision.actor_id}`（{decision.actor_role}）",
                    f"- 说明：{_markdown_text(decision.comment)}",
                    f"- 决定证据：{', '.join(f'`{item}`' for item in decision.evidence_ids)}",
                ]
            )
        else:
            lines.append("人工决定：尚未记录。")
        lines.append("")

    if result.version_comparisons:
        lines.extend(["## 合同版本比对", ""])
        for comparison in result.version_comparisons:
            lines.append(
                f"- `{comparison.comparison_id}`："
                f"{_markdown_text(comparison.base_filename)} → "
                f"{_markdown_text(comparison.compare_filename)}；"
                f"相似度 {comparison.similarity:.2%}；"
                f"新增 {comparison.added}、删除 {comparison.deleted}、"
                f"修改 {comparison.modified}；证据 "
                f"{', '.join(f'`{item}`' for item in comparison.evidence_ids)}"
            )
            for change in comparison.changes:
                lines.append(
                    f"  - `{change.kind.value}` `{change.change_id}`："
                    f"基准：{_markdown_text(change.base_text) or '（无）'}；"
                    f"比对：{_markdown_text(change.compare_text) or '（无）'}；"
                    f"条款 {', '.join(f'`{item}`' for item in change.clause_ids) or '未定位'}；"
                    f"证据 {', '.join(f'`{item}`' for item in change.evidence_ids)}"
                )
            if comparison.impacts:
                lines.append(
                    "  - 业务影响："
                    f"重触发规则 {', '.join(f'`{item}`' for item in comparison.retrigger_rule_ids) or '无'}"
                )
                for impact in comparison.impacts:
                    lines.append(
                        f"    - `{impact.impact_id}`"
                        + (
                            f" · 规则 `{impact.rule_id}`"
                            if impact.rule_id
                            else " · 未映射规则"
                        )
                        + f" · 业务义务：{_markdown_text('；'.join(impact.changed_obligations))}"
                        + f" · 付款风险 `{impact.payment_risk.value}`"
                        + f" · 责任风险 `{impact.liability_risk.value}`"
                        + f" · 责任上限变化 `{impact.liability_cap_impact.value}`"
                        + f" · 交付/验收绑定 `{impact.delivery_acceptance_binding.value}`"
                        + f" · 文件优先 `{impact.precedence_resolution}`"
                        + f" · Playbook 重审：{'是' if impact.playbook_retrigger_required else '否'}"
                        + f" · {_markdown_text(impact.reason)}"
                    )

    if result.revision_sets:
        lines.extend(["", "## 红线/修订建议", ""])
        for revision in result.revision_sets:
            lines.append(
                f"- `{revision.revision_id}` · `{revision.status}`；"
                f"来源 `{revision.source_version}`；变更 {len(revision.changes)} 条"
            )
            for change in revision.changes:
                lines.append(
                    f"  - `{change.operation.value}` · 发现 `{change.finding_id}`"
                    + (
                        f" · 条款 `{change.clause_id}`"
                        if change.clause_id
                        else ""
                    )
                    + f"；原文：{_markdown_text(change.original_text)}"
                    + f"；建议：{_markdown_text(change.proposed_text)}"
                    + f"；证据 {', '.join(f'`{item}`' for item in change.evidence_ids)}"
                )

    lines.extend(
        [
            "## 审计与回放",
            "",
            f"- 输入指纹：`{result.run.configuration_fingerprint}`",
            f"- 结果指纹：`{result.run.result_fingerprint or '未生成'}`",
            f"- 证据数量：{len(result.evidence)}",
            f"- 条款片段数量：{len(result.clauses)}",
            f"- 条款关系数量：{len(result.clause_relations)}",
            f"- 履约义务数量：{len(result.obligations)}",
            f"- 审查问题数量：{len(result.review_questions)}",
            f"- 知识片段数量：{len(result.knowledge_chunks)}",
            f"- 检索轨迹数量：{len(result.retrieval_traces)}",
            f"- 版本比对数量：{len(result.version_comparisons)}",
            f"- 红线/修订集合数量：{len(result.revision_sets)}",
        ]
    )
    if not audit.passed:
        lines.extend(
            ["", "### 审计问题", "", *[f"- {issue}" for issue in audit.issues]]
        )
    return "\n".join(lines) + "\n"


def write_markdown_report(result: ReviewResult, path: str | Path) -> Path:
    """Write a human-readable report; the JSON audit artifact remains authoritative."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown_report(result), encoding="utf-8")
    return target
