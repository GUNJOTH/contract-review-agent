"""规则引擎库："合同检查标准"的编辑层（与 v1 同构，保存即生效）。

v1 的规则库是一个可编辑的规则池：新增/编辑/启用/停用/删除立即生效，
规则引擎评分矩阵同步更新。v2 的规则源是版本化快照（基础 + 核心扩展），
不可运行期改写，因此编辑动作落到本模块维护的**覆盖层**文件：

- ``custom_rules``：新增的自定义规则，或对基础/扩展规则的同 rule_id 覆盖编辑；
- ``disabled_ids``：停用的规则 ID（列表中保留，开关为"否"，不参与审查）；
- ``removed_ids``：删除的基础/扩展规则 ID（从列表移除）。

每次变更原子落盘后即生效：审查用的正式规则包由
``active_rule_bundle()`` 现场合成并过 ``publish_playbook_bundle``
指纹门禁，不需要任何"发布"步骤。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contract_review.models import RiskAnalysisItem, Rule, RuleBundle
from contract_review.playbook import publish_playbook_bundle
from contract_review.rules import RuleBundleError, load_rule_bundle, validate_rule

from contract_review_app.config import settings


OVERLAY_SCHEMA_VERSION = "1.0"


class RuleEditError(RuntimeError):
    """规则引擎库编辑失败或覆盖层损坏。"""


def custom_rules_path() -> Path:
    return settings.resolve_path(settings.CONTRACT_CUSTOM_RULES_PATH)


class RuleOverlay:
    """规则覆盖层：自定义规则 + AI 候选规则 + 停用/删除标记。"""

    def __init__(
        self,
        custom_rules: list[Rule] | None = None,
        disabled_ids: list[str] | None = None,
        removed_ids: list[str] | None = None,
        ai_candidates: list[Rule] | None = None,
    ) -> None:
        self.custom_rules = list(custom_rules or [])
        self.disabled_ids = list(disabled_ids or [])
        self.removed_ids = list(removed_ids or [])
        self.ai_candidates = list(ai_candidates or [])

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "custom_rules": [
                rule.model_dump(mode="json") for rule in self.custom_rules
            ],
            "ai_candidates": [
                rule.model_dump(mode="json") for rule in self.ai_candidates
            ],
            "disabled_ids": self.disabled_ids,
            "removed_ids": self.removed_ids,
        }


def load_overlay() -> RuleOverlay:
    """加载覆盖层；文件缺失视为空覆盖层。"""

    file_path = custom_rules_path()
    if not file_path.is_file():
        return RuleOverlay()
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuleEditError(f"规则覆盖层文件无法解析: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise RuleEditError("规则覆盖层文件 schema_version 不受支持")

    custom: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.get("custom_rules") or []):
        try:
            rule = Rule.model_validate(raw)
        except ValueError as exc:
            raise RuleEditError(f"自定义规则第 {index + 1} 条无效: {exc}") from exc
        if rule.rule_id in seen:
            raise RuleEditError(f"自定义规则存在重复 rule_id: {rule.rule_id}")
        seen.add(rule.rule_id)
        custom.append(rule)

    def _id_list(key: str) -> list[str]:
        values = payload.get(key) or []
        result: list[str] = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise RuleEditError(f"{key} 必须是非空字符串数组")
            result.append(item.strip())
        return result

    return RuleOverlay(
        custom, _id_list("disabled_ids"), _id_list("removed_ids"),
        _load_candidate_rules(payload),
    )


def _load_candidate_rules(payload: dict[str, Any]) -> list[Rule]:
    """加载 AI 候选规则；缺省为空，损坏 fail-closed（与 custom_rules 同门禁）。"""

    candidates: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.get("ai_candidates") or []):
        try:
            rule = Rule.model_validate(raw)
        except ValueError as exc:
            raise RuleEditError(f"AI 候选规则第 {index + 1} 条无效: {exc}") from exc
        if rule.rule_id in seen:
            raise RuleEditError(f"AI 候选规则存在重复 rule_id: {rule.rule_id}")
        seen.add(rule.rule_id)
        candidates.append(rule)
    return candidates


def _write_overlay(overlay: RuleOverlay) -> None:
    path = custom_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(overlay.to_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def baseline_rules() -> list[Rule]:
    """基础 + 核心扩展快照的规则全集（用户未改动的原始口径）。"""

    rules: list[Rule] = []
    rules.extend(
        load_rule_bundle(settings.resolve_path(settings.CONTRACT_RULES_PATH)).rules
    )
    core_configured = settings.CONTRACT_CORE_RULES_PATH.strip()
    if core_configured:
        rules.extend(
            load_rule_bundle(
                settings.resolve_path(settings.CONTRACT_CORE_RULES_PATH)
            ).rules
        )
    return rules


def library_rules() -> list[tuple[Rule, bool]]:
    """规则库当前展示清单：``(规则, 是否启用)``，顺序稳定。

    口径 = （基础 + 扩展 − 已删除）应用自定义覆盖 + 纯自定义规则；
    ``disabled_ids`` 里的规则保留在列表中但开关为"否"（v1 同款）。
    """

    overlay = load_overlay()
    custom_by_id = {rule.rule_id: rule for rule in overlay.custom_rules}
    removed = set(overlay.removed_ids)
    rows: list[tuple[Rule, bool]] = []
    seen: set[str] = set()
    for rule in baseline_rules():
        if rule.rule_id in removed:
            continue
        effective = custom_by_id.get(rule.rule_id, rule)
        rows.append((effective, rule.rule_id not in overlay.disabled_ids))
        seen.add(rule.rule_id)
    for rule in overlay.custom_rules:
        if rule.rule_id not in seen:
            rows.append((rule, rule.rule_id not in overlay.disabled_ids))
    return rows


def enabled_rules() -> list[Rule]:
    """审查实际执行的规则：展示清单中开关为"是"的部分。"""

    return [rule for rule, enabled in library_rules() if enabled]


def active_rule_bundle() -> RuleBundle:
    """审查用正式规则包：启用规则现场合成并过发布指纹门禁。"""

    rules = enabled_rules()
    digest = hashlib.sha256(
        json.dumps(
            [rule.model_dump(mode="json") for rule in rules],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    bundle = RuleBundle(
        bundle_id=f"contract-rules-active-{digest[:8]}",
        source_filename=custom_rules_path().name,
        source_sha256=digest,
        source_sheet="rules-engine",
        source_range="A1:A1",
        source_notes=["规则引擎库：基础/扩展快照与「合同检查标准」编辑层合成。"],
        rules=rules,
    )
    try:
        return publish_playbook_bundle(bundle)
    except RuleBundleError as exc:
        raise RuleEditError(f"规则包合成失败: {exc}") from exc


def _build_rule(payload: dict[str, Any], *, rule_id: str | None) -> Rule:
    applies_to = [
        str(item).strip()
        for item in (payload.get("applies_to") or [])
        if str(item).strip()
    ]
    if not applies_to:
        # v1 表单没有"适用类型"输入：默认适用全部标准合同类型。
        applies_to = [
            "软件产品销售",
            "软件开发/转让服务",
            "一般商品销售合同",
            "混合合同",
            "其它服务合同",
        ]
    # 风险等级口径是 low/medium/high/critical；空值或旧口径值统一归为
    # unclassified（留空），避免措辞漂移把脏值带进快照。
    risk_level = str(payload.get("risk_level") or "").strip().lower()
    if risk_level not in {"low", "medium", "high", "critical"}:
        risk_level = None
    merged = {
        **payload,
        # v1 表单没有版本输入：默认 v1，编辑覆盖时允许沿用传入值。
        "version": str(payload.get("version") or "v1"),
        "applies_to": applies_to,
        "risk_level": risk_level,
        # 适用性声明由后端统一补齐：声明的类型一律按"该检查适用"处理。
        "applicability": {
            item: {"applicability": "required"} for item in applies_to
        },
        "source_snapshot": str(payload.get("source_snapshot") or "rules-engine#1"),
    }
    try:
        rule = Rule.model_validate(merged)
    except ValueError as exc:
        raise RuleEditError(f"规则定义无效: {exc}") from exc
    try:
        validate_rule(rule)
    except RuleBundleError as exc:
        raise RuleEditError(str(exc)) from exc
    if rule_id is not None and rule.rule_id != rule_id:
        raise RuleEditError("规则 ID 不允许修改")
    return rule


def upsert_rule(payload: dict[str, Any], *, rule_id: str | None = None) -> Rule:
    """新增或编辑一条规则，落盘即生效（v1 的"创建并启用/保存修改"）。

    编辑一条 AI 候选规则 = 采纳它：条目转入"合同检查标准"并从候选池移除。
    """

    overlay = load_overlay()
    baseline_ids_ = baseline_ids()
    if rule_id is None:
        rule_id = str(payload.get("rule_id") or "").strip() or generate_rule_id()
    if rule_id in baseline_ids_ or any(
        item.rule_id == rule_id for item in overlay.custom_rules
    ) or any(item.rule_id == rule_id for item in overlay.ai_candidates):
        # 编辑：同 rule_id 覆盖（基础规则与 AI 候选也允许编辑）。
        rule = _build_rule({**payload, "rule_id": rule_id}, rule_id=rule_id)
        overlay.custom_rules = [
            item for item in overlay.custom_rules if item.rule_id != rule_id
        ]
        overlay.ai_candidates = [
            item for item in overlay.ai_candidates if item.rule_id != rule_id
        ]
        overlay.custom_rules.append(rule)
        overlay.removed_ids = [
            item for item in overlay.removed_ids if item != rule_id
        ]
    else:
        rule = _build_rule({**payload, "rule_id": rule_id}, rule_id=rule_id)
        overlay.custom_rules.append(rule)
    _write_overlay(overlay)
    return rule


def remove_rule(rule_id: str) -> dict[str, str]:
    """删除规则（v1 同款）：自定义/AI 候选规则移除条目；基础/扩展规则记入删除标记。

    被"覆盖编辑"过的基础规则一并记入删除标记——覆盖条目只是修改形态，
    删除的语义是"这条规则从清单里消失"。
    """

    overlay = load_overlay()
    base_ids = baseline_ids()
    in_custom = any(item.rule_id == rule_id for item in overlay.custom_rules)
    in_candidates = any(item.rule_id == rule_id for item in overlay.ai_candidates)
    if rule_id in base_ids:
        overlay.custom_rules = [
            item for item in overlay.custom_rules if item.rule_id != rule_id
        ]
        if rule_id not in overlay.removed_ids:
            overlay.removed_ids.append(rule_id)
        overlay.disabled_ids = [
            item for item in overlay.disabled_ids if item != rule_id
        ]
    elif in_custom or in_candidates:
        overlay.custom_rules = [
            item for item in overlay.custom_rules if item.rule_id != rule_id
        ]
        overlay.ai_candidates = [
            item for item in overlay.ai_candidates if item.rule_id != rule_id
        ]
        overlay.disabled_ids = [
            item for item in overlay.disabled_ids if item != rule_id
        ]
    else:
        raise RuleEditError(f"规则不存在: {rule_id}")
    _write_overlay(overlay)
    return {"status": "deleted", "rule_id": rule_id}


def set_rule_enabled(rule_id: str, *, enabled: bool) -> dict[str, str]:
    """启用/停用开关（v1 同款）：停用保留在列表中，开关显示"否"。"""

    overlay = load_overlay()
    known_ids = baseline_ids() | {item.rule_id for item in overlay.custom_rules}
    if rule_id not in known_ids:
        raise RuleEditError(f"规则不存在: {rule_id}")
    if enabled:
        overlay.disabled_ids = [
            item for item in overlay.disabled_ids if item != rule_id
        ]
    elif rule_id not in overlay.disabled_ids:
        overlay.disabled_ids.append(rule_id)
    _write_overlay(overlay)
    return {"status": "enabled" if enabled else "disabled", "rule_id": rule_id}


def baseline_ids() -> set[str]:
    return {rule.rule_id for rule in baseline_rules()}


def generate_rule_id() -> str:
    return f"CUSTOM-{uuid.uuid4().hex[:8].upper()}"


def generate_ai_rule_id() -> str:
    return f"AI-{uuid.uuid4().hex[:8].upper()}"


AI_RULE_LEVEL_MAP = {"BLOCK": "high", "WARN": "medium", "INFO": "low", "critical": "critical"}


def _candidate_from_analysis(item: RiskAnalysisItem, *, response_id: str) -> Rule:
    """把一条通读风险分析结论转成候选规则（v1 规则自进化的提炼口径）。

    评分标准沿用 v1 AI 规则的通用三档（v1 的 sqlite 里 AI 规则大多就是
    这一组），否则评分矩阵会整列显示横线。
    """

    return _build_rule(
        {
            "rule_id": generate_ai_rule_id(),
            "version": "v1",
            "title": str(item.title or "").strip()[:80],
            "category": (str(item.module or "").strip() or "其他检查"),
            "check_method": "semantic",
            "risk_level": AI_RULE_LEVEL_MAP.get(
                str(item.risk_level or "").upper()
            ),
            "condition": str(item.reason or "").strip() or str(item.title or ""),
            "weight": 12,
            "high_standard": "约定完整、口径一致",
            "mid_standard": "约定不完整或口径不清",
            "low_standard": "未约定或明显不符",
            "source_snapshot": f"ai-analysis:{response_id}",
        },
        rule_id=None,
    )


def add_ai_candidates(items: Sequence[RiskAnalysisItem], *, response_id: str) -> int:
    """把通读分析结论提炼成 AI 候选规则，进入「AI 自进化规则」池。

    按 title 去重（对候选池/合同检查标准/基础规则三方比对），已确认或
    已存在的检查点不重复生成。候选规则不参与审查，确认启用后转入
    「合同检查标准」。返回本次新增数量。
    """

    overlay = load_overlay()
    existing_titles = {
        rule.title.strip()
        for rule in [*overlay.ai_candidates, *overlay.custom_rules, *baseline_rules()]
    }
    existing_ids = {rule.rule_id for rule in overlay.ai_candidates}
    added = 0
    for item in items:
        title = str(item.title or "").strip()
        if not title or title in existing_titles:
            continue
        rule = _candidate_from_analysis(item, response_id=response_id)
        existing_titles.add(title)
        existing_ids.add(rule.rule_id)
        overlay.ai_candidates.append(rule)
        added += 1
    if added:
        _write_overlay(overlay)
    return added


def confirm_candidate(rule_id: str) -> Rule:
    """确认启用 AI 候选规则：转入「合同检查标准」并立即生效。"""

    overlay = load_overlay()
    candidate = next(
        (item for item in overlay.ai_candidates if item.rule_id == rule_id), None
    )
    if candidate is None:
        raise RuleEditError(f"AI 候选规则不存在: {rule_id}")
    overlay.ai_candidates = [
        item for item in overlay.ai_candidates if item.rule_id != rule_id
    ]
    overlay.custom_rules = [
        item for item in overlay.custom_rules if item.rule_id != rule_id
    ]
    overlay.custom_rules.append(candidate)
    overlay.disabled_ids = [item for item in overlay.disabled_ids if item != rule_id]
    overlay.removed_ids = [item for item in overlay.removed_ids if item != rule_id]
    _write_overlay(overlay)
    return candidate


RULE_TOPICS: list[str] = [
    "合同类型",
    "金额",
    "付款",
    "发票",
    "源代码相关（按关键字搜索）",
    "知识产权",
    "新技术架构描述相关",
    "合同主体",
    "合规性/交付问题",
    "软件开发服务合同（0税率）重点检查项",
]


def _rule_row(rule: Rule, *, enabled: bool, overridden: bool) -> dict[str, Any]:
    return {
        "id": rule.rule_id,
        "code": rule.rule_id,
        "title": rule.title,
        "condition": rule.condition or "",
        "topic": rule.category,
        "risk_level": rule.risk_level,
        "status": "active",
        "enabled": enabled,
        "weight": rule.weight,
        "high_standard": rule.high_standard,
        "mid_standard": rule.mid_standard,
        "low_standard": rule.low_standard,
        "suggested_action": None,
        "check_method": rule.check_method,
        "applies_to": list(rule.applies_to),
        "overridden": overridden,
    }


def _group_rows(
    rows: list[dict[str, Any]], topics: list[str]
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row["topic"] or "其他检查", []).append(row)
    names = [name for name in topics if name in buckets]
    names.extend(name for name in buckets if name not in names)
    return [
        {"name": name, "count": len(buckets[name]), "rules": buckets[name]}
        for name in names
    ]


def build_rules_engine_view() -> dict[str, Any]:
    """v1 ``/ai-rules`` 同构的规则引擎库视图数据。"""

    rows: list[dict[str, Any]] = []
    overlay = load_overlay()
    overridden_ids = {rule.rule_id for rule in overlay.custom_rules}
    for rule, enabled in library_rules():
        rows.append(
            _rule_row(
                rule,
                enabled=enabled,
                overridden=rule.rule_id in overridden_ids
                and rule.rule_id in baseline_ids(),
            )
        )

    topics = list(RULE_TOPICS)
    known = set(topics)
    for row in rows:
        if row["topic"] not in known:
            topics.append(row["topic"])
            known.add(row["topic"])

    approval_rules = [row for row in rows]
    # AI 自进化规则池：模型提炼的候选检查点（待确认，不参与审查）。
    overlay = load_overlay()
    ai_rows = []
    for rule in overlay.ai_candidates:
        row = _rule_row(rule, enabled=True, overridden=False)
        row["status"] = "draft"
        ai_rows.append(row)
    return {
        "topics": topics,
        "packs": {
            "approval": {
                "rules": approval_rules,
                "groups": _group_rows(approval_rules, topics),
            },
            # AI 自进化规则：完成审查后由模型提炼的候选检查点会出现在
            # 这里，确认后才进入"合同检查标准"（v1 同款两套规则池）。
            "ai": {
                "rules": ai_rows,
                "groups": _group_rows(ai_rows, topics),
            },
        },
        "editable": True,
    }
