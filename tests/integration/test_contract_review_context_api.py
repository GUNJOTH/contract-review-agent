"""合同审查业务上下文 API 契约测试。"""

import fitz
from fastapi.testclient import TestClient

from contract_review_app.config import settings
from contract_review_app.main import app

client = TestClient(app)


def _auth_headers() -> dict[str, str]:
    if settings.API_TOKEN:
        return {settings.AUTH_HEADER_NAME: settings.API_TOKEN}
    return {}


def _make_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "合同金额为人民币一百万元，双方应在十个工作日内完成交付。")
    payload = document.tobytes()
    document.close()
    return payload


def test_contract_review_persists_context_and_applies_rule_scope(monkeypatch):
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
        files=[("files", ("合同.pdf", _make_pdf(), "application/pdf"))],
        data={
            "PackageId": "pkg-context-contract",
            "ContractType": "软件开发/转让服务",
            "PartyPosition": "甲方",
            "Jurisdiction": "中国大陆",
            "TransactionContext": "软件开发项目采购，关注付款与金额口径。",
            "ReviewScope": "金额",
        },
    )

    assert response.status_code == 200, response.text
    review = response.json()["review_result"]
    assert review["review_context"] == {
        "context_version": "1.0",
        "contract_type": "软件开发/转让服务",
        "party_position": "buyer",
        "jurisdiction": "中国大陆",
        "transaction_context": "软件开发项目采购，关注付款与金额口径。",
        "review_scope": ["金额"],
    }
    assert review["run"]["configuration"]["selected_rule_ids"]
    assert all(
        question["category"] == "金额"
        for question in review["review_questions"]
    )
    assert all(
        review["rule_bundle"]["rules"][
            next(
                index
                for index, rule in enumerate(review["rule_bundle"]["rules"])
                if rule["rule_id"] == finding["rule_id"]
            )
        ]["category"]
        == "金额"
        for finding in review["findings"]
    )

