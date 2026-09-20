"""通读式 AI 风险分析：请求构造、响应门禁与身份指纹。

这是 v1 ``ai_analysis``"模型通读合同、以 AI 判断为主"能力的 v2 化改造。
与 v1 的两点不同：

1. **证据门禁**：BLOCK/WARN/INFO 风险项必须回指候选证据白名单内的
   ``evidence_id``，模型不能凭常识编风险；规则判定项中"符合"（PASS）允许
   无证据引用——判定本身的依据写在 reason 里，由审计核对证据存在性。
2. **覆盖率在提示词里恢复（v1 口径）**：模型必须对规则清单里的每一条规则
   输出判定（PASS/BLOCK/WARN/UNKNOWN），再补充规则外风险——这样审查清单
   才会展示全部规则（v1 的覆盖率硬校验由"提示词强制 + 门禁归一"承接）。

并发口径：通读摘录按 v1 同样截断（单请求可容纳），因此本判据是单批请求；
逐规则语义批的并发由语义链路自己的线程池承担，两者互不影响。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import (
    CandidateEvidence,
    KnowledgeSourceKind,
    RiskAnalysisItem,
    RiskAnalysisRequest,
    RiskAnalysisResponse,
    ReviewResult,
    Rule,
)


RISK_ANALYSIS_VERSION = "contract-risk-analysis-0.3.0"
MAX_RISK_ANALYSIS_CANDIDATES = 32
MAX_RISK_ITEM_TITLE_LENGTH = 80
MAX_RISK_ITEM_REASON_LENGTH = 600
MAX_RISK_QUOTE_LENGTH = 100
# 防失控上限：每条适用规则一条判定 + 额外风险。
MAX_RISK_ANALYSIS_ITEMS = 200
# 单次调用的规则条数上限：覆盖率的关键。同一份合同、同一天的实测里，一次
# 把 57 条规则全喂给模型，它分别只回了 55 / 31 / 15 / 1 条判定——清单越长
# 越容易提前收尾，且 JSON 断尾抢救只能救回已经写完整的那些条目。拆成小块
# 后每块要输出的条目数可控，覆盖率才稳定。
RISK_ANALYSIS_RULE_CHUNK_SIZE = 15
# 单片覆盖率低于该比例即视为"模型没答完"，用同一请求重试一次（只重试一次）。
RISK_ANALYSIS_CHUNK_MIN_COVERAGE = 0.5
# 条目合法等级。NOT_APPLICABLE（本规则不适用）与规则引擎、语义判据同口径：
# 已确定的合同类型不匹配某个候选类型时是"不适用"，不是"摘录不足以判定"。
RISK_ITEM_LEVELS = frozenset(
    {"BLOCK", "WARN", "INFO", "PASS", "UNKNOWN", "NOT_APPLICABLE"}
)
# 允许不带证据的等级：这三个等级都不构成"发现问题"，不要求回指摘录。
LEVELS_WITHOUT_EVIDENCE = frozenset({"PASS", "NOT_APPLICABLE", "UNKNOWN"})
# 合同类型分类规则的分类名：5 个候选类型是单选题，不是 5 道判断题。
CONTRACT_TYPE_CATEGORY = "合同类型"
RISK_ANALYSIS_SYSTEM_INSTRUCTION = (
    "你是资深合同风险审查专家。先判断本合同属于哪个合同类型（可选值："
    "软件产品销售 / 软件开发/转让服务 / 一般商品销售合同 / 混合合同 / "
    "其它服务合同；都不完全符合时选择最接近的并说明依据）。\n"
    "然后对 rules 清单里的每一条规则逐条审查，并补充规则清单之外的风险。\n"
    "硬性规则：\n"
    "1. rules 里的每一条规则都必须输出一条判定项：rule_id 逐字回填该规则的"
    "rule_id，title 用该规则的标题（逐字）——两者必须成对回填，只填标题不填"
    "rule_id 会被当成规则清单外的风险，不算作该规则的判定；risk_level 取 "
    "PASS（符合）/BLOCK（重大违反）/WARN（违反或部分违反）/UNKNOWN（摘录不足"
    "以判定）/NOT_APPLICABLE（本规则与合同类型或内容无关，不适用）；"
    "reason 说明判定依据与影响。\n"
    "2. 合同类型是单选题：第 1 步选定的那个类型判 PASS，其余候选类型一律判 "
    "NOT_APPLICABLE（不适用）——不要给未选中的候选类型判 WARN 或 UNKNOWN。\n"
    "3. risk_level 为 BLOCK/WARN/INFO 的条目必须给出 evidence_id（该结论在"
    "合同摘录中的出处）；PASS / NOT_APPLICABLE 条目可省略 evidence_id；"
    "UNKNOWN 建议给出支撑摘录。规则判定项的 reason 控制在 60 字以内，额外"
    "风险的 reason 不超过 200 字——输出必须完整，不要省略任何规则的判定。\n"
    "4. 规则判定之外，发现规则清单没有覆盖的风险点时，额外输出条目：不带"
    "rule_id，module 按问题性质从 风险点/合理性/内控/资信 中选择，且必须有"
    "evidence_id。\n"
    "5. evidence_id 只能从 allowed_evidence_ids 中逐字选择，禁止编造；"
    "quote 尽量摘录原文短句（不超过 100 字）。\n"
    "6. 与 known_findings 中已列出的问题重复的不要重复输出（每条规则的判定"
    "项除外——那是必须输出的）。\n"
    "7. confidence 是你对该判断的把握程度，必须小于 1。\n"
    "8. contract_type.name 必须从上述可选值中逐字选择；basis 说明判定依据，"
    "引用摘录中的原句片段。\n"
    '只输出 JSON：{"contract_type":{"name":"...","basis":"..."},'
    '"items":[{"rule_id":"...","title":"...","risk_level":"PASS","reason":"...",'
    '"evidence_id":"...","quote":"...","module":"内控","confidence":0.8}]}'
)
CONTRACT_TYPE_OPTIONS = (
    "软件产品销售",
    "软件开发/转让服务",
    "一般商品销售合同",
    "混合合同",
    "其它服务合同",
)


class RiskAnalysisClientError(RuntimeError):
    """风险分析响应不可用或未通过门禁。"""


class RiskAnalysisClient(Protocol):
    def analyze(
        self, request: RiskAnalysisRequest
    ) -> RiskAnalysisResponse:
        """为给定的通读请求返回结构化风险分析响应。"""


class RiskAnalysisUnavailableError(RiskAnalysisClientError):
    """风险分析提供方在有限重试后仍不可用。"""

    def __init__(self, *, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(
            f"risk analysis provider unavailable after {attempts} attempts"
        )


def build_risk_analysis_request(
    result: ReviewResult,
    *,
    rules: Sequence[Rule],
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str = RISK_ANALYSIS_SYSTEM_INSTRUCTION,
    configuration: Mapping[str, Any] | None = None,
) -> RiskAnalysisRequest | None:
    """构造通读风险分析请求；没有候选证据时返回 ``None``。

    ``rules`` 是本次审查实际执行的适用规则，只取 title/category 作为提示——
    模型不需要规则全文，规则对照由语义判据负责，这里只要它知道"哪些面
    已经有人查过"，避免重复。合同摘录就是候选证据池的正文（32 块以内，
    与 v1 的 15000 字符全文摘录同量级）。
    """

    pool = _select_risk_candidates(result)
    if not pool:
        return None
    allowed_evidence_ids = sorted(
        {
            evidence_id
            for candidate in pool
            for evidence_id in candidate.evidence_ids
        }
    )
    if not allowed_evidence_ids:
        return None
    rule_hints = [
        {
            "rule_id": rule.rule_id,
            "title": rule.title,
            "category": rule.category,
        }
        for rule in rules
    ]
    known_findings = [
        {
            "title": finding.title,
            "risk_level": str(finding.risk_level or finding.status),
        }
        for finding in result.findings
    ]
    request_fingerprint = build_risk_analysis_request_fingerprint(
        result=result,
        candidates=pool,
        allowed_evidence_ids=allowed_evidence_ids,
        rule_hints=rule_hints,
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        system_instruction=system_instruction,
        configuration=configuration,
    )
    return RiskAnalysisRequest(
        request_id=f"risk-analysis-request-{uuid.uuid4().hex}",
        provider=provider,
        model_version=model_version,
        prompt_version=prompt_version,
        request_fingerprint=request_fingerprint,
        candidate_evidence=list(pool),
        allowed_evidence_ids=allowed_evidence_ids,
        rule_hints=rule_hints,
        known_findings=known_findings,
        system_instruction=system_instruction,
        configuration=dict(configuration or {}),
    )


def _select_risk_candidates(result: ReviewResult) -> list[CandidateEvidence]:
    """从已获资格的合同候选中挑通读摘录：同一 chunk 保分最高者，稳定截断。

    只取 ``source_kind=CONTRACT`` 的块：规则定义块（"CONTRACT-CHECK-… 合同名称"
    这类标题行）不是合同正文，混进摘录会让模型把规则文本当成被审对象，
    产出"摘录无正文"之类的元噪声。
    """

    best_by_chunk: dict[str, CandidateEvidence] = {}
    for candidate in result.candidate_evidence:
        if candidate.source_kind != KnowledgeSourceKind.CONTRACT:
            continue
        current = best_by_chunk.get(candidate.chunk_id)
        if current is None or candidate.candidate_id < current.candidate_id:
            best_by_chunk[candidate.chunk_id] = candidate
    ranked = sorted(
        best_by_chunk.values(),
        key=lambda item: (-item.score, item.chunk_id, item.candidate_id),
    )
    return ranked[:MAX_RISK_ANALYSIS_CANDIDATES]


def build_risk_analysis_request_fingerprint(
    *,
    result: ReviewResult,
    candidates: Sequence[CandidateEvidence],
    allowed_evidence_ids: Sequence[str],
    rule_hints: Sequence[Mapping[str, Any]],
    provider: str,
    model_version: str,
    prompt_version: str,
    system_instruction: str,
    configuration: Mapping[str, Any] | None = None,
) -> str:
    """绑定结果身份、候选证据、规则提示与提示词的稳定指纹。"""

    payload = {
        "version": RISK_ANALYSIS_VERSION,
        "provider": provider,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "system_instruction": system_instruction,
        "result_fingerprint": result.run.result_fingerprint,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "chunk_id": candidate.chunk_id,
                "document_id": candidate.document_id,
                "evidence_ids": list(candidate.evidence_ids),
            }
            for candidate in candidates
        ],
        "allowed_evidence_ids": list(allowed_evidence_ids),
        "rule_hints": [dict(hint) for hint in rule_hints],
        "configuration": json.loads(
            json.dumps(configuration or {}, ensure_ascii=False, sort_keys=True, default=str)
        ),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_risk_analysis_response(
    request: RiskAnalysisRequest,
    response: RiskAnalysisResponse,
) -> list[RiskAnalysisItem]:
    """校验并归一风险分析响应，返回可用风险项。

    结构性错误（指纹/提供方/模型/提示词不一致、证据越界）整份拒绝；
    单条内容问题（未知枚举、缺标题）丢弃该条并保留其余——风险项是提示性
    判据而非结论，模型单条抖动不应让整个补充视角消失。丢弃情况由调用方
    记录进运行配置。
    """

    if request.request_fingerprint != response.request_fingerprint:
        raise RiskAnalysisClientError(
            "risk analysis request and response fingerprints do not match"
        )
    if request.provider != response.provider:
        raise RiskAnalysisClientError(
            "risk analysis request and response providers do not match"
        )
    if request.model_version != response.model_version:
        raise RiskAnalysisClientError(
            "risk analysis request and response model versions do not match"
        )
    if request.prompt_version != response.prompt_version:
        raise RiskAnalysisClientError(
            "risk analysis request and response prompt versions do not match"
        )

    allowed_evidence_ids = set(request.allowed_evidence_ids)
    valid_levels = RISK_ITEM_LEVELS
    # 必须带证据的等级：符合（PASS）、不适用（NOT_APPLICABLE）与摘录不足
    # （UNKNOWN）都不构成"发现问题"，允许空证据。
    levels_requiring_evidence = RISK_ITEM_LEVELS - LEVELS_WITHOUT_EVIDENCE
    valid_modules = {"风险点", "合理性", "内控", "资信"}
    known_rule_ids = {
        str(hint["rule_id"])
        for hint in request.rule_hints
        if isinstance(hint, Mapping) and hint.get("rule_id")
    }
    rule_ids_by_title = _rule_ids_by_title(request.rule_hints)
    category_by_rule_id = {
        str(hint["rule_id"]): str(hint.get("category") or "")
        for hint in request.rule_hints
        if isinstance(hint, Mapping) and hint.get("rule_id")
    }
    items: list[RiskAnalysisItem] = []
    for item in response.items[:MAX_RISK_ANALYSIS_ITEMS]:
        level = str(item.risk_level).upper()
        if level not in valid_levels:
            continue
        evidence_ids = [
            evidence_id
            for evidence_id in item.evidence_ids
            if evidence_id in allowed_evidence_ids
        ]
        if level in levels_requiring_evidence and not evidence_ids:
            continue
        title = item.title.strip()
        if not title:
            continue
        module = item.module if item.module in valid_modules else "内控"
        items.append(
            item.model_copy(
                update={
                    "title": title[:MAX_RISK_ITEM_TITLE_LENGTH],
                    "risk_level": level,
                    "reason": item.reason.strip()[:MAX_RISK_ITEM_REASON_LENGTH],
                    "quote": item.quote.strip()[:MAX_RISK_QUOTE_LENGTH],
                    "evidence_ids": evidence_ids,
                    "module": module,
                    # 规则身份：模型回填的 rule_id 不在本次适用规则清单里时视为
                    # 笔误；回填缺失时按标题逐字对账补回（模型经常只回填标题，
                    # 缺了 ID 就拿不到规则分类与检查方式，只能当规则外风险）。
                    "rule_id": _resolve_rule_id(
                        item.rule_id,
                        title,
                        known_rule_ids,
                        rule_ids_by_title,
                    ),
                    "confidence": min(max(item.confidence, 0.01), 0.99),
                }
            )
        )
    return _apply_contract_type_single_choice(
        items,
        response.contract_type,
        category_by_rule_id,
    )


def _rule_ids_by_title(
    rule_hints: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """规则标题 → rule_id，供判定项按标题对账回填规则身份。

    同标题对应多个 rule_id 时无法判定归属，整体剔除——宁可不回填，也不把
    判定挂到错的规则上。
    """

    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    for hint in rule_hints:
        if not isinstance(hint, Mapping):
            continue
        rule_id = hint.get("rule_id")
        key = _normalize_rule_title(hint.get("title"))
        if not rule_id or not key:
            continue
        if key in mapping and mapping[key] != str(rule_id):
            ambiguous.add(key)
            continue
        mapping[key] = str(rule_id)
    for key in ambiguous:
        mapping.pop(key, None)
    return mapping


def _normalize_rule_title(value: object) -> str:
    """归一标题写法：去掉空白与常见包裹符号，用于逐字对账。"""

    if value is None:
        return ""
    text = re.sub(r"[\s\u3000]+", "", str(value))
    return text.strip("【】[]{}()（）<>《》\"'“”‘’")


def _resolve_rule_id(
    raw_rule_id: object,
    title: str,
    known_rule_ids: set[str],
    rule_ids_by_title: Mapping[str, str],
) -> str | None:
    """恢复条目的规则身份：先认模型回填的 rule_id，再按标题逐字对账。"""

    if isinstance(raw_rule_id, str) and raw_rule_id in known_rule_ids:
        return raw_rule_id
    return rule_ids_by_title.get(_normalize_rule_title(title))


def _apply_contract_type_single_choice(
    items: Sequence[RiskAnalysisItem],
    contract_type: Mapping[str, Any] | None,
    category_by_rule_id: Mapping[str, str],
) -> list[RiskAnalysisItem]:
    """按单选语义归一合同类型候选：未被选中的候选类型改判"不适用"。

    5 个候选合同类型是单选题，规则引擎的 classification 判据同口径（类型已
    确定且不匹配 → NOT_APPLICABLE）。模型逐项打分时会把落选项判成 WARN，
    说不清的那项判成 UNKNOWN，页面上就冒出「混合合同 = 待确认」这类假结论：
    类型明明已经定了，却还在等人工确认。

    这里只做归一、不改判：选中的类型保持模型给的等级，其余候选类型统一记
    NOT_APPLICABLE，并把模型原话保留在 reason 里。选定类型取自模型自己声明
    的 ``contract_type.name``；没声明时退回"分类组里唯一被判 PASS 的那项"。
    两者都定不出来时原样返回（不猜）。
    """

    classification_indexes = [
        index
        for index, item in enumerate(items)
        if category_by_rule_id.get(item.rule_id or "") == CONTRACT_TYPE_CATEGORY
    ]
    if not classification_indexes:
        return list(items)
    selected = _selected_contract_type(
        items, classification_indexes, contract_type
    )
    if selected is None:
        return list(items)
    normalized: list[RiskAnalysisItem] = []
    for index, item in enumerate(items):
        if (
            index not in classification_indexes
            or _normalize_rule_title(item.title) == selected
        ):
            normalized.append(item)
            continue
        reason = (
            f"本合同类型判定为「{selected}」，本候选类型不适用。{item.reason}"
        )
        normalized.append(
            item.model_copy(
                update={
                    "risk_level": "NOT_APPLICABLE",
                    "reason": reason[:MAX_RISK_ITEM_REASON_LENGTH],
                }
            )
        )
    return normalized


def _selected_contract_type(
    items: Sequence[RiskAnalysisItem],
    classification_indexes: Sequence[int],
    contract_type: Mapping[str, Any] | None,
) -> str | None:
    """定出本次选中的合同类型：优先模型声明的名称，其次分类组里唯一的 PASS。"""

    if isinstance(contract_type, Mapping):
        name = contract_type.get("name")
        if isinstance(name, str) and name.strip() in CONTRACT_TYPE_OPTIONS:
            return _normalize_rule_title(name)
    options = {_normalize_rule_title(option) for option in CONTRACT_TYPE_OPTIONS}
    passed = [
        _normalize_rule_title(items[index].title)
        for index in classification_indexes
        if str(items[index].risk_level).upper() == "PASS"
    ]
    if len(passed) == 1 and passed[0] in options:
        return passed[0]
    return None


def apply_contract_type_single_choice(
    items: Sequence[RiskAnalysisItem],
    contract_type: Mapping[str, Any] | None,
    rule_hints: Sequence[Mapping[str, Any]],
) -> list[RiskAnalysisItem]:
    """合并多批判定后统一做一次合同类型单选归一（分片调用专用）。

    分片调用把规则清单切成多批，"合同类型"那一组候选项一旦被切散，逐批归一
    各自只看得到半组，选不出"一选四不适用"。合并全部批次后再归一一次，与
    单次调用的结果同口径。``rule_hints`` 传本次实际发出的全部规则提示。
    """

    category_by_rule_id = {
        str(hint["rule_id"]): str(hint.get("category") or "")
        for hint in rule_hints
        if isinstance(hint, Mapping) and hint.get("rule_id")
    }
    return _apply_contract_type_single_choice(
        items, contract_type, category_by_rule_id
    )


def build_risk_analysis_response_fingerprint(response: RiskAnalysisResponse) -> str:
    """响应内容指纹：审计用它确认存档响应未被篡改。"""

    payload = {
        "response_id": response.response_id,
        "provider": response.provider,
        "model_version": response.model_version,
        "prompt_version": response.prompt_version,
        "request_fingerprint": response.request_fingerprint,
        "items": [item.model_dump(mode="json") for item in response.items],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# v1 的四栏面板（RULE_MODULES）：按规则分类映射到检查视角。
_CATEGORY_MODULE_MAP: dict[str, str] = {
    "合同主体": "资信",
    "金额": "合理性",
    "付款": "合理性",
    "发票": "合理性",
    "合规性·交付": "合理性",
}
DEFAULT_MODULE = "风险点"
RISK_PANEL_NAMES = ("风险点", "合理性", "内控", "资信")
# v1 的三分类映射（_verdict_of）：PASS/NOT_APPLICABLE→符合；BLOCK/WARN→不符；其余→待确认。
_VERDICT_PASS = {"PASS", "NOT_APPLICABLE"}
_VERDICT_FAIL = {"BLOCK", "WARN"}


def rule_module_of(rule: Rule) -> str:
    """按规则分类映射四栏面板，未登记的分类归入默认的"风险点"。"""

    for prefix, module in _CATEGORY_MODULE_MAP.items():
        if rule.category.startswith(prefix):
            return module
    return DEFAULT_MODULE


def verdict_of(finding) -> str:
    """把 finding 的状态映射成 v1 的三分类结论。"""

    level = str(finding.risk_level or finding.status)
    if level in _VERDICT_PASS or str(finding.status) in _VERDICT_PASS:
        return "符合"
    if level in _VERDICT_FAIL or str(finding.status) in _VERDICT_FAIL:
        return "不符"
    return "待确认"


def project_risk_panels(result: ReviewResult) -> dict[str, Any]:
    """把审查结果投影成 v1 的四栏视图（纯只读投影，不产生新结论）。

    每栏条目来自两个判据：规则/语义判据的 findings（带 rule_id，映射三分类
    结论）与通读风险分析的 items（module 由模型声明）。运行配置里的
    ``rule_id → 分类`` 映射取自规则包本身，因此投影可离线复算。
    """

    rules_by_id = {rule.rule_id: rule for rule in result.rule_bundle.rules}
    panels: dict[str, list[dict[str, Any]]] = {name: [] for name in RISK_PANEL_NAMES}

    for finding in result.findings:
        rule = rules_by_id.get(finding.rule_id)
        module = rule_module_of(rule) if rule is not None else DEFAULT_MODULE
        panels.setdefault(module, []).append(
            {
                "source": "rule",
                "title": finding.title,
                "verdict": verdict_of(finding),
                "risk_level": str(finding.risk_level or finding.status),
                "reason": finding.reason,
                "recommended_action": finding.recommended_action,
                "rule_id": finding.rule_id,
                "finding_id": finding.finding_id,
            }
        )

    analysis = result.risk_analysis_response
    if analysis is not None:
        for item in analysis.items:
            if item.risk_level in _VERDICT_PASS:
                verdict = "符合"
            elif item.risk_level in _VERDICT_FAIL:
                verdict = "不符"
            else:
                verdict = "待确认"
            panels.setdefault(item.module, []).append(
                {
                    "source": "ai",
                    "title": item.title,
                    "verdict": verdict,
                    "risk_level": item.risk_level,
                    "reason": item.reason,
                    "quote": item.quote,
                    "evidence_ids": list(item.evidence_ids),
                    "confidence": item.confidence,
                }
            )

    verdicts = {"符合": 0, "不符": 0, "待确认": 0}
    for entries in panels.values():
        for entry in entries:
            verdicts[entry["verdict"]] += 1
    return {
        "panels": panels,
        "verdicts": verdicts,
        "risk_analysis": {
            "status": (result.run.configuration.get("risk_analysis") or {}).get("status"),
            "item_count": len(analysis.items) if analysis is not None else 0,
        },
    }
