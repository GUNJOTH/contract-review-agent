"""PII fail-closed 门禁与外部语义客户端边界测试。"""

import pytest
import fitz

from contract_review.models import KnowledgeChunk, SemanticModelRequest
from contract_review.semantic import SemanticClientError
from contract_review_app.config import settings
from contract_review_app.services.pii_gate import gate_external_model_input, scan_text
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.review_service import run_contract_review
from contract_review_app.services.ai_analysis import run_ai_analysis
from contract_review_app.services.vector_knowledge_index import embed_texts


def test_scan_text_returns_types_without_raw_values():
    findings = scan_text(
        "联系人电话：13800138000，邮箱：legal@example.test，身份证：11010519491231002X。"
    )
    assert {item.kind for item in findings} == {
        "email",
        "mainland_id",
        "mainland_mobile",
    }
    payload = gate_external_model_input([{"text": "联系人电话：13800138000"}])
    assert payload.decision == "block"
    assert "13800138000" not in payload.model_dump_json()


def test_scanner_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "contract_review_app.services.pii_gate.scan_text",
        lambda _text: (_ for _ in ()).throw(RuntimeError("scanner unavailable")),
    )
    result = gate_external_model_input([{"text": "普通合同正文"}])
    assert result.blocked
    assert result.findings[0].kind == "scanner_error"


def test_invalid_gate_mode_is_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_AI_PII_MODE", "redact-without-review")
    result = gate_external_model_input([{"text": "普通合同正文"}])
    assert result.blocked
    assert result.findings[0].kind == "invalid_configuration"


def test_invalid_gate_switch_is_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_AI_PII_GATE_ENABLED", None)
    result = gate_external_model_input([{"text": "普通合同正文"}])
    assert result.blocked
    assert result.findings[0].kind == "invalid_configuration"


def test_semantic_client_blocks_before_http_call(monkeypatch):
    request = SemanticModelRequest(
        request_id="req-pii",
        provider="test-provider",
        model_version="test-model",
        prompt_version="prompt-v1",
        request_fingerprint="f" * 64,
        rule_ids=["R1"],
        system_instruction="只输出 JSON",
        context_chunks=[
            KnowledgeChunk(
                chunk_id="chunk-1",
                source_name="contract.pdf",
                source_sha256="a" * 64,
                source_version="parser-v1",
                content="联系人电话：13800138000",
                evidence_ids=["e1"],
            )
        ],
    )
    monkeypatch.setattr(
        "contract_review_app.services.semantic_client.httpx.post",
        lambda *_args, **_kwargs: pytest.fail("PII gate must run before HTTP"),
    )
    client = RelaySemanticReviewer(
        endpoint="http://fake/v1/chat/completions",
        model_version="test-model",
    )
    with pytest.raises(SemanticClientError, match="PII"):
        client.review(request)


def test_embedding_input_is_also_blocked(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "http://fake/embeddings")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setattr(
        "contract_review_app.services.vector_knowledge_index.httpx.post",
        lambda *_args, **_kwargs: pytest.fail("PII gate must run before embedding HTTP"),
    )
    with pytest.raises(ValueError, match="PII"):
        embed_texts(["联系人电话：13800138000"], use_cache=False)


def test_ai_analysis_returns_explicit_block_without_http_call(monkeypatch, tmp_path):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "联系人电话：13800138000", fontname="china-s")
    pdf = document.tobytes()
    document.close()
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/model")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-model")
    monkeypatch.setattr(settings, "CONTRACT_SEAL_DETECTION_ENABLED", False)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_CACHE_DIR", str(tmp_path / "review-cache"))
    monkeypatch.setattr(settings, "CONTRACT_AI_RULES_DB_PATH", str(tmp_path / "ai-rules.db"))
    monkeypatch.setattr(
        "contract_review_app.services.ai_analysis._post",
        lambda *_args, **_kwargs: pytest.fail("PII gate must run before AI HTTP"),
    )
    result = run_contract_review(
        [("pii.pdf", pdf)], package_id="pkg-pii-ai", contract_type="其它服务合同"
    )
    analysis = run_ai_analysis(result)
    assert analysis is not None
    assert analysis.status == "blocked"
    assert analysis.provider == "pii-gate"
    assert analysis.blocked_pii_types == ["mainland_mobile"]
