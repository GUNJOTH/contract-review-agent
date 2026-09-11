"""AI 风险分析：模型通读合同全文输出风险清单，并融合规则层的 BLOCK/WARN 发现。

主输出是 AI 分析：模型通读合同全文，全面识别风险点（期限矛盾、金额/付款、
发票税率、验收、知识产权、保密、违约、空白字段等）；规则检查器发现的
BLOCK/WARN 作为参考提示一并提供给模型核实，同时以 ``source=rule`` 的形式
融合进最终清单，保证规则层结论不丢失。每个风险项尽量绑定正文证据。
"""

from __future__ import annotations

import json
import uuid

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from contract_review.models import EvidenceType, ReviewResult

from contract_review_app.config import settings
from contract_review_app.services.result_cache import cache_get, cache_set, fingerprint
from contract_review_app.services.rule_evolution import (
    RULE_MODULES,
    auto_disable_stale,
    bump_unadopted_misses,
    load_active_rules,
    record_hits,
    resolve_module,
    retrieve_relevant_rules,
    save_candidate_rules,
)
from contract_review_app.services.semantic_client import RelaySemanticReviewer
from contract_review_app.services.vector_knowledge_index import _cosine, embed_texts

AI_ANALYSIS_VERSION = "ai-analysis-0.3.0"

# AI 项与规则项的融合阈值：标题+理由的向量相似度达到该值视为同一风险
MERGE_SIMILARITY_THRESHOLD = 0.55

AI_ANALYSIS_SYSTEM_INSTRUCTION = (
    "你是资深合同审查专家。请先判断本合同属于哪个合同类型（可选值："
    "软件产品销售 / 软件开发/转让服务 / 一般商品销售合同 / 混合合同 / 其它服务合同；"
    "都不完全符合时选择最接近的并说明依据），再通读合同全文，按四栏输出审查结果，不要遗漏。"
    "你可以通过 search_rules 工具按需查询规则知识库（不要把整库当提示词）："
    "规则按四栏归类：风险点 / 合理性 / 内控 / 资信，既含内置规则也含用户自定义规则。"
    "用户消息中的 rule_hints 仅供核实参考：确有依据的纳入分析，没有依据的不要采纳。"
    "只报告确有合同原文依据的内容，没有把握的不要编造。"
    "完成工具查询后，请只输出一个 JSON 对象，不要输出任何其他文字："
    '{"contract_type": {"name": "<合同类型>", "basis": "<判定依据的合同原文>"}, '
    '"items": [{"title": "<标题>", "risk_level": "BLOCK|WARN|INFO", '
    '"module": "风险点|合理性|内控|资信", '
    '"category": "<分组，如财务风险/项目风险/需要明确的点/需要关注点/风险点与陷阱/客户资信风险/经营风险>", '
    '"metric": "<指标名，如利润率/资金要求/项目预算/注册资本>", '
    '"value": "<指标值或结论短句>", '
    '"reason": "<说明>", "quote": "<合同原文>", '
    '"suggested_action": "<建议>", "section": "<条款编号，如 7.1>"}], '
    '"rule_suggestions": [{"title": "<可复用检查点名称>", '
    '"condition": "<检查内容/判定条件>", "risk_level": "BLOCK|WARN|INFO", '
    '"module": "风险点|合理性|内控|资信", '
    '"topic": "期限履约|金额税务|付款结算|验收交付|知识产权|附件资料|违约责任|其他检查"}]}。'
    "四栏要求："
    "1) 内控：对照企业合规阈值和用户自定义规则，拦截缺失或不合规条款。"
    "每条必须给 title、quote（合同原文）、suggested_action（插入调整建议）、section（能定位则给条款号）。"
    "例如质保期、检验期限、发票类型、强制性标准、合法性。"
    "2) 合理性：对照公司规章制度做采购合理性提醒，不要写成内控缺条款清单。"
    "按 category 分成三类：需要明确的点、需要关注点、风险点与陷阱。"
    "需要明确的点：合同要采购/建设的内容是否完整、范围和目标是否写清；"
    "需要关注点：信创、国产化、云架构、数据治理、实施周期等制度关注项，说明合同怎么写、是否合理；"
    "风险点与陷阱：需求不清、范围膨胀、验收标准缺失、知识产权或数据权属不清等潜在陷阱。"
    "合理性条目用 reason 写清制度提醒，quote 引用合同原句，suggested_action 给修订建议。"
    "3) 风险点：经营风险指标卡。财务风险用 metric=利润率/资金要求，给出 value 和 reason；"
    "项目风险用 metric=项目预算/收款进度，给出 value 和 reason。能从合同算出或判断的才给数值，算不出就写“未约定/无法判断”并引用原文。"
    "4) 资信：对方资信指标卡。category 用客户资信风险或经营风险；"
    "metric 用注册资本、合作历史、当前合同、经营状况、诉讼风险等，value 给短结论，reason 说明依据。"
    "没有征信数据时，只能依据合同文本判断，不要编造征信分数。"
    "risk_level 含义：BLOCK=重大风险，WARN=需关注，INFO=提示。"
    "items 最多输出 50 条。最后提炼 3-8 条可复用检查项（rule_suggestions）。"
)

