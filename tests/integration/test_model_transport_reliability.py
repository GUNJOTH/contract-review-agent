"""外部模型传输失败后的应用层降级、持久化与回放回归。"""

import pymupdf

from contract_review.models import Rule, RuleBundle, ReviewContext
from contract_review.playbook import publish_playbook_bundle
from contract_review.semantic import SemanticProviderUnavailableError
from contract_review_app.config import settings
from contract_review_app.config.settings import Settings
from contract_review_app.services.model_transport import HttpxModelTransport
from contract_review_app.services import review_service
from contract_review_app.services import rule_edits
from contract_review_app.services.review_result_store import (
    load_authoritative_review_result,
)


def _make_contract_pdf(path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "This software contract defines breach responsibility for both parties.",
    )
    document.save(str(path))
    document.close()


def _semantic_bundle() -> RuleBundle:
    return publish_playbook_bundle(
        RuleBundle(
            bundle_id="transport-reliability-rules-v1",
            source_filename="transport-reliability-rules.xlsx",
            source_sha256="t" * 64,
            source_sheet="Sheet1",
            source_range="A1:C2",
            rules=[
                Rule(
                    rule_id="semantic-breach",
                    version="v1",
                    title="breach responsibility",
                    category="contract",
                    applies_to=["software"],
                    check_method="semantic",
                    source_snapshot="transport-rules#1",
                )
            ],
        )
    )


def test_model_transport_default_attempts_are_five(monkeypatch):
    monkeypatch.delenv("CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS", raising=False)
    configured = Settings(_env_file=None)
    transport = HttpxModelTransport()
    try:
        assert configured.CONTRACT_REVIEW_MODEL_MAX_ATTEMPTS == 5
        assert transport.max_attempts == 5
    finally:
        transport.close()


class _UnavailableRelayClient:
    instances = []

    def __init__(self, **kwargs):
        del kwargs
        self.review_calls = 0
        self.closed = False
        self.instances.append(self)

    def review(self, _request):
        self.review_calls += 1
        raise SemanticProviderUnavailableError(attempts=3)

    def close(self):
        self.closed = True


def test_provider_transport_degradation_is_not_cached_and_replays_as_baseline(
    monkeypatch,
    tmp_path,
):
    bundle = _semantic_bundle()
    contract_path = tmp_path / "contract.pdf"
    _make_contract_pdf(contract_path)
    _UnavailableRelayClient.instances.clear()

    monkeypatch.setattr(
        rule_edits,
        "active_rule_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        review_service,
        "RelaySemanticReviewer",
        _UnavailableRelayClient,
    )
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "http://fake/chat")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_MODEL", "test-chat-model")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_MODEL", "")
    monkeypatch.setattr(settings, "CONTRACT_SEAL_DETECTION_ENABLED", False)
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_CACHE_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_CACHE_DIR",
        str(tmp_path / "review-cache"),
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_RESULT_STORE_DIR",
        str(tmp_path / "review-results"),
    )

    first_execution = review_service.run_contract_review(
        [("contract.pdf", contract_path.read_bytes())],
        package_id="pkg-provider-transport-fallback",
        review_context=ReviewContext(contract_type="software"),
        return_cache_status=True,
    )
    first = first_execution.result

    assert first_execution.cached is False
    assert first.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "provider_unavailable",
    }
    assert first.semantic_request is None
    assert first.semantic_response is None
    assert list((tmp_path / "review-cache").glob("*.json")) == []
    assert _UnavailableRelayClient.instances[0].review_calls == 1
    assert _UnavailableRelayClient.instances[0].closed

    authoritative = load_authoritative_review_result(first)
    assert authoritative.run.result_fingerprint == first.run.result_fingerprint
    assert authoritative.semantic_request is None
    assert authoritative.semantic_response is None

    replayed = review_service.replay_contract_review(
        first,
        [contract_path],
        rule_bundle=bundle,
    )
    assert replayed.run.result_fingerprint == first.run.result_fingerprint
    assert replayed.run.configuration["semantic_review_fallback"] == {
        "status": "DEGRADED",
        "reason": "provider_unavailable",
    }
    assert replayed.semantic_request is None
    assert replayed.semantic_response is None

    second_execution = review_service.run_contract_review(
        [("contract.pdf", contract_path.read_bytes())],
        package_id="pkg-provider-transport-fallback",
        review_context=ReviewContext(contract_type="software"),
        return_cache_status=True,
    )
    assert second_execution.cached is False
    assert len(_UnavailableRelayClient.instances) == 2
    assert _UnavailableRelayClient.instances[1].closed
