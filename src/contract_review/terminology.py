"""合同审查的中文术语归一化边界。"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


TERMINOLOGY_NORMALIZATION_VERSION = "contract-terminology-0.1.0"
"""术语别名规则版本，随检索查询和证据门禁一起进入审计指纹。"""

_MAX_EXPANDED_TERMS = 32


@dataclass(frozen=True)
class TerminologyGroup:
    """表示只用于候选召回和证据匹配的高置信中文词组。"""

    canonical: str
    variants: tuple[str, ...]


# 这里只收录可作为同一业务事实表达的词形变体。
# 法律效果可能不同的概念（例如“解除”和“终止”）不在此处合并，
# 继续由规则的 required_fact_anchors 显式声明其适用范围。
CONTRACT_TERMINOLOGY_GROUPS: tuple[TerminologyGroup, ...] = (
    TerminologyGroup("付款", ("付款", "支付")),
    TerminologyGroup(
        "付款条件",
        ("付款条件", "付款条款", "支付条件", "支付条款"),
    ),
    TerminologyGroup("付款比例", ("付款比例", "支付比例")),
    TerminologyGroup("付款节点", ("付款节点", "支付节点")),
    TerminologyGroup("付款金额", ("付款金额", "支付金额")),
    TerminologyGroup("交付期限", ("交付期限", "交货期限")),
    TerminologyGroup("验收标准", ("验收标准", "验收要求")),
    TerminologyGroup("验收方法", ("验收方法", "验收方式")),
    TerminologyGroup("续期", ("续期", "续约", "续签")),
    TerminologyGroup("合同金额", ("合同金额", "合同价款")),
    TerminologyGroup("金额", ("金额", "价款")),
    TerminologyGroup("不含税金额", ("不含税金额", "未税金额")),
    TerminologyGroup("税率", ("税率", "增值税率")),
    TerminologyGroup("税额", ("税额", "增值税额")),
    TerminologyGroup("项目名称", ("项目名称", "项目名")),
    TerminologyGroup("源代码", ("源代码", "源码", "源程序")),
    TerminologyGroup("补充协议", ("补充协议", "补充合同")),
    TerminologyGroup("发票金额", ("发票金额", "开票金额")),
    TerminologyGroup("发票合计", ("发票合计", "发票总额")),
)


def _compact(value: str) -> str:
    """统一全角字符、大小写和空白，供术语匹配使用。"""

    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _surface(value: str) -> str:
    """保留可读形式，仅清理首尾空白和全角差异。"""

    return unicodedata.normalize("NFKC", value).strip()


def _append_unique(values: list[str], seen: set[str], value: str) -> None:
    normalized = _compact(value)
    if normalized and normalized not in seen:
        seen.add(normalized)
        values.append(value)


def _expanded_term_variants(term: str) -> list[str]:
    """生成一个查询词的有限变体，不对合同正文做替换。"""

    surface = _surface(term)
    compact_term = _compact(surface)
    if not compact_term:
        return []

    variants: list[str] = []
    seen: set[str] = set()
    _append_unique(variants, seen, surface)

    # 词组按长度优先用于替换，避免“合同金额”先被短词“金额”拆散。
    groups = sorted(
        CONTRACT_TERMINOLOGY_GROUPS,
        key=lambda group: max(len(_compact(item)) for item in group.variants),
        reverse=True,
    )
    for group in groups:
        matched_sources = sorted(
            (
                _compact(item)
                for item in group.variants
                if _compact(item) in compact_term
            ),
            key=len,
            reverse=True,
        )
        for source in matched_sources:
            for replacement in group.variants:
                expanded = compact_term.replace(source, _compact(replacement))
                _append_unique(variants, seen, expanded)
                if len(variants) >= _MAX_EXPANDED_TERMS:
                    return variants
    return variants


def expand_terminology_terms(terms: Sequence[str]) -> list[str]:
    """按稳定顺序扩展查询词，并去除全角/空白归一后的重复项。"""

    expanded: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for variant in _expanded_term_variants(term):
            _append_unique(expanded, seen, variant)
    return expanded


def expand_terminology_text(text: str) -> str:
    """为可读查询追加有限术语变体，保留原始查询作为第一部分。"""

    surface = _surface(text)
    if not surface:
        return ""
    variants = expand_terminology_terms([surface])
    return "；".join(variants)


def matched_terminology_terms(
    terms: Sequence[str],
    content: str,
) -> list[str]:
    """返回命中的原始查询词，别名命中也归属到原始查询词。"""

    normalized_content = _compact(content)
    matched: list[str] = []
    for term in terms:
        variants = _expanded_term_variants(term)
        if any(
            _compact(variant) in normalized_content
            for variant in variants
            if _compact(variant)
        ):
            matched.append(term)
    return list(dict.fromkeys(matched))


def terminology_matches(content: str, term: str) -> bool:
    """判断合同片段是否包含该查询词或其登记别名。"""

    return bool(matched_terminology_terms([term], content))
