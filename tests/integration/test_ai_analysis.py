"""AI 风险分析服务测试（mock 模型调用与 embedding，不打外网）。"""

import json

import fitz

from contract_review_app.config import settings
from contract_review_app.services.ai_analysis import (
    AIRiskItem,
    run_ai_analysis,
)
from contract_review_app.services.review_service import run_contract_review


def _chat_fake(responses: list[str]):
    """按调用顺序返回模型响应内容的 fake（第一轮/第二轮），并记录请求体。"""
    state = {"n": 0, "payloads": []}

    def fake_post(endpoint: str, json=None, headers=None, timeout=None):
        del endpoint, headers, timeout
        state["payloads"].append(json)
        index = min(state["n"], len(responses) - 1)
        state["n"] += 1
        return _FakeResponse(
            {"choices": [{"message": {"content": responses[index]}}]}
        )

    fake_post.state = state
    return fake_post


def _tool_then_json_fake(final_content: str, followup_content: str = '{"items": []}'):
    """第一轮先发起 search_rules，再输出最终 JSON；后续轮次返回 followup。"""
    state = {"n": 0, "payloads": []}

    def fake_post(endpoint: str, json=None, headers=None, timeout=None):
        del endpoint, headers, timeout
        state["payloads"].append(json)
        state["n"] += 1
        if state["n"] == 1:
            return _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_search_1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rules",
                                            "arguments": '{"query": "付款比例"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        if state["n"] == 2:
            return _FakeResponse(
                {"choices": [{"message": {"content": final_content}}]}
            )
        return _FakeResponse(
            {"choices": [{"message": {"content": followup_content}}]}
        )

    fake_post.state = state
    return fake_post


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeEmbedding:
    """按文本关键词返回确定性三维向量（替换 _call_embedding_api）。"""

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "期限" in text:
                vectors.append([1.0, 0.0, 0.0])
            elif "付款" in text:
                vectors.append([0.5, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


def _mock_embeddings(monkeypatch, tmp_path) -> None:
    """隔离 embedding（_call_embedding_api 替换为本地假向量）与规则库。"""
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "qwen3-embedding-8b"
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_EMBEDDING_CACHE_DIR",
        str(tmp_path / "embedding_cache"),
    )
    monkeypatch.setattr(
        settings, "CONTRACT_AI_RULES_DB_PATH", str(tmp_path / "ai_rules.db")
    )
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index._call_embedding_api",
        _FakeEmbedding(),
    )


def _make_contract_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "甲方与乙方签订技术开发合同，合同金额为人民币一百万元整，含税，"
        "付款方式为验收合格后一次性支付。",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def _build_review_result(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )
    return run_contract_review(
        [("合同主文.pdf", _make_contract_pdf())],
        package_id="pkg-ai-001",
        contract_type="软件开发/转让服务",
    )


def test_ai_analysis_disabled_without_endpoint(monkeypatch):
    result = _build_review_result(monkeypatch)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    assert run_ai_analysis(result) is None


