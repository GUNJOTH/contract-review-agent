"""合同规则引擎库：用户自定义规则 + AI 提炼检查点，供审查按模块命中。

存储使用标准库 sqlite3（零依赖），默认 ``data/ai_rules.db``。
规则状态流转：draft（AI 提案，待人工确认）→ active（进入审查提示池）→ disabled。
规则按合同审查四栏归类：风险点 / 合理性 / 内控 / 资信。
入库前去重：标题完全相同或向量相似度达到阈值的不重复入库。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from contract_review_app.config import settings
from contract_review_app.services.vector_knowledge_index import _cosine, embed_texts

# 去重阈值：候选规则与现有规则"标题+条件"的向量相似度达到该值视为重复
RULE_DEDUPE_THRESHOLD = 0.6

RULE_MODULES = ("风险点", "合理性", "内控", "资信")
RULE_STATUSES = ("draft", "active", "disabled")
RULE_RISK_LEVELS = ("BLOCK", "WARN", "INFO")
MODULE_CODE_PREFIX = {
    "风险点": "FX",
    "合理性": "HL",
    "内控": "NK",
    "资信": "KHSX",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    condition TEXT NOT NULL DEFAULT '',
    contract_type TEXT,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    source_analysis_id TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    topic TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

TOPIC_KEYWORDS = (
    ("期限履约", ("期限", "签订时间", "工期", "交付日期")),
    ("金额税务", ("税", "金额", "不含税", "发票")),
    ("付款结算", ("付款", "支付", "总额")),
    ("验收交付", ("验收", "交付", "检验", "质保")),
    ("知识产权", ("知识产权", "成果", "源代码", "著作权", "专利")),
    ("附件资料", ("附件", "技术协议")),
    ("违约责任", ("违约",)),
)

MODULE_KEYWORDS = (
    ("资信", ("授信", "资信", "征信", "合作历史", "经营状况", "偿付", "诉讼", "社保")),
    ("风险点", ("利润", "财务风险", "资金", "预算", "项目风险", "成本估算")),
    ("合理性", ("采购指南", "需求点", "规章制度", "合理性", "制度", "微服务", "信创", "国产化", "云架构", "大数据", "数据治理")),
    ("内控", ("检验", "质保", "发票", "异议", "内控", "强制性标准", "合规", "金额", "付款", "税率", "税额", "保函", "知识产权", "源代码", "骑缝章")),
)

ENGINE_CATEGORY_MODULE = {
    "合同类型": "风险点",
    "金额": "内控",
    "付款": "内控",
    "发票": "内控",
    "源代码相关（按关键字搜索）": "内控",
    "知识产权": "内控",
    "Qx新技术架构描述相关": "合理性",
    "合同主体": "内控",
    "合规性/交付问题": "内控",
    "软件开发服务合同（0税率）重点检查项": "内控",
}

CHECK_METHOD_LABEL = {
    "classification": "合同类型分类检查",
    "deterministic": "确定性核对",
    "keyword": "按关键字检索合同原文",
    "semantic": "语义审查",
    "human": "需人工复核",
    "visual": "视觉证据检查",
}

DEMO_CREDIT_CODES = (
    "NK-202511-001",
    "NK-202511-002",
    "NK-202511-003",
    "NK-202511-004",
    "NK-202511-005",
    "KHSX-202511-001",
    "KHSX-202511-002",
    "KHSX-202511-003",
    "KHSX-202511-004",
    "KHSX-202511-005",
)

ENGINE_TOPIC_ORDER = tuple(ENGINE_CATEGORY_MODULE.keys())


def infer_topic(title: str, condition: str = "") -> str:
    """按标题/条件把规则归到检查面，便于树形存储与展示。"""
    text = f"{title or ''} {condition or ''}"
    for topic, keywords in TOPIC_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return topic
    return "其他检查"


def infer_module(title: str, condition: str = "", topic: str | None = None) -> str:
    """按标题/条件把规则归到审查四栏；默认为内控（用户自定义风险规则）。"""
    text = f"{title or ''} {condition or ''} {topic or ''}"
    for module, keywords in MODULE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return module
    return "内控"


def normalize_module(value: str | None) -> str:
    text = str(value or "").strip()
    if text in RULE_MODULES:
        return text
    return infer_module(text, "")


def resolve_module(
    value: str | None,
    title: str = "",
    condition: str = "",
    topic: str | None = None,
) -> str:
    text = str(value or "").strip()
    if text in RULE_MODULES:
        return text
    return infer_module(title, condition, topic)


def normalize_risk_level(value: str | None, default: str = "WARN") -> str:
    level = str(value or default).upper()
    return level if level in RULE_RISK_LEVELS else default


def normalize_status(value: str | None, default: str = "draft") -> str:
    status = str(value or default).strip().lower()
    return status if status in RULE_STATUSES else default


SCORE_BY_LEVEL = {
    "BLOCK": (
        18,
        "完全符合、约定明确",
        "部分符合或存在瑕疵",
        "缺失或不符，构成重大风险",
    ),
    "WARN": (
        12,
        "约定完整、口径一致",
        "约定不完整或口径不清",
        "未约定或明显不符",
    ),
    "INFO": (
        6,
        "信息完整可核验",
        "信息部分缺失",
        "关键信息缺失",
    ),
}

BLOCK_TITLE_KEYWORDS = (
    "大小写",
    "付款总额",
    "发票总额",
    "骑缝章",
    "合同完整性",
    "违约责任",
    "产权归属",
    "验收标准",
    "税率",
    "税额",
    "不含税",
    "标的物",
    "合法性",
)
INFO_CATEGORIES = {
    "合同类型",
    "Qx新技术架构描述相关",
    "源代码相关（按关键字搜索）",
}
GENERIC_HIGH_STANDARDS = {
    SCORE_BY_LEVEL["BLOCK"][1],
    SCORE_BY_LEVEL["WARN"][1],
    SCORE_BY_LEVEL["INFO"][1],
    "约定完整、口径一致",
    "完全符合、约定明确",
    "信息完整可核验",
}


def infer_engine_risk_level(
    title: str, category: str = "", check_method: str = ""
) -> str:
    """合同审批规则按条款重要性分级：重大 / 需关注 / 提示。"""
    text = f"{title or ''} {category or ''}"
    if any(keyword in text for keyword in BLOCK_TITLE_KEYWORDS):
        return "BLOCK"
    if category in INFO_CATEGORIES or check_method in {"classification", "keyword"}:
        return "INFO"
    if check_method == "visual":
        return "BLOCK"
    return "WARN"


def infer_score_bands(
    risk_level: str | None,
    *,
    title: str = "",
    category: str = "",
) -> dict:
    """按风险等级给出 PPT 规则引擎用的权重和高中低分标准。"""
    level = normalize_risk_level(risk_level)
    weight, high, mid, low = SCORE_BY_LEVEL.get(level, SCORE_BY_LEVEL["WARN"])
    name = title or "该检查项"
    if category == "金额":
        high, mid, low = (
            f"{name}与合同口径完全一致",
            f"{name}可核对但存在口径差异",
            f"{name}缺失、矛盾或无法核对",
        )
    elif category == "付款":
        high, mid, low = (
            f"{name}约定完整且与合同额相符",
            f"{name}有约定但比例/总额不清",
            f"{name}未约定或总额不符",
        )
    elif category == "发票":
        high, mid, low = (
            f"{name}类型、金额与合同一致",
            f"{name}部分约定不清",
            f"{name}缺失或与合同额不符",
        )
    elif category == "合同类型":
        high, mid, low = (
            "合同类型判定依据充分",
            "合同类型接近但需人工确认",
            "无法判定合同类型",
        )
    elif "源代码" in category:
        high, mid, low = (
            f"未出现不受控的「{name}」交付承诺，或已明确范围",
            f"出现「{name}」相关表述但范围不清",
            f"承诺交付「{name}」且无限制条款",
        )
    elif level == "BLOCK":
        high, mid, low = (
            f"{name}约定明确、可执行",
            f"{name}有约定但不完整",
            f"{name}缺失或约定无效，构成重大风险",
        )
    return {
        "weight": weight,
        "high_standard": high,
        "mid_standard": mid,
        "low_standard": low,
    }


def _score_fields(payload: dict, risk_level: str) -> dict:
    bands = infer_score_bands(
        risk_level,
        title=str(payload.get("title") or ""),
        category=str(payload.get("topic") or payload.get("category") or ""),
    )
    try:
        weight = int(payload.get("weight") or bands["weight"])
    except (TypeError, ValueError):
        weight = bands["weight"]
    return {
        "weight": max(1, min(weight, 100)),
        "high_standard": str(payload.get("high_standard") or "").strip()
        or bands["high_standard"],
        "mid_standard": str(payload.get("mid_standard") or "").strip()
        or bands["mid_standard"],
        "low_standard": str(payload.get("low_standard") or "").strip()
        or bands["low_standard"],
    }


def _db_path() -> Path:
    return settings.resolve_path(settings.CONTRACT_AI_RULES_DB_PATH)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute(_SCHEMA)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(rules)")]
    if "miss_streak" not in columns:
        connection.execute(
            "ALTER TABLE rules ADD COLUMN miss_streak INTEGER NOT NULL DEFAULT 0"
        )
        connection.commit()
        columns.append("miss_streak")
    if "topic" not in columns:
        connection.execute("ALTER TABLE rules ADD COLUMN topic TEXT")
        connection.commit()
        for row in connection.execute("SELECT id, title, condition FROM rules").fetchall():
            connection.execute(
                "UPDATE rules SET topic = ? WHERE id = ?",
                (infer_topic(row[1], row[2] or ""), row[0]),
            )
        connection.commit()
        columns.append("topic")
    added = False
    for name, ddl in (
        ("module", "ALTER TABLE rules ADD COLUMN module TEXT"),
        ("code", "ALTER TABLE rules ADD COLUMN code TEXT"),
        ("source", "ALTER TABLE rules ADD COLUMN source TEXT"),
        ("enabled", "ALTER TABLE rules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"),
        ("suggested_action", "ALTER TABLE rules ADD COLUMN suggested_action TEXT"),
        ("weight", "ALTER TABLE rules ADD COLUMN weight INTEGER NOT NULL DEFAULT 10"),
        ("high_standard", "ALTER TABLE rules ADD COLUMN high_standard TEXT"),
        ("mid_standard", "ALTER TABLE rules ADD COLUMN mid_standard TEXT"),
        ("low_standard", "ALTER TABLE rules ADD COLUMN low_standard TEXT"),
    ):
        if name not in columns:
            connection.execute(ddl)
            added = True
    if added:
        connection.commit()
    _backfill_module_fields(connection)
    return connection


def _backfill_module_fields(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM rules").fetchall()
    changed = False
    for row in rows:
        keys = row.keys()
        module = resolve_module(
            row["module"] if "module" in keys else None,
            row["title"],
            row["condition"] or "",
            row["topic"] if "topic" in keys else None,
        )
        code = (row["code"] if "code" in keys else None) or _next_rule_code(
            connection, module
        )
        source = (row["source"] if "source" in keys else None) or "ai"
        scores = infer_score_bands(
            row["risk_level"],
            title=row["title"],
            category=(row["topic"] if "topic" in keys else "") or "",
        )
        current_high = row["high_standard"] if "high_standard" in keys else None
        current_weight = row["weight"] if "weight" in keys else None
        if not current_high or current_high in GENERIC_HIGH_STANDARDS:
            weight, high, mid, low = (
                scores["weight"],
                scores["high_standard"],
                scores["mid_standard"],
                scores["low_standard"],
            )
        else:
            weight = current_weight or scores["weight"]
            high = current_high
            mid = (row["mid_standard"] if "mid_standard" in keys else None) or scores[
                "mid_standard"
            ]
            low = (row["low_standard"] if "low_standard" in keys else None) or scores[
                "low_standard"
            ]
        current = (
            row["module"] if "module" in keys else None,
            row["code"] if "code" in keys else None,
            row["source"] if "source" in keys else None,
            row["weight"] if "weight" in keys else None,
            row["high_standard"] if "high_standard" in keys else None,
            row["mid_standard"] if "mid_standard" in keys else None,
            row["low_standard"] if "low_standard" in keys else None,
        )
        target = (module, code, source, weight, high, mid, low)
        if current != target:
            connection.execute(
                "UPDATE rules SET module = ?, code = ?, source = ?, weight = ?,"
                " high_standard = ?, mid_standard = ?, low_standard = ? WHERE id = ?",
                (module, code, source, weight, high, mid, low, row["id"]),
            )
            changed = True
    if changed:
        connection.commit()


def _next_rule_code(
    connection: sqlite3.Connection, module: str, prefix: str | None = None
) -> str:
    prefix = prefix or "HTSP"
    stamp = datetime.now(timezone.utc).strftime("%Y%m")
    pattern = f"{prefix}-{stamp}-%"
    row = connection.execute(
        "SELECT code FROM rules WHERE code LIKE ? ORDER BY code DESC LIMIT 1",
        (pattern,),
    ).fetchone()
    serial = 1
    if row and row["code"]:
        try:
            serial = int(str(row["code"]).rsplit("-", 1)[-1]) + 1
        except ValueError:
            serial = 1
    return f"{prefix}-{stamp}-{serial:03d}"


def _row_to_rule(row: sqlite3.Row) -> dict:
    keys = row.keys()
    title = row["title"]
    condition = row["condition"] or ""
    topic = row["topic"] or infer_topic(title, condition)
    module = resolve_module(
        row["module"] if "module" in keys else None, title, condition, topic
    )
    return {
        "id": row["id"],
        "code": (row["code"] if "code" in keys else None) or "",
        "title": title,
        "condition": condition,
        "contract_type": row["contract_type"],
        "risk_level": row["risk_level"],
        "status": row["status"],
        "module": module,
        "topic": topic,
        "source": (row["source"] if "source" in keys else None) or "ai",
        "enabled": bool(row["enabled"]) if "enabled" in keys and row["enabled"] is not None else True,
        "suggested_action": (row["suggested_action"] if "suggested_action" in keys else None) or None,
        "source_analysis_id": row["source_analysis_id"] if "source_analysis_id" in keys else None,
        "hit_count": row["hit_count"],
        "miss_streak": row["miss_streak"],
        "created_at": row["created_at"] if "created_at" in keys else None,
        "updated_at": row["updated_at"] if "updated_at" in keys else None,
        "weight": int(row["weight"]) if "weight" in keys and row["weight"] is not None else infer_score_bands(row["risk_level"])["weight"],
        "high_standard": (row["high_standard"] if "high_standard" in keys else None)
        or infer_score_bands(row["risk_level"])["high_standard"],
        "mid_standard": (row["mid_standard"] if "mid_standard" in keys else None)
        or infer_score_bands(row["risk_level"])["mid_standard"],
        "low_standard": (row["low_standard"] if "low_standard" in keys else None)
        or infer_score_bands(row["risk_level"])["low_standard"],
    }


def save_candidate_rules(
    suggestions: list[dict],
    *,
    analysis_id: str,
    contract_type: str | None,
) -> int:
    """把 AI 提炼的候选规则入库（draft）；与现有规则重复的跳过。返回新增条数。"""
    if not suggestions:
        return 0
    connection = _connect()
    try:
        existing = _load_all_rules(connection)
        saved = 0
        for raw in suggestions:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            condition = str(raw.get("condition") or "").strip()
            if not title:
                continue
            risk_level = str(raw.get("risk_level") or "WARN").upper()
            if risk_level not in {"BLOCK", "WARN", "INFO"}:
                risk_level = "WARN"
            if _is_duplicate(title, condition, existing):
                continue
            now = _now_iso()
            rule_id = f"ai-rule-{uuid.uuid4().hex[:16]}"
            topic = str(raw.get("topic") or "").strip() or infer_topic(title, condition)
            module = resolve_module(raw.get("module"), title, condition, topic)
            code = str(raw.get("code") or "").strip() or _next_rule_code(
                connection, module, prefix="AI"
            )
            suggested_action = str(raw.get("suggested_action") or "").strip() or None
            scores = _score_fields(raw, risk_level)
            connection.execute(
                "INSERT INTO rules (id, title, condition, contract_type, risk_level,"
                " status, source_analysis_id, hit_count, created_at, updated_at, topic,"
                " module, code, source, enabled, suggested_action, weight,"
                " high_standard, mid_standard, low_standard)"
                " VALUES (?, ?, ?, ?, ?, 'draft', ?, 0, ?, ?, ?, ?, ?, 'ai', 1, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    title,
                    condition,
                    contract_type,
                    risk_level,
                    analysis_id,
                    now,
                    now,
                    topic,
                    module,
                    code,
                    suggested_action,
                    scores["weight"],
                    scores["high_standard"],
                    scores["mid_standard"],
                    scores["low_standard"],
                ),
            )
            existing.append({"title": title, "condition": condition})
            saved += 1
        connection.commit()
        return saved
    finally:
        connection.close()


def load_active_rules() -> list[dict]:
    """加载全部已启用的 active 规则（指纹/去重用）；提示词注入请用 retrieve_relevant_rules。"""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT * FROM rules WHERE status = 'active' AND enabled = 1"
            " ORDER BY created_at"
        ).fetchall()
        return [_row_to_rule(row) for row in rows]
    finally:
        connection.close()


def retrieve_relevant_rules(
    query_text: str,
    *,
    contract_type: str | None = None,
    module: str | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """从 active 规则知识库中检索与本合同相关的一小撮规则，供提示词注入。

    先按合同类型过滤（通用规则 + 绑定本类型的规则），再用合同正文与
    「标题+条件」的向量相似度排序；embedding 不可用时退化为命中次数优先。
    未召回的规则既不算命中也不算冷落。
    """
    limit = top_k if top_k is not None else settings.CONTRACT_AI_RULE_RETRIEVAL_TOP_K
    if limit <= 0:
        return []
    wanted_module = module if module in RULE_MODULES else (normalize_module(module) if module else None)
    candidates = [
        rule
        for rule in load_active_rules()
        if (
            not rule.get("contract_type")
            or not contract_type
            or rule["contract_type"] == contract_type
        )
        and (not wanted_module or rule.get("module") == wanted_module)
    ]
    if not candidates:
        return []
    if len(candidates) <= limit:
        return candidates
    query = (query_text or "").strip()[:2000]
    if not query:
        return _rank_by_hits(candidates)[:limit]
    texts = [query] + [
        f"{rule['title']}。{str(rule.get('condition') or '')[:120]}"
        for rule in candidates
    ]
    try:
        vectors = embed_texts(texts)
        query_vector = vectors[0]
        scored = [
            (rule, max(0.0, _cosine(query_vector, vectors[index + 1])))
            for index, rule in enumerate(candidates)
        ]
        scored.sort(
            key=lambda item: (
                -round(item[1], 2),
                -int(item[0].get("hit_count") or 0),
                int(item[0].get("miss_streak") or 0),
                item[0]["id"],
            )
        )
        return [rule for rule, _ in scored[:limit]]
    except Exception as exc:
        logger.debug(f"规则知识库检索不可用，按命中次数取 top-k: {exc}")
        return _rank_by_hits(candidates)[:limit]


def _rank_by_hits(rules: list[dict]) -> list[dict]:
    return sorted(
        rules,
        key=lambda rule: (
            -int(rule.get("hit_count") or 0),
            int(rule.get("miss_streak") or 0),
            rule["id"],
        ),
    )


def count_rules(status: str | None = None) -> int:
    """规则总数（可按状态过滤），供测试与运维查看。"""
    connection = _connect()
    try:
        if status is None:
            row = connection.execute("SELECT COUNT(*) FROM rules").fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM rules WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def list_rules(
    status: str | None = None,
    *,
    module: str | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """列出规则（可按状态 / 模块 / 是否启用过滤）。"""
    connection = _connect()
    try:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if module:
            clauses.append("module = ?")
            params.append(normalize_module(module))
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"SELECT * FROM rules{where} ORDER BY module, code, created_at",
            params,
        ).fetchall()
        return [_row_to_rule(row) for row in rows]
    finally:
        connection.close()


def get_rule(rule_id: str) -> dict | None:
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT * FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return _row_to_rule(row) if row else None
    finally:
        connection.close()


def delete_rule(rule_id: str) -> None:
    """删除规则，列表与规则引擎同步消失。"""
    connection = _connect()
    try:
        cursor = connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(rule_id)
    finally:
        connection.close()


def is_ai_rule(rule: dict) -> bool:
    source = str(rule.get("source") or "")
    code = str(rule.get("code") or "")
    rule_id = str(rule.get("id") or "")
    return source == "ai" or code.startswith("AI-") or rule_id.startswith("ai-rule-")


def split_rule_packs(rules: list[dict] | None = None) -> dict[str, list[dict]]:
    """把规则拆成合同审批包和 AI 自进化包，两套都走列表 + 规则引擎。"""
    items = rules if rules is not None else list_rules()
    approval, ai_rules = [], []
    for rule in items:
        if is_ai_rule(rule):
            ai_rules.append(rule)
        else:
            approval.append(rule)
    return {"approval": approval, "ai": ai_rules}


def grouped_rules(rules: list[dict] | None = None) -> list[dict]:
    """按检查维度分组，供规则引擎与规则列表联动展示。"""
    items = rules if rules is not None else list_rules()
    buckets: dict[str, list[dict]] = {}
    for rule in items:
        topic = str(rule.get("topic") or "").strip() or infer_topic(
            rule.get("title") or "", rule.get("condition") or ""
        )
        buckets.setdefault(topic, []).append(rule)
    ordered = [name for name in ENGINE_TOPIC_ORDER if name in buckets]
    ordered.extend(sorted(name for name in buckets if name not in ENGINE_TOPIC_ORDER))
    return [
        {"name": name, "count": len(buckets[name]), "rules": buckets[name]}
        for name in ordered
    ]


def create_rule(payload: dict) -> dict:
    """用户新增规则，默认立即启用并进入审查提示池。"""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("规则名称不能为空")
    condition = str(payload.get("condition") or "").strip()
    topic = str(payload.get("topic") or "").strip() or infer_topic(title, condition)
    module = ENGINE_CATEGORY_MODULE.get(topic) or resolve_module(
        payload.get("module"), title, condition, topic
    )
    risk_level = normalize_risk_level(payload.get("risk_level"))
    status = normalize_status(payload.get("status"), "active")
    enabled = 0 if payload.get("enabled") in {False, 0, "0", "false", "False"} else 1
    contract_type = str(payload.get("contract_type") or "").strip() or None
    suggested_action = str(payload.get("suggested_action") or "").strip() or None
    scores = _score_fields(payload, risk_level)
    connection = _connect()
    try:
        existing = _load_all_rules(connection)
        if _is_duplicate(title, condition, existing):
            raise ValueError("已存在相同或高度相似的规则")
        now = _now_iso()
        rule_id = f"user-rule-{uuid.uuid4().hex[:16]}"
        code = str(payload.get("code") or "").strip() or _next_rule_code(
            connection, module, prefix="HTSP"
        )
        if _code_exists(connection, code):
            raise ValueError(f"规则编号 {code} 已存在")
        connection.execute(
            "INSERT INTO rules (id, title, condition, contract_type, risk_level,"
            " status, source_analysis_id, hit_count, miss_streak, created_at,"
            " updated_at, topic, module, code, source, enabled, suggested_action,"
            " weight, high_standard, mid_standard, low_standard)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)",
            (
                rule_id,
                title,
                condition,
                contract_type,
                risk_level,
                status,
                now,
                now,
                topic,
                module,
                code,
                enabled,
                suggested_action,
                scores["weight"],
                scores["high_standard"],
                scores["mid_standard"],
                scores["low_standard"],
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_rule(rule_id) or {"id": rule_id}


def update_rule(rule_id: str, payload: dict) -> dict:
    current = get_rule(rule_id)
    if current is None:
        raise KeyError(rule_id)
    title = str(payload.get("title") or current["title"]).strip()
    if not title:
        raise ValueError("规则名称不能为空")
    condition = str(payload.get("condition", current["condition"]) or "").strip()
    topic = str(payload.get("topic") or current.get("topic") or "").strip() or infer_topic(
        title, condition
    )
    module = ENGINE_CATEGORY_MODULE.get(topic) or resolve_module(
        payload.get("module") or current.get("module"), title, condition, topic
    )
    risk_level = normalize_risk_level(
        payload.get("risk_level") or current["risk_level"]
    )
    status = normalize_status(payload.get("status") or current["status"])
    if "enabled" in payload:
        enabled = 0 if payload.get("enabled") in {False, 0, "0", "false", "False"} else 1
    else:
        enabled = 1 if current.get("enabled", True) else 0
    contract_type = payload.get("contract_type", current.get("contract_type"))
    contract_type = str(contract_type or "").strip() or None
    suggested_action = payload.get(
        "suggested_action", current.get("suggested_action")
    )
    suggested_action = str(suggested_action or "").strip() or None
    code = str(payload.get("code") or current.get("code") or "").strip()
    merged = {
        "weight": payload.get("weight", current.get("weight")),
        "high_standard": payload.get("high_standard", current.get("high_standard")),
        "mid_standard": payload.get("mid_standard", current.get("mid_standard")),
        "low_standard": payload.get("low_standard", current.get("low_standard")),
    }
    scores = _score_fields(merged, risk_level)
    connection = _connect()
    try:
        if not code:
            code = _next_rule_code(connection, module)
        elif code != current.get("code") and _code_exists(connection, code):
            raise ValueError(f"规则编号 {code} 已存在")
        connection.execute(
            "UPDATE rules SET title = ?, condition = ?, contract_type = ?,"
            " risk_level = ?, status = ?, topic = ?, module = ?, code = ?,"
            " enabled = ?, suggested_action = ?, weight = ?, high_standard = ?,"
            " mid_standard = ?, low_standard = ?, updated_at = ? WHERE id = ?",
            (
                title,
                condition,
                contract_type,
                risk_level,
                status,
                topic,
                module,
                code,
                enabled,
                suggested_action,
                scores["weight"],
                scores["high_standard"],
                scores["mid_standard"],
                scores["low_standard"],
                _now_iso(),
                rule_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_rule(rule_id) or current


def set_rule_enabled(rule_id: str, enabled: bool) -> dict:
    current = get_rule(rule_id)
    if current is None:
        raise KeyError(rule_id)
    connection = _connect()
    try:
        status = "active" if enabled else "disabled"
        connection.execute(
            "UPDATE rules SET enabled = ?, status = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, status, _now_iso(), rule_id),
        )
        connection.commit()
    finally:
        connection.close()
    return get_rule(rule_id) or current


def seed_builtin_rules() -> int:
    """写入合同审批检查标准；清掉演示用的客户授信数据。"""
    connection = _connect()
    try:
        connection.execute(
            f"DELETE FROM rules WHERE code IN ({','.join('?' * len(DEMO_CREDIT_CODES))})",
            DEMO_CREDIT_CODES,
        )
        existing_codes = {
            row["code"]
            for row in connection.execute(
                "SELECT code FROM rules WHERE code IS NOT NULL"
            ).fetchall()
            if row["code"]
        }
        existing_titles = {
            row["title"]
            for row in connection.execute("SELECT title FROM rules").fetchall()
        }
        saved = 0
        refreshed = 0
        now = _now_iso()
        for item in _engine_snapshot_rules():
            scores = _score_fields(item, item["risk_level"])
            existing = connection.execute(
                "SELECT id, weight, high_standard FROM rules WHERE code = ? OR title = ?",
                (item["code"], item["title"]),
            ).fetchone()
            if existing:
                current_weight = existing["weight"]
                current_high = existing["high_standard"] or ""
                if (
                    current_weight in {None, 8, 10, 15}
                    or current_high in GENERIC_HIGH_STANDARDS
                    or not current_high
                ):
                    connection.execute(
                        "UPDATE rules SET risk_level = ?, topic = ?, module = ?,"
                        " condition = ?, suggested_action = ?, weight = ?,"
                        " high_standard = ?, mid_standard = ?, low_standard = ?,"
                        " source = ?, updated_at = ? WHERE id = ?",
                        (
                            item["risk_level"],
                            item["topic"],
                            item["module"],
                            item["condition"],
                            item.get("suggested_action"),
                            scores["weight"],
                            scores["high_standard"],
                            scores["mid_standard"],
                            scores["low_standard"],
                            item.get("source") or "engine",
                            now,
                            existing["id"],
                        ),
                    )
                    refreshed += 1
                continue
            connection.execute(
                "INSERT INTO rules (id, title, condition, contract_type, risk_level,"
                " status, source_analysis_id, hit_count, miss_streak, created_at,"
                " updated_at, topic, module, code, source, enabled, suggested_action,"
                " weight, high_standard, mid_standard, low_standard)"
                " VALUES (?, ?, ?, NULL, ?, 'active', NULL, 0, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    f"builtin-{item['code'].lower()}",
                    item["title"],
                    item["condition"],
                    item["risk_level"],
                    now,
                    now,
                    item["topic"],
                    item["module"],
                    item["code"],
                    item.get("source") or "builtin",
                    item.get("suggested_action"),
                    scores["weight"],
                    scores["high_standard"],
                    scores["mid_standard"],
                    scores["low_standard"],
                ),
            )
            existing_codes.add(item["code"])
            existing_titles.add(item["title"])
            saved += 1
        connection.commit()
        if saved or refreshed:
            logger.info(
                f"规则引擎库：写入 {saved} 条预置规则，刷新 {refreshed} 条权重/评分"
            )
        return saved
    finally:
        connection.close()


def _engine_snapshot_rules() -> list[dict]:
    """把合同审批检查标准 50 条转成规则引擎库条目。"""
    path = settings.resolve_path(settings.CONTRACT_RULES_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取合同审批规则快照失败: {exc}")
        return []
    items: list[dict] = []
    for raw in payload.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        category = str(raw.get("category") or "").strip()
        method = str(raw.get("check_method") or "").strip()
        method_label = CHECK_METHOD_LABEL.get(method, method or "规则检查")
        condition = (
            str(raw.get("condition") or "").strip()
            or f"{category}：{title}（{method_label}）"
        )
        module = ENGINE_CATEGORY_MODULE.get(category) or infer_module(
            title, condition, category
        )
        legacy_id = raw.get("legacy_id")
        try:
            serial = int(legacy_id)
        except (TypeError, ValueError):
            serial = len(items) + 1
        risk_level = infer_engine_risk_level(title, category, method)
        scores = infer_score_bands(risk_level, title=title, category=category)
        items.append(
            {
                "code": f"HTSP-202511-{serial:03d}",
                "title": title,
                "condition": condition,
                "module": module,
                "risk_level": risk_level,
                "topic": category or infer_topic(title, condition),
                "suggested_action": f"按「{category}」检查「{title}」",
                "source": "engine",
                **scores,
            }
        )
    return items


def _code_exists(connection: sqlite3.Connection, code: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM rules WHERE code = ?", (code,)
    ).fetchone()
    return row is not None


def confirm_rule(rule_id: str) -> None:
    """人工确认：draft → active（进入下次审查的提示池）。"""
    connection = _connect()
    try:
        connection.execute(
            "UPDATE rules SET status = 'active', enabled = 1, updated_at = ?"
            " WHERE id = ?",
            (_now_iso(), rule_id),
        )
        connection.commit()
    finally:
        connection.close()


def disable_rule(rule_id: str) -> None:
    """停用规则：active/draft → disabled。"""
    connection = _connect()
    try:
        connection.execute(
            "UPDATE rules SET status = 'disabled', enabled = 0, updated_at = ?"
            " WHERE id = ?",
            (_now_iso(), rule_id),
        )
        connection.commit()
    finally:
        connection.close()


def record_hits(rule_ids: list[str]) -> int:
    """命中统计：本次审查中 AI 实际采纳/覆盖了哪些 active 规则，累加 hit_count。

    被采纳的规则同时清零 miss_streak（连续未采纳计数）。返回更新的行数。
    """
    if not rule_ids:
        return 0
    connection = _connect()
    try:
        cursor = connection.execute(
            f"UPDATE rules SET hit_count = hit_count + 1, miss_streak = 0,"
            f" updated_at = ? WHERE id IN ({','.join('?' * len(rule_ids))})",
            [_now_iso(), *rule_ids],
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def bump_unadopted_misses(
    adopted_ids: list[str],
    contract_type: str | None = None,
    retrieved_ids: list[str] | None = None,
) -> None:
    """本轮被召回但未被 AI 采纳的规则 miss_streak + 1。

    只对「本轮写入提示词的规则」计数：未召回的既不算命中也不算冷落。
    retrieved_ids 为 None 时退化为按合同类型隔离的全量 active 计数（兼容旧调用）。
    合同类型未判定时不累计。
    """
    if contract_type is None:
        return
    connection = _connect()
    try:
        now = _now_iso()
        if retrieved_ids is not None:
            pending = [rid for rid in retrieved_ids if rid not in set(adopted_ids)]
            if pending:
                placeholders = ",".join("?" * len(pending))
                connection.execute(
                    f"UPDATE rules SET miss_streak = miss_streak + 1, updated_at = ?"
                    f" WHERE status = 'active' AND id IN ({placeholders})",
                    [now, *pending],
                )
        elif adopted_ids:
            placeholders = ",".join("?" * len(adopted_ids))
            connection.execute(
                f"UPDATE rules SET miss_streak = miss_streak + 1, updated_at = ?"
                f" WHERE status = 'active' AND id NOT IN ({placeholders})"
                f" AND (contract_type IS NULL OR contract_type = ?)",
                [now, *adopted_ids, contract_type],
            )
        else:
            connection.execute(
                "UPDATE rules SET miss_streak = miss_streak + 1, updated_at = ?"
                " WHERE status = 'active'"
                " AND (contract_type IS NULL OR contract_type = ?)",
                (now, contract_type),
            )
        connection.commit()
    finally:
        connection.close()


def auto_disable_stale(threshold: int) -> int:
    """自动进化：连续 ``threshold`` 次审查未被 AI 采纳的 active 规则自动停用。

    threshold <= 0 表示关闭自动淘汰。返回本次自动停用的条数。
    """
    if threshold <= 0:
        return 0
    connection = _connect()
    try:
        cursor = connection.execute(
            "UPDATE rules SET status = 'disabled', updated_at = ?"
            " WHERE status = 'active' AND miss_streak >= ?",
            (_now_iso(), threshold),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def rule_exists(rule_id: str) -> bool:
    """规则是否存在（管理接口 404 判断用）。"""
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT 1 FROM rules WHERE id = ?", (rule_id,)
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _load_all_rules(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT title, condition FROM rules"
    ).fetchall()
    return [{"title": row[0], "condition": row[1]} for row in rows]


def _is_duplicate(title: str, condition: str, existing: list[dict]) -> bool:
    if not existing:
        return False
    for rule in existing:
        if rule["title"] == title:
            return True
    # 向量相似度判重（embedding 不可用时跳过相似度判断）
    text = f"{title}。{condition[:120]}"
    try:
        vectors = embed_texts(
            [text]
            + [
                f"{rule['title']}。{rule['condition'][:120]}"
                for rule in existing
            ]
        )
    except Exception as exc:
        logger.debug(f"规则去重不可用，跳过相似度判断: {exc}")
        return False
    scores = [_cosine(vectors[0], vector) for vector in vectors[1:]]
    return max(scores) >= RULE_DEDUPE_THRESHOLD


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
