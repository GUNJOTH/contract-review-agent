"""AI 自进化规则库测试（SQLite 标准库，临时库文件）。"""

from contract_review_app.config import settings
from contract_review_app.services.rule_evolution import (
    auto_disable_stale,
    bump_unadopted_misses,
    confirm_rule,
    count_rules,
    disable_rule,
    list_rules,
    load_active_rules,
    record_hits,
    retrieve_relevant_rules,
    rule_exists,
    save_candidate_rules,
)


def _mock_db(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        settings, "CONTRACT_AI_RULES_DB_PATH", str(tmp_path / "ai_rules.db")
    )
    # 去重的向量相似度依赖 embedding 服务与磁盘缓存，测试中全部隔离，
    # 使其退化为标题精确匹配
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_CACHE_DIR",
        str(tmp_path / "embedding_cache"),
    )


def test_save_confirm_disable_roundtrip(monkeypatch, tmp_path):
    _mock_db(monkeypatch, tmp_path)
    saved = save_candidate_rules(
        [
            {
                "title": "履行期限不得早于签订时间",
                "condition": "合同履行期限必须晚于签订日期",
                "risk_level": "BLOCK",
            },
            {
                "title": "付款总额应与合同总额一致",
                "condition": "各期付款比例合计应等于100%",
                "risk_level": "WARN",
            },
        ],
        analysis_id="analysis-test-1",
        contract_type="软件开发/转让服务",
    )
    assert saved == 2
    assert count_rules() == 2
    assert count_rules("draft") == 2
    assert load_active_rules() == []  # 未确认不进入提示池

    rules = {rule["title"]: rule for rule in list_rules()}
    assert len(rules) == 2
    first = rules["履行期限不得早于签订时间"]
    assert first["status"] == "draft"
    assert first["contract_type"] == "软件开发/转让服务"
    assert first["module"]
    assert first["code"]

    confirm_rule(first["id"])
    active = load_active_rules()
    assert len(active) == 1
    assert active[0]["title"] == "履行期限不得早于签订时间"

    disable_rule(first["id"])
    assert load_active_rules() == []
    assert count_rules("disabled") == 1
    assert count_rules("draft") == 1


def test_dedupe_exact_title(monkeypatch, tmp_path):
    _mock_db(monkeypatch, tmp_path)
    suggestion = [
        {
            "title": "付款总额一致",
            "condition": "各期付款比例合计应等于100%",
            "risk_level": "WARN",
        }
    ]
    assert save_candidate_rules(suggestion, analysis_id="a1", contract_type=None) == 1
    assert save_candidate_rules(suggestion, analysis_id="a2", contract_type=None) == 0
    assert count_rules() == 1


def test_invalid_suggestions_skipped(monkeypatch, tmp_path):
    _mock_db(monkeypatch, tmp_path)
    saved = save_candidate_rules(
        [
            {"title": "", "condition": "无标题", "risk_level": "WARN"},
            {"title": "有效检查点", "condition": "检查条件", "risk_level": "block"},
            "not-a-dict",
        ],
        analysis_id="a1",
        contract_type=None,
    )
    assert saved == 1
    rules = list_rules()
    assert rules[0]["title"] == "有效检查点"
    assert rules[0]["risk_level"] == "BLOCK"  # 小写归一化为大写


def test_record_hits_increments_and_ignores_missing(monkeypatch, tmp_path):
    _mock_db(monkeypatch, tmp_path)
    save_candidate_rules(
        [{"title": "履行期限不得早于签订时间", "condition": "期限晚于签订日期", "risk_level": "BLOCK"}],
        analysis_id="a1",
        contract_type=None,
    )
    rule_id = list_rules()[0]["id"]
    confirm_rule(rule_id)

    assert record_hits([rule_id]) == 1
    assert record_hits([rule_id, "no-such-id"]) == 1
    assert record_hits(["no-such-id"]) == 0

    rules = list_rules()
    assert rules[0]["hit_count"] == 2

    assert rule_exists(rule_id) is True
    assert rule_exists("no-such-id") is False


