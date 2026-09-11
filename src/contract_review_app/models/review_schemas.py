"""合同审查 API 的输入输出 DTO。"""

from __future__ import annotations

from pydantic import BaseModel

from contract_review import ReviewAnalysisProjection
from contract_review.models import ContractRevisionSet, ReviewResult


class ContractReviewResponse(BaseModel):
    """同步合同审查用例的稳定响应契约。"""

    review_result: ReviewResult
    # 兼容历史客户端；内容完全由 review_result 投影生成，不触发第二条模型链。
    ai_analysis: ReviewAnalysisProjection | None = None
    cached: bool = False


class ContractRevisionSetResponse(BaseModel):
    """从审查结果生成的修订提案响应契约。"""

    revision_set: ContractRevisionSet
