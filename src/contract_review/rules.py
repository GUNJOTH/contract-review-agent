"""Load and validate versioned rule snapshots derived from source documents."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from .models import (
    ApplicabilityException,
    ApplicabilitySpec,
    DocumentKind,
    PartyPosition,
    ReviewContext,
    Rule,
    RuleBundle,
)
from .playbook import (
    PlaybookReleaseError,
    assert_playbook_bundle_compatible,
    publish_playbook_bundle,
    validate_playbook_bundle,
    validate_playbook_spec,
)
from .rule_checkers import is_supported_checker


class RuleBundleError(ValueError):
    """Raised when a rule snapshot is malformed or cannot be read."""


_CONTRACT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    # API/任务入口中的短名称只在规则解析层归一化，避免散落到检索器、
    # 检查器和路由中；规则快照中的规范名称始终优先。
    "software": ("软件开发/转让服务",),
    "software_development": ("软件开发/转让服务",),
}


def _contract_type_candidates(contract_type: str) -> tuple[str, ...]:
    """返回当前合同类型及其已登记的规范候选，保持顺序稳定。"""

    return tuple(
        dict.fromkeys(
            (contract_type, *_CONTRACT_TYPE_ALIASES.get(contract_type, ()))
        )
    )


def _applicability_spec(rule: Rule, contract_type: str) -> ApplicabilitySpec | None:
    """按规范合同类型查找规则适用性声明。"""

    for candidate in _contract_type_candidates(contract_type):
        spec = rule.applicability.get(candidate)
        if spec is not None:
            return spec
    return None


def _rule_applies_to(rule: Rule, contract_type: str) -> bool:
    """判断规则是否声明适用于当前合同类型或其规范别名。"""

    return any(
        candidate in rule.applies_to
        for candidate in _contract_type_candidates(contract_type)
    )


def _context_condition_match(
    condition: ApplicabilitySpec | ApplicabilityException,
    review_context: ReviewContext,
) -> bool | None:
    """评估结构化适用条件；``None`` 表示输入事实不足。"""

    if condition.party_positions and review_context.party_position not in condition.party_positions:
        if review_context.party_position == PartyPosition.UNKNOWN:
            return None
        return False
    if condition.jurisdictions:
        if not review_context.jurisdiction:
            return None
        if review_context.jurisdiction.casefold() not in {
            item.casefold() for item in condition.jurisdictions
        }:
            return False
    if condition.transaction_tags:
        if not review_context.transaction_tags:
            return None
        if not set(condition.transaction_tags).issubset(
            set(review_context.transaction_tags)
        ):
            return False
    if (
        condition.transaction_amount_min is not None
        or condition.transaction_amount_max is not None
    ):
        if review_context.transaction_amount is None:
            return None
        if (
            condition.transaction_amount_min is not None
            and review_context.transaction_amount < condition.transaction_amount_min
        ):
            return False
        if (
            condition.transaction_amount_max is not None
            and review_context.transaction_amount > condition.transaction_amount_max
        ):
            return False
    if condition.document_kinds:
        if not review_context.document_kinds:
            return None
        if DocumentKind.UNKNOWN in review_context.document_kinds:
            # 合同包中仍有未识别角色时，不能把“未覆盖该角色”误判为
            # 规则不适用；角色归类完成前必须保持 UNKNOWN，避免自动放行。
            return None
        if not set(condition.document_kinds).issubset(
            set(review_context.document_kinds)
        ):
            return False
    if condition.document_kinds_any:
        if not review_context.document_kinds:
            return None
        actual_document_kinds = set(review_context.document_kinds)
        if actual_document_kinds.intersection(condition.document_kinds_any):
            return True
        if DocumentKind.UNKNOWN in actual_document_kinds:
            # “至少一种角色”在已知角色均未命中但仍存在未识别文档时，
            # 不能把未知角色压成 not_applicable。
            return None
        return False
    return True


def _resolve_structured_applicability(
    spec: ApplicabilitySpec,
    review_context: ReviewContext,
) -> str:
    """先执行例外，再执行基础条件，缺失上下文时严格返回 unknown。"""

    uncertain_exception = False
    for exception in spec.exceptions:
        match = _context_condition_match(exception, review_context)
        if match is True:
            return exception.result
        if match is None:
            uncertain_exception = True
    match = _context_condition_match(spec, review_context)
    if match is None:
        return "unknown"
    if not match:
        return "not_applicable"
    if uncertain_exception:
        # 基础条件已满足但例外条件缺少事实时，不能把“可能不适用”
        # 折叠成 required；否则企业立场、金额或法域缺失会直接放行规则。
        return "unknown"
    return spec.applicability


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
    report = validate_playbook_bundle(bundle)
    if not report.valid:
        messages = "；".join(issue.message for issue in report.issues[:5])
        raise RuleBundleError(f"规则包 Playbook 门禁失败：{messages}")
    return bundle


def load_active_rule_bundle(
    base_path: str | Path,
    extension_path: str | Path | None = None,
    published_path: str | Path | None = None,
) -> RuleBundle:
    """加载正式基础规则并合并版本化核心扩展规则与规则引擎库发布层。

    基础快照仍保持原始来源和兼容性，扩展规则以独立快照进入合并结果。
    各快照均须通过校验和发布状态门禁；基础层与扩展层之间的重复规则 ID
    会在启动/审查前失败，而不是静默覆盖旧规则。

    ``published_path`` 是规则引擎库发布的**生效全集快照**：发布时把
    基础 + 扩展 + 草稿变更（新增/覆盖编辑/停用剔除）合成一个完整快照，
    因此存在发布层时直接以它为审查用的正式规则包——这正是"编辑基础
    规则"与"停用基础规则"能生效的机制。基础/扩展层升级后需要重新发布
    才会带上新变化，发布层不会静默混入旧变更。
    """

    base_bundle = load_rule_bundle(base_path)
    if extension_path is None:
        extension_candidate = Path(base_path).with_name(
            "contract_core_rules_v0.15.json"
        )
        extension_path = extension_candidate if extension_candidate.is_file() else None
    if extension_path is None:
        assert_rule_bundle_compatible(base_bundle)
        if published_path is None:
            return base_bundle
        return _load_published_active_bundle(base_bundle, published_path)

    extension_bundle = load_rule_bundle(extension_path)
    assert_rule_bundle_compatible(base_bundle)
    assert_rule_bundle_compatible(extension_bundle)
    base_ids = {rule.rule_id for rule in base_bundle.rules}
    extension_ids = {rule.rule_id for rule in extension_bundle.rules}
    duplicate_ids = base_ids.intersection(extension_ids)
    if duplicate_ids:
        raise RuleBundleError(
            f"基础规则与扩展规则包含重复 rule_id：{sorted(duplicate_ids)}"
        )
    merged = base_bundle.model_copy(
        update={
            "bundle_id": f"{base_bundle.bundle_id}+{extension_bundle.bundle_id}",
            "source_sha256": hashlib.sha256(
                f"{base_bundle.source_sha256}\x1f{extension_bundle.source_sha256}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            "source_filename": (
                f"{base_bundle.source_filename};{extension_bundle.source_filename}"
            ),
            "source_notes": [
                *base_bundle.source_notes,
                *extension_bundle.source_notes,
                "核心扩展规则以独立快照合并，未修改基础 Excel 来源。",
            ],
            "rules": [*base_bundle.rules, *extension_bundle.rules],
            "release_status": "validated",
            "parent_bundle_id": base_bundle.bundle_id,
            "release_fingerprint": None,
            "published_at": None,
        }
    )
    # 合并结果是新的规则快照，必须重新生成自己的正式发布指纹，不能
    # 复用任一输入快照的 release_fingerprint。
    merged_bundle = publish_playbook_bundle(merged)
    if published_path is None:
        return merged_bundle
    return _load_published_active_bundle(merged_bundle, published_path)


def _load_published_active_bundle(
    fallback_bundle: RuleBundle, published_path: str | Path
) -> RuleBundle:
    """发布层是完整生效快照：存在即整体取代基础/扩展合并结果。"""

    candidate = Path(published_path)
    if not candidate.is_file():
        return fallback_bundle
    published_bundle = load_rule_bundle(candidate)
    assert_rule_bundle_compatible(published_bundle)
    return published_bundle


def validate_rule(rule: Rule) -> None:
    """Validate policy semantics that are not expressible as field types."""

    if not rule.applies_to and not rule.applicability:
        raise RuleBundleError(f"rule has no contract applicability: {rule.rule_id}")
    if rule.applicability:
        missing_applicability = [
            contract_type
            for contract_type in rule.applies_to
            if _applicability_spec(rule, contract_type) is None
        ]
        if missing_applicability:
            raise RuleBundleError(
                f"规则的 applicability 必须覆盖 applies_to，缺少："
                f"{rule.rule_id} / {sorted(missing_applicability)}"
            )
    if rule.human_review and rule.check_method == "deterministic":
        raise RuleBundleError(
            f"deterministic rule cannot require human review without an explicit policy: {rule.rule_id}"
        )
    if rule.playbook is not None:
        playbook_issues = validate_playbook_spec(rule.playbook)
        if playbook_issues:
            raise RuleBundleError(
                f"Playbook 规则校验失败 {rule.rule_id}: "
                + "；".join(issue.message for issue in playbook_issues)
            )
        if rule.playbook.evaluation_mode == "checker" and rule.checker is None:
            raise RuleBundleError(
                f"checker 模式 Playbook 必须绑定 checker: {rule.rule_id}"
            )
    if rule.checker is not None and not is_supported_checker(rule.checker):
        raise RuleBundleError(
            f"规则声明了未注册的 checker: {rule.rule_id} / {rule.checker}"
        )


def assert_rule_bundle_compatible(
    bundle: RuleBundle,
    *,
    review_schema_version: str = "2.0",
) -> None:
    """审查执行前的规则包发布和 Schema 兼容门禁。"""

    for rule in bundle.rules:
        validate_rule(rule)
    try:
        assert_playbook_bundle_compatible(
            bundle,
            review_schema_version=review_schema_version,
            require_published=True,
        )
    except PlaybookReleaseError as exc:
        raise RuleBundleError(str(exc)) from exc


def is_rule_in_scope(rule: Rule, review_context: ReviewContext) -> bool:
    """判断规则是否属于本次审查范围。

    ``review_scope`` 支持规则 ID 和规则 category 两种稳定入口；空白范围
    表示使用完整规则快照。该判断集中在规则模块，API 和任务处理器不复制
    规则选择逻辑。
    """

    if not review_context.review_scope:
        return True
    scope = set(review_context.review_scope)
    return rule.rule_id in scope or rule.category in scope


def select_rules(
    rule_bundle: RuleBundle,
    review_context: ReviewContext,
) -> list[Rule]:
    """根据审查上下文从完整规则快照中选择本次执行的规则。"""

    return [
        rule for rule in rule_bundle.rules if is_rule_in_scope(rule, review_context)
    ]


def resolve_rule_applicability(
    rule: Rule,
    *,
    review_context: ReviewContext,
) -> str:
    """按规则快照解析合同类型适用性。

    规则的 ``applicability`` 优先于 ``applies_to``；缺少合同类型或快照没有
    明确映射时返回 ``unknown``，由执行器生成可见复核项，而不是自动通过。
    """

    effective_contract_type = review_context.contract_type
    if not effective_contract_type:
        return "unknown"
    spec = _applicability_spec(rule, effective_contract_type)
    if spec is not None:
        return _resolve_structured_applicability(spec, review_context)
    if _rule_applies_to(rule, effective_contract_type):
        return "required"
    return "unknown"


def rule_document_kinds(
    rule: Rule,
    *,
    review_context: ReviewContext,
) -> list[DocumentKind]:
    """返回当前合同类型声明的候选文档角色白名单。

    ``ApplicabilitySpec.document_kinds`` 表示规则需要关注的合同包角色，
    不配置时保留合同包全部已知角色。例外条件不会在此处臆测转换，
    其适用性仍由 ``resolve_rule_applicability`` 统一裁决。
    """

    contract_type = review_context.contract_type
    if not contract_type:
        return []
    spec = _applicability_spec(rule, contract_type)
    if spec is None:
        return []
    return list(dict.fromkeys([*spec.document_kinds, *spec.document_kinds_any]))


def expected_rule_value(
    rule: Rule,
    *,
    review_context: ReviewContext,
) -> object | None:
    """返回当前合同类型在规则快照中声明的预期值。"""

    effective_contract_type = review_context.contract_type
    if not effective_contract_type:
        return None
    spec = _applicability_spec(rule, effective_contract_type)
    return spec.expected_value if spec is not None else None
