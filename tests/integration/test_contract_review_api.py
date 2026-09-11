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
    assert payload["ai_analysis"] is not None
    assert payload["ai_analysis"]["projection_version"]
    assert payload["ai_analysis"]["analysis_id"].startswith("review-")
    assert payload["ai_analysis"]["provider"] == "deterministic-rule-engine"
    assert payload["ai_analysis"]["items"]


def test_revision_set_api_returns_evidence_bound_contract(monkeypatch):
    """修订提案接口只生成结构化结果，不修改原始合同文件。"""
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")

    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None

    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )
    review_response = client.post(
        "/api/v1/contract-review",
        headers=_auth_headers(),
        files=[("files", ("合同主文.pdf", _make_contract_pdf(), "application/pdf"))],
        data={"PackageId": "pkg-revision-api", "ContractType": "software"},
    )
    assert review_response.status_code == 200, review_response.text

    response = client.post(
        "/api/v1/contract-review/revision-set",
        headers=_auth_headers(),
        json=review_response.json()["review_result"],
    )

    assert response.status_code == 200, response.text
    revision_set = response.json()["revision_set"]
    assert revision_set["run_id"] == review_response.json()["review_result"]["run"][
        "run_id"
    ]
    assert revision_set["base_result_fingerprint"]
    assert revision_set["revision_fingerprint"]
    assert all(change["evidence_ids"] for change in revision_set["changes"])


def test_contract_review_internal_failure_does_not_leak_exception(monkeypatch):
    def fail_review(*_args, **_kwargs):
        raise RuntimeError("provider response contains internal secret")

    monkeypatch.setattr(
        "contract_review_app.api.review_routes.run_contract_review",
        fail_review,
    )
    request_id = "review-failure-request"
    response = client.post(
        "/api/v1/contract-review",
        headers={**_auth_headers(), "X-Request-ID": request_id},
        files=[("files", ("合同主文.pdf", _make_contract_pdf(), "application/pdf"))],
        data={"PackageId": "pkg-failure", "ContractType": "software"},
    )

    assert response.status_code == 500, response.text
    assert "provider response contains internal secret" not in response.text
    assert response.json()["Response"]["Error"]["Code"] == "FailedOperation.ContractReviewFailed"
    assert response.json()["Response"]["RequestId"] == request_id


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


def test_formal_rule_bundle_is_read_only():
    """规则目录由正式 RuleBundle 投影，应用层不再提供第二套 CRUD。"""
    headers = _auth_headers()
    response = client.get("/api/v1/rules", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["bundle_id"]
    assert payload["rules"]
    assert all(rule["read_only"] is True for rule in payload["rules"])

    # 历史路径保留 GET 兼容投影，但过滤和写操作不再复活旧 SQLite 规则库。
    alias = client.get("/api/v1/ai-rules", headers=headers)
    assert alias.status_code == 200, alias.text
    assert alias.json()["bundle_id"] == payload["bundle_id"]
    draft = client.get("/api/v1/ai-rules?status=draft", headers=headers)
    assert draft.status_code == 200
    assert draft.json()["rules"] == []
    assert draft.json()["packs"]["approval"]["rules"] == []
    invalid_module = client.get("/api/v1/rules?module=不存在", headers=headers)
    assert invalid_module.status_code == 400

    rule_id = payload["rules"][0]["rule_id"]
    assert client.post("/api/v1/ai-rules", headers=headers, json={}).status_code == 405
    assert client.put(f"/api/v1/ai-rules/{rule_id}", headers=headers, json={}).status_code in {404, 405}
    assert client.delete(f"/api/v1/ai-rules/{rule_id}", headers=headers).status_code in {404, 405}


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


def test_contract_elements_extracts_fillable_fields(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
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


def test_contract_element_fields_are_read_only():
    headers = _auth_headers()
    resp = client.get("/api/v1/contract-element-fields", headers=headers)
    assert resp.status_code == 200, resp.text
    fields = resp.json()["fields"]
    keys = {item["key"] for item in fields}
    assert "party_a" in keys
    assert "amount" in keys
    assert all(item["enabled"] is True for item in fields)
    assert client.post("/api/v1/contract-element-fields", headers=headers, json={}).status_code == 405
    assert client.put("/api/v1/contract-element-fields/party_a", headers=headers, json={}).status_code in {404, 405}
    assert client.delete("/api/v1/contract-element-fields/party_a", headers=headers).status_code in {404, 405}
