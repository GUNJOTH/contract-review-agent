"""运行固定合同夹具的离线回归评测。

评测只生成临时文字 DOCX 并调用确定性引擎，不访问 OCR、Redis、Celery 或
外部模型。它同时验证证据/阶段账本审计和结果指纹可重放，适合作为 CI 的
轻量质量门。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

from contract_review import (
    ReviewContext,
    audit_result,
    load_active_rule_bundle,
    run_review,
)
from contract_review_app.config import settings
from contract_review_app.services.pii_gate import gate_external_model_input


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "contract_review_cases.json"


def _make_docx(path: Path, text: str) -> None:
    """生成保留中文文本层的最小 DOCX 评测输入。"""

    paragraphs = text.splitlines() or [text]
    body = "".join(
        "<w:p><w:r><w:t xml:space='preserve'>"
        + escape(paragraph)
        + "</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def evaluate_case(case: dict, bundle) -> dict:
    with tempfile.TemporaryDirectory(prefix="contract-eval-") as tmp:
        docx_path = Path(tmp) / f"{case['id']}.docx"
        _make_docx(docx_path, case["text"])
        result = run_review(
            [docx_path],
            package_id=case["package_id"],
            rule_bundle=bundle,
            review_context=ReviewContext(contract_type=case.get("contract_type")),
            ocr_provider=None,
            run_id=f"eval-{case['id']}",
        )
        audit = audit_result(result)
        replay = run_review(
            [docx_path],
            package_id=case["package_id"],
            rule_bundle=bundle,
            review_context=ReviewContext(contract_type=case.get("contract_type")),
            ocr_provider=None,
            run_id=f"replay-{case['id']}",
        )
    gate = gate_external_model_input([{"text": case["text"]}])
    evidence_types = {item.evidence_type.value for item in result.evidence}
    checker_statuses = {
        rule.checker: finding.status.value
        for rule in bundle.rules
        for finding in result.findings
        if rule.rule_id == finding.rule_id and rule.checker
    }
    expected_checker_statuses = case.get("expected_checker_statuses", {})
    checks = {
        "schema_v2": result.schema_version == "2.0",
        "audit": audit.passed,
        "result_replay": result.run.result_fingerprint == replay.run.result_fingerprint,
        "min_findings": len(result.findings) >= int(case.get("min_findings", 0)),
        "min_clauses": len(result.clauses) >= int(case.get("min_clauses", 1)),
        "min_clause_relations": len(result.clause_relations)
        >= int(case.get("min_clause_relations", 0)),
        "min_obligations": len(result.obligations)
        >= int(case.get("min_obligations", 0)),
        "min_financial_facts": sum(
            fact.fact_type.startswith("financial.") for fact in result.facts
        )
        >= int(case.get("min_financial_facts", 0)),
        "checker_statuses": all(
            checker_statuses.get(checker) == status
            for checker, status in expected_checker_statuses.items()
        ),
        "question_coverage": len(result.review_questions) == len(bundle.rules),
        "assessment_coverage": len(result.question_assessments) == len(result.findings),
        "report_status": result.report.overall_status.value
        == case["expected_report_status"],
        "evidence_types": set(case.get("required_evidence_types", [])).issubset(
            evidence_types
        ),
        "retrieval_trace_contract": all(
            trace.top_k > 0
            and trace.retrieval_mode.value in {"lexical", "vector", "hybrid"}
            and all(
                set(hit.retrieval_sources).issubset({"lexical", "vector"})
                for hit in trace.hits
            )
            for trace in result.retrieval_traces
        ),
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
        "clause_count": len(result.clauses),
        "relation_count": len(result.clause_relations),
        "obligation_count": len(result.obligations),
        "financial_fact_count": sum(
            fact.fact_type.startswith("financial.") for fact in result.facts
        ),
        "checker_statuses": checker_statuses,
        "assessment_count": len(result.question_assessments),
        "evidence_count": len(result.evidence),
        "report_status": result.report.overall_status.value,
        "pii_gate": gate.decision,
        "checks": checks,
    }


def main() -> int:
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    bundle = load_active_rule_bundle(
        settings.resolve_path(settings.CONTRACT_RULES_PATH),
        settings.resolve_path(settings.CONTRACT_CORE_RULES_PATH),
    )
    summaries = [evaluate_case(case, bundle) for case in cases]
    print(
        json.dumps(
            {"fixture_count": len(summaries), "cases": summaries}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"offline fixture evaluation failed: {exc}", file=sys.stderr)
        raise
