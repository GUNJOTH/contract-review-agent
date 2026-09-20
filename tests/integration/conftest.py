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


@pytest.fixture(autouse=True)
def _isolate_external_model_endpoints(monkeypatch):
    """默认清空外部模型端点并关闭通读风险分析，避免本地 `.env` 让离线测试隐式发起真实网络调用。

    ``Settings`` 会读取仓库根的 `.env`，本机若配置了真实端点，任何走
    应用层审查的测试都会真的去连模型服务（并在重试耗尽后拖慢整个测试）。
    需要外部模型的用例必须自己显式设置端点并使用隔离的 fake/mock；需要
    通读风险分析的用例同样要显式打开 ``CONTRACT_RISK_ANALYSIS_ENABLED``，
    否则测试会跟着本机开关跑出与 CI 不一致的分片/降级结果。
    """

    monkeypatch.setattr(settings, "CONTRACT_REVIEW_ENDPOINT", "")
    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(settings, "CONTRACT_RISK_ANALYSIS_ENABLED", False)
