from contract_review.models import (
    ClauseKind,
    ContractClause,
    FindingStatus,
    MissingClausePolicy,
    PlaybookAction,
    PlaybookSpec,
    RiskLevel,
    Rule,
)
from contract_review.playbook import evaluate_playbook_rule
from contract_review.semantic import is_model_judged_rule


def _clause(text: str, title: str = "付款条款") -> ContractClause:
    return ContractClause(
        clause_id="clause-1",
        document_id="doc-1",
        clause_kind=ClauseKind.NUMBERED,
        clause_number="第1条",
        title=title,
        text=text,
        order=0,
        source_chunk_ids=["chunk-1"],
        evidence_ids=["evidence-clause-1"],
        extractor_version="test",
    )


def _rule(playbook: PlaybookSpec) -> Rule:
    return Rule(
        rule_id="rule-payment-playbook",
        version="v1",
        title="付款方式",
        category="付款结算",
        applies_to=["软件开发/转让服务"],
        check_method="deterministic",
        risk_level=RiskLevel.HIGH,
        applicability={
            "软件开发/转让服务": {"applicability": "required"},
        },
        source_snapshot="rules#payment",
        playbook=playbook,
    )


def _playbook(**overrides) -> PlaybookSpec:
    values = {
        "playbook_id": "payment-position-v1",
        "version": "v1",
        "clause_types": ["付款"],
        "preferred_position": "验收合格后支付",
        "fallback_positions": ["分阶段付款"],
        "prohibited_positions": ["100%预付"],
        "missing_clause_policy": MissingClausePolicy.BLOCK,
        "action_on_preferred": PlaybookAction.ACCEPT,
        "action_on_fallback": PlaybookAction.REVISE,
        "action_on_prohibited": PlaybookAction.REJECT,
        "suggested_language": "补充付款节点和比例。",
        "escalation_condition": "超出授权比例时升级。",
    }
    values.update(overrides)
    return PlaybookSpec(**values)


def test_playbook_preferred_position_passes_with_clause_evidence():
    result = evaluate_playbook_rule(
        _rule(_playbook()),
        [_clause("付款条款：验收合格后支付合同价款。")],
        default_evidence_ids=["evidence-rule-1"],
    )

    assert result is not None
    assert result.status is FindingStatus.PASS
    assert result.action is PlaybookAction.ACCEPT
    assert result.clause_ids == ["clause-1"]
    assert set(result.evidence_ids) == {"evidence-rule-1", "evidence-clause-1"}


def test_playbook_fallback_and_prohibited_positions_are_distinct():
    fallback = evaluate_playbook_rule(
        _rule(_playbook()),
        [_clause("付款条款：合同采用分阶段付款。")],
        default_evidence_ids=["evidence-rule-1"],
    )
    prohibited = evaluate_playbook_rule(
        _rule(_playbook()),
        [_clause("付款条款：合同签订后100%预付。")],
        default_evidence_ids=["evidence-rule-1"],
    )

    assert fallback is not None
    assert fallback.status is FindingStatus.WARN
    assert fallback.action is PlaybookAction.REVISE
    assert fallback.comparison["match_kind"] == "fallback"
    assert prohibited is not None
    assert prohibited.status is FindingStatus.BLOCK
    assert prohibited.action is PlaybookAction.REJECT
    assert prohibited.comparison["match_kind"] == "prohibited"


def test_playbook_missing_clause_is_blocked_and_unknown_text_is_not_pass():
    missing = evaluate_playbook_rule(
        _rule(_playbook()),
        [_clause("交付条款：乙方按期交付。", title="交付条款")],
        default_evidence_ids=["evidence-rule-1"],
    )
    unknown = evaluate_playbook_rule(
        _rule(_playbook()),
        [_clause("付款条款：双方另行协商付款安排。")],
        default_evidence_ids=["evidence-rule-1"],
    )

    assert missing is not None
    assert missing.status is FindingStatus.BLOCK
    assert missing.action is PlaybookAction.ESCALATE
    assert missing.comparison["suggested_language"] == "补充付款节点和比例。"
    assert unknown is not None
    assert unknown.status is FindingStatus.UNKNOWN
    assert unknown.action is PlaybookAction.ESCALATE


def test_deterministic_playbook_rule_does_not_enter_model_review():
    assert is_model_judged_rule(_rule(_playbook())) is False