def test_ai_analysis_parses_items_and_binds_evidence(monkeypatch, tmp_path):
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    monkeypatch.setattr(
        settings, "CONTRACT_AI_ANALYSIS_PROMPT_VERSION", "contract-ai-analysis-v1"
    )
    monkeypatch.setattr(
        "contract_review_app.services.ai_analysis._post",
        _chat_fake(
            [
                '{"contract_type": {"name": "软件开发/转让服务", '
                '"basis": "技术开发与服务（委托）合同"}, '
                '"items": [{"title": "付款条件风险", "risk_level": "warn", '
                '"reason": "验收合格后一次性支付，乙方垫资风险高", '
                '"quote": "付款方式为验收合格后一次性支付", '
                '"suggested_action": "建议改为分阶段付款"}], '
                '"rule_suggestions": [{"title": "履行期限不得早于签订时间", '
                '"condition": "合同履行期限必须晚于签订日期", '
                '"risk_level": "BLOCK"}]}',
                '{"items": []}',  # 第二轮：无补充
            ]
        ),
    )

    analysis = run_ai_analysis(result)

    assert analysis is not None
    assert analysis.model_version == "test-model"
    assert analysis.contract_type == {
        "name": "软件开发/转让服务",
        "basis": "技术开发与服务（委托）合同",
    }
    # AI 提炼的候选规则已入库（draft，待人工确认）
    from contract_review_app.services.rule_evolution import count_rules, list_rules

    assert count_rules("draft") == 1
    assert list_rules()[0]["title"] == "履行期限不得早于签订时间"
    # 清单 = AI 项（含合同类型判定）+ 规则层 UNKNOWN 兜底（PASS/NA 不展示）
    assert len(analysis.items) > 2
    item = analysis.items[0]
    assert isinstance(item, AIRiskItem)
    assert item.source == "ai"
    assert item.risk_level == "WARN"  # 小写归一化为大写
    assert item.module in {"风险点", "合理性", "内控", "资信"}
    assert set(analysis.panels.keys()) == {"风险点", "合理性", "内控", "资信"}
    assert sum(len(group) for group in analysis.panels.values()) == len(analysis.items)
    assert item.evidence_ids, "引用片段应绑定到文本块证据"
    # 验证绑定的证据确实对应正文块
    evidence_ids = {e.evidence_id for e in result.evidence}
    assert item.evidence_ids[0] in evidence_ids
    # AI 判定类型后：分类规则的"适用性未定"UNKNOWN 由类型判定项替代
    from contract_review_app.services.ai_analysis import CONTRACT_TYPE_NAMES

    assert not any(
        i.risk_level == "UNKNOWN" and i.title in CONTRACT_TYPE_NAMES
        for i in analysis.items
    ), "分类规则 UNKNOWN 应由类型判定项替代"
    assert any(
        "合同类型判定" in i.title and i.source == "ai"
        for i in analysis.items
    ), "应展示 AI 的合同类型判定项"
    # 规则层兜底项存在，PASS/NOT_APPLICABLE 不展示，UNKNOWN 展示
    rule_items = [i for i in analysis.items if i.source == "rule"]
    assert rule_items, "规则层结论应兜底展示"
    assert not any(i.risk_level == "PASS" for i in analysis.items), "PASS 不应展示"
    assert not any(
        i.risk_level == "NOT_APPLICABLE" for i in analysis.items
    ), "NOT_APPLICABLE 不应展示"
    assert any(i.risk_level == "UNKNOWN" for i in analysis.items), "UNKNOWN 应展示"


def test_ai_analysis_model_failure_keeps_rule_items(monkeypatch, tmp_path):
    """模型调用失败时，风险清单保留规则层 BLOCK/WARN/UNKNOWN/NOT_APPLICABLE 兜底。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )

    class _BrokenResponse:
        def raise_for_status(self):
            raise RuntimeError("model down")

    monkeypatch.setattr(
        "contract_review_app.services.ai_analysis._post",
        lambda *a, **k: _BrokenResponse(),
    )

    analysis = run_ai_analysis(result)
    assert analysis is not None
    assert analysis.items, "模型失败时仍应返回规则层兜底项"
    assert all(item.source == "rule" for item in analysis.items)
    assert not any(item.risk_level == "PASS" for item in analysis.items)


def test_ai_analysis_second_pass_covers_uncovered_hints(monkeypatch, tmp_path):
    """未覆盖的规则点由第二轮 AI 复核补充，不再展示规则层 BLOCK/WARN 兜底。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    monkeypatch.setattr(
        "contract_review_app.services.ai_analysis._post",
        _chat_fake(
            [
                # 第一轮：只输出付款风险（不含"不含税"提示 → 触发第二轮）
                '{"items": [{"title": "付款条件风险", "risk_level": "WARN", '
                '"reason": "验收合格后一次性支付", "quote": "一次性支付", '
                '"suggested_action": "分阶段付款"}]}',
                # 第二轮：补充未覆盖的"含税"风险点
                '{"items": [{"title": "含税金额口径风险", "risk_level": "WARN", '
                '"reason": "合同出现含税表述，需确认不含税金额口径", '
                '"quote": "含税", "suggested_action": "人工核对口径"}]}',
            ]
        ),
    )

    analysis = run_ai_analysis(result)

    ai_titles = [item.title for item in analysis.items if item.source == "ai"]
    rule_titles = [item.title for item in analysis.items if item.source == "rule"]
    assert any("含税" in title for title in ai_titles), "第二轮 AI 应补充未覆盖的规则点"
    assert not any("不含税" in title for title in rule_titles), (
        "规则层 BLOCK/WARN 不再兜底展示（已由第二轮 AI 复核）"
    )


