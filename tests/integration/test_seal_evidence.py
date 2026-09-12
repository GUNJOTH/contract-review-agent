"""印章视觉证据测试（mock OCR 网关印章接口，无需 Triton/GPU）。"""

import fitz
import pytest

from contract_review import parse_contract_package
from contract_review.models import EvidenceType, ReviewContext

from contract_review_app.config import settings
from contract_review_app.services.review_service import run_contract_review
from contract_review_app.services.seal_evidence import SealEvidenceDetector

SEAL_RULE_ID = "CONTRACT-CHECK-1B59A23E6377"


class FakeOCRClient:
    def recognize_seal(self, image_bytes: bytes, *, page_number: int = 1) -> dict | None:
        del image_bytes, page_number
        return {
            "count": 1,
            "source": "ocr-gateway",
            "seal_infos": [
                {
                    "SealBody": "某某科技有限公司",
                    "Location": [[100, 100], [200, 100], [200, 200], [100, 200]],
                }
            ],
        }


class EmptyOCRClient:
    def recognize_seal(self, image_bytes: bytes, *, page_number: int = 1) -> dict | None:
        del image_bytes, page_number
        return None


def _make_contract_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "甲方与乙方签订软件开发合同，合同金额为人民币一百万元整。",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def test_seal_detector_registers_visual_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        FakeOCRClient(),
    )
    pdf_path = tmp_path / "合同主文.pdf"
    pdf_path.write_bytes(_make_contract_pdf())

    package, parsed = parse_contract_package(
        [str(pdf_path)], package_id="pkg-seal", ocr_provider=None
    )
    detector = SealEvidenceDetector()
    evidence = detector.detect([str(pdf_path)], [item.document for item in parsed])

    assert len(evidence) == 1
    ev = evidence[0]
    assert ev.evidence_type == EvidenceType.VISUAL_REGION
    assert ev.document_id == parsed[0].document.document_id
    assert ev.source_sha256 == parsed[0].document.source_sha256
    assert ev.package_id == package.package_id
    assert ev.locator.page_number == 1
    assert "某某科技有限公司" in (ev.raw_excerpt or "")
    assert ev.extraction_method.startswith("seal-ocr-gateway")
    assert ev.locator.bbox is not None
    assert ev.locator.bbox.x1 == pytest.approx(100 * 612 / 1700, abs=0.1)
    assert ev.locator.bbox.x2 == pytest.approx(200 * 612 / 1700, abs=0.1)


def test_contract_review_includes_seal_evidence_and_visual_finding(monkeypatch):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        FakeOCRClient(),
    )

    result = run_contract_review(
        [("合同主文.pdf", _make_contract_pdf())],
        package_id="pkg-seal-001",
        review_context=ReviewContext(contract_type="软件开发/转让服务"),
    )

    seal_evidence = [
        item
        for item in result.evidence
        if item.evidence_type == EvidenceType.VISUAL_REGION
    ]
    assert len(seal_evidence) == 1

    visual_findings = [
        finding
        for finding in result.findings
        if finding.rule_id == SEAL_RULE_ID
    ]
    assert visual_findings, "骑缝章规则应产出审核发现"
    assert seal_evidence[0].evidence_id in visual_findings[0].evidence_ids
    assert "印章" in visual_findings[0].reason
    assert visual_findings[0].status.value == "UNKNOWN"


def test_seal_detection_skipped_when_clients_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        EmptyOCRClient(),
    )
    pdf_path = tmp_path / "合同主文.pdf"
    pdf_path.write_bytes(_make_contract_pdf())

    package, parsed = parse_contract_package(
        [str(pdf_path)], package_id="pkg-seal", ocr_provider=None
    )
    evidence = SealEvidenceDetector().detect(
        [str(pdf_path)], [item.document for item in parsed]
    )
    assert evidence == []
