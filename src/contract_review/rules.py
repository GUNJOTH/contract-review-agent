"""Load and validate versioned rule snapshots derived from source documents."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ReviewContext, Rule, RuleBundle


class RuleBundleError(ValueError):
    """Raised when a rule snapshot is malformed or cannot be read."""


def load_rule_bundle(path: str | Path) -> RuleBundle:
    """Load a JSON rule snapshot and validate every rule at the boundary."""

    file_path = Path(path)
    if not file_path.is_file():
        raise RuleBundleError(f"rule bundle does not exist: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        bundle = RuleBundle.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuleBundleError(f"invalid rule bundle {file_path.name}: {exc}") from exc

    rule_ids = [rule.rule_id for rule in bundle.rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise RuleBundleError("rule bundle contains duplicate rule_id values")
    for rule in bundle.rules:
        validate_rule(rule)
    return bundle


def validate_rule(rule: Rule) -> None:
    """Validate policy semantics that are not expressible as field types."""

    if not rule.applies_to and not rule.applicability:
        raise RuleBundleError(f"rule has no contract applicability: {rule.rule_id}")
    if rule.human_review and rule.check_method == "deterministic":
        raise RuleBundleError(
            f"deterministic rule cannot require human review without an explicit policy: {rule.rule_id}"
        )
    if rule.playbook is not None and not rule.playbook.has_deterministic_positions:
        raise RuleBundleError(
            f"Playbook 规则必须至少配置条款类型或一种可识别立场: {rule.rule_id}"
        )


def is_rule_in_scope(rule: Rule, review_context: ReviewContext | None = None) -> bool:
    """判断规则是否属于本次审查范围。

    ``review_scope`` 支持规则 ID 和规则 category 两种稳定入口；空白范围
    表示使用完整规则快照。该判断集中在规则模块，API 和任务处理器不复制
    规则选择逻辑。
    """

    if review_context is None or not review_context.review_scope:
        return True
    scope = set(review_context.review_scope)
    return rule.rule_id in scope or rule.category in scope


def select_rules(
    rule_bundle: RuleBundle,
    review_context: ReviewContext | None = None,
) -> list[Rule]:
    """根据审查上下文从完整规则快照中选择本次执行的规则。"""

    return [
        rule for rule in rule_bundle.rules if is_rule_in_scope(rule, review_context)
    ]


def resolve_rule_applicability(
    rule: Rule,
    *,
    review_context: ReviewContext | None = None,
    contract_type: str | None = None,
) -> str:
    """按规则快照解析合同类型适用性。

    规则的 ``applicability`` 优先于 ``applies_to``；缺少合同类型或快照没有
    明确映射时返回 ``unknown``，由执行器生成可见复核项，而不是自动通过。
    """

    context_contract_type = (
        review_context.contract_type if review_context is not None else None
    )
    normalized_contract_type = (contract_type or "").strip() or None
    if (
        context_contract_type
        and normalized_contract_type
        and context_contract_type != normalized_contract_type
    ):
        raise RuleBundleError(
            "contract_type 与 review_context.contract_type 不一致"
        )
    effective_contract_type = context_contract_type or normalized_contract_type
    if not effective_contract_type:
        return "unknown"
    spec = rule.applicability.get(effective_contract_type)
    if spec is not None:
        return spec.applicability
    if effective_contract_type in rule.applies_to:
        return "required"
    return "unknown"


def expected_rule_value(
    rule: Rule,
    *,
    review_context: ReviewContext | None = None,
    contract_type: str | None = None,
) -> object | None:
    """返回当前合同类型在规则快照中声明的预期值。"""

    context_contract_type = (
        review_context.contract_type if review_context is not None else None
    )
    normalized_contract_type = (contract_type or "").strip() or None
    if (
        context_contract_type
        and normalized_contract_type
        and context_contract_type != normalized_contract_type
    ):
        raise RuleBundleError(
            "contract_type 与 review_context.contract_type 不一致"
        )
    effective_contract_type = context_contract_type or normalized_contract_type
    if not effective_contract_type:
        return None
    spec = rule.applicability.get(effective_contract_type)
    return spec.expected_value if spec is not None else None
