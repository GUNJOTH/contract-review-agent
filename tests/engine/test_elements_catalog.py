"""标准要素字段目录快照的加载、校验与指纹测试。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

from contract_review import audit_result
from contract_review.elements import (
    CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION,
    CONTRACT_ELEMENT_DEFINITIONS,
    ContractElementCatalogError,
    build_builtin_contract_element_catalog,
    extract_contract_element_facts_from_candidates,
    load_contract_element_catalog,
    project_contract_element_form,
)
from contract_review.knowledge import build_knowledge_corpus
from contract_review.models import (
    CandidateEvidence,
    DocumentKind,
    KnowledgeSourceKind,
    RetrievalSource,
    ReviewContext,
    Rule,
    RuleBundle,
)
from contract_review.parser import parse_pdf
from contract_review.pipeline import ReplayMismatch, replay_review, run_review
from contract_review.playbook import publish_playbook_bundle
from tests.test_support.workspace import create_test_workspace

CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "contract_element_fields_v1.json"
)


def _candidates(parsed):
    chunks, _evidence = build_knowledge_corpus([parsed])
    return [
        CandidateEvidence(
            candidate_id=f"candidate-element-catalog-{index}",
            query_id="query-element-catalog",
            rule_id="element-catalog-test",
            rule_version="v1",
            rank=index,
            chunk_id=chunk.chunk_id,
            document_id=parsed.document.document_id,
            source_name=chunk.source_name,
            source_sha256=chunk.source_sha256,
            source_version=chunk.source_version,
            source_kind=KnowledgeSourceKind.CONTRACT,
            content=chunk.content,
            evidence_ids=chunk.evidence_ids,
            clause_ids=chunk.clause_ids,
            score=1.0,
            retrieval_sources=[RetrievalSource.LEXICAL],
            metadata=chunk.metadata,
        )
        for index, chunk in enumerate(
            (
                chunk
                for chunk in chunks
                if chunk.source_kind == KnowledgeSourceKind.CONTRACT
            ),
            start=1,
        )
    ]


def _parsed_contract(temp_dir: str):
    pdf_path = Path(temp_dir) / "contract.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "合同名称：软件开发合同\n"
        "合同编号：HT-2026-001\n"
        "甲方：某某科技有限公司\n"
        "乙方：某某软件有限公司\n"
        "合同金额：人民币1000000元\n"
        "税率：13%\n"
        "付款方式：银行转账\n"
        "质保期：12个月免费维保",
        fontname="china-s",
    )
    document.save(str(pdf_path))
    document.close()
    return parse_pdf(
        pdf_path,
        package_id="pkg-element-catalog",
        document_kind=DocumentKind.MAIN_CONTRACT,
    )


class ContractElementCatalogLoadTests(unittest.TestCase):
    def test_snapshot_has_no_drift_against_builtin_definitions(self) -> None:
        """外置目录必须与内置定义逐字段一致，否则外置本身就是行为变更。"""

        catalog = load_contract_element_catalog(CATALOG_PATH)
        builtin = build_builtin_contract_element_catalog()

        self.assertEqual(catalog.schema_version, CONTRACT_ELEMENT_CATALOG_SCHEMA_VERSION)
        self.assertEqual(catalog.catalog_id, "contract-element-fields-v1")
        self.assertEqual(len(catalog.definitions), len(CONTRACT_ELEMENT_DEFINITIONS))
        self.assertEqual(
            catalog.fingerprint,
            builtin.fingerprint,
            "快照与内置定义指纹不一致，说明外置过程引入了漂移",
        )

        snapshot_by_key = {item.key: item for item in catalog.enabled_definitions}
        builtin_by_key = {item.key: item for item in builtin.enabled_definitions}
        self.assertEqual(set(snapshot_by_key), set(builtin_by_key))
        for key, snapshot_item in snapshot_by_key.items():
            builtin_item = builtin_by_key[key]
            self.assertEqual(snapshot_item.label, builtin_item.label)
            self.assertEqual(snapshot_item.aliases, builtin_item.aliases)
            self.assertEqual(snapshot_item.patterns, builtin_item.patterns)
            self.assertEqual(snapshot_item.required, builtin_item.required)

    def test_fingerprint_is_stable_across_loads(self) -> None:
        first = load_contract_element_catalog(CATALOG_PATH)
        second = load_contract_element_catalog(CATALOG_PATH)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_required_keys_are_exposed_for_missing_value_reporting(self) -> None:
        catalog = load_contract_element_catalog(CATALOG_PATH)
        self.assertEqual(catalog.required_keys, ("contract_name",))

    def test_definition_edit_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="element-catalog-edit-") as temp_dir:
            target = Path(temp_dir) / "catalog.json"
            payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            payload["fields"][0]["patterns"] = [r"合同名称[:：]\s*([^\n]{4,80})"]
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            edited = load_contract_element_catalog(target)

        self.assertNotEqual(
            edited.fingerprint, load_contract_element_catalog(CATALOG_PATH).fingerprint
        )

    def test_disabling_field_removes_it_and_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="element-catalog-disable-") as temp_dir:
            target = Path(temp_dir) / "catalog.json"
            payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            for field in payload["fields"]:
                if field["key"] == "warranty":
                    field["enabled"] = False
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            catalog = load_contract_element_catalog(target)

        self.assertNotIn(
            "warranty", {item.key for item in catalog.enabled_definitions}
        )
        self.assertIn("warranty", {item.key for item in catalog.definitions})
        self.assertNotEqual(
            catalog.fingerprint,
            load_contract_element_catalog(CATALOG_PATH).fingerprint,
        )

    def test_duplicate_key_is_rejected(self) -> None:
        with self._mutated_catalog(
            lambda payload: payload["fields"].append(dict(payload["fields"][0]))
        ) as path:
            with self.assertRaises(ContractElementCatalogError) as context:
                load_contract_element_catalog(path)
        self.assertIn("重复 key", str(context.exception))

    def test_uncompilable_pattern_is_rejected(self) -> None:
        def _break_pattern(payload: dict) -> None:
            payload["fields"][1]["patterns"] = ["(unclosed"]

        with self._mutated_catalog(_break_pattern) as path:
            with self.assertRaises(ContractElementCatalogError) as context:
                load_contract_element_catalog(path)
        self.assertIn("不可编译", str(context.exception))

    def test_unknown_schema_version_is_rejected(self) -> None:
        with self._mutated_catalog(
            lambda payload: payload.update({"schema_version": "9.9"})
        ) as path:
            with self.assertRaises(ContractElementCatalogError) as context:
                load_contract_element_catalog(path)
        self.assertIn("schema_version", str(context.exception))

    def test_disabled_required_field_is_rejected(self) -> None:
        def _disable_required(payload: dict) -> None:
            for field in payload["fields"]:
                if field["key"] == "contract_name":
                    field["enabled"] = False

        with self._mutated_catalog(_disable_required) as path:
            with self.assertRaises(ContractElementCatalogError) as context:
                load_contract_element_catalog(path)
        self.assertIn("必填字段不允许被停用", str(context.exception))

    def test_missing_and_malformed_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="element-catalog-missing-") as temp_dir:
            missing = Path(temp_dir) / "nope.json"
            broken = Path(temp_dir) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ContractElementCatalogError):
                load_contract_element_catalog(missing)
            with self.assertRaises(ContractElementCatalogError):
                load_contract_element_catalog(broken)

    def _mutated_catalog(self, mutate):
        class _CatalogMutation:
            def __enter__(self):
                self._temp_dir = tempfile.mkdtemp(prefix="element-catalog-mutate-")
                payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
                mutate(payload)
                path = Path(self._temp_dir) / "catalog.json"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return path

            def __exit__(self, *_exc) -> None:
                shutil.rmtree(self._temp_dir, ignore_errors=True)

        return _CatalogMutation()


class ContractElementCatalogExtractionTests(unittest.TestCase):
    def test_catalog_extraction_equals_default_extraction(self) -> None:
        """用内置目录抽取必须与不传目录的默认路径完全一致。"""

        with tempfile.TemporaryDirectory(prefix="element-catalog-extract-") as temp_dir:
            candidates = _candidates(_parsed_contract(temp_dir))
            default_facts = extract_contract_element_facts_from_candidates(candidates)
            catalog_facts = extract_contract_element_facts_from_candidates(
                candidates, catalog=load_contract_element_catalog(CATALOG_PATH)
            )

        self.assertEqual(
            [(fact.fact_type, fact.value) for fact in default_facts],
            [(fact.fact_type, fact.value) for fact in catalog_facts],
        )
        self.assertTrue(all(fact.extractor_version for fact in catalog_facts))

    def test_custom_catalog_controls_extracted_fact_types(self) -> None:
        with tempfile.TemporaryDirectory(prefix="element-catalog-custom-") as temp_dir:
            target = Path(temp_dir) / "catalog.json"
            payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
            payload["extractor_version"] = "contract-elements-facts-test-9.9.9"
            payload["fields"] = [
                {
                    "key": "amount",
                    "label": "合同金额",
                    "aliases": ["合同金额"],
                    "patterns": [r"合同金额[:：]?\s*([0-9,，\.]+)\s*元"],
                    "required": False,
                    "enabled": True,
                    "sort_order": 10,
                }
            ]
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            catalog = load_contract_element_catalog(target)
            candidates = _candidates(_parsed_contract(temp_dir))
            facts = extract_contract_element_facts_from_candidates(
                candidates, catalog=catalog
            )

        self.assertEqual(catalog.extractor_version, "contract-elements-facts-test-9.9.9")
        self.assertEqual({fact.fact_type for fact in facts}, {"contract_element:amount"})
        self.assertTrue(
            all(
                fact.extractor_version == "contract-elements-facts-test-9.9.9"
                for fact in facts
            )
        )


class ContractElementFormProjectionTests(unittest.TestCase):
    """回填表单必须保留**全部**目录字段：抽不到只是没值，不是字段消失。"""

    def setUp(self) -> None:
        self._temp_dir = create_test_workspace("element-form-projection-")
        self.addCleanup(self._temp_dir.cleanup)
        self.work_path = Path(self._temp_dir.name)
        self.pdf_path = self.work_path / "element-form-contract.pdf"
        pdf = pymupdf.open()
        page = pdf.new_page(width=600, height=800)
        page.insert_text((60, 80), "合同名称：软件开发合同", fontname="china-s")
        page.insert_text((60, 120), "合同金额：人民币1000000元", fontname="china-s")
        pdf.save(str(self.pdf_path))
        pdf.close()
        self.bundle = publish_playbook_bundle(
            RuleBundle(
                bundle_id="element-form-rules-v1",
                source_filename="element-form-rules.xlsx",
                source_sha256="d" * 64,
                source_sheet="Sheet1",
                source_range="A1:C2",
                rules=[
                    Rule(
                        rule_id="keyword-contract-amount",
                        version="v1",
                        title="合同金额",
                        category="金额",
                        applies_to=["software"],
                        check_method="keyword",
                        source_snapshot="element-form-rules#1",
                    ),
                ],
            )
        )
        self.catalog = load_contract_element_catalog(CATALOG_PATH)
        self.result = run_review(
            [self.pdf_path],
            package_id="pkg-element-form-projection",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            element_catalog=self.catalog,
        )

    def test_uncaptured_fields_stay_in_form_with_empty_value(self) -> None:
        """没抽到的字段也必须出现在表单里，留空并标注未抽到。"""

        form = project_contract_element_form(self.result, catalog=self.catalog)
        expected_keys = [item.key for item in self.catalog.enabled_definitions]

        self.assertEqual([field.key for field in form.fields], expected_keys)
        captured = {field.key for field in form.fields if field.value}
        self.assertTrue(captured, "本用例需要至少一个抽到的字段作为对照")
        self.assertLess(
            len(captured),
            len(expected_keys),
            "该合同不含全部要素，必须存在未抽到的字段才能验证留空行为",
        )
        for field in form.fields:
            if field.key in captured:
                continue
            self.assertEqual(field.value, "")
            self.assertEqual(field.source, "empty")
            self.assertEqual(field.candidates, ())
            self.assertIsNone(field.confidence)
        self.assertEqual(set(form.fillable), captured)


class ContractElementCatalogReplayTests(unittest.TestCase):
    """要素目录身份必须进入运行配置，并在回放与审计时被强制比对。"""

    def setUp(self) -> None:
        self._temp_dir = create_test_workspace("element-catalog-replay-")
        self.addCleanup(self._temp_dir.cleanup)
        self.work_path = Path(self._temp_dir.name)
        self.pdf_path = self.work_path / "element-catalog-contract.pdf"
        pdf = pymupdf.open()
        page = pdf.new_page(width=600, height=800)
        page.insert_text((60, 80), "合同名称：软件开发合同", fontname="china-s")
        page.insert_text((60, 120), "合同金额：人民币1000000元", fontname="china-s")
        pdf.save(str(self.pdf_path))
        pdf.close()
        self.bundle = publish_playbook_bundle(
            RuleBundle(
                bundle_id="element-catalog-rules-v1",
                source_filename="element-catalog-rules.xlsx",
                source_sha256="c" * 64,
                source_sheet="Sheet1",
                source_range="A1:C2",
                rules=[
                    Rule(
                        rule_id="keyword-contract-amount",
                        version="v1",
                        title="合同金额",
                        category="金额",
                        applies_to=["software"],
                        check_method="keyword",
                        source_snapshot="element-catalog-rules#1",
                    ),
                ],
            )
        )

    def _run(self, *, catalog=None, run_id: str = "run-element-catalog"):
        return run_review(
            [self.pdf_path],
            package_id="pkg-element-catalog-replay",
            rule_bundle=self.bundle,
            review_context=ReviewContext(contract_type="software"),
            run_id=run_id,
            element_catalog=catalog,
        )

    def test_default_run_records_builtin_catalog_identity(self) -> None:
        """未显式传入目录时，运行配置必须记录内置目录身份。"""

        result = self._run()
        builtin = build_builtin_contract_element_catalog()
        identity = result.run.configuration["element_catalog"]

        self.assertEqual(identity["fingerprint"], builtin.fingerprint)
        self.assertEqual(identity["catalog_id"], builtin.catalog_id)
        self.assertEqual(identity["extractor_version"], builtin.extractor_version)
        self.assertEqual(len(identity["fingerprint"]), 64)
        self.assertEqual(
            identity["enabled_keys"],
            [item.key for item in builtin.enabled_definitions],
        )

    def test_external_catalog_identity_is_recorded_and_replays(self) -> None:
        """外置目录身份必须 round-trip：保存值能恢复为同一执行实现。"""

        catalog = load_contract_element_catalog(CATALOG_PATH)
        result = self._run(catalog=catalog)
        identity = result.run.configuration["element_catalog"]
        self.assertEqual(identity["catalog_id"], "contract-element-fields-v1")
        self.assertEqual(identity["fingerprint"], catalog.fingerprint)
        self.assertEqual(
            identity["enabled_keys"],
            [item.key for item in catalog.enabled_definitions],
        )

        replayed = replay_review(
            result,
            [self.pdf_path],
            rule_bundle=self.bundle,
            element_catalog=load_contract_element_catalog(CATALOG_PATH),
        )

        self.assertEqual(
            replayed.run.result_fingerprint, result.run.result_fingerprint
        )

    def test_changed_catalog_is_rejected_at_replay(self) -> None:
        """目录内容变化必须让回放前置失败，而不是事后靠结果不同才发现。"""

        original = load_contract_element_catalog(CATALOG_PATH)
        result = self._run(catalog=original, run_id="run-element-catalog-changed")
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        payload["fields"][0]["patterns"] = [r"合同名称[:：]\s*([^\n]{4,80})"]
        with tempfile.TemporaryDirectory(prefix="element-catalog-changed-") as temp_dir:
            target = Path(temp_dir) / "catalog.json"
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            changed = load_contract_element_catalog(target)
            with self.assertRaises(ReplayMismatch) as context:
                replay_review(
                    result,
                    [self.pdf_path],
                    rule_bundle=self.bundle,
                    element_catalog=changed,
                )

        self.assertIn("要素抽取目录不一致", str(context.exception))
        self.assertNotEqual(changed.fingerprint, original.fingerprint)

    def test_audit_rejects_facts_from_another_extractor_version(self) -> None:
        """配置声明的抽取器版本与要素事实不一致时必须被审计拒绝。"""

        result = self._run()
        element_facts = [
            fact
            for fact in result.facts
            if fact.fact_type.startswith("contract_element:")
        ]
        self.assertTrue(
            element_facts, "本用例需要至少一条 contract_element:* 事实作为断言基础"
        )
        self.assertTrue(audit_result(result).checks["element_catalog_identity"])

        tampered_configuration = {
            **result.run.configuration,
            "element_catalog": {
                **result.run.configuration["element_catalog"],
                "extractor_version": "contract-elements-facts-9.9.9",
            },
        }
        tampered = result.model_copy(
            update={
                "run": result.run.model_copy(
                    update={"configuration": tampered_configuration}
                )
            }
        )

        self.assertFalse(
            audit_result(tampered).checks["element_catalog_identity"]
        )


if __name__ == "__main__":
    unittest.main()
