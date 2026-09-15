import pymupdf

from contract_review.models import (
    PlaybookAction,
    PlaybookSpec,
    ReviewContext,
    RiskLevel,
    Rule,
    RuleBundle,
    RevisionOperation,
)
from contract_review.pipeline import run_review
from contract_review.playbook import publish_playbook_bundle
from contract_review.revisions import build_revision_set


def _bundle() -> RuleBundle:
    return publish_playbook_bundle(RuleBundle(
        bundle_id="revision-rules-v1",
        source_filename="revision-rules.json",
        source_sha256="c" * 64,
        source_sheet="rules",
        source_range="A1:C1",
        rules=[
            Rule(
                rule_id="payment-revision-rule",
                version="v1",
                title="付款方式",
                category="付款结算",
                applies_to=["software"],
                check_method="deterministic",
                risk_level=RiskLevel.HIGH,
                applicability={"software": {"applicability": "required"}},
                source_snapshot="revision-rules#payment",
                playbook=PlaybookSpec(
                    playbook_id="payment-v1",
                    version="v1",
                    clause_types=["Payment"],
                    preferred_position="pay after acceptance",
                    fallback_positions=["pay in installments"],
                    prohibited_positions=["pay 100% in advance"],
                    action_on_preferred=PlaybookAction.ACCEPT,
                    action_on_fallback=PlaybookAction.REVISE,
                    action_on_prohibited=PlaybookAction.REJECT,
                    suggested_language="Pay the balance after acceptance.",
                    escalation_condition="Escalate if the advance ratio exceeds the limit.",
                ),
            )
        ],
    ))


def test_revision_set_turns_playbook_revise_into_evidence_bound_replace(tmp_path):
    pdf_path = tmp_path / "contract.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 80), "Payment clause: pay in installments after delivery.")
    document.save(str(pdf_path))
    document.close()

    result = run_review(
        [pdf_path],
        package_id="revision-package",
        rule_bundle=_bundle(),
        review_context=ReviewContext(contract_type="software"),
        run_id="revision-run",
    )
    finding = result.findings[0]
    assert finding.action is PlaybookAction.REVISE
    assert finding.status.value == "WARN"
    assert finding.clause_ids

    revisions = build_revision_set(result)

    assert revisions.run_id == "revision-run"
    assert revisions.base_result_fingerprint == result.run.result_fingerprint
    assert len(revisions.changes) == 1
    change = revisions.changes[0]
    assert change.operation is RevisionOperation.REPLACE
    assert change.clause_id == finding.clause_ids[0]
    assert change.proposed_text == "Pay the balance after acceptance."
    assert set(change.evidence_ids) == set(finding.evidence_ids)
    assert revisions.revision_fingerprint


def test_revision_set_keeps_escalation_as_comment_when_clause_is_missing(tmp_path):
    pdf_path = tmp_path / "contract-without-payment.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 80), "Delivery clause: delivery is due in ten days.")
    document.save(str(pdf_path))
    document.close()

    result = run_review(
        [pdf_path],
        package_id="revision-missing-package",
        rule_bundle=_bundle(),
        review_context=ReviewContext(contract_type="software"),
        run_id="revision-missing-run",
    )
    finding = result.findings[0]
    revisions = build_revision_set(result)

    assert finding.status.value == "UNKNOWN"
    assert finding.action is PlaybookAction.REQUEST_INFORMATION
    assert revisions.changes[0].operation is RevisionOperation.COMMENT
    assert "Pay the balance after acceptance." in revisions.changes[0].proposed_text
