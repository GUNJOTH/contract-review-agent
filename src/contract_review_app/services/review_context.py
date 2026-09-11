"""API/任务边界上的审查上下文解析。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from contract_review.models import PartyPosition, ReviewContext


class ReviewContextInputError(ValueError):
    """外部请求中的审查上下文不符合接口契约时抛出。"""


_PARTY_POSITION_ALIASES = {
    "buyer": PartyPosition.BUYER,
    "买方": PartyPosition.BUYER,
    "甲方": PartyPosition.BUYER,
    "seller": PartyPosition.SELLER,
    "卖方": PartyPosition.SELLER,
    "乙方": PartyPosition.SELLER,
    "both": PartyPosition.BOTH,
    "双方": PartyPosition.BOTH,
    "unknown": PartyPosition.UNKNOWN,
    "未知": PartyPosition.UNKNOWN,
}


def build_review_context(
    *,
    contract_type: str | None = None,
    party_position: str | None = None,
    jurisdiction: str | None = None,
    transaction_context: str | None = None,
    review_scope: str | Sequence[str] | None = None,
) -> ReviewContext:
    """把 multipart 表单或任务选项转换为唯一的核心审查上下文对象。

    ``review_scope`` 兼容逗号分隔文本和 JSON 字符串数组，最终只向领域层
    传递去重后的字符串列表；不支持的交易立场直接返回输入错误。
    """

    normalized_party_position = _normalize_party_position(party_position)
    normalized_scope = _parse_review_scope(review_scope)
    try:
        return ReviewContext(
            contract_type=contract_type,
            party_position=normalized_party_position,
            jurisdiction=jurisdiction,
            transaction_context=transaction_context,
            review_scope=normalized_scope,
        )
    except ValueError as exc:
        raise ReviewContextInputError(str(exc)) from exc


def _normalize_party_position(value: str | None) -> PartyPosition:
    if value is None or not value.strip():
        return PartyPosition.UNKNOWN
    normalized = value.strip().casefold()
    position = _PARTY_POSITION_ALIASES.get(normalized)
    if position is None:
        raise ReviewContextInputError(
            "PartyPosition 仅支持 buyer/买方、seller/卖方、both/双方 或 unknown/未知"
        )
    return position


def _parse_review_scope(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ReviewContextInputError(
                    "ReviewScope 为 JSON 数组时必须是有效 JSON"
                ) from exc
            value = parsed
        else:
            value = re.split(r"[,，;；\n]", text)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ReviewContextInputError(
            "ReviewScope 必须是逗号分隔文本或字符串数组"
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ReviewContextInputError("ReviewScope 的每一项必须是字符串")
        scope_item = item.strip()
        if scope_item and scope_item not in normalized:
            normalized.append(scope_item)
    return normalized
