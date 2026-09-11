"""运行固定合同夹具的离线回归评测。

评测只生成临时文字 PDF 并调用确定性引擎，不访问 OCR、Redis、Celery 或
外部模型。它同时验证证据/阶段账本审计和结果指纹可重放，适合作为 CI 的
轻量质量门。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pymupdf

from contract_review import audit_result, load_rule_bundle, run_review
from contract_review_app.config import settings
from contract_review_app.services.pii_gate import gate_external_model_input


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "contract_review_cases.json"


def _make_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s")
    document.save(str(path))
    document.close()


def evaluate_case(case: dict, bundle) -> dict:
    with tempfile.TemporaryDirectory(prefix="contract-eval-") as tmp:
        pdf_path = Path(tmp) / f"{case['id']}.pdf"
        _make_pdf(pdf_path, case["text"])
        result = run_review(
            [pdf_path],
            package_id=case["package_id"],
            rule_bundle=bundle,
            contract_type=case.get("contract_type"),
            ocr_provider=None,
            run_id=f"eval-{case['id']}",
        )
        audit = audit_result(result)
        replay = run_review(
            [pdf_path],
            package_id=case["package_id"],
            rule_bundle=bundle,
            contract_type=case.get("contract_type"),
            ocr_provider=None,
            run_id=f"replay-{case['id']}",
        )
    gate = gate_external_model_input([{"text": case["text"]}])
    evidence_types = {item.evidence_type.value for item in result.evidence}
    checks = {
        "audit": audit.passed,
        "result_replay": result.run.result_fingerprint == replay.run.result_fingerprint,
        "min_findings": len(result.findings) >= int(case.get("min_findings", 0)),
        "report_status": result.report.overall_status.value == case["expected_report_status"],
        "evidence_types": set(case.get("required_evidence_types", [])).issubset(evidence_types),
    }
    if "pii_gate_decision" in case:
        checks["pii_gate_decision"] = gate.decision == case["pii_gate_decision"]
        checks["pii_types"] = set(case.get("expected_pii_types", [])).issubset(
            {item.kind for item in gate.findings}
        )
    if not all(checks.values()):
        raise AssertionError(
            f"fixture {case['id']} failed: "
            f"{json.dumps(checks, ensure_ascii=False, sort_keys=True)}"
        )
    return {
        "id": case["id"],
        "run_id": result.run.run_id,
        "result_fingerprint": result.run.result_fingerprint,
        "finding_count": len(result.findings),
        "evidence_count": len(result.evidence),
        "report_status": result.report.overall_status.value,
        "pii_gate": gate.decision,
        "checks": checks,
    }


def main() -> int:
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = load_rule_bundle(settings.resolve_path(settings.CONTRACT_RULES_PATH))
    summaries = [evaluate_case(case, bundle) for case in cases]
    print(json.dumps({"fixture_count": len(summaries), "cases": summaries}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"offline fixture evaluation failed: {exc}", file=sys.stderr)
        raise
