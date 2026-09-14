"""合同审查相关测试的公共隔离：审查结果缓存目录指向临时目录，避免跨测试污染。"""

import pytest

from contract_review_app.config import settings


@pytest.fixture(autouse=True)
def _isolate_review_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "CONTRACT_REVIEW_CACHE_DIR", str(tmp_path / "review_cache")
    )
    monkeypatch.setattr(
        settings,
        "CONTRACT_REVIEW_RESULT_STORE_DIR",
        str(tmp_path / "review_results"),
    )
