"""Small JSON CLI for inspecting parser output and creating run snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import DocumentKind
from .parser import find_text_evidence, parse_pdf
from .pipeline import parse_contract_package, run_review
from .report import write_markdown_report
from .rules import load_rule_bundle
from .run import create_review_run
from .store import JsonAuditStore


def _write_json(payload: object, output: str | None) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output:
        Path(output).write_text(serialized + "\n", encoding="utf-8")
    else:
        sys.stdout.write(serialized + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contract-review")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_command = subparsers.add_parser("parse-pdf", help="解析数字 PDF 并输出带坐标的 JSON")
    parse_command.add_argument("path", type=Path)
    parse_command.add_argument("--package-id", required=True)
    parse_command.add_argument("--document-kind", choices=[kind.value for kind in DocumentKind], default="unknown")
    parse_command.add_argument("--query", action="append", default=[])
    parse_command.add_argument("--output", type=str)

    run_command = subparsers.add_parser("create-run", help="创建一个可回放的审查运行快照")
    run_command.add_argument("paths", nargs="+", type=Path)
    run_command.add_argument("--package-id", required=True)
    run_command.add_argument("--rules", required=True, type=Path)
    run_command.add_argument("--output", type=str)

    review_command = subparsers.add_parser("review", help="执行证据化规则审查并输出复核报告")
    review_command.add_argument("paths", nargs="+", type=Path)
    review_command.add_argument("--package-id", required=True)
    review_command.add_argument("--rules", required=True, type=Path)
    review_command.add_argument("--contract-type")
    review_command.add_argument("--audit-root", type=str)
    review_command.add_argument("--output", type=str)
    review_command.add_argument("--report-output", type=str)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "parse-pdf":
        parsed = parse_pdf(
            args.path,
            package_id=args.package_id,
            document_kind=DocumentKind(args.document_kind),
        )
        payload = {
            "document": parsed.document.model_dump(mode="json"),
            "pages": [page.model_dump(mode="json") for page in parsed.pages],
            "evidence": [
                evidence.model_dump(mode="json")
                for query in args.query
                for evidence in find_text_evidence(parsed, query, evidence_prefix="cli")
            ],
        }
        _write_json(payload, args.output)
        return 0

    if args.command == "review":
        bundle = load_rule_bundle(args.rules)
        result = run_review(
            args.paths,
            package_id=args.package_id,
            rule_bundle=bundle,
            contract_type=args.contract_type,
        )
        if args.audit_root:
            JsonAuditStore(args.audit_root).save(result)
        if args.report_output:
            write_markdown_report(result, args.report_output)
        _write_json(result.model_dump(mode="json"), args.output)
        return 0

    package, parsed_documents = parse_contract_package(
        args.paths,
        package_id=args.package_id,
    )
    documents = [parsed.document for parsed in parsed_documents]
    bundle = load_rule_bundle(args.rules)
    run = create_review_run(
        package,
        documents,
        bundle,
        parser_version="+".join(sorted({document.parser_version for document in documents})),
    )
    _write_json(
        {
            "package": package.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "documents": [document.model_dump(mode="json") for document in documents],
        },
        args.output,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
