import unittest
from pathlib import Path

from contract_review.rules import load_rule_bundle


class RuleBundleTests(unittest.TestCase):
    def test_imported_excel_bundle_is_valid_and_complete(self) -> None:
        bundle_path = Path(__file__).parents[2] / "data" / "contract_rules_v0.14.json"
        bundle = load_rule_bundle(bundle_path)

        self.assertEqual(len(bundle.rules), 50)
        self.assertEqual(bundle.source_sheet, "Sheet1")
        self.assertEqual(bundle.source_range, "A1:M54")
        self.assertTrue(any(not rule.applies_to for rule in bundle.rules))
        self.assertTrue(all(rule.source_locator is not None for rule in bundle.rules))
        self.assertTrue(
            {
                "amount_case_consistency",
                "amount_detail_total",
                "untaxed_amount",
                "tax_rate",
                "tax_amount",
                "payment_ratio",
                "guarantee_requirement",
                "payment_total",
                "invoice_type",
                "invoice_amount",
                "invoice_total",
                "attachment_completeness",
            }.issubset({rule.checker for rule in bundle.rules})
        )
