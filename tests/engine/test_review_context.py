"""审查上下文与规则选择的领域契约测试。"""

import pytest
from pydantic import ValidationError

from contract_review import (
    ApplicabilitySpec,
    PartyPosition,
    ReviewContext,
    Rule,
    RuleBundle,
    resolve_rule_applicability,
    select_rules,
)
from contract_review_app.services.review_context import (
    ReviewContextInputError,
    build_review_context,
)


def _bundle() -> RuleBundle:
    return RuleBundle(
        bundle_id="context-rules-v1",
        source_filename="rules.xlsx",
        source_sha256="c" * 64,
        source_sheet="Sheet1",
        source_range="A1:C3",
        rules=[
            Rule(
                rule_id="amount-001",
                version="v1",
                title="金额一致",
                category="金额",
                applies_to=["software"],
                check_method="deterministic",
                applicability={
                    "software": ApplicabilitySpec(
                        applicability="expected_value",
                        expected_value=100,
                    )
                },
                source_snapshot="rules#amount",
            ),
            Rule(
                rule_id="delivery-001",
                version="v1",
                title="交付日期",
                category="交付",
                applies_to=["software"],
                check_method="semantic",
                source_snapshot="rules#delivery",
            ),
        ],
    )


def test_review_context_normalizes_legacy_contract_type() -> None:
    context = build_review_context(
        contract_type=" software ",
        party_position="甲方",
        transaction_tags="软件,重点,软件",
        transaction_amount="1000000.00",
        review_scope="金额,金额",
    )

    assert context.contract_type == "software"
    assert context.party_position == PartyPosition.BUYER
    assert context.transaction_tags == ["软件", "重点"]
    assert str(context.transaction_amount) == "1000000.00"
    assert context.review_scope == ["金额"]


def test_application_context_rejects_unknown_party_position() -> None:
    with pytest.raises(ReviewContextInputError):
        build_review_context(party_position="甲乙方")


def test_rule_scope_and_applicability_are_resolved_by_rule_module() -> None:
    bundle = _bundle()
    context = ReviewContext(contract_type="software", review_scope=["金额"])

    assert [rule.rule_id for rule in select_rules(bundle, context)] == ["amount-001"]
    assert resolve_rule_applicability(
        bundle.rules[0], review_context=context
    ) == "expected_value"


def test_rule_applicability_resolves_registered_contract_type_alias() -> None:
    rule = Rule(
        rule_id="software-alias-001",
        version="v1",
        title="软件交付",
        category="交付",
        applies_to=["软件开发/转让服务"],
        check_method="semantic",
        applicability={
            "软件开发/转让服务": ApplicabilitySpec(applicability="required")
        },
        source_snapshot="rules#software-alias",
    )

    assert resolve_rule_applicability(
        rule, review_context=ReviewContext(contract_type="software")
    ) == "required"


def test_review_scope_accepts_only_string_arrays() -> None:
    with pytest.raises(ValidationError):
        ReviewContext(review_scope="金额")
