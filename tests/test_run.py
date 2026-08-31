import unittest

from contract_review.models import (
    ContractPackage,
    Document,
    DocumentKind,
    ReviewStatus,
    Rule,
    RuleBundle,
)
from contract_review.run import ReviewRunError, advance_review_run, create_review_run


class ReviewRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document(
            document_id="doc-1",
            package_id="pkg-1",
            filename="main.pdf",
            mime_type="application/pdf",
            source_sha256="a" * 64,
            document_kind=DocumentKind.MAIN_CONTRACT,
            parser_version="pdf-text-0.1.0",
            parse_status="parsed",
        )
        self.package = ContractPackage(
            package_id="pkg-1",
            document_ids=["doc-1"],
            source_snapshot="package-snapshot-1",
        )
        self.bundle = RuleBundle(
            bundle_id="bundle-v0.14",
            source_filename="rules.xlsx",
            source_sha256="b" * 64,
            source_sheet="Sheet1",
            source_range="A1:M54",
            rules=[
                Rule(
                    rule_id="rule-1",
                    version="v0.14",
                    title="测试规则",
                    category="测试",
                    check_method="deterministic",
                    source_snapshot="rules.xlsx#snapshot",
                )
            ],
        )

    def test_run_records_inputs_and_append_only_transitions(self) -> None:
        run = create_review_run(
            self.package,
            [self.document],
            self.bundle,
            parser_version="pdf-text-0.1.0",
            model_version="model-test",
            run_id="run-fixed",
        )
        run = advance_review_run(
            run,
            ReviewStatus.PARSED,
            action="parse_documents",
            reason="全部页面已解析。",
        )
        run = advance_review_run(
            run,
            ReviewStatus.QUALITY_GATED,
            action="quality_gate",
            reason="没有发现需要 OCR 的页面。",
        )

        self.assertEqual(run.input_document_sha256, {"doc-1": "a" * 64})
        self.assertEqual(run.status, ReviewStatus.QUALITY_GATED)
        self.assertEqual(len(run.transitions), 3)
        self.assertEqual(run.transitions[-1].from_status, ReviewStatus.PARSED)
        self.assertTrue(len(run.configuration_fingerprint) == 64)
        self.assertIsNone(run.finished_at)

    def test_invalid_transition_is_rejected(self) -> None:
        run = create_review_run(self.package, [self.document], self.bundle, parser_version="test")

        with self.assertRaises(ReviewRunError):
            advance_review_run(
                run,
                ReviewStatus.FINALIZED,
                action="skip",
                reason="不能跳过前置阶段。",
            )

    def test_replay_fingerprint_changes_when_source_changes(self) -> None:
        first = create_review_run(self.package, [self.document], self.bundle, parser_version="test")
        changed = self.document.model_copy(update={"source_sha256": "c" * 64})
        second = create_review_run(self.package, [changed], self.bundle, parser_version="test")

        self.assertNotEqual(first.configuration_fingerprint, second.configuration_fingerprint)

    def test_empty_package_cannot_start_a_run(self) -> None:
        empty_package = self.package.model_copy(update={"document_ids": []})

        with self.assertRaises(ReviewRunError):
            create_review_run(empty_package, [], self.bundle, parser_version="test")
