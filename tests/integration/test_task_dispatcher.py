from __future__ import annotations

from contract_review_app import bootstrap
from contract_review_app.config import settings
from contract_review_app.services.task_dispatcher import get_dispatch_config


def test_task_dispatcher_uses_configured_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CELERY_DEFAULT_QUEUE", "isolated.contract")

    config = get_dispatch_config("contract-review")

    assert config.queue_name == "isolated.contract"


def test_worker_command_uses_configured_queue(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap.settings, "CELERY_DEFAULT_QUEUE", "isolated.contract")

    command = bootstrap._worker_command()

    assert command[command.index("-Q") + 1] == "isolated.contract"
