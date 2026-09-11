"""审查结果缓存测试（临时缓存目录）。"""

from contract_review_app.config import settings
import contract_review_app.services.result_cache as result_cache
from contract_review_app.services.result_cache import cache_get, cache_set, fingerprint


def _mock_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_CACHE_DIR", str(tmp_path / "cache")
    )


def test_fingerprint_changes_with_input(monkeypatch, tmp_path):
    _mock_cache(monkeypatch, tmp_path)
    first = fingerprint(["pkg-1", "file:abc"])
    changed = fingerprint(["pkg-1", "file:abd"])
    same = fingerprint(["pkg-1", "file:abc"])
    assert first != changed
    assert first == same


def test_cache_roundtrip(monkeypatch, tmp_path):
    _mock_cache(monkeypatch, tmp_path)
    key = fingerprint(["pkg-1"])
    assert cache_get(key) is None
    cache_set(key, {"result": "{}"})
    assert cache_get(key) == {"result": "{}"}


def test_cache_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_CACHE_ENABLED", False)
    _mock_cache(monkeypatch, tmp_path)
    key = fingerprint(["pkg-1"])
    cache_set(key, {"result": "{}"})
    assert cache_get(key) is None


def test_cache_write_failure_cleans_temporary_file(monkeypatch, tmp_path):
    _mock_cache(monkeypatch, tmp_path)
    key = fingerprint(["atomic-write"])

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(result_cache.os, "replace", fail_replace)
    cache_set(key, {"result": "{}"})

    cache_dir = tmp_path / "cache"
    assert cache_get(key) is None
    assert list(cache_dir.glob("*.tmp")) == []