def test_auto_evolve_disables_stale_rules(monkeypatch, tmp_path):
    """自动进化：连续 N 次未被采纳的 active 规则自动停用，被采纳的规则清零计数。"""
    _mock_db(monkeypatch, tmp_path)
    save_candidate_rules(
        [
            {"title": "A 规则", "condition": "检查 A", "risk_level": "BLOCK"},
            {"title": "B 规则", "condition": "检查 B", "risk_level": "WARN"},
        ],
        analysis_id="a1",
        contract_type=None,
    )
    by_title = {rule["title"]: rule for rule in list_rules()}
    id_a, id_b = by_title["A 规则"]["id"], by_title["B 规则"]["id"]
    confirm_rule(id_a)
    confirm_rule(id_b)

    # 4 次审查：A 从未被采纳，B 在第 3 次被采纳一次
    for i in range(4):
        adopted = [id_b] if i == 2 else []
        bump_unadopted_misses(adopted, "软件开发/转让服务")
        if adopted:
            record_hits(adopted)
    assert auto_disable_stale(5) == 0  # 未达阈值，不淘汰
    rows = {r["id"]: r for r in list_rules()}
    assert rows[id_a]["miss_streak"] == 4
    # B 第 3 次被采纳时清零，第 4 次未采纳又 +1
    assert rows[id_b]["miss_streak"] == 1
    assert rows[id_b]["hit_count"] == 1

    # 再 1 次未采纳：A 达 5 次 → 自动停用
    bump_unadopted_misses([], "软件开发/转让服务")
    assert auto_disable_stale(5) == 1
    rows = {r["id"]: r for r in list_rules()}
    assert rows[id_a]["status"] == "disabled"
    assert rows[id_b]["status"] == "active"

    # threshold <= 0 关闭自动淘汰
    bump_unadopted_misses([], "软件开发/转让服务")
    assert auto_disable_stale(0) == 0
    rows = {r["id"]: r for r in list_rules()}
    assert rows[id_b]["status"] == "active"


def test_miss_streak_isolated_by_contract_type(monkeypatch, tmp_path):
    """未采纳计数按合同类型隔离：连续审查其他类型合同不会误淘汰本类型规则。"""
    _mock_db(monkeypatch, tmp_path)
    save_candidate_rules(
        [{"title": "软件开发规则", "condition": "检查", "risk_level": "BLOCK"}],
        analysis_id="a1",
        contract_type="软件开发/转让服务",
    )
    save_candidate_rules(
        [{"title": "服务合同规则", "condition": "检查", "risk_level": "WARN"}],
        analysis_id="a2",
        contract_type="其它服务合同",
    )
    save_candidate_rules(
        [{"title": "通用规则", "condition": "检查", "risk_level": "WARN"}],
        analysis_id="a3",
        contract_type=None,
    )
    for rule in list_rules():
        confirm_rule(rule["id"])

    # 连续 5 次审查"软件开发/转让服务"类型，三条规则都未被采纳
    for _ in range(5):
        bump_unadopted_misses([], "软件开发/转让服务")

    rows = {r["title"]: r for r in list_rules()}
    assert rows["软件开发规则"]["miss_streak"] == 5
    assert rows["通用规则"]["miss_streak"] == 5
    assert rows["服务合同规则"]["miss_streak"] == 0  # 类型不符不累计

    # 淘汰仅作用于匹配类型与通用规则，服务合同规则保留
    assert auto_disable_stale(5) == 2
    rows = {r["title"]: r for r in list_rules()}
    assert rows["服务合同规则"]["status"] == "active"

    # 合同类型未判定（None）：不累计，避免不公平淘汰
    bump_unadopted_misses([], None)
    rows = {r["title"]: r for r in list_rules()}
    assert rows["服务合同规则"]["miss_streak"] == 0