SEARCH_RULES_TOOL = {
    "type": "function",
    "function": {
        "name": "search_rules",
        "description": (
            "从合同审查规则知识库检索与当前检查点相关的规则。"
            "按检查面分别查询，例如：付款比例、税率发票、验收程序、"
            "知识产权归属、履行期限、附件完整性。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词，用检查面或风险点的关键词，不要一次塞整份合同",
                },
                "contract_type": {
                    "type": "string",
                    "description": "已判定的合同类型；未判定可省略",
                },
                "module": {
                    "type": "string",
                    "description": "审查栏：风险点 / 合理性 / 内控 / 资信；省略则四栏一起检索",
                },
            },
            "required": ["query"],
        },
    },
}

MAX_TOOL_ROUNDS = 6

MAX_DOCUMENT_CHARS = 15000
MAX_QUOTE_CHARS = 100

# 引擎在合同类型未定时对规则适用性打的 UNKNOWN 标志
APPLICABILITY_UNKNOWN_MARKER = "未能从来源规则中确定本规则的适用性"


class AIRiskItem(BaseModel):
    risk_id: str
    title: str = Field(min_length=1)
    risk_level: str = Field(
        pattern="^(BLOCK|WARN|INFO|UNKNOWN|PASS|NOT_APPLICABLE)$"
    )
    reason: str = Field(min_length=1)
    quote: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    suggested_action: str | None = None
    source: str = Field(default="ai", pattern="^(ai|rule|merged)$")
    module: str = Field(default="内控")
    category: str | None = None
    metric: str | None = None
    value: str | None = None
    section: str | None = None


class AIAnalysisResult(BaseModel):
    analysis_id: str
    provider: str
    model_version: str
    prompt_version: str
    contract_type: dict | None = None
    items: list[AIRiskItem] = Field(default_factory=list)
    panels: dict[str, list[AIRiskItem]] = Field(default_factory=dict)


# 规则包内定义的合同类型（分类规则的 title）
CONTRACT_TYPE_NAMES = {
    "软件产品销售",
    "软件开发/转让服务",
    "一般商品销售合同",
    "混合合同",
    "其它服务合同",
}


