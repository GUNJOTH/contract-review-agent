"""规则覆盖层编辑、候选确认和并发写入回归测试。"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from pathlib import Path

from contract_review.models import Rule
from contract_review_app.services import rule_edits


def _existing_rule() -> Rule:
    return Rule(
        rule_id="BASE-RULE-001",
        version="v0.14",
        title="原始合同类型规则",
        category="合同类型",
        applies_to=["软件产品销售"],
        check_method="classification",
        applicability={
            "软件产品销售": {"applicability": "required"},
            "软件开发/转让服务": {
                "applicability": "unspecified",
                "note": "保留原始适用性声明",
            },
        },
        source_snapshot="合同审批检查自查标准_v0.14.xlsx#BASE-RULE-001",
        required_evidence=["contract_type"],
        human_review=True,
    )


def _new_rule_payload(rule_id: str) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "version": "v1",
        "title": f"并发规则 {rule_id}",
        "category": "金额",
        "check_method": "keyword",
        "condition": "合同中应出现付款节点",
        "risk_level": "medium",
        "weight": 10,
        "high_standard": "约定完整",
        "mid_standard": "约定不完整",
        "low_standard": "未约定",
        "source_snapshot": "rules-engine#test",
    }


def test_editing_existing_rule_preserves_scope_and_provenance(monkeypatch, tmp_path):
    """编辑表单缺少高级字段时，不能把原规则扩大成全类型新规则。"""

    original = _existing_rule()
    target = Path(tmp_path) / "custom-rules.json"
    monkeypatch.setattr(rule_edits, "custom_rules_path", lambda: target)
    monkeypatch.setattr(rule_edits, "baseline_rules", lambda: [original])

    updated = rule_edits.upsert_rule(
        {
            "title": "修改后的合同类型规则",
            "category": "合同类型",
            "check_method": "classification",
            "condition": "修改后的判定条件",
            "weight": 12,
        },
        rule_id=original.rule_id,
    )

    assert updated.applies_to == original.applies_to
    assert updated.applicability == original.applicability
    assert updated.version == original.version
    assert updated.source_snapshot == original.source_snapshot
    assert updated.required_evidence == original.required_evidence
    assert updated.human_review is True


def test_concurrent_overlay_updates_are_serialized(monkeypatch, tmp_path):
    """两个并发编辑都必须保留，不能发生最后一次写入覆盖前一次写入。"""

    target = Path(tmp_path) / "custom-rules.json"
    monkeypatch.setattr(rule_edits, "custom_rules_path", lambda: target)
    monkeypatch.setattr(rule_edits, "baseline_rules", lambda: [])

    active_loads = 0
    max_active_loads = 0
    state_lock = threading.Lock()
    original_load = rule_edits.load_overlay

    def tracking_load():
        nonlocal active_loads, max_active_loads
        with state_lock:
            active_loads += 1
            max_active_loads = max(max_active_loads, active_loads)
        try:
            # 放大未加锁读改写的重叠窗口，确保回归测试能稳定触发旧缺陷。
            time.sleep(0.02)
            return original_load()
        finally:
            with state_lock:
                active_loads -= 1

    monkeypatch.setattr(rule_edits, "load_overlay", tracking_load)
    rule_ids = ["CUSTOM-CONCURRENT-A", "CUSTOM-CONCURRENT-B"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda item: rule_edits.upsert_rule(_new_rule_payload(item)),
                rule_ids,
            )
        )

    overlay = original_load()
    assert {rule.rule_id for rule in overlay.custom_rules} == set(rule_ids)
    assert max_active_loads == 1


def test_ai_candidate_table_exposes_direct_confirmation_action():
    """候选规则必须显示确认动作，而不是把确认误绑到关闭开关。"""

    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "contract_review_app"
        / "static"
        / "js"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'const isDraft = rule.status === "draft";' in source
    assert 'text: isDraft ? "确认启用"' in source
    assert "onclick: () => isDraft ? confirmCandidate(rule)" in source
    assert "async function confirmCandidate(rule)" in source