def test_ai_analysis_merges_rule_risk_items(monkeypatch, tmp_path):
    """模型空输出时：规则层 UNKNOWN/NOT_APPLICABLE 仍兜底展示，PASS 不展示。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")

    def fake_post(endpoint: str, json=None, headers=None, timeout=None):
        del endpoint, json, headers, timeout
        content = '{"items": []}'  # 模型本次没有输出
        return _FakeResponse({"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake_post)

    analysis = run_ai_analysis(result)

    assert analysis is not None
    rule_items = [item for item in analysis.items if item.source == "rule"]
    assert rule_items, "规则层 UNKNOWN/NOT_APPLICABLE 应兜底展示"
    assert not any(item.risk_level == "PASS" for item in analysis.items)
    evidence_ids = {e.evidence_id for e in result.evidence}
    assert any(
        item.evidence_ids and item.evidence_ids[0] in evidence_ids
        for item in rule_items
    )


def test_ai_rules_injected_into_prompt_pool(monkeypatch, tmp_path):
    """active 规则改为通过 search_rules 工具查询，不再预注入 rule_hints。"""
    from contract_review_app.services.rule_evolution import (
        confirm_rule,
        list_rules,
        save_candidate_rules,
    )

    _mock_embeddings(monkeypatch, tmp_path)
    save_candidate_rules(
        [
            {
                "title": "履行期限不得早于签订时间",
                "condition": "履行期限必须晚于签订日期",
                "risk_level": "BLOCK",
            }
        ],
        analysis_id="analysis-prev",
        contract_type="软件开发/转让服务",
    )
    confirm_rule(list_rules()[0]["id"])

    result = _build_review_result(monkeypatch)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    fake = _chat_fake(
        [
            '{"items": [{"title": "付款条件风险", "risk_level": "WARN", '
            '"reason": "一次性支付", "quote": "一次性支付"}]}',
            '{"items": []}',
        ]
    )
    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake)

    run_ai_analysis(result)

    first_body = fake.state["payloads"][0]
    assert "tools" in first_body
    assert first_body["tools"][0]["function"]["name"] == "search_rules"
    user_content = json.loads(first_body["messages"][1]["content"])
    hints = user_content.get("rule_hints") or []
    assert all(hint.get("source") != "ai-rule" for hint in hints)


def test_ai_analysis_cached_result_is_consistent(monkeypatch, tmp_path):
    """同一审查结果第二次分析直接返回缓存，不再调用模型，结果完全一致。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    fake = _chat_fake(
        [
            '{"items": [{"title": "付款条件风险", "risk_level": "WARN", '
            '"reason": "验收合格后一次性支付", "quote": "一次性支付", '
            '"suggested_action": "分阶段付款"}]}'
        ]
    )
    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake)

    first = run_ai_analysis(result)
    calls_after_first = fake.state["n"]
    second = run_ai_analysis(result)

    assert fake.state["n"] == calls_after_first, "第二次应命中缓存，不再调用模型"
    assert first is not None and second is not None
    assert first.model_dump() == second.model_dump()


def _inject_applicability_unknown(monkeypatch):
    """往规则层结论里注入一条引擎'适用性未定'的 UNKNOWN 项（如税率规则）。"""
    import contract_review_app.services.ai_analysis as ai_analysis

    real = ai_analysis._rule_finding_items

    def fake_rule_items(result):
        items = real(result)
        items.append(
            AIRiskItem(
                risk_id="rule-risk-999",
                title="税率",
                risk_level="UNKNOWN",
                reason="当前合同类型未能从来源规则中确定本规则的适用性。",
                quote=None,
                evidence_ids=[],
                suggested_action="补充合同类型事实及其原文证据。",
                source="rule",
            )
        )
        return items

    monkeypatch.setattr(ai_analysis, "_rule_finding_items", fake_rule_items)