def run_ai_analysis(result: ReviewResult) -> AIAnalysisResult | None:
    """对合同全文做 AI 风险分析（两轮：通读 + 未覆盖规则点复核）。

    第一轮：模型先判定合同类型，再通读全文，规则层 BLOCK/WARN 作为提示核实纳入；
    第二轮：仍有未覆盖的规则点时，让模型重新核实并补充输出。
    清单 = AI 最终结论 + 规则层 UNKNOWN 兜底（PASS/NOT_APPLICABLE 不展示；
    AI 判定出合同类型后，分类规则的"适用性未定"UNKNOWN 由类型判定项替代）。
    模型完全不可用时才用规则层 BLOCK/WARN 应急兜底。
    """
    if (
        not settings.CONTRACT_REVIEW_ENDPOINT
        or not settings.CONTRACT_AI_ANALYSIS_ENABLED
    ):
        return None
    documents = _collect_document_text(result)
    if not documents:
        return None
    # 结果按指纹缓存：同一审查结果 + 同一提示词/模型/规则库状态 → 直接返回
    cache_key = _analysis_fingerprint(result)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return AIAnalysisResult.model_validate_json(cached["analysis"])
        except Exception as exc:
            logger.warning(f"AI 分析缓存读取失败，重新分析: {exc}")
    chunks = list(result.knowledge_chunks)
    engine_hints = _rule_risk_hints(result)
    payload: dict = {
        "documents": documents,
        "rule_hints": engine_hints,
    }
    if settings.CONTRACT_ENGINE_RULES_ENABLED:
        payload["reference_rules"] = [rule.title for rule in result.rule_bundle.rules]

    ai_items, contract_type, suggestions, retrieved_ai_rules = _call_ai_analysis(
        payload, chunks
    )

    rule_items = _rule_finding_items(result)
    if ai_items is not None:
        # 第一轮成功：提炼的候选规则沉淀进自进化规则库（draft，待人工确认）
        analysis_id = f"analysis-{uuid.uuid4().hex[:16]}"
        try:
            saved = save_candidate_rules(
                suggestions or [],
                analysis_id=analysis_id,
                contract_type=(
                    contract_type.get("name") if contract_type else None
                ),
            )
            if saved:
                logger.info(f"AI 自进化规则：本次提炼 {saved} 条候选规则入库（draft）")
        except Exception as exc:
            logger.warning(f"AI 规则入库失败（不影响本次分析）: {exc}")
        ai_type = contract_type.get("name") if contract_type else None
        retrieved_hints = _ai_rule_hints(retrieved_ai_rules or [])
        coverage_hints = [*engine_hints, *retrieved_hints]
        # 命中统计只针对本轮工具查询到的规则
        try:
            retrieved_ids = [rule["id"] for rule in (retrieved_ai_rules or []) if rule.get("id")]
            uncovered_ids = {
                hint["rule_id"]
                for hint in _uncovered_hints(retrieved_hints, ai_items)
                if hint.get("rule_id")
            }
            adopted = [rid for rid in retrieved_ids if rid not in uncovered_ids]
            if adopted:
                hits = record_hits(adopted)
                if hits:
                    logger.info(f"AI 自进化规则：本次 {hits} 条规则命中（hit_count +1）")
            bump_unadopted_misses(
                adopted, ai_type, retrieved_ids=retrieved_ids
            )
            stale = auto_disable_stale(
                settings.CONTRACT_AI_RULE_EVOLVE_STALE_REVIEWS
            )
            if stale:
                logger.info(
                    f"AI 自进化规则：{stale} 条连续未被采纳的规则自动停用"
                )
        except Exception as orig_exc:
            logger.warning(f"AI 规则命中统计失败（不影响本次分析）: {orig_exc}")
        # 未覆盖的规则点交由模型第二轮重新核实，不再兜底展示
        applicability_unknown = [
            item
            for item in rule_items
            if item.risk_level == "UNKNOWN"
            and APPLICABILITY_UNKNOWN_MARKER in (item.reason or "")
        ]
        pending = _uncovered_hints(coverage_hints, ai_items)
        pending.extend(
            {
                "title": item.title,
                "risk_level": item.risk_level,
                "reason": item.reason,
            }
            for item in applicability_unknown
        )
        followup_resolved = False
        if pending:
            followup_payload: dict = {
                "documents": documents,
                "uncovered_hints": pending,
            }
            if ai_type:
                followup_payload["contract_type"] = ai_type
            extra, _, _, _ = _call_ai_analysis(
                followup_payload, chunks, followup=True
            )
            followup_resolved = extra is not None
            if extra:
                ai_items = _append_unique(ai_items, extra)
        final_items = [*ai_items]
        if ai_type:
            # AI 已判定合同类型：分类规则的"适用性未定"UNKNOWN 由判定项替代
            final_items.append(_contract_type_item(contract_type, chunks))
            rule_items = [
                item
                for item in rule_items
                if not (
                    item.risk_level == "UNKNOWN"
                    and item.title in CONTRACT_TYPE_NAMES
                )
            ]
        final_items.extend(
            item
            for item in rule_items
            if item.risk_level not in {"BLOCK", "WARN", "NOT_APPLICABLE"}
            and not (
                ai_type is not None
                and followup_resolved
                and item in applicability_unknown
            )
        )
    else:
        # 模型不可用：规则层 BLOCK/WARN/UNKNOWN 应急兜底（PASS/NA 不展示）
        final_items = [
            item
            for item in rule_items
            if item.risk_level != "NOT_APPLICABLE"
        ]
        analysis_id = f"analysis-{uuid.uuid4().hex[:16]}"

    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    for item in final_items:
        if not item.quote and item.evidence_ids:
            quote, _ = _quote_from_evidence(item.evidence_ids, evidence_by_id)
            item.quote = quote
    # 以 AI 分析为主：按严重程度排序（BLOCK > WARN > INFO > UNKNOWN > NOT_APPLICABLE）
    final_items.sort(key=lambda item: -_SEVERITY.get(item.risk_level, 0))
    analysis = AIAnalysisResult(
        analysis_id=analysis_id,
        provider=settings.CONTRACT_REVIEW_PROVIDER,
        model_version=settings.CONTRACT_REVIEW_MODEL,
        prompt_version=settings.CONTRACT_AI_ANALYSIS_PROMPT_VERSION,
        contract_type=contract_type,
        items=final_items,
        panels=_group_items_by_module(final_items),
    )
    cache_set(cache_key, {"analysis": analysis.model_dump_json()})
    return analysis