def test_retrieve_relevant_rules_caps_and_filters_type(monkeypatch, tmp_path):
    """规则知识库只返回 top-k，且按合同类型过滤（通用 + 本类型）。"""
    _mock_db(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "CONTRACT_AI_RULE_RETRIEVAL_TOP_K", 2)
    for index in range(5):
        save_candidate_rules(
            [
                {
                    "title": f"软件规则 {index}",
                    "condition": "检查软件开发合同条款",
                    "risk_level": "WARN",
                }
            ],
            analysis_id=f"a-sw-{index}",
            contract_type="软件开发/转让服务",
        )
    save_candidate_rules(
        [{"title": "销售专用规则", "condition": "仅销售合同", "risk_level": "BLOCK"}],
        analysis_id="a-sale",
        contract_type="软件产品销售",
    )
    save_candidate_rules(
        [{"title": "通用付款规则", "condition": "付款总额应一致", "risk_level": "WARN"}],
        analysis_id="a-gen",
        contract_type=None,
    )
    for rule in list_rules():
        confirm_rule(rule["id"])

    retrieved = retrieve_relevant_rules(
        "软件开发合同付款与验收",
        contract_type="软件开发/转让服务",
        top_k=2,
    )
    assert len(retrieved) == 2
    titles = {rule["title"] for rule in retrieved}
    assert "销售专用规则" not in titles

    # 未召回的规则不累计 miss_streak
    retrieved_ids = [rule["id"] for rule in retrieved]
    bump_unadopted_misses([], "软件开发/转让服务", retrieved_ids=retrieved_ids)
    rows = {rule["title"]: rule for rule in list_rules()}
    assert rows["销售专用规则"]["miss_streak"] == 0
    for title in titles:
        assert rows[title]["miss_streak"] == 1


def test_user_create_rule_and_seed_builtin(monkeypatch, tmp_path):
    """用户新增规则立即进入审查池；内置内控规则按编号幂等写入。"""
    _mock_db(monkeypatch, tmp_path)
    from contract_review_app.services.rule_evolution import (
        create_rule,
        seed_builtin_rules,
        set_rule_enabled,
    )

    created = create_rule(
        {
            "title": "验收标准必须明确",
            "condition": "合同应约定验收标准和方法",
            "topic": "合规性/交付问题",
            "risk_level": "WARN",
        }
    )
    assert created["topic"] == "合规性/交付问题"
    assert created["code"].startswith("HTSP-")
    assert created["status"] == "active"
    assert created["weight"] == 12
    assert created["high_standard"]
    assert created["mid_standard"]
    assert created["low_standard"]
    assert load_active_rules()[0]["id"] == created["id"]

    set_rule_enabled(created["id"], False)
    assert load_active_rules() == []

    first = seed_builtin_rules()
    assert first >= 50
    assert seed_builtin_rules() == 0
    codes = {rule["code"] for rule in list_rules()}
    assert "HTSP-202511-001" in codes
    assert "HTSP-202511-050" in codes
    assert "KHSX-202511-001" not in codes
    assert any(rule["source"] == "engine" for rule in list_rules())
    from contract_review_app.services.rule_evolution import (
        delete_rule,
        grouped_rules,
        save_candidate_rules,
        split_rule_packs,
    )
    groups = grouped_rules()
    names = [group["name"] for group in groups]
    assert "合同类型" in names
    assert "金额" in names
    seeded = [rule for rule in list_rules() if rule["code"] == "HTSP-202511-006"][0]
    assert seeded["weight"] == 18
    assert seeded["risk_level"] == "BLOCK"
    assert "大小写" in seeded["high_standard"]
    type_rule = [rule for rule in list_rules() if rule["code"] == "HTSP-202511-001"][0]
    assert type_rule["weight"] == 6
    assert type_rule["risk_level"] == "INFO"
    save_candidate_rules(
        [{"title": "AI 付款检查点", "condition": "付款比例合计应为100%", "risk_level": "WARN"}],
        analysis_id="analysis-pack",
        contract_type=None,
    )
    packs = split_rule_packs()
    assert any(rule["code"].startswith("HTSP-") for rule in packs["approval"])
    assert any(rule["source"] == "ai" for rule in packs["ai"])
    extra = [rule for rule in list_rules() if rule["title"] == "验收标准必须明确"][0]
    delete_rule(extra["id"])
    assert extra["id"] not in [rule["id"] for rule in list_rules()]