def test_applicability_unknown_resolved_by_determined_type(monkeypatch, tmp_path):
    """AI 判定合同类型后，'适用性未定'的 UNKNOWN 规则项按该类型交 AI 二轮复核，
    复核成功即按 AI 结论处置，不再兜底展示。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    _inject_applicability_unknown(monkeypatch)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    fake = _chat_fake(
        [
            '{"contract_type": {"name": "软件开发/转让服务", '
            '"basis": "技术开发与服务（委托）合同"}, "items": []}',
            '{"items": []}',  # 二轮复核成功：按判定类型核实，无补充
        ]
    )
    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake)

    analysis = run_ai_analysis(result)

    assert analysis is not None
    assert analysis.contract_type == {"name": "软件开发/转让服务", "basis": "技术开发与服务（委托）合同"}
    # 复核成功后：适用性未定的 UNKNOWN（含注入项）一律不再兜底展示
    from contract_review_app.services.ai_analysis import APPLICABILITY_UNKNOWN_MARKER

    assert all(
        APPLICABILITY_UNKNOWN_MARKER not in (item.reason or "")
        for item in analysis.items
    ), "AI 已判定类型且复核成功，适用性未定的 UNKNOWN 不应再展示"
    assert not any(item.risk_id == "rule-risk-999" for item in analysis.items)
    # 二轮请求应带上判定的合同类型和该规则点（在用户消息 JSON 内）
    import json as _json

    followup_body = fake.state["payloads"][1]
    followup_msg = _json.loads(followup_body["messages"][1]["content"])
    assert followup_msg["contract_type"] == "软件开发/转让服务"
    assert any(h["title"] == "税率" for h in followup_msg["uncovered_hints"])


def test_applicability_unknown_kept_when_followup_fails(monkeypatch, tmp_path):
    """二轮复核失败时，'适用性未定'的 UNKNOWN 规则项保留展示（可见兜底）。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    _inject_applicability_unknown(monkeypatch)
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    state = {"n": 0}
    round1 = (
        '{"contract_type": {"name": "软件开发/转让服务", '
        '"basis": "技术开发与服务（委托）合同"}, "items": []}'
    )

    def fake_post(endpoint, json=None, headers=None, timeout=None):
        del endpoint, json, headers, timeout
        state["n"] += 1
        if state["n"] == 1:
            return _FakeResponse({"choices": [{"message": {"content": round1}}]})
        raise RuntimeError("followup failed")

    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake_post)

    analysis = run_ai_analysis(result)

    assert analysis is not None
    assert any(
        item.risk_id == "rule-risk-999"
        and "未能从来源规则中确定本规则的适用性" in (item.reason or "")
        for item in analysis.items
    ), "二轮复核失败时适用性未定的 UNKNOWN 应保留兜底展示"


def test_ai_analysis_search_rules_tool_then_json(monkeypatch, tmp_path):
    """模型先 search_rules 查知识库，再输出 JSON；规则不预注入提示词。"""
    result = _build_review_result(monkeypatch)
    _mock_embeddings(monkeypatch, tmp_path)
    from contract_review_app.services.rule_evolution import confirm_rule, save_candidate_rules

    save_candidate_rules(
        [
            {
                "title": "付款比例应明确分阶段",
                "condition": "验收合格后一次性支付视为垫资风险",
                "risk_level": "WARN",
            }
        ],
        analysis_id="analysis-tool-test",
        contract_type="软件开发/转让服务",
    )
    confirm_rule(
        __import__(
            "contract_review_app.services.rule_evolution", fromlist=["list_rules"]
        ).list_rules()[0]["id"]
    )
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/v1/chat/completions"
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    fake = _tool_then_json_fake(
        '{"contract_type": {"name": "软件开发/转让服务", '
        '"basis": "技术开发合同"}, '
        '"items": [{"title": "付款条件风险", "risk_level": "WARN", '
        '"reason": "验收合格后一次性支付", "quote": "一次性支付", '
        '"suggested_action": "分阶段付款"}], '
        '"rule_suggestions": []}'
    )
    monkeypatch.setattr("contract_review_app.services.ai_analysis._post", fake)

    analysis = run_ai_analysis(result)

    assert analysis is not None
    first_body = fake.state["payloads"][0]
    assert "tools" in first_body
    user_payload = json.loads(first_body["messages"][1]["content"])
    assert all(hint.get("source") != "ai-rule" for hint in user_payload.get("rule_hints", []))
    assert fake.state["n"] >= 2
    assert any(item.title == "付款条件风险" for item in analysis.items)
