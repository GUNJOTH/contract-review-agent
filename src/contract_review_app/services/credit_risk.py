"""客商风险视图：合同主体 + 主体相关审查发现 + 外部核验项清单。

**当前状态：企业征信数据源尚未接入。** 所以这里刻意分成两层，而不是给一个
好看的"未发现风险"：

1. **本地能给的**：合同主体（甲方/乙方，取自标准要素事实，带来源与置信度）
   以及与合同主体相关的审查发现。这些完全来自已有的 ``ReviewResult``。
2. **必须外部数据源才能给的**：注册资本、经营状态、涉诉与被执行、失信与严重
   违法、信用评级与资质。这些在数据源接入前一律返回 ``UNAVAILABLE`` 并注明
   原因——按 v2 的 fail-closed 取向，没有证据就不能说"核验通过"。

未来接入征信源时只需实现一个 provider（``(subjects) -> {code: (status, detail)}``）
并在这里注入，接口、前端与"待接入"语义都不用改。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from contract_review import project_contract_element_form
from contract_review.models import Finding, ReviewResult, Rule

from contract_review_app.models import (
    CreditRiskCheckResponse,
    CreditRiskFindingResponse,
    CreditRiskSubjectResponse,
    CreditRiskViewResponse,
)


#: 需要外部企业征信数据源才能得出结论的核验点。
CREDIT_RISK_CHECKS: tuple[tuple[str, str], ...] = (
    ("registered_capital", "注册资本与实缴"),
    ("business_status", "经营状态（存续/吊销/注销）"),
    ("litigation", "涉诉与被执行记录"),
    ("dishonesty", "失信被执行与严重违法"),
    ("credit_rating", "信用评级与资质证书"),
)

#: 主体相关发现的识别信号：规则分类或标题命中这些词即视为合同主体类问题。
SUBJECT_FINDING_KEYWORDS: tuple[str, ...] = (
    "合同主体",
    "资信",
    "征信",
    "客商",
    "注册资本",
    "经营状况",
    "经营状态",
    "营业执照",
    "涉诉",
    "诉讼",
    "被执行",
    "失信",
    "合作历史",
)

#: 主体核验项的数据源状态取值。
CheckOutcome = tuple[str, str]
CreditRiskProvider = Callable[
    [Sequence[CreditRiskSubjectResponse]], Mapping[str, CheckOutcome]
]

_SUBJECT_KEYS: tuple[tuple[str, str], ...] = (("party_a", "甲方"), ("party_b", "乙方"))

_STATUS_SEVERITY: dict[str, int] = {"BLOCK": 0, "WARN": 1, "UNKNOWN": 2}
_RISK_LEVEL_SEVERITY: dict[str, int] = {
    "critical": 0,
    "high": 0,
    "medium": 1,
    "low": 3,
}


def build_credit_risk_view(
    result: ReviewResult,
    *,
    provider: CreditRiskProvider | None = None,
) -> CreditRiskViewResponse:
    """把审查结果投影成客商风险视图。

    ``provider`` 为空表示企业征信数据源尚未接入：主体与主体类发现照常给出，
    外部核验项全部标为 ``UNAVAILABLE``。
    """

    subjects = _subjects(result)
    findings = _subject_findings(result)
    checks = _checks(subjects, provider=provider)
    connected = provider is not None
    if connected:
        message = "已接入企业征信数据源，外部核验项为实时结果。"
        provider_name = "external"
    else:
        message = (
            "企业征信数据源尚未接入：外部核验项无法得出结论，"
            "以下仅为合同正文中的主体信息与主体类审查发现。"
        )
        provider_name = "not_configured"
    return CreditRiskViewResponse(
        data_source_connected=connected,
        data_source_provider=provider_name,
        data_source_message=message,
        subjects=subjects,
        checks=checks,
        subject_findings=findings,
        summary=_summary(subjects, findings, connected=connected),
    )


def _subjects(result: ReviewResult) -> list[CreditRiskSubjectResponse]:
    """从要素事实取合同主体，复用表单投影的来源/置信度口径。"""

    form = project_contract_element_form(result)
    by_key = {field.key: field for field in form.fields}
    subjects: list[CreditRiskSubjectResponse] = []
    for key, role in _SUBJECT_KEYS:
        field = by_key.get(key)
        subjects.append(
            CreditRiskSubjectResponse(
                role=role,
                key=key,
                value=field.value if field else "",
                source=field.source if field else "empty",
                confidence=field.confidence if field else None,
                fact_ids=list(field.fact_ids) if field else [],
            )
        )
    return subjects


def _subject_findings(result: ReviewResult) -> list[CreditRiskFindingResponse]:
    """挑出与合同主体相关的发现，按风险等级稳定排序。"""

    rules_by_id: dict[str, Rule] = {
        rule.rule_id: rule for rule in result.rule_bundle.rules
    }
    selected = [
        finding
        for finding in result.findings
        if _is_subject_finding(finding, rules_by_id.get(finding.rule_id))
    ]
    selected.sort(
        key=lambda item: (
            _severity_rank(item),
            item.rule_id,
            item.finding_id,
        )
    )
    return [
        CreditRiskFindingResponse(
            finding_id=finding.finding_id,
            rule_id=finding.rule_id,
            title=finding.title,
            status=str(finding.status),
            risk_level=str(finding.risk_level) if finding.risk_level else None,
            reason=finding.reason,
            recommended_action=finding.recommended_action,
            evidence_count=len(finding.evidence_ids),
        )
        for finding in selected
    ]


def _is_subject_finding(finding: Finding, rule: Rule | None) -> bool:
    """判断一条发现是否属于合同主体/资信范畴。"""

    haystack = " ".join(
        part
        for part in (
            rule.category if rule is not None else "",
            rule.title if rule is not None else "",
            finding.title,
            finding.reason,
        )
        if part
    )
    return any(keyword in haystack for keyword in SUBJECT_FINDING_KEYWORDS)


def _severity_rank(finding: Finding) -> int:
    """BLOCK > WARN > UNKNOWN > 其他，让最该看的排在前面。

    ``status`` 取大写枚举（PASS/WARN/BLOCK/UNKNOWN），``risk_level`` 取小写枚举
    （low/medium/high/critical），所以先看 status，PASS 或不适用的再按风险等级排。
    """

    status = str(finding.status).upper()
    if status in _STATUS_SEVERITY:
        return _STATUS_SEVERITY[status]
    return _RISK_LEVEL_SEVERITY.get(str(finding.risk_level or "").lower(), 4)


def _checks(
    subjects: Sequence[CreditRiskSubjectResponse],
    *,
    provider: CreditRiskProvider | None,
) -> list[CreditRiskCheckResponse]:
    """生成外部核验项：没有数据源时全部标为待接入。"""

    outcomes: Mapping[str, CheckOutcome] = {}
    if provider is not None:
        try:
            outcomes = provider(subjects) or {}
        except Exception as exc:  # noqa: BLE001
            # 数据源异常不能让整页失败，但也不能伪装成"已核验"。
            return [
                CreditRiskCheckResponse(
                    code=code,
                    label=label,
                    status="UNKNOWN",
                    detail=f"企业征信数据源调用失败：{exc}",
                )
                for code, label in CREDIT_RISK_CHECKS
            ]
    checks: list[CreditRiskCheckResponse] = []
    for code, label in CREDIT_RISK_CHECKS:
        outcome = outcomes.get(code)
        if outcome is None:
            checks.append(
                CreditRiskCheckResponse(
                    code=code,
                    label=label,
                    status="UNAVAILABLE",
                    detail="需接入企业征信数据源后核验",
                )
            )
            continue
        status, detail = outcome
        checks.append(
            CreditRiskCheckResponse(
                code=code, label=label, status=str(status), detail=str(detail)
            )
        )
    return checks


def _summary(
    subjects: Sequence[CreditRiskSubjectResponse],
    findings: Sequence[CreditRiskFindingResponse],
    *,
    connected: bool,
) -> str:
    """一句人话总结，把"没数据源"这件事说清楚。"""

    named = [f"{item.role}={item.value}" for item in subjects if item.value]
    who = "、".join(named) if named else "未从合同正文识别出合同主体"
    head = f"{who}；主体类审查发现 {len(findings)} 条。"
    tail = (
        "外部核验项已实时核验。"
        if connected
        else "注册资本、经营状态、涉诉、失信、信用评级等外部核验项需接入企业征信数据源，当前无法给出结论。"
    )
    return head + tail