def _analysis_fingerprint(result: ReviewResult) -> str:
    """AI 分析指纹：引擎结果指纹 + 提示词/模型版本 + AI 规则库身份（不含命中计数）。"""
    ai_rules = load_active_rules()
    identity = [
        {
            "id": rule["id"],
            "code": rule.get("code") or "",
            "title": rule["title"],
            "condition": rule.get("condition") or "",
            "contract_type": rule.get("contract_type"),
            "risk_level": rule["risk_level"],
            "module": rule.get("module") or "",
            "enabled": bool(rule.get("enabled", True)),
        }
        for rule in ai_rules
    ]
    return fingerprint(
        [
            result.run.result_fingerprint,
            settings.CONTRACT_AI_ANALYSIS_PROMPT_VERSION,
            settings.CONTRACT_REVIEW_MODEL,
            str(settings.CONTRACT_AI_RULE_RETRIEVAL_TOP_K),
            str(settings.CONTRACT_ENGINE_RULES_ENABLED),
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
        ]
    )


def _contract_type_item(
    contract_type: dict, chunks
) -> AIRiskItem:
    """AI 判定的合同类型转成清单项（INFO，带判定依据）。"""
    name = str(contract_type.get("name") or "").strip()
    basis = str(contract_type.get("basis") or "").strip() or None
    return AIRiskItem(
        risk_id="ai-contract-type",
        title=f"合同类型判定：{name}",
        risk_level="INFO",
        reason=f"AI 依据合同原文判定本合同类型为“{name}”，分类规则按该类型适用。",
        quote=basis,
        evidence_ids=_bind_evidence(basis, chunks),
        suggested_action=None,
        source="ai",
        module="风险点",
    )


AI_ANALYSIS_FOLLOWUP_INSTRUCTION = (
    "你是资深合同审查专家。以下是规则检查器提出、你上一轮分析未覆盖的风险点，"
    "请逐一重新核实合同原文：确有依据的，必须在 items 中补充输出"
    "（结构与之前相同：title / risk_level / module / category / metric / value / "
    "reason / quote / suggested_action / section）；"
    "确实没有依据的，不要输出。不得遗漏任何确有依据的风险点。"
    "补充项也必须按四栏归类：内控给条款原文和建议；合理性按需要明确的点/需要关注点/风险点与陷阱；"
    "风险点给财务或项目指标；资信给对方经营/合作结论。"
    "如果用户消息中包含 contract_type（合同类型已由 AI 判定为该类型）："
    "对其中标注适用性未定的规则点，按该合同类型判断其是否适用——"
    "该类型不适用的不要输出；适用且存在风险的必须输出。"
    '只输出一个 JSON 对象：{"items": [...]}，不要输出任何其他文字。'
)


def _post(*args, **kwargs):
    """模型调用入口（独立函数便于测试注入 fake）。"""
    return httpx.post(*args, **kwargs)

# 规则点覆盖判定阈值：AI 项与规则提示的相似度达到该值视为已覆盖
COVERAGE_SIMILARITY_THRESHOLD = 0.5


