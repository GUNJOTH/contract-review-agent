import unittest
from pathlib import Path

from contract_review.rules import load_rule_bundle


class RuleBundleTests(unittest.TestCase):
    def test_imported_excel_bundle_is_valid_and_complete(self) -> None:
        bundle_path = Path(__file__).parents[1] / "data" / "contract_rules_v0.14.json"
        bundle = load_rule_bundle(bundle_path)

        self.assertEqual(len(bundle.rules), 50)
        self.assertEqual(bundle.source_sheet, "Sheet1")
        self.assertEqual(bundle.source_range, "A1:M54")
        self.assertTrue(any(not rule.applies_to for rule in bundle.rules))
        self.assertTrue(all(rule.source_locator is not None for rule in bundle.rules))
