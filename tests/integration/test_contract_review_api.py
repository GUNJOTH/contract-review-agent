"""合同审查 API 端到端测试（文字版 PDF，无需 GPU/Triton）。"""

import fitz
from fastapi.testclient import TestClient

from contract_review_app.config import settings
from contract_review_app.main import app

client = TestClient(app)


def _auth_headers() -> dict:
    """本地 .env 配置了 API_TOKEN 时按真实鉴权路径携带。"""
    if settings.API_TOKEN:
        return {settings.AUTH_HEADER_NAME: settings.API_TOKEN}
    return {}


def _make_contract_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "甲方与乙方签订软件开发合同，合同金额为人民币一百万元整，付款方式为银行转账。",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def test_contract_review_returns_evidence_first_result(monkeypatch):
    # 测试环境不配置语义端点、不走印章识别（不打外网、不连 Triton）
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )
    pdf = _make_contract_pdf()
    response = client.post(
        "/api/v1/contract-review",
        headers=_auth_headers(),
        files=[("files", ("合同主文.pdf", pdf, "application/pdf"))],
        data={"PackageId": "pkg-test-001", "ContractType": "software"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "review_result" in payload and "ai_analysis" in payload
    review = payload["review_result"]
    assert review["package"]["package_id"] == "pkg-test-001"
    assert review["documents"], "应返回文档信息"
    assert review["documents"][0]["parse_status"] == "parsed"
    assert review["findings"], "确定性规则应产生审核发现"
    assert review["report"]["finding_counts"], "报告应有状态统计"
    assert review["evidence"], "应生成证据对象"
    assert review["run"]["status"] is not None
    assert payload["ai_analysis"] is None  # 测试未配置模型端点


def test_contract_review_engine_rules_can_be_disabled(monkeypatch):
    """关闭 50 条引擎规则时只解析合同，不逐条出结论。"""
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    monkeypatch.setattr(settings, "CONTRACT_ENGINE_RULES_ENABLED", False)
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )
    pdf = _make_contract_pdf()
    response = client.post(
        "/api/v1/contract-review",
        headers=_auth_headers(),
        files=[("files", ("合同主文.pdf", pdf, "application/pdf"))],
        data={"PackageId": "pkg-engine-off", "ContractType": "software"},
    )
    assert response.status_code == 200, response.text
    review = response.json()["review_result"]
    assert review["documents"][0]["parse_status"] == "parsed"
    assert review["findings"] == []
    assert len(review["rule_bundle"]["rules"]) == 1


def test_contract_review_rejects_empty_package(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )
    response = client.post(
        "/api/v1/contract-review",
        headers=_auth_headers(),
        files=[],
        data={"PackageId": "pkg-empty"},
    )
    assert response.status_code == 422  # FastAPI 要求至少一个文件


def test_ai_rules_management_api(monkeypatch, tmp_path):
    """AI 规则库管理 API：列表 → 确认 → 停用 → 404。"""
    monkeypatch.setattr(
        settings, "CONTRACT_AI_RULES_DB_PATH", str(tmp_path / "ai_rules.db")
    )
    from contract_review_app.services.rule_evolution import (
        confirm_rule,
        list_rules,
        save_candidate_rules,
    )

    save_candidate_rules(
        [
            {"title": "履行期限不得早于签订时间", "condition": "期限晚于签订日期", "risk_level": "BLOCK"},
            {"title": "付款总额一致", "condition": "比例合计100%", "risk_level": "WARN"},
        ],
        analysis_id="analysis-api-test",
        contract_type="软件开发/转让服务",
    )
    by_title = {rule["title"]: rule for rule in list_rules()}
    confirm_rule(by_title["履行期限不得早于签订时间"]["id"])
    rule_id = by_title["付款总额一致"]["id"]

    # 列表（全部 + 按状态过滤）
    resp = client.get("/api/v1/ai-rules", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rules"]) == 2

    resp = client.get("/api/v1/ai-rules?status=draft", headers=_auth_headers())
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 1 and rules[0]["status"] == "draft"

    resp = client.get("/api/v1/ai-rules?status=bogus", headers=_auth_headers())
    assert resp.status_code == 400

    # 确认启用 draft → active
    resp = client.post(f"/api/v1/ai-rules/{rule_id}/confirm", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert rule_id in [r["id"] for r in list_rules("active")]

    # 停用 active → disabled
    resp = client.post(f"/api/v1/ai-rules/{rule_id}/disable", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert len(list_rules("disabled")) == 1

    # 不存在的规则 → 404
    resp = client.post("/api/v1/ai-rules/no-such-id/confirm", headers=_auth_headers())
    assert resp.status_code == 404


def test_ai_rules_user_crud_and_module_filter(monkeypatch, tmp_path):
    """用户可新增/编辑规则，按模块过滤，启用开关直接进审查池。"""
    monkeypatch.setattr(
        settings, "CONTRACT_AI_RULES_DB_PATH", str(tmp_path / "ai_rules.db")
    )
    headers = _auth_headers()
    resp = client.post(
        "/api/v1/ai-rules",
        headers=headers,
        json={
            "title": "检验方式及程序是否具体",
            "condition": "若合同未约定检验的具体标准、方法、程序则违规",
            "topic": "合规性/交付问题",
            "risk_level": "WARN",
            "suggested_action": "补充检验标准",
        },
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()["rule"]
    assert created["topic"] == "合规性/交付问题"
    assert created["status"] == "active"
    assert created["enabled"] is True
    assert created["code"].startswith("HTSP-")
    assert created["weight"] == 12
    assert created["high_standard"]

    resp = client.get("/api/v1/ai-rules", headers=headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["rules"]) == 1
    assert payload["modules"] == ["风险点", "合理性", "内控", "资信"]
    assert payload["groups"][0]["name"] == "合规性/交付问题"
    assert payload["packs"]["approval"]["rules"][0]["id"] == created["id"]
    assert payload["packs"]["ai"]["rules"] == []

    resp = client.get("/api/v1/ai-rules?module=资信", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["rules"] == []

    resp = client.put(
        f"/api/v1/ai-rules/{created['id']}",
        headers=headers,
        json={"title": "检验方式及程序是否具体", "risk_level": "BLOCK"},
    )
    assert resp.status_code == 200
    assert resp.json()["rule"]["risk_level"] == "BLOCK"

    resp = client.post(f"/api/v1/ai-rules/{created['id']}/disable", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    resp = client.post(f"/api/v1/ai-rules/{created['id']}/enable", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    resp = client.request(
        "DELETE", f"/api/v1/ai-rules/{created['id']}", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    resp = client.get("/api/v1/ai-rules", headers=headers)
    assert resp.json()["rules"] == []


def _make_element_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "合同名称：软件开发合同\n"
        "合同编号：HT-2026-001\n"
        "甲方：某某科技有限公司\n"
        "乙方：某某软件有限公司\n"
        "合同金额：人民币1000000元\n"
        "付款方式：银行转账\n"
        "税率：13%\n"
        "签订日期：2026年1月15日",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def test_contract_elements_extracts_fillable_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    monkeypatch.setattr(
        settings, "CONTRACT_ELEMENT_SCHEMA_PATH", str(tmp_path / "element_fields.db")
    )
    response = client.post(
        "/api/v1/contract-elements",
        headers=_auth_headers(),
        files=[("files", ("合同主文.pdf", _make_element_pdf(), "application/pdf"))],
        data={"PackageId": "pkg-extract-001"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    fillable = payload["fillable"]
    assert fillable["party_a"] == "某某科技有限公司"
    assert fillable["party_b"] == "某某软件有限公司"
    assert fillable["contract_no"] == "HT-2026-001"
    assert "1000000" in fillable["amount"]
    assert fillable["tax_rate"] == "13%"
    assert fillable["payment_method"] == "银行转账"
    assert "某某科技有限公司" in payload["suggestions"]["party_a"]
    party_a = next(item for item in payload["fields"] if item["key"] == "party_a")
    assert party_a["candidates"][0]["value"] == "某某科技有限公司"


def test_contract_element_fields_can_be_customized(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "CONTRACT_ELEMENT_SCHEMA_PATH", str(tmp_path / "element_fields.db")
    )
    headers = _auth_headers()
    resp = client.get("/api/v1/contract-element-fields", headers=headers)
    assert resp.status_code == 200, resp.text
    keys = {item["key"] for item in resp.json()["fields"]}
    assert "party_a" in keys
    assert "amount" in keys

    resp = client.post(
        "/api/v1/contract-element-fields",
        headers=headers,
        json={"label": "联系人", "aliases": ["联系人", "项目联系人"]},
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()["field"]
    assert created["label"] == "联系人"
    assert created["enabled"] is True

    resp = client.put(
        f"/api/v1/contract-element-fields/{created['key']}",
        headers=headers,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["field"]["enabled"] is False

    resp = client.delete(
        f"/api/v1/contract-element-fields/{created['key']}",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
