"""基于合同证据相关性的确定性候选二阶段精排。"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .knowledge import _retrieval_trace_id
from .models import KnowledgeChunk, RetrievalHit, RetrievalQuery, RetrievalTrace
from .terminology import expand_terminology_terms, matched_terminology_terms


LEGAL_RELEVANCE_RERANKER_VERSION = "legal-relevance-reranker-0.1.1"

# 先扩大一个有限候选池再精排，避免把初排靠后的可采信条款永久排除；
# 上限用于控制合同包很大时的 CPU 成本，真正的模型精排仍应在独立评测后接入。
RERANK_CANDIDATE_POOL_MULTIPLIER = 3
RERANK_MAX_CANDIDATE_POOL = 50

# 这些是可审计的无训练基线权重，不代表最终业务最优值；后续应使用律师分级
# 标注集做消融和校准，不能在没有评测证据时继续凭经验调整。
_RERANK_FEATURE_WEIGHTS = {
    "initial_rank": 0.20,
    "exact_anchor_coverage": 0.25,
    "required_fact_coverage": 0.30,
    "constraint_coverage": 0.15,
    "anchor_proximity": 0.10,
}
_PROXIMITY_WINDOW = 80
_MIN_PROXIMITY_ANCHOR_LENGTH = 2


@dataclass(frozen=True)
class _RelevanceFeatures:
    """保存一个候选的可解释精排特征；空值表示查询没有该类约束。"""

    initial_rank: float
    exact_anchor_coverage: float | None
    required_fact_coverage: float | None
    constraint_coverage: float | None
    anchor_proximity: float | None

    def active_values(self) -> dict[str, float]:
        values: dict[str, float] = {"initial_rank": self.initial_rank}
        optional_values = {
            "exact_anchor_coverage": self.exact_anchor_coverage,
            "required_fact_coverage": self.required_fact_coverage,
            "constraint_coverage": self.constraint_coverage,
            "anchor_proximity": self.anchor_proximity,
        }
        values.update(
            {
                name: value
                for name, value in optional_values.items()
                if value is not None
            }
        )
        return values


def _compact(text: str) -> str:
    """统一全角字符并去除空白，保证中文锚点和数字格式可比较。"""

    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _normalised_anchors(anchors: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for anchor in anchors:
        normalised = _compact(anchor)
        if normalised and normalised not in seen:
            seen.add(normalised)
            result.append(normalised)
    return result


def _weighted_coverage(anchors: Sequence[str], content: str) -> float | None:
    """按锚点长度计算覆盖率，避免短泛词完全压过长法律短语。"""

    normalised_anchors = _normalised_anchors(anchors)
    if not normalised_anchors:
        return None
    total_weight = sum(len(anchor) for anchor in normalised_anchors)
    matched_anchors = {
        _compact(anchor) for anchor in matched_terminology_terms(anchors, content)
    }
    matched_weight = sum(
        len(anchor) for anchor in normalised_anchors if anchor in matched_anchors
    )
    return matched_weight / total_weight if total_weight else None


def _constraint_coverage(query: RetrievalQuery, content: str) -> float | None:
    coverages: list[float] = []
    for anchors in (query.numeric_anchors, query.negation_anchors):
        coverage = _weighted_coverage(anchors, content)
        if coverage is not None:
            coverages.append(coverage)
    return sum(coverages) / len(coverages) if coverages else None


def _anchor_proximity(query: RetrievalQuery, content: str) -> float | None:
    """衡量关键锚点是否在同一局部证据片段中共同出现。"""

    anchors = _normalised_anchors(
        [
            *query.required_fact_anchors,
            *query.numeric_anchors,
            *query.negation_anchors,
            *(
                anchor
                for anchor in query.exact_anchors
                if len(_compact(anchor)) >= _MIN_PROXIMITY_ANCHOR_LENGTH
            ),
        ]
    )
    positions = sorted(
        {
            position
            for anchor in anchors
            for variant in expand_terminology_terms([anchor])
            if (position := content.find(_compact(variant))) >= 0
        }
    )
    if len(positions) < 2:
        return None
    closest_gap = min(right - left for left, right in zip(positions, positions[1:]))
    return max(0.0, 1.0 - closest_gap / _PROXIMITY_WINDOW)


def _features_for_hit(
    query: RetrievalQuery,
    chunk: KnowledgeChunk,
    *,
    initial_rank: int,
    candidate_count: int,
) -> _RelevanceFeatures:
    initial_rank_score = (
        1.0
        if candidate_count <= 1
        else 1.0 - (initial_rank - 1) / (candidate_count - 1)
    )
    content = _compact(chunk.content)
    return _RelevanceFeatures(
        initial_rank=initial_rank_score,
        exact_anchor_coverage=_weighted_coverage(query.exact_anchors, content),
        required_fact_coverage=_weighted_coverage(
            query.required_fact_anchors,
            content,
        ),
        constraint_coverage=_constraint_coverage(query, content),
        anchor_proximity=_anchor_proximity(query, content),
    )


def _score_features(features: _RelevanceFeatures) -> float:
    active_values = features.active_values()
    active_weight = sum(_RERANK_FEATURE_WEIGHTS[name] for name in active_values)
    if active_weight <= 0:
        return 0.0
    return round(
        sum(
            _RERANK_FEATURE_WEIGHTS[name] * value
            for name, value in active_values.items()
        )
        / active_weight,
        6,
    )


def rerank_candidate_pool_size(top_k: int) -> int:
    """返回精排前的有限候选池大小。"""

    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k 必须是正整数")
    return max(
        top_k,
        min(top_k * RERANK_CANDIDATE_POOL_MULTIPLIER, RERANK_MAX_CANDIDATE_POOL),
    )


def rerank_retrieval_trace(
    trace: RetrievalTrace,
    chunks_by_id: Mapping[str, KnowledgeChunk],
    *,
    top_k: int | None = None,
) -> RetrievalTrace:
    """在初排候选池内按证据相关性重排并保留审计特征。

    ``trace`` 的 ``score`` 和词法/向量名次保持原始检索器语义；最终顺序由
    ``rerank_score`` 决定，避免破坏既有 RRF 分数校验和跨适配器兼容性。
    缺失知识块直接报错，不能静默丢弃候选或用空文本继续精排。
    """

    final_top_k = trace.top_k if top_k is None else top_k
    if (
        isinstance(final_top_k, bool)
        or not isinstance(final_top_k, int)
        or final_top_k <= 0
    ):
        raise ValueError("top_k 必须是正整数")

    scored_hits: list[tuple[RetrievalHit, float]] = []
    candidate_count = len(trace.hits)
    for initial_rank, hit in enumerate(trace.hits, start=1):
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk is None:
            raise ValueError(f"精排轨迹引用不存在的知识块: {hit.chunk_id}")
        features = _features_for_hit(
            trace.retrieval_query,
            chunk,
            initial_rank=initial_rank,
            candidate_count=candidate_count,
        )
        rerank_score = _score_features(features)
        reranked_hit = hit.model_copy(
            update={
                "rerank_score": rerank_score,
                "rerank_features": {
                    name: round(value, 6)
                    for name, value in features.active_values().items()
                },
            }
        )
        scored_hits.append((reranked_hit, rerank_score))

    scored_hits.sort(key=lambda item: (-item[1], -item[0].score, item[0].chunk_id))
    final_hits = [hit for hit, _ in scored_hits[:final_top_k]]
    return trace.model_copy(
        update={
            "trace_id": _retrieval_trace_id(
                trace.retrieval_query,
                trace.used_for_rule_ids,
                top_k=final_top_k,
            ),
            "top_k": final_top_k,
            "hits": final_hits,
            "reranker_version": LEGAL_RELEVANCE_RERANKER_VERSION,
        }
    )
