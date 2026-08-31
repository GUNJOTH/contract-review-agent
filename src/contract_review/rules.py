"""Load and validate versioned rule snapshots derived from source documents."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Rule, RuleBundle


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
