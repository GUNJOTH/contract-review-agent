"""合同审查 API 端到端测试（文字版 PDF，无需 GPU/Triton）。"""

from collections import Counter

import pymupdf
from fastapi.testclient import TestClient

from contract_review.models import AssessmentOutcome, FindingStatus, ReviewResult
from contract_review.replay import build_result_fingerprint
from contract_review_app.config import settings
from contract_review_app.main import app

client = TestClient(app)


def _auth_headers() -> dict:
    """本地 .env 配置了 API_TOKEN 时按真实鉴权路径携带。"""
    if settings.API_TOKEN:
        return {settings.AUTH_HEADER_NAME: settings.API_TOKEN}
    return {}


def _make_contract_pdf() -> bytes:
    doc = pymupdf.open()
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
    assert set(payload) == {"review_result", "cached"}
    assert payload["cached"] is False
    review = payload["review_result"]
    assert review["package"]["package_id"] == "pkg-test-001"
    assert review["documents"], "应返回文档信息"
    assert review["documents"][0]["parse_status"] == "parsed"
    assert review["findings"], "确定性规则应产生审核发现"
    assert review["report"]["finding_counts"], "报告应有状态统计"
    assert review["evidence"], "应生成证据对象"
    assert review["run"]["status"] is not None


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


def test_review_decision_and_finalize_api_update_the_core_result(monkeypatch):
    """人工动作只接收完整 ReviewResult，并返回同一核心对象的更新版本。"""
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
        data={"PackageId": "pkg-actions-api", "ContractType": "software"},
    )
    assert review_response.status_code == 200, review_response.text
    review_result = review_response.json()["review_result"]
    actionable = [
        finding
        for finding in review_result["findings"]
        if finding["status"] in {"WARN", "BLOCK", "UNKNOWN"}
    ]
    assert actionable

    for finding in actionable:
        decision_response = client.post(
            "/api/v1/contract-review/decision",
            headers=_auth_headers(),
            json={
                "review_result": review_result,
                "finding_id": finding["finding_id"],
                "decision": "ACCEPT",
                "actor_id": "reviewer-api",
                "actor_role": "legal",
                "comment": "已核对合同原文和规则依据。",
            },
        )
        assert decision_response.status_code == 200, decision_response.text
        review_result = decision_response.json()["review_result"]

    finalize_response = client.post(
        "/api/v1/contract-review/finalize",
        headers=_auth_headers(),
        json={
            "review_result": review_result,
            "actor_id": "reviewer-api",
            "comment": "完成合同审查人工确认。",
        },
    )
    assert finalize_response.status_code == 200, finalize_response.text
    finalized = finalize_response.json()["review_result"]
    assert finalized["run"]["status"] == "FINALIZED"
    assert finalized["report"]["review_required"] is False
    assert len(finalized["decisions"]) == len(actionable)


def test_client_recomputed_result_fingerprint_is_rejected(monkeypatch):
    """客户端重算公开哈希后，仍不能提交被篡改的发现结论。"""

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
        data={"PackageId": "pkg-client-tamper-api", "ContractType": "software"},
    )
    assert review_response.status_code == 200, review_response.text
    result = ReviewResult.model_validate(review_response.json()["review_result"])
    target = next(
        finding for finding in result.findings if finding.status != FindingStatus.BLOCK
    )
    tampered_findings = [
        finding.model_copy(
            update={
                "status": FindingStatus.BLOCK,
                "automatic": False,
                "reason": "客户端伪造的高风险结论",
            }
        )
        if finding.finding_id == target.finding_id
        else finding
        for finding in result.findings
    ]
    tampered_assessments = [
        assessment.model_copy(
            update={
                "outcome": AssessmentOutcome.CONTRADICTED,
                "reason": "客户端伪造的高风险结论",
            }
        )
        if assessment.finding_id == target.finding_id
        else assessment
        for assessment in result.question_assessments
    ]
    counts = Counter(finding.status.value for finding in tampered_findings)
    tampered = result.model_copy(
        update={
            "findings": tampered_findings,
            "question_assessments": tampered_assessments,
            "report": result.report.model_copy(
                update={
                    "overall_status": FindingStatus.BLOCK,
                    "finding_counts": dict(counts),
                }
            ),
        }
    )
    tampered = tampered.model_copy(
        update={
            "run": tampered.run.model_copy(
                update={"result_fingerprint": build_result_fingerprint(tampered)}
            )
        }
    )

    response = client.post(
        "/api/v1/contract-review/decision",
        headers=_auth_headers(),
        json={
            "review_result": tampered.model_dump(mode="json"),
            "finding_id": target.finding_id,
            "decision": "ACCEPT",
            "actor_id": "attacker-probe",
            "actor_role": "legal",
            "comment": "不应接受重算指纹后的伪造结果。",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["Response"]["Error"]["Code"] == (
        "Conflict.ReviewResultChanged"
    )


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
    """规则接口直接返回正式 RuleBundle，不创建第二套列表契约。"""
    headers = _auth_headers()
    response = client.get("/api/v1/contract-review/rule-bundle", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["bundle_id"]
    assert payload["rules"]


def test_legacy_review_interfaces_are_removed():
    """旧风险、要素和规则列表接口不再出现在产品路由中。"""
    headers = _auth_headers()
    for path in (
        "/api/v1/rules",
        "/api/v1/ai-rules",
        "/api/v1/contract-elements",
        "/api/v1/contract-elements-async",
        "/api/v1/contract-element-fields",
    ):
        assert client.get(path, headers=headers).status_code == 404
    assert client.post(
        "/api/v1/tasks",
        headers=headers,
        data={"task_type": "contract-review"},
    ).status_code == 405