def _call_ai_analysis(
    payload: dict,
    chunks,
    *,
    followup: bool = False,
) -> tuple[list[AIRiskItem] | None, dict | None, list[dict], list[dict]]:
    """调用 AI 风险分析（可多轮 tool calling 查规则库）。

    返回 (items, contract_type, suggestions, retrieved_rules)；
    调用失败返回 (None, None, [], [])。
    """
    messages = [
        {
            "role": "system",
            "content": (
                AI_ANALYSIS_FOLLOWUP_INSTRUCTION
                if followup
                else AI_ANALYSIS_SYSTEM_INSTRUCTION
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    headers = {"Content-Type": "application/json"}
    if settings.CONTRACT_REVIEW_API_KEY:
        headers["Authorization"] = f"Bearer {settings.CONTRACT_REVIEW_API_KEY}"
    retrieved_by_id: dict[str, dict] = {}
    try:
        for _round in range(MAX_TOOL_ROUNDS + 1):
            body: dict = {
                "model": settings.CONTRACT_REVIEW_MODEL,
                "temperature": 0,
                "messages": messages,
            }
            if not followup:
                body["tools"] = [SEARCH_RULES_TOOL]
            response = _post(
                settings.CONTRACT_REVIEW_ENDPOINT,
                json=body,
                headers=headers,
                timeout=settings.CONTRACT_AI_ANALYSIS_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if tool_calls and not followup:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    result_text, rules = _execute_search_rules(call)
                    for rule in rules:
                        if rule.get("id"):
                            retrieved_by_id[rule["id"]] = rule
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(
                                (call.get("id") if isinstance(call, dict) else "")
                                or ""
                            ),
                            "content": result_text,
                        }
                    )
                continue
            content = message.get("content") or ""
            parsed_content = RelaySemanticReviewer._parse_content(content)
            raw_items = parsed_content["items"]
            items: list[AIRiskItem] = []
            for index, raw in enumerate(raw_items):
                item = _build_ai_item(raw, index, chunks)
                if item is not None:
                    items.append(item)
            contract_type = None
            if not followup and isinstance(parsed_content.get("contract_type"), dict):
                name = str(parsed_content["contract_type"].get("name") or "").strip()
                if name:
                    contract_type = {
                        "name": name,
                        "basis": str(
                            parsed_content["contract_type"].get("basis") or ""
                        ).strip(),
                    }
            suggestions: list[dict] = []
            if not followup:
                raw_suggestions = parsed_content.get("rule_suggestions") or []
                if isinstance(raw_suggestions, list):
                    suggestions = [
                        {
                            "title": str(raw.get("title") or "").strip(),
                            "condition": str(raw.get("condition") or "").strip(),
                            "risk_level": str(raw.get("risk_level") or "WARN").upper(),
                            "topic": str(raw.get("topic") or "").strip(),
                            "module": str(raw.get("module") or "").strip(),
                        }
                        for raw in raw_suggestions
                        if isinstance(raw, dict) and str(raw.get("title") or "").strip()
                    ]
            return items, contract_type, suggestions, list(retrieved_by_id.values())
        logger.warning("AI 风险分析超过工具调用轮次上限，按失败处理")
        return None, None, [], []
    except Exception as orig_exc:
        logger.warning(f"AI 风险分析调用失败: {orig_exc}")
        return None, None, [], []


def _execute_search_rules(call: object) -> tuple[str, list[dict]]:
    """执行 search_rules 工具调用，返回给模型的 JSON 文本和检索到的规则。"""
    if not isinstance(call, dict):
        return json.dumps({"rules": [], "error": "invalid tool call"}, ensure_ascii=False), []
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    if name != "search_rules":
        return json.dumps({"rules": [], "error": f"unknown tool {name}"}, ensure_ascii=False), []
    raw_args = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (TypeError, ValueError):
        arguments = {}
    query = str(arguments.get("query") or "").strip()
    contract_type = str(arguments.get("contract_type") or "").strip() or None
    module = str(arguments.get("module") or "").strip() or None
    if not query:
        return json.dumps({"rules": [], "error": "query required"}, ensure_ascii=False), []
    per_call = min(8, settings.CONTRACT_AI_RULE_RETRIEVAL_TOP_K)
    rules = retrieve_relevant_rules(
        query, contract_type=contract_type, module=module, top_k=per_call
    )
    slim = [
        {
            "id": rule["id"],
            "code": rule.get("code") or "",
            "title": rule["title"],
            "condition": rule.get("condition") or "",
            "risk_level": rule["risk_level"],
            "module": rule.get("module") or "内控",
            "contract_type": rule.get("contract_type"),
        }
        for rule in rules
    ]
    return json.dumps({"rules": slim}, ensure_ascii=False), rules


def _uncovered_hints(
    hints: list[dict[str, str]], ai_items: list[AIRiskItem]
) -> list[dict[str, str]]:
    """找出 AI 输出未覆盖的规则提示；相似度不可用时视为全部未覆盖。"""
    if not hints or not ai_items:
        return hints
    texts = [
        f"{hint['title']}。{hint['reason'][:120]}" for hint in hints
    ] + [f"{item.title}。{item.reason[:120]}" for item in ai_items]
    try:
        vectors = embed_texts(texts)
        hint_vectors = vectors[: len(hints)]
        ai_vectors = vectors[len(hints):]
    except Exception as exc:
        logger.debug(f"覆盖判定不可用，视为全部未覆盖: {exc}")
        return hints
    uncovered: list[dict[str, str]] = []
    for index, hint in enumerate(hints):
        scores = [_cosine(hint_vectors[index], vector) for vector in ai_vectors]
        if max(scores) < COVERAGE_SIMILARITY_THRESHOLD:
            uncovered.append(hint)
    return uncovered


def _append_unique(
    existing: list[AIRiskItem], extra: list[AIRiskItem]
) -> list[AIRiskItem]:
    """追加补充项，与已有项相似度达标（重复）的跳过。"""
    texts = [
        f"{item.title}。{item.reason[:120]}" for item in [*existing, *extra]
    ]
    base_vectors: list[list[float]] | None = None
    extra_vectors: list[list[float]] | None = None
    try:
        vectors = embed_texts(texts)
        base_vectors = vectors[: len(existing)]
        extra_vectors = vectors[len(existing):]
    except Exception as exc:
        logger.debug(f"去重判定不可用，直接追加: {exc}")
    result = list(existing)
    for index, item in enumerate(extra):
        if base_vectors is not None:
            scores = [
                _cosine(extra_vectors[index], vector)
                for vector in base_vectors
            ]
            if scores and max(scores) >= MERGE_SIMILARITY_THRESHOLD:
                continue  # 与已有项重复
        result.append(item)
    return result


_SEVERITY = {
    "BLOCK": 5,
    "WARN": 4,
    "INFO": 3,
    "UNKNOWN": 2,
    "PASS": 1,
    "NOT_APPLICABLE": 0,
}


def _group_items_by_module(items: list[AIRiskItem]) -> dict[str, list[AIRiskItem]]:
    """把风险项按审查四栏切开，缺栏也返回空列表方便前端固定 Tab。"""
    grouped = {name: [] for name in RULE_MODULES}
    for item in items:
        module = item.module if item.module in grouped else "内控"
        grouped[module].append(item)
    return grouped


def _build_ai_item(raw: object, index: int, chunks) -> AIRiskItem | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if not title or not reason:
        return None
    risk_level = str(raw.get("risk_level") or "INFO").upper()
    if risk_level not in {"BLOCK", "WARN", "INFO"}:
        risk_level = "INFO"
    quote = str(raw.get("quote") or "").strip() or None
    module = resolve_module(
        raw.get("module"), title, reason, str(raw.get("topic") or "")
    )
    category = str(raw.get("category") or "").strip() or None
    if module == "合理性":
        category = _normalize_reasonableness_category(category, title, reason)
    if module == "风险点" and not category:
        category = _infer_risk_category(title, reason, str(raw.get("metric") or ""))
    if module == "资信" and not category:
        category = "客户资信风险"
    return AIRiskItem(
        risk_id=f"ai-risk-{index + 1}",
        title=title,
        risk_level=risk_level,
        reason=reason,
        quote=quote,
        evidence_ids=_bind_evidence(quote, chunks),
        suggested_action=(
            str(raw.get("suggested_action") or "").strip() or None
        ),
        source="ai",
        module=module,
        category=category,
        metric=str(raw.get("metric") or "").strip() or None,
        value=str(raw.get("value") or "").strip() or None,
        section=str(raw.get("section") or "").strip() or None,
    )


REASONABLENESS_CATEGORIES = ("需要明确的点", "需要关注点", "风险点与陷阱")
_REASONABLENESS_ALIASES = {
    "采购需求": "需要明确的点",
    "需求点": "需要明确的点",
    "需要明确": "需要明确的点",
    "关注点": "需要关注点",
    "需要关注": "需要关注点",
    "风险陷阱": "风险点与陷阱",
    "陷阱": "风险点与陷阱",
}


def _normalize_reasonableness_category(
    category: str | None, title: str, reason: str
) -> str:
    raw = (category or "").strip()
    if raw in REASONABLENESS_CATEGORIES:
        return raw
    for key, mapped in _REASONABLENESS_ALIASES.items():
        if key in raw:
            return mapped
    return _infer_reasonableness_category(title, reason)


def _infer_reasonableness_category(title: str, reason: str) -> str:
    text = f"{title} {reason}"
    if any(key in text for key in ("陷阱", "膨胀", "不清", "权属", "验收标准缺失")):
        return "风险点与陷阱"
    if any(key in text for key in ("信创", "国产", "云架构", "数据治理", "周期", "关注")):
        return "需要关注点"
    return "需要明确的点"


def _infer_risk_category(title: str, reason: str, metric: str) -> str:
    text = f"{title} {reason} {metric}"
    if any(key in text for key in ("预算", "收款", "项目")):
        return "项目风险"
    return "财务风险"


def _ai_rule_hints(rules: list[dict]) -> list[dict[str, str]]:
    """把检索到的 AI 规则转成提示词里的 rule_hints。"""
    return [
        {
            "title": rule["title"],
            "risk_level": rule["risk_level"],
            "reason": rule.get("condition") or rule["title"],
            "source": "ai-rule",
            "rule_id": rule["id"],
            "module": rule.get("module") or "内控",
        }
        for rule in rules
        if rule.get("id") and rule.get("title")
    ]


def _rule_risk_hints(result: ReviewResult) -> list[dict[str, str]]:
    """规则检查器的 BLOCK/WARN 发现，作为 AI 核实的参考提示。"""
    hints: list[dict[str, str]] = []
    rule_titles = {rule.rule_id: rule.title for rule in result.rule_bundle.rules}
    for finding in result.findings:
        if finding.status.value not in {"BLOCK", "WARN"}:
            continue
        hints.append(
            {
                "title": rule_titles.get(finding.rule_id, finding.rule_id),
                "risk_level": finding.status.value,
                "reason": finding.reason,
            }
        )
    return hints


def _rule_finding_items(result: ReviewResult) -> list[AIRiskItem]:
    """规则层结论转成风险项（source=rule）；PASS 不展示，其余并入清单兜底。"""
    rule_titles = {rule.rule_id: rule.title for rule in result.rule_bundle.rules}
    evidence_by_id = {item.evidence_id: item for item in result.evidence}
    items: list[AIRiskItem] = []
    for index, finding in enumerate(result.findings):
        if finding.status.value == "PASS":
            continue
        quote, bound_evidence = _quote_from_evidence(finding.evidence_ids, evidence_by_id)
        title = rule_titles.get(finding.rule_id, finding.rule_id)
        items.append(
            AIRiskItem(
                risk_id=f"rule-risk-{index + 1}",
                title=title,
                risk_level=finding.status.value,
                reason=finding.reason,
                quote=quote,
                evidence_ids=bound_evidence,
                suggested_action=finding.recommended_action,
                source="rule",
                module=resolve_module(None, title, finding.reason or ""),
            )
        )
    return items


def _quote_from_evidence(
    evidence_ids: list[str], evidence_by_id: dict[str, object]
) -> tuple[str | None, list[str]]:
    """从发现引用的证据里取原文片段作为引用。"""
    bound: list[str] = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None or evidence.evidence_type == EvidenceType.MISSING_ARTIFACT:
            continue
        excerpt = (
            getattr(evidence, "raw_excerpt", None)
            or getattr(evidence, "display_excerpt", None)
            or ""
        )
        if excerpt:
            bound = [evidence_id, *bound]
            return excerpt[:MAX_QUOTE_CHARS], bound
    return None, bound


def _collect_document_text(result: ReviewResult) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    for parsed in result.parsed_documents:
        parts = [page.normalized_text for page in parsed.pages]
        parts.extend(node.text for node in parsed.nodes)
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            continue
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS] + "\n…（超出长度截断）"
        documents.append({"filename": parsed.document.filename, "text": text})
    return documents


def _bind_evidence(quote: str | None, chunks) -> list[str]:
    """尽力把模型引用的原文片段映射回文本块证据；映射不到返回空。"""
    if not quote:
        return []
    head = quote[:24]
    for chunk in chunks:
        content = chunk.content
        if head in content or content[:24] in head:
            return list(chunk.evidence_ids)
    return []
