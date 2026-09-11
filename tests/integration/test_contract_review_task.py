"""合同审查异步任务处理器测试（manifest 驱动，不依赖 Redis/worker）。"""

import asyncio

import fitz

from contract_review_app.config import settings
from contract_review_app.services.task_handlers import run_task_handler
from contract_review_app.storage.task_file_store import task_file_store


def _make_contract_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "甲方与乙方签订软件开发合同，合同金额为人民币一百万元整。",
        fontname="china-s",
    )
    data = doc.tobytes()
    doc.close()
    return data


def test_run_task_handler_contract_review_files_mode(monkeypatch):
    # 测试环境不走语义模型和印章识别
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    class _EmptyOCR:
        def recognize_seal(self, image_bytes, *, page_number=1):
            del image_bytes, page_number
            return None
    monkeypatch.setattr(
        "contract_review_app.services.seal_evidence.ocr_gateway_client",
        _EmptyOCR(),
    )

    task_id = "ocr_test_review_handler"
    manifest_path = task_file_store.save_files(
        task_id=task_id,
        files=[("合同主文.pdf", _make_contract_pdf(), "application/pdf")],
        options={
            "PackageId": "pkg-task-001",
            "ContractType": "software",
        },
    )
    try:
        manifest = task_file_store.load_manifest(manifest_path)
        assert manifest["input_mode"] == "files"

        result = asyncio.run(run_task_handler("contract-review", manifest))

        assert "review_result" in result and "ai_analysis" in result
        review = result["review_result"]
        assert review["package"]["package_id"] == "pkg-task-001"
        assert review["documents"], "应返回文档信息"
        assert review["findings"], "确定性规则应产出审核发现"
        assert review["report"]["finding_counts"]
        assert result["ai_analysis"]["projection_version"]
        assert result["ai_analysis"]["analysis_id"].startswith("review-")
        assert result["ai_analysis"]["provider"] == "deterministic-rule-engine"
    finally:
        task_file_store.delete_task_files(task_id)
