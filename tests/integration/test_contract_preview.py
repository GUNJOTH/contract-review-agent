"""合同原文预览：DOCX 转成可内嵌 HTML，不再把 Word 丢进浏览器。"""

from fastapi.testclient import TestClient

from contract_review_app.config import settings
from contract_review_app.main import app
from contract_review_app.services.document_preview import preview_contract_document
from tests.integration.docx_fixtures import sample_contract_docx

client = TestClient(app)
SAMPLE_DOCX_NAME = "合同样例.docx"


def _auth_headers() -> dict:
    if settings.API_TOKEN:
        return {settings.AUTH_HEADER_NAME: settings.API_TOKEN}
    return {}


def test_preview_docx_renders_embeddable_html():
    content = sample_contract_docx()
    result = preview_contract_document(SAMPLE_DOCX_NAME, content)
    assert result["kind"] == "html"
    assert "技术开发与服务" in result["html"]
    assert "xx集团有限公司" in result["html"]
    assert "<table>" in result["html"]
    assert "当前格式不便内嵌预览" not in result["html"]
    assert result["message"] == ""


def test_preview_doc_explains_unsupported_format():
    result = preview_contract_document("合同.doc", b"legacy-doc")
    assert result["kind"] == "empty"
    assert ".doc" in result["message"]


def test_contract_preview_api_returns_html_for_docx():
    content = sample_contract_docx()
    response = client.post(
        "/api/v1/contract-preview",
        headers=_auth_headers(),
        files=[
            (
                "file",
                (
                    SAMPLE_DOCX_NAME,
                    content,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )
        ],
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "html"
    assert "珠海市同海科技股份有限公司" in payload["html"]
    assert payload["filename"] == SAMPLE_DOCX_NAME
