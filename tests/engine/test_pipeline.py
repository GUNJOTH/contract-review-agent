import json
import shutil
import unittest
from pathlib import Path
from zipfile import ZipFile

import fitz
from pydantic import ValidationError

from contract_review.models import (
    ContractFact,
    EvidenceType,
    KnowledgeSourceKind,
    RetrievalMode,
    RetrievalSource,
    Rule,
    RuleBundle,
    ReviewContext,
    ReviewStatus,
    SemanticReviewItem,
    SemanticReviewResponse,
)
from contract_review.pipeline import (
    ReplayMismatch,
    finalize_review,
    parse_contract_package,
    record_review_decision,
    replay_review,
    run_review,
    run_review_with_semantic_client,
)
from contract_review.semantic import (
    StaticSemanticReviewer,
    build_semantic_model_request,
)
from contract_review.parser import find_text_evidence, parse_pdf
from contract_review.playbook import publish_playbook_bundle
from contract_review.store import AuditStoreError, JsonAuditStore


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_path = Path.cwd() / ".test-work"
        self.work_path.mkdir(exist_ok=True)
        self.pdf_path = self.work_path / "pipeline-contract.pdf"
        pdf = fitz.open()
        page = pdf.new_page(width=600, height=800)
        page.insert_text((60, 80), "This contract includes source code delivery.")
        page.insert_text((60, 130), "The parties should define breach responsibility.")
        page.insert_text((60, 180), "The tax rate is 13%.")
        pdf.save(str(self.pdf_path))
        pdf.close()
        self.bundle = publish_playbook_bundle(RuleBundle(
            bundle_id="pipeline-rules-v1",
            source_filename="pipeline-rules.xlsx",
            source_sha256="b" * 64,
            source_sheet="Sheet1",
            source_range="A1:C3",
            rules=[
                Rule(
                    rule_id="keyword-source-code",
                    version="v1",
                    title="source code",
                    category="source code",
                    applies_to=["software"],
                    check_method="keyword",
                    source_snapshot="pipeline-rules#1",
                ),
                Rule(
                    rule_id="semantic-breach",
                    version="v1",
                    title="breach responsibility",
                    category="合同主体",
                    applies_to=["software"],
                    check_method="semantic",
                    source_snapshot="pipeline-rules#2",
                ),
                Rule(
                    rule_id="deterministic-tax-rate",
                    version="v1",
                    title="税率",
                    category="金额",
                    applies_to=["software"],
                    check_method="deterministic",
                    applicability={
                        "software": {
                            "applicability": "expected_value",
                            "expected_value": 0.13,
                        }
                    },
                    source_snapshot="pipeline-rules#3",
                ),
            ],
        ))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.work_path.is_dir():
            for path in self.work_path.glob("pipeline-contract.pdf"):
                path.unlink(missing_ok=True)
            store_path = self.work_path / "audit-store"
            if store_path.exists():
                shutil.rmtree(store_path)
            revision_store_path = self.work_path / "audit-store-revision"
            if revision_store_path.exists():
                shutil.rmtree(revision_store_path)
            version_store_path = self.work_path / "audit-store-version"
            if version_store_path.exists():
                shutil.rmtree(version_store_path)

    def test_run_review_produces_auditable_findings_and_replayable_result(self) -> None:
        first = run_review(
            [self.pdf_path],
            package_id="pkg-pipeline",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-one",
        )
        second = run_review(
            [self.pdf_path],
            package_id="pkg-pipeline",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-two",
        )

        evidence_ids = {item.evidence_id for item in first.evidence}
        self.assertEqual(first.run.status, ReviewStatus.HUMAN_REVIEW)
        self.assertEqual(first.report.overall_status, "UNKNOWN")
        self.assertTrue(first.report.review_required)
        self.assertTrue(first.findings)
        self.assertEqual(first.schema_version, "2.0")
        self.assertTrue(first.clauses)
        self.assertEqual(len(first.review_questions), len(self.bundle.rules))
        self.assertEqual(len(first.question_assessments), len(first.findings))
        self.assertTrue(first.knowledge_chunks)
        self.assertTrue(first.retrieval_traces)
        chunks = {chunk.chunk_id: chunk for chunk in first.knowledge_chunks}
        self.assertTrue(
            all(
                hit.chunk_id in chunks
                and set(hit.evidence_ids) == set(chunks[hit.chunk_id].evidence_ids)
                for trace in first.retrieval_traces
                for hit in trace.hits
            )
        )
        self.assertTrue(
            all(
                set(finding.evidence_ids).issubset(evidence_ids)
                for finding in first.findings
            )
        )
        self.assertEqual(first.run.result_fingerprint, second.run.result_fingerprint)

    def test_review_scope_keeps_full_snapshot_and_audits_selected_rules(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-scoped-review",
            rule_bundle=self.bundle,
            review_context=ReviewContext(
                contract_type="software",
                review_scope=["source code"],
            ),
            run_id="run-scoped-review",
        )

        self.assertEqual(len(result.rule_bundle.rules), len(self.bundle.rules))
        self.assertEqual(
            result.run.configuration["selected_rule_ids"], ["keyword-source-code"]
        )
        self.assertEqual(
            [question.rule_id for question in result.review_questions],
            ["keyword-source-code"],
        )
        from contract_review.audit import audit_result

        self.assertTrue(audit_result(result).passed)

    def test_semantic_retrieval_requires_explicit_rule_applicability(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-inapplicable-semantic",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="hardware"),
            run_id="run-inapplicable-semantic",
        )

        self.assertEqual(
            {trace.retrieval_query.rule_id for trace in result.retrieval_traces},
            {rule.rule_id for rule in self.bundle.rules},
        )
        self.assertTrue(result.candidate_evidence)
        self.assertIsNone(
            build_semantic_model_request(
                result,
                provider="test-provider",
                model_version="test-model",
                prompt_version="prompt-v1",
            )
        )
        self.assertEqual(
            next(
                finding
                for finding in result.findings
                if finding.rule_id == "semantic-breach"
            ).status,
            "UNKNOWN",
        )

    def test_audit_rejects_mismatched_retrieval_mode_and_hit_source(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-retrieval-audit",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-retrieval-audit",
        )
        trace = next(trace for trace in result.retrieval_traces if trace.hits)
        tampered_hit = trace.hits[0].model_copy(
            update={"retrieval_sources": [RetrievalSource.VECTOR]}
        )
        tampered_trace = trace.model_copy(
            update={
                "retrieval_mode": RetrievalMode.LEXICAL,
                "hits": [tampered_hit, *trace.hits[1:]],
            }
        )
        tampered = result.model_copy(
            update={"retrieval_traces": [tampered_trace, *result.retrieval_traces[1:]]}
        )

        from contract_review.audit import audit_result

        report = audit_result(tampered)
        self.assertFalse(report.checks["knowledge_integrity"])

    def test_v1_result_without_schema_version_is_rejected(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-schema-v2",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-schema-v2",
        )
        payload = result.model_dump(mode="json")
        payload.pop("schema_version")

        with self.assertRaises(ValidationError):
            type(result).model_validate(payload)

    def test_review_decisions_are_required_before_finalization(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-pipeline",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-decisions",
        )
        with self.assertRaises(ValueError):
            finalize_review(result, actor_id="reviewer-1", comment="尚未逐条确认")

        for finding in result.findings:
            if finding.status in {"WARN", "BLOCK", "UNKNOWN"}:
                result = record_review_decision(
                    result,
                    finding.finding_id,
                    decision="ACCEPT",
                    actor_id="reviewer-1",
                    actor_role="legal",
                    comment="已核对原文和规则依据。",
                )
        finalized = finalize_review(
            result,
            actor_id="reviewer-1",
            comment="完成最终确认。",
        )

        self.assertEqual(finalized.run.status, ReviewStatus.FINALIZED)
        self.assertFalse(finalized.report.review_required)
        required_count = sum(
            finding.status in {"WARN", "BLOCK", "UNKNOWN"}
            for finding in result.findings
        )
        self.assertEqual(len(finalized.decisions), required_count)
        replayed = replay_review(finalized, [self.pdf_path], rule_bundle=self.bundle)
        self.assertEqual(replayed.run.status, ReviewStatus.FINALIZED)
        self.assertEqual(
            replayed.run.result_fingerprint, finalized.run.result_fingerprint
        )

    def test_review_action_rejects_a_tampered_core_result(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-tampered-action",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-tampered-action",
        )
        finding = result.findings[0]
        tampered = result.model_copy(
            update={
                "findings": [
                    finding.model_copy(update={"reason": "客户端篡改的结论"}),
                    *result.findings[1:],
                ]
            }
        )

        with self.assertRaises(ValueError):
            record_review_decision(
                tampered,
                finding.finding_id,
                decision="ACCEPT",
                actor_id="reviewer-1",
                actor_role="legal",
                comment="不能绕过结果完整性门禁。",
            )

    def test_replay_requires_same_inputs_and_result(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-replay",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-replay-original",
        )
        replayed = replay_review(result, [self.pdf_path], rule_bundle=self.bundle)
        self.assertEqual(replayed.run.result_fingerprint, result.run.result_fingerprint)

        changed_path = self.work_path / "pipeline-contract-changed.pdf"
        pdf = fitz.open()
        page = pdf.new_page(width=600, height=800)
        page.insert_text((60, 80), "This contract has changed source code terms.")
        pdf.save(str(changed_path))
        pdf.close()
        self.addCleanup(lambda: changed_path.unlink(missing_ok=True))

        with self.assertRaises(ReplayMismatch):
            replay_review(result, [changed_path], rule_bundle=self.bundle)

    def test_semantic_response_is_evidence_gated_and_replayable(self) -> None:
        baseline = run_review(
            [self.pdf_path],
            package_id="pkg-semantic",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-semantic-baseline",
        )
        semantic_finding = next(
            finding
            for finding in baseline.findings
            if finding.rule_id == "semantic-breach"
        )
        contract_evidence_id = next(
            evidence.evidence_id
            for evidence in baseline.evidence
            if evidence.evidence_id in semantic_finding.evidence_ids
            and evidence.evidence_type != EvidenceType.EXTERNAL_REFERENCE
            and any(
                chunk.source_kind == KnowledgeSourceKind.CONTRACT
                and evidence.evidence_id in chunk.evidence_ids
                for chunk in baseline.knowledge_chunks
            )
        )
        prompt_version = "contract-review-prompt-v1"
        model_version = "test-model-v1"
        semantic_request = build_semantic_model_request(
            baseline,
            provider="test-provider",
            model_version=model_version,
            prompt_version=prompt_version,
        )
        response = SemanticReviewResponse(
            response_id="response-1",
            provider="test-provider",
            model_version=model_version,
            prompt_version=prompt_version,
            request_fingerprint=semantic_request.request_fingerprint,
            items=[
                SemanticReviewItem(
                    rule_id="semantic-breach",
                    status="WARN",
                    reason="条款虽有责任约定，但赔偿上限不清晰。",
                    evidence_ids=[contract_evidence_id],
                    confidence=0.9,
                )
            ],
        )
        result = run_review(
            [self.pdf_path],
            package_id="pkg-semantic",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            semantic_request=semantic_request,
            semantic_response=response,
            run_id="run-semantic",
        )
        item = next(
            finding
            for finding in result.findings
            if finding.rule_id == "semantic-breach"
        )
        self.assertEqual(item.status, "WARN")
        self.assertEqual(result.run.status, ReviewStatus.HUMAN_REVIEW)
        self.assertIn(
            "SEMANTIC_REVIEWED",
            [event.to_stage for event in result.run.stage_events],
        )
        self.assertEqual(
            replay_review(
                result, [self.pdf_path], rule_bundle=self.bundle
            ).run.result_fingerprint,
            result.run.result_fingerprint,
        )

        rule_evidence_id = next(
            evidence.evidence_id
            for evidence in baseline.evidence
            if evidence.evidence_type == EvidenceType.EXTERNAL_REFERENCE
        )
        with self.assertRaisesRegex(ValueError, "cannot cite rule source evidence"):
            run_review(
                [self.pdf_path],
                package_id="pkg-semantic-rule-evidence",
                rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
                semantic_request=semantic_request,
                semantic_response=response.model_copy(
                    update={
                        "items": [
                            response.items[0].model_copy(
                                update={"evidence_ids": [rule_evidence_id]}
                            )
                        ]
                    }
                ),
            )

        with self.assertRaises(ValueError):
            run_review(
                [self.pdf_path],
                package_id="pkg-semantic-invalid",
                rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
                semantic_request=semantic_request,
                semantic_response=response.model_copy(
                    update={
                        "items": [
                            response.items[0].model_copy(
                                update={"evidence_ids": ["not-real"]}
                            )
                        ]
                    }
                ),
            )

    def test_semantic_client_path_binds_configuration_and_audit_snapshot(self) -> None:
        baseline = run_review(
            [self.pdf_path],
            package_id="pkg-semantic-client",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-semantic-client-baseline",
        )
        request = build_semantic_model_request(
            baseline,
            provider="captured-provider",
            model_version="captured-model-v1",
            prompt_version="contract-review-prompt-v2",
            system_instruction="只能引用给定证据；无法确定则返回 UNKNOWN。",
            configuration={"temperature": 0, "top_k": 5},
        )
        semantic_finding = next(
            finding
            for finding in baseline.findings
            if finding.rule_id == "semantic-breach"
        )
        contract_evidence_id = next(
            evidence.evidence_id
            for evidence in baseline.evidence
            if evidence.evidence_id in semantic_finding.evidence_ids
            and evidence.evidence_type != EvidenceType.EXTERNAL_REFERENCE
            and any(
                chunk.source_kind == KnowledgeSourceKind.CONTRACT
                and evidence.evidence_id in chunk.evidence_ids
                for chunk in baseline.knowledge_chunks
            )
        )
        response = SemanticReviewResponse(
            response_id="captured-response-1",
            provider="captured-provider",
            model_version="captured-model-v1",
            prompt_version="contract-review-prompt-v2",
            request_fingerprint=request.request_fingerprint,
            items=[
                SemanticReviewItem(
                    rule_id="semantic-breach",
                    status="WARN",
                    reason="违约责任条款需要明确赔偿范围。",
                    evidence_ids=[contract_evidence_id],
                    confidence=0.95,
                )
            ],
        )
        result = run_review_with_semantic_client(
            [self.pdf_path],
            package_id="pkg-semantic-client",
            rule_bundle=self.bundle,
            client=StaticSemanticReviewer(response),
            provider="captured-provider",
            model_version="captured-model-v1",
            prompt_version="contract-review-prompt-v2",
            system_instruction="只能引用给定证据；无法确定则返回 UNKNOWN。",
            review_context=ReviewContext(contract_type="software"),
            configuration={"temperature": 0, "top_k": 5},
            run_id="run-semantic-client",
        )

        from contract_review.audit import audit_result

        self.assertEqual(result.run.status, ReviewStatus.HUMAN_REVIEW)
        self.assertEqual(result.semantic_request, request)
        self.assertEqual(result.semantic_response, response)
        self.assertTrue(audit_result(result).passed)
        self.assertEqual(
            replay_review(
                result, [self.pdf_path], rule_bundle=self.bundle
            ).run.result_fingerprint,
            result.run.result_fingerprint,
        )

    def test_semantic_client_skips_rule_without_contract_evidence(self) -> None:
        unmatched_rule = self.bundle.rules[1].model_copy(
            update={"title": "unmatched semantic requirement"}
        )
        bundle = self.bundle.model_copy(
            update={
                "rules": [self.bundle.rules[0], unmatched_rule, self.bundle.rules[2]],
                "release_status": "draft",
                "release_fingerprint": None,
                "published_at": None,
            }
        )
        bundle = publish_playbook_bundle(bundle)

        class _UnexpectedSemanticCall:
            def review(self, _request):
                raise AssertionError("没有合同正文证据时不应调用语义模型")

        result = run_review_with_semantic_client(
            [self.pdf_path],
            package_id="pkg-semantic-no-contract-evidence",
            rule_bundle=bundle,
            client=_UnexpectedSemanticCall(),
            provider="test-provider",
            model_version="test-model-v1",
            prompt_version="contract-review-prompt-v1",
            review_context=ReviewContext(contract_type="software"),
        )

        finding = next(
            finding for finding in result.findings if finding.rule_id == unmatched_rule.rule_id
        )
        self.assertEqual(finding.status, "UNKNOWN")
        self.assertIsNone(result.semantic_request)
        self.assertIsNone(result.semantic_response)

    def test_decision_must_cite_the_finding_evidence(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-decision-evidence",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-decision-evidence",
        )
        finding = next(
            finding for finding in result.findings if finding.status == "UNKNOWN"
        )
        unrelated = next(
            evidence.evidence_id
            for evidence in result.evidence
            if evidence.evidence_id not in finding.evidence_ids
        )
        with self.assertRaises(ValueError):
            record_review_decision(
                result,
                finding.finding_id,
                decision="ACCEPT",
                actor_id="reviewer-1",
                actor_role="legal",
                comment="错误引用示例",
                evidence_ids=[unrelated],
            )

    def test_external_contract_type_fact_and_evidence_are_replayable(self) -> None:
        parsed = parse_pdf(self.pdf_path, package_id="pkg-contract-type")
        fact_evidence = find_text_evidence(
            parsed,
            "source code",
            evidence_prefix="contract-type",
        )
        fact = ContractFact(
            fact_id="fact-contract-type-1",
            fact_type="contract_type",
            value="software",
            normalized_value="software",
            evidence_ids=[item.evidence_id for item in fact_evidence],
            confidence=0.95,
            extractor_version="human-confirmed-v1",
        )
        result = run_review(
            [self.pdf_path],
            package_id="pkg-contract-type",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            contract_type_fact=fact,
            contract_type_evidence=fact_evidence,
            run_id="run-contract-type",
        )
        self.assertTrue(
            any(
                item.evidence_id.startswith(
                    f"contract-type-{parsed.document.document_id}-p1-b0-"
                )
                for item in result.evidence
            )
        )
        self.assertEqual(
            replay_review(
                result, [self.pdf_path], rule_bundle=self.bundle
            ).run.result_fingerprint,
            result.run.result_fingerprint,
        )

    def test_package_order_is_canonical_for_replay(self) -> None:
        docx_path = self.work_path / "order-attachment.docx"
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
          <w:body><w:p><w:r><w:t>附件内容</w:t></w:r></w:p><w:sectPr/></w:body>
        </w:document>"""
        with ZipFile(docx_path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        self.addCleanup(lambda: docx_path.unlink(missing_ok=True))

        first_package, first_documents = parse_contract_package(
            [self.pdf_path, docx_path],
            package_id="pkg-order",
        )
        second_package, second_documents = parse_contract_package(
            [docx_path, self.pdf_path],
            package_id="pkg-order",
        )
        self.assertEqual(first_package.document_ids, second_package.document_ids)
        self.assertEqual(first_package.source_snapshot, second_package.source_snapshot)
        self.assertEqual(
            [item.document.document_id for item in first_documents],
            [item.document.document_id for item in second_documents],
        )

    def test_missing_referenced_attachment_enters_review_queue(self) -> None:
        docx_path = self.work_path / "attachment-main.docx"
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>
          <w:body><w:p><w:r><w:t>见附件：技术协议</w:t></w:r></w:p><w:sectPr/></w:body>
        </w:document>"""
        with ZipFile(docx_path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        self.addCleanup(lambda: docx_path.unlink(missing_ok=True))
        bundle = publish_playbook_bundle(RuleBundle(
            bundle_id="attachment-rules-v1",
            source_filename="attachment-rules.xlsx",
            source_sha256="c" * 64,
            source_sheet="Sheet1",
            source_range="A1:C1",
            rules=[
                Rule(
                    rule_id="attachment-technical-agreement",
                    version="v1",
                    title="技术协议",
                    category="附件完整性",
                    applies_to=["software"],
                    check_method="deterministic",
                    checker="attachment_completeness",
                    source_snapshot="attachment-rules#1",
                )
            ],
        ))

        result = run_review(
            [docx_path],
            package_id="pkg-attachment",
            rule_bundle=bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-attachment",
        )

        finding = result.findings[0]
        evidence = {item.evidence_id: item for item in result.evidence}
        self.assertEqual(finding.status, "UNKNOWN")
        self.assertIn("未发现匹配文件", finding.reason)
        self.assertIn(
            "missing_artifact",
            [evidence[item].evidence_type.value for item in finding.evidence_ids],
        )
        self.assertTrue(set(finding.evidence_ids) <= set(evidence))

    def test_json_store_is_write_once_and_detects_tampering(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-store",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-store",
        )
        store = JsonAuditStore(self.work_path / "audit-store")
        artifact = store.save(result)
        loaded = store.load(result.run.run_id)
        self.assertEqual(loaded.run.result_fingerprint, result.run.result_fingerprint)

        event_ledger = (
            artifact / "stage-events" / f"review_run-{result.run.run_id}.jsonl"
        )
        event_ledger.write_text(
            event_ledger.read_text(encoding="utf-8") + "{}\n", encoding="utf-8"
        )
        with self.assertRaises(AuditStoreError):
            store.load(result.run.run_id)

        with self.assertRaises(AuditStoreError):
            store.save(result)

        payload_path = artifact / "review.json"
        payload_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(AuditStoreError):
            store.load(result.run.run_id)

    def test_json_store_appends_immutable_human_review_revisions(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-store-revision",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-store-revision",
        )
        store = JsonAuditStore(self.work_path / "audit-store-revision")
        store.save(result)
        finding = next(
            finding for finding in result.findings if finding.status == "UNKNOWN"
        )
        reviewed = record_review_decision(
            result,
            finding.finding_id,
            decision="ACCEPT",
            actor_id="reviewer-1",
            actor_role="legal",
            comment="已核对证据。",
        )
        revision = store.append_revision(reviewed)

        loaded = store.load(result.run.run_id)
        self.assertTrue(revision.is_dir())
        self.assertEqual(len(loaded.decisions), 1)
        self.assertEqual(loaded.run.result_fingerprint, reviewed.run.result_fingerprint)

    def test_json_store_rejects_previous_store_version(self) -> None:
        result = run_review(
            [self.pdf_path],
            package_id="pkg-store-version",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id="run-store-version",
        )
        store = JsonAuditStore(self.work_path / "audit-store-version")
        artifact = store.save(result)
        manifest_path = artifact / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["store_version"] = "json-audit-store-0.2.0"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(AuditStoreError):
            store.load(result.run.run_id)
