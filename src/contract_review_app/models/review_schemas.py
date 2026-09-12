"""合同审查 API 的输入输出 DTO。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from contract_review.models import ContractRevisionSet, DecisionType, ReviewResult


class ContractReviewResponse(BaseModel):
    """同步合同审查用例的稳定响应契约。"""

    review_result: ReviewResult
    cached: bool = False


class ReviewResultResponse(BaseModel):
    """只返回核心 ReviewResult 的 HTTP 响应契约。"""

    review_result: ReviewResult


class ContractRevisionSetResponse(BaseModel):
    """从审查结果生成的修订提案响应契约。"""

    revision_set: ContractRevisionSet
    review_result: ReviewResult


class ReviewDecisionRequest(BaseModel):
    """记录一条人工决定所需的完整核心结果和定位信息。"""

    review_result: ReviewResult
    finding_id: str = Field(min_length=1)
    decision: DecisionType
    actor_id: str = Field(min_length=1)
    actor_role: str = Field(min_length=1)
    comment: str = Field(min_length=1)
    evidence_ids: list[str] | None = None


class ReviewFinalizationRequest(BaseModel):
    """完成审查运行所需的人工确认信息。"""

    review_result: ReviewResult
    actor_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)
