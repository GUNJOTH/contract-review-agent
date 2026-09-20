"""应用层是否按外置要素目录执行审查的接线回归测试。

要素字段目录决定 ``ReviewResult.facts`` 中 ``contract_element:*`` 事实的
抽取口径。这组测试锁定两件事：应用层审查必须记录实际生效的目录身份；目录
内容变化必须改变审查输入指纹，否则改完目录会命中按旧口径算出的缓存结果。
"""

import json
from pathlib import Path

import pymupdf
import pytest

from contract_review import audit_result, load_contract_element_catalog
from contract_review.elements import ContractElementCatalogError
from contract_review.models import ReviewContext
from contract_review_app.config import settings
from contract_review_app.services import review_service

CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "contract_element_fields_v1.json"
)


def _make_contract_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "合同名称：软件开发合同。合同金额：人民币1000000元。",
        fontname="china-s",
    )
    data = document.tobytes()
    document.close()
    return data


def _offline_review(monkeypatch, content: bytes, *, package_id: str):
    """在离线边界内跑一次应用层审查：不调印章检测，也不调语义模型。"""

    monkeypatch.setattr(settings, "CONTRACT_REVIEW_EMBEDDING_ENDPOINT", "")
    monkeypatch.setattr(review_service, "_collect_seal_evidence", lambda *_a, **_k: [])
    return review_service.run_contract_review(
        [("合同主文.pdf", content)],
        package_id=package_id,
        review_context=ReviewContext(contract_type="软件开发/转让服务"),
        allow_semantic=False,
    )


def test_upload_review_records_external_element_catalog_identity(monkeypatch) -> None:
    """运行配置必须记录外置快照的身份，而不是内置定义的身份。"""

    catalog = load_contract_element_catalog(CATALOG_PATH)
    result = _offline_review(
        monkeypatch,
        _make_contract_pdf(),
        package_id="pkg-element-catalog-wiring",
    )

    identity = result.run.configuration["element_catalog"]
    assert identity["catalog_id"] == "contract-element-fields-v1"
    assert identity["fingerprint"] == catalog.fingerprint
    assert identity["extractor_version"] == catalog.extractor_version
    assert audit_result(result).passed

    element_facts = [
        fact
        for fact in result.facts
        if fact.fact_type.startswith("contract_element:")
    ]
    assert element_facts, "外置目录下应至少抽出一个标准要素事实"
    assert all(
        fact.extractor_version == catalog.extractor_version for fact in element_facts
    )


def test_review_fingerprint_includes_element_catalog_source(monkeypatch) -> None:
    """目录来源必须进入缓存身份：外置快照与内置回退不能共用同一条缓存。"""

    content = _make_contract_pdf()
    files = [("合同主文.pdf", content)]
    kwargs = {
        "package_id": "pkg-element-catalog-fingerprint",
        "review_context": ReviewContext(contract_type="软件开发/转让服务"),
        "allow_semantic": False,
    }

    with_external = review_service._review_fingerprint(files, **kwargs)
    monkeypatch.setattr(settings, "CONTRACT_ELEMENT_FIELDS_PATH", "")
    with_builtin = review_service._review_fingerprint(files, **kwargs)

    assert with_external != with_builtin


def test_review_fingerprint_changes_when_catalog_content_changes(
    monkeypatch, tmp_path
) -> None:
    """目录内容变化必须改变输入指纹，使旧缓存与新口径不再混淆。"""

    content = _make_contract_pdf()
    files = [("合同主文.pdf", content)]
    kwargs = {
        "package_id": "pkg-element-catalog-fingerprint-change",
        "review_context": ReviewContext(contract_type="软件开发/转让服务"),
        "allow_semantic": False,
    }
    baseline = review_service._review_fingerprint(files, **kwargs)

    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["fields"][0]["patterns"] = [r"合同名称[:：]\s*([^\n]{4,80})"]
    edited_path = tmp_path / "edited-catalog.json"
    edited_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(
        settings, "CONTRACT_ELEMENT_FIELDS_PATH", str(edited_path)
    )

    assert review_service._review_fingerprint(files, **kwargs) != baseline


def test_configured_but_broken_catalog_fails_the_review(monkeypatch, tmp_path) -> None:
    """目录损坏必须让审查显式失败，不能静默回退成少抽字段。"""

    monkeypatch.setattr(settings, "CONTRACT_ELEMENT_FIELDS_PATH", "")
    assert (
        review_service.load_element_catalog().catalog_id
        == "contract-element-fields-builtin"
    )

    broken_path = tmp_path / "broken-catalog.json"
    broken_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(settings, "CONTRACT_ELEMENT_FIELDS_PATH", str(broken_path))

    with pytest.raises(ContractElementCatalogError):
        review_service.load_element_catalog()
