from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.expert_eval_schema import ExpertDataset, load_expert_dataset


DATASET_PATH = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "expert_contract_review_cases.json"
)


def test_expert_dataset_is_composed_of_complete_contract_packages() -> None:
    dataset = load_expert_dataset(DATASET_PATH)

    assert dataset.schema_version == "2.0"
    assert dataset.dataset_kind == "expert_contract_package"
    assert len(dataset.cases) == 8

    for case in dataset.cases:
        package = case.package
        annotation = case.expert_annotation

        assert package.main_contract.document_kind == "main_contract"
        assert package.supporting_documents
        assert package.version_or_amendment
        assert package.business_background.review_focus
        assert package.enterprise_position.non_negotiables
        assert annotation.clause_localization
        assert annotation.evidence_scope
        assert annotation.rule_conclusions
        assert annotation.unknown_reasons
        assert annotation.financial_facts
        assert annotation.financial_calculations
        assert annotation.version_changes.expected_changes
        assert annotation.redline_recommendations


def test_expert_dataset_rejects_the_removed_flat_case_shape() -> None:
    legacy_payload = {
        "dataset_id": "legacy",
        "schema_version": "2.0",
        "dataset_kind": "expert_contract_package",
        "language": "zh-CN",
        "annotation_status": "seed_requires_legal_signoff",
        "annotation_note": "legacy",
        "privacy_note": "legacy",
        "required_package_sections": [
            "main_contract",
            "supporting_documents",
            "version_or_amendment",
            "business_background",
            "enterprise_position",
            "expert_annotation",
        ],
        "cases": [
            {
                "case_id": "legacy-case",
                "text": "旧合同文本",
                "annotations": {"rule": "PASS"},
            }
        ],
    }

    with pytest.raises(ValidationError):
        ExpertDataset.model_validate(legacy_payload)


def test_expert_case_references_are_checked_by_the_schema() -> None:
    dataset = load_expert_dataset(DATASET_PATH)
    source = dataset.cases[0].model_dump(mode="json")
    source["expert_annotation"]["version_changes"]["base_document_id"] = (
        "missing-document"
    )

    with pytest.raises(ValidationError, match="base_document_id"):
        ExpertDataset.model_validate(
            {
                **dataset.model_dump(mode="json"),
                "cases": [source],
            }
        )
