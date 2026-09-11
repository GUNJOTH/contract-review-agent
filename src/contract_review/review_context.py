"""审查上下文的归一化与兼容边界。"""

from __future__ import annotations

from .models import ReviewContext

REVIEW_CONTEXT_VERSION = "review-context-0.1.0"


class ReviewContextError(ValueError):
    """审查上下文缺失、冲突或无法归一化时抛出。"""


def resolve_review_context(
    review_context: ReviewContext | None = None,
    *,
    contract_type: str | None = None,
) -> ReviewContext:
    """合并新上下文与旧 ``contract_type`` 参数，返回唯一业务对象。

    ``contract_type`` 是现有 CLI、任务和 API 的兼容入口；一旦新旧参数
    同时提供且含义冲突，直接拒绝请求，避免同一运行出现两个合同类型真相。
    """

    legacy_contract_type = _normalize_contract_type(contract_type)
    if review_context is None:
        return ReviewContext(contract_type=legacy_contract_type)

    context_contract_type = _normalize_contract_type(review_context.contract_type)
    if (
        legacy_contract_type is not None
        and context_contract_type is not None
        and legacy_contract_type != context_contract_type
    ):
        raise ReviewContextError(
            "contract_type 与 review_context.contract_type 不一致"
        )
    if legacy_contract_type is not None and context_contract_type is None:
        return review_context.model_copy(
            update={"contract_type": legacy_contract_type}
        )
    return review_context


def _normalize_contract_type(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewContextError("contract_type 必须是字符串")
    normalized = value.strip()
    return normalized or None

