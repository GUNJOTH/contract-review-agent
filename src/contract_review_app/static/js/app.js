/* ============================================================
 * 合同审查控制台 — 前端逻辑
 * 依赖后端：合同审查智能体（/api/v1），扫描合同由 OCR 网关补识别
 * ============================================================ */
"use strict";

/* ---------------- 工具函数 ---------------- */
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children || []).flat(Infinity)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function shortId(id) {
  return id && id.length > 12 ? id.slice(0, 12) + "…" : id;
}

function fileFormat(filename) {
  const match = /\.([A-Za-z0-9]+)$/.exec(filename || "");
  return match ? match[1].toUpperCase() : "unknown";
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板", "ok");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("已复制到剪贴板", "ok");
  }
}

function toast(msg, type = "ok", timeout = 3500) {
  const wrap = $("#toast-wrap");
  const t = el("div", { class: `toast ${type}`, text: msg });
  wrap.appendChild(t);
  setTimeout(() => t.remove(), timeout);
}

/* ---------------- API 客户端 ---------------- */
const DEFAULT_API_BASE = "/api/v1";
const DEFAULT_AUTH_HEADER_NAME = "X-API-Token";
const TOKEN_STORAGE_KEY = "contract_api_token";

const api = {
  authHeaderName: DEFAULT_AUTH_HEADER_NAME,
  get base() {
    return localStorage.getItem("contract_api_base") || DEFAULT_API_BASE;
  },
  headers(extra = {}) {
    const token = sessionStorage.getItem(TOKEN_STORAGE_KEY)?.trim();
    const auth = token ? { [api.authHeaderName]: token } : {};
    return { ...auth, ...extra };
  },
  async request(method, path, options = {}) {
    const url = api.base.replace(/\/$/, "") + path;
    const res = await fetch(url, { method, headers: api.headers(options.headers), body: options.body });
    return api.unwrap(res);
  },
  get(path) {
    return api.request("GET", path);
  },
  /** 带 JSON 请求体的 POST；调用方只负责给出对象，序列化与头在此统一处理。 */
  post(path, body) {
    return api.request("POST", path, {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  put(path, body) {
    return api.request("PUT", path, {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  del(path) {
    return api.request("DELETE", path);
  },
  /** 带上传进度的 multipart 请求（XHR） */
  upload(path, formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", api.base.replace(/\/$/, "") + path);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      Object.entries(api.headers()).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.onload = () => {
        let json = null;
        try { json = JSON.parse(xhr.responseText); } catch { /* 非 JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(json);
        } else {
          reject(api.errorFrom(json, xhr.status));
        }
      };
      xhr.onerror = () => reject(new Error("网络错误，无法连接服务"));
      xhr.send(formData);
    });
  },
  async unwrap(res) {
    let json = null;
    try { json = await res.json(); } catch { /* 非 JSON 响应 */ }
    if (!res.ok) throw api.errorFrom(json, res.status);
    return json;
  },
  errorFrom(json, status) {
    const e = new Error();
    e.status = status;
    try {
      const err = json?.Response?.Error;
      if (err) {
        e.code = err.Code;
        e.message = err.Message || err.Code;
      } else if (json?.detail) {
        e.message = String(json.detail);
      } else if (json?.message) {
        e.message = String(json.message);
      } else {
        e.message = `请求失败 (HTTP ${status})`;
      }
    } catch {
      e.message = `请求失败 (HTTP ${status})`;
    }
    if (/connecting to (localhost|127\.0\.0\.1|redis)/i.test(e.message)) {
      e.message = "无法连接 Redis：异步任务功能依赖 Redis 服务，请确认 Redis 已启动（同步审查不受影响）";
    }
    return e;
  },
};

/* ---------------- 配置持久化 ---------------- */
function applyConfig() {
  let base = $("#api-base").value.trim().replace(/\/+$/, "");
  if (!base) base = DEFAULT_API_BASE;
  localStorage.setItem("contract_api_base", base);
  const tokenInput = $("#token-input");
  if (tokenInput) {
    const token = tokenInput.value.trim();
    if (token) sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    else sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    tokenInput.classList.toggle("token-empty", !token);
  }
}

function loadConfigUI() {
  const input = $("#api-base");
  if (!input) return;
  input.value = api.base === DEFAULT_API_BASE ? "" : api.base;
  const tokenInput = $("#token-input");
  if (tokenInput) {
    tokenInput.value = sessionStorage.getItem(TOKEN_STORAGE_KEY) || "";
    tokenInput.classList.toggle("token-empty", !tokenInput.value);
  }
  input.addEventListener("input", applyConfig);
}
function saveConfig() {
  applyConfig();
  toast("配置已保存", "ok");
  refreshHealth();
}


/* ---------------- JSON 查看器 ---------------- */
function jsonView(data) {
  const text = JSON.stringify(data, null, 2);
  const esc = escapeHtml(text);
  const colored = esc
    .replace(/(&quot;.*?&quot;)(\s*:)/g, '<span style="color:#7dd3fc">$1</span>$2')
    .replace(/: (&quot;.*?&quot;)/g, ': <span style="color:#86efac">$1</span>')
    .replace(/: (true|false)/g, ': <span style="color:#fbbf24">$1</span>')
    .replace(/: (-?\d+\.?\d*)/g, ': <span style="color:#f472b6">$1</span>');
  return el("div", { class: "json-view", html: colored });
}

/* ---------------- 模态框 ---------------- */
function openModal(title, bodyNode, options = {}) {
  $("#modal-title").textContent = title;
  const modal = $("#modal");
  modal.classList.toggle("modal-wide", !!options.wide);
  const body = $("#modal-body");
  body.innerHTML = "";
  if (typeof bodyNode === "string") body.innerHTML = bodyNode;
  else body.appendChild(bodyNode);
  $("#modal-mask").classList.remove("hidden");
}
function closeModal() {
  $("#modal-mask").classList.add("hidden");
  $("#modal").classList.remove("modal-wide");
}

/* ---------------- 徽标 ---------------- */
const STATUS_META = {
  PENDING: { label: "排队中", cls: "gray" },
  RUNNING: { label: "执行中", cls: "blue" },
  SUCCEEDED: { label: "成功", cls: "green" },
  FAILED: { label: "失败", cls: "red" },
  CANCELED: { label: "已取消", cls: "gray" },
  EXPIRED: { label: "已过期", cls: "orange" },
};

// 审查流水线状态（ReviewStatus）：HUMAN_REVIEW 表示「分析已完成、等人工核实」，
// 不是「还在跑」。直接上屏英文枚举会让人误判审查未结束，这里统一成中文文案。
const REVIEW_STATUS_META = {
  RECEIVED: "已接收",
  PARSED: "已解析",
  QUALITY_GATED: "已过质量门禁",
  INDEXED: "已建索引",
  EXTRACTED: "要素已抽取",
  RULE_CHECKED: "规则已判定",
  SEMANTIC_REVIEWED: "语义已判定",
  HUMAN_REVIEW: "分析完成 · 待人工复核",
  FINALIZED: "已定稿",
  FAILED: "失败",
};

function statusText(status) {
  if (!status) return "-";
  if (REVIEW_STATUS_META[status]) return REVIEW_STATUS_META[status];
  const meta = STATUS_META[status];
  return meta ? meta.label : String(status);
}

const FINDING_META = {
  PASS: { label: "通过", cls: "green" },
  WARN: { label: "警告", cls: "yellow" },
  BLOCK: { label: "拦截", cls: "red" },
  UNKNOWN: { label: "未知", cls: "gray" },
  NOT_APPLICABLE: { label: "不适用", cls: "gray" },
};
const RISK_META = {
  critical: { label: "严重", cls: "red" },
  high: { label: "高", cls: "orange" },
  medium: { label: "中", cls: "yellow" },
  low: { label: "低", cls: "blue" },
  unclassified: { label: "未分级", cls: "gray" },
};
const AI_LEVEL_META = {
  BLOCK: { label: "重大风险", cls: "red" },
  WARN: { label: "需关注", cls: "yellow" },
  INFO: { label: "提示", cls: "blue" },
  UNKNOWN: { label: "未知", cls: "gray" },
  PASS: { label: "符合", cls: "green" },
  NOT_APPLICABLE: { label: "符合", cls: "green" },
};
const VERDICT_META = {
  不符: { label: "不符", cls: "red", levels: ["BLOCK", "WARN"] },
  待确认: { label: "待确认", cls: "gray", levels: ["UNKNOWN", "INFO"] },
  符合: { label: "符合", cls: "green", levels: ["PASS", "NOT_APPLICABLE"] },
};
const VERDICT_ORDER = ["不符", "待确认", "符合"];
const VERDICT_ALIASES = {
  不通过: "不符",
  不清楚: "待确认",
  通过: "符合",
};

function itemVerdict(item) {
  const raw = item?.verdict;
  if (raw && VERDICT_META[raw]) return raw;
  if (raw && VERDICT_ALIASES[raw]) return VERDICT_ALIASES[raw];
  const level = String(item?.risk_level || item?.status || "UNKNOWN").toUpperCase();
  if (level === "PASS" || level === "NOT_APPLICABLE") return "符合";
  if (level === "BLOCK" || level === "WARN") return "不符";
  return "待确认";
}

function badge(key, meta) {
  const m = meta[key];
  return el("span", { class: `badge ${m ? m.cls : "gray"}`, text: m ? m.label : key });
}

const TASK_TYPE_LABELS = {
  "contract-review": "合同审查",
};

/* ---------------- 导航 ---------------- */
const PAGES = { review: renderReviewPage, draft: renderDraftPage, rules: renderRulesPage, tasks: renderTasksPage };
let currentPage = null;

function pageHead(kicker, title, desc) {
  return el("div", { class: "page-head" }, [
    el("div", { class: "kicker", text: kicker }),
    el("h1", { text: title }),
    el("p", { text: desc }),
  ]);
}

function navigate(page) {
  currentPage = page;
  document.querySelectorAll(".nav-item[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  const fn = PAGES[page];
  const content = $("#content");
  if (!content) return;
  content.innerHTML = "";
  if (typeof fn !== "function") {
    content.appendChild(el("p", { class: "muted", text: `页面未找到：${page}` }));
    return;
  }
  fn(content);
}

function stat(label, value, color = "blue", sub = null) {
  return el("div", { class: `stat tone-${color}` }, [
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value", style: `color: var(--${color})`, text: value }),
    sub ? el("div", { class: "stat-sub" }, [sub]) : null,
  ]);
}

/* ============================================================
 * 合同审查页
 * ============================================================ */
const CONTRACT_TYPES = ["", "软件产品销售", "软件开发/转让服务", "一般商品销售合同", "混合合同", "其它服务合同"];

function newPackageId() {
  const stamp = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const date = `${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}`;
  const time = `${p(stamp.getHours())}${p(stamp.getMinutes())}${p(stamp.getSeconds())}`;
  const rand = Math.random().toString(36).slice(2, 6);
  return `pkg-${date}-${time}-${rand}`;
}

let reviewState = {
  files: [],
  packageId: newPackageId(),
  contractType: "",
  partyPosition: "",
  jurisdiction: "",
  transactionContext: "",
  reviewScope: "",
  result: null,
};

function renderReviewPage(content) {
  content.appendChild(pageHead("CONTRACT REVIEW", "合同审查", "上传合同后按风险点 / 合理性 / 内控 / 资信四栏展示，规则来自当前版本化 RuleBundle"));

  const card = el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "合同包" }, [el("span", { class: "hint", text: "支持多文件" })]),

    el("div", { class: "form-row" }, [
      el("label", {}, [el("span", { class: "req", text: "*" }), " 合同附件文件（可多选）"]),
      buildReviewDropzone(),
      el("div", { id: "review-files" }),
    ]),

    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", {}, [el("span", { class: "req", text: "*" }), " 合同包 ID（PackageId）"]),
        el("input", { class: "input", id: "review-pkg", placeholder: "每次新文件自动生成，可改", value: reviewState.packageId, oninput: (e) => (reviewState.packageId = e.target.value.trim()) }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "合同类型（ContractType）" }),
        el("select", { class: "select", id: "review-type", onchange: (e) => (reviewState.contractType = e.target.value) },
          CONTRACT_TYPES.map((t) => el("option", { value: t, text: t || "— 不指定 —", selected: t === reviewState.contractType ? "" : null }))),
      ]),
    ]),

    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "本方交易立场（PartyPosition）" }),
        el("select", { class: "select", onchange: (e) => (reviewState.partyPosition = e.target.value) }, [
          el("option", { value: "", text: "— 不指定 —", selected: !reviewState.partyPosition ? "" : null }),
          el("option", { value: "buyer", text: "甲方 / 买方", selected: reviewState.partyPosition === "buyer" ? "" : null }),
          el("option", { value: "seller", text: "乙方 / 卖方", selected: reviewState.partyPosition === "seller" ? "" : null }),
          el("option", { value: "both", text: "双方", selected: reviewState.partyPosition === "both" ? "" : null }),
        ]),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "适用法域（Jurisdiction）" }),
        el("input", { class: "input", placeholder: "如：中国大陆", value: reviewState.jurisdiction, oninput: (e) => (reviewState.jurisdiction = e.target.value.trim()) }),
      ]),
    ]),

    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "交易背景（TransactionContext）" }),
        el("textarea", { class: "input", rows: "3", placeholder: "如：软件开发项目采购，重点关注付款与验收", oninput: (e) => (reviewState.transactionContext = e.target.value.trim()) }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "审查范围（ReviewScope）" }),
        el("input", { class: "input", placeholder: "规则 category 或 ID，逗号分隔；缺省为全部", value: reviewState.reviewScope, oninput: (e) => (reviewState.reviewScope = e.target.value.trim()) }),
      ]),
    ]),

    el("div", { class: "action-bar mt-8" }, [
      el("div", { class: "flex", style: "flex-wrap:wrap" }, [
        el("button", { class: "btn btn-primary", id: "btn-review-sync", text: "同步审查", onclick: submitReviewSync }),
        el("button", { class: "btn btn-secondary", id: "btn-review-async", text: "异步审查", onclick: submitReviewAsync }),
        el("button", { class: "btn btn-ghost", text: "客商风险", onclick: openCreditRiskDialog }),
        el("button", { class: "btn btn-ghost", text: "版本比对", onclick: openCompareDialog }),
        el("span", { class: "muted", text: "大批量请走异步，结果可在任务中心查看" }),
      ]),
    ]),
  ]);
  content.appendChild(card);
  content.appendChild(el("div", { id: "review-result" }));
}

let compareState = { baseFile: null, compareFile: null, result: null, options: {} };

function fileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function mergeFiles(current, incoming) {
  const seen = new Set(current.map(fileKey));
  const merged = [...current];
  for (const file of incoming) {
    const key = fileKey(file);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(file);
  }
  return merged;
}


/**
 * 单一清单：以规则包**全量规则**为骨架，AI 判定按 rule_id 覆盖到对应规则上。
 *
 * 规则侧 findings 覆盖 RuleBundle 的每一条规则（含符合/不适用），是清单的
 * 底座——「所有规则都要展示出来」靠这一层保证，模型漏答或降级都不会丢行。
 * AI 侧只贡献判定结论（等级/理由/原文），规则身份（编号、分类、条文位置）
 * 仍取自规则包。AI 在规则之外额外发现的风险点（无 rule_id）追加在末尾。
 */
function reviewItems(resp) {
  const rr = resp?.review_result;
  // 全量 findings（含 PASS/不适用）参与投影——内控栏的符合/不符/待确认
  // 分栏需要完整规则清单。
  const ruleItems = (rr?.findings || []).map((finding) => projectReviewFinding(finding, rr));
  // 通读式 AI 风险分析（批次 2）：规则库外的补充判据，module 由模型声明。
  const aiItems = (rr?.risk_analysis_response?.items || []).map((item) => ({
    ...item,
    risk_id: item.item_id,
    source: "ai",
    module: item.module || "风险点",
    quote: item.quote || null,
    verdict: itemVerdict(item),
  }));
  if (!aiItems.length) return sortBySeverity(ruleItems);

  // rule_id 优先；个别条目没回填 rule_id 时退化为标题逐字匹配（兼容历史存档）。
  // 标题索引建在规则侧：规则侧每条都有 rule_id，AI 侧才可能缺，只有把标题
  // 映射回规则 id，两边才落在同一个 key 上。
  const ruleIdByTitle = new Map();
  ruleItems.forEach((item) => {
    const title = String(item.title || "").trim();
    if (title && !ruleIdByTitle.has(title)) ruleIdByTitle.set(title, item.rule_id);
  });
  const matchKey = (item) => {
    if (item?.rule_id) return `id:${item.rule_id}`;
    const title = String(item?.title || "").trim();
    const mapped = ruleIdByTitle.get(title);
    return mapped ? `id:${mapped}` : `title:${title}`;
  };

  const pending = new Map();
  aiItems.forEach((item) => {
    const key = matchKey(item);
    if (!pending.has(key)) pending.set(key, []);
    pending.get(key).push(item);
  });

  const merged = ruleItems.map((base) => {
    const hits = pending.get(`id:${base.rule_id}`);
    if (!hits || !hits.length) return base;
    const ai = hits.shift();
    if (!hits.length) pending.delete(`id:${base.rule_id}`);
    const level = ai.risk_level || base.risk_level;
    return {
      ...base,
      // AI 判定的等级与理由优先，规则身份/分类/条文位置保留规则侧
      risk_level: level,
      // 只喂 risk_level：base 里继承来的旧 verdict 会让 itemVerdict 直接短路
      verdict: itemVerdict({ risk_level: level }),
      reason: ai.reason || base.reason,
      quote: ai.quote || base.quote,
      confidence: ai.confidence != null ? ai.confidence : base.confidence,
      ai_risk_level: ai.risk_level || null,
      ai_reason: ai.reason || null,
    };
  });

  // 规则清单之外的风险点，以及未能对上任何规则的 AI 条目
  const extras = [];
  pending.forEach((hits) => extras.push(...hits));
  return sortBySeverity([...merged, ...extras]);
}

function sortBySeverity(items) {
  return items.slice().sort(
    (left, right) => findingSeverity(right.risk_level) - findingSeverity(left.risk_level),
  );
}

function projectReviewFinding(finding, rr) {
  const rules = rr?.rule_bundle?.rules || [];
  const rule = rules.find((item) => item.rule_id === finding.rule_id);
  const evidenceById = Object.fromEntries((rr?.evidence || []).map((item) => [item.evidence_id, item]));
  const factsById = Object.fromEntries((rr?.facts || []).map((item) => [item.fact_id, item]));
  const clausesById = Object.fromEntries((rr?.clauses || []).map((item) => [item.clause_id, item]));
  const status = finding.status || "UNKNOWN";
  const text = `${finding.title || ""} ${finding.reason || ""}`;
  const clause = (finding.clause_ids || []).map((id) => clausesById[id]).find((item) => item?.clause_number);
  const quote = (finding.evidence_ids || [])
    .map((id) => evidenceById[id])
    .find((item) => item && item.evidence_type !== "missing_artifact" && (item.display_excerpt || item.raw_excerpt));
  const semanticRuleIds = new Set((rr?.semantic_response?.items || []).map((item) => item.rule_id));
  return {
    ...finding,
    risk_id: finding.finding_id,
    risk_level: status,
    verdict: itemVerdict({ ...finding, risk_level: status }),
    suggested_action: finding.recommended_action,
    quote: quote?.display_excerpt || quote?.raw_excerpt || null,
    source: semanticRuleIds.has(finding.rule_id) ? "ai" : "rule",
    module: reviewModule(rule, text),
    category: rule?.category || null,
    metric: reviewMetric(text),
    value: (finding.fact_ids || [])
      .map((id) => factsById[id]?.value)
      .find((value) => value !== undefined && value !== null && value !== "")
      ?.toString() || null,
    section: clause?.clause_number || null,
  };
}

function reviewModule(rule, text) {
  const category = rule?.category || "";
  if (category === "合同类型" || text.includes("合同类型")) return "风险点";
  if (category.startsWith("Qx") || /合理性|信创|国产化|云架构|数据治理/.test(text)) return "合理性";
  if (["资信", "客户资信风险"].includes(category) || /资信|征信|注册资本|诉讼/.test(text)) return "资信";
  return "内控";
}

function reviewMetric(text) {
  const mappings = [
    ["利润率", /利润/],
    ["资金要求", /资金/],
    ["项目预算", /预算/],
    ["收款进度", /收款|进度/],
    ["履行期限", /期限|工期|签订时间/],
    ["权属范围", /权属|知识产权|源代码/],
    ["注册资本", /注册资本/],
    ["合作历史", /合作历史/],
    ["当前合同", /当前合同/],
    ["经营状况", /经营状况|经营/],
  ];
  return mappings.find(([, pattern]) => pattern.test(text))?.[0] || null;
}

function findingSeverity(status) {
  return { BLOCK: 5, WARN: 4, INFO: 3, UNKNOWN: 2, PASS: 1, NOT_APPLICABLE: 1 }[status] || 0;
}

function requireReviewResult(action) {
  if (reviewState.result?.review_result) return true;
  toast(`请先完成合同审查后再${action}`, "warn");
  return false;
}

function compareIgnoreFlags() {
  return [
    ["ignore_symbols", "忽略符号"],
    ["ignore_watermark", "忽略水印"],
    ["ignore_seals", "忽略印章"],
    ["ignore_images", "忽略图片"],
    ["ignore_header_footer", "忽略页眉页脚"],
    ["ignore_tables", "忽略表格"],
    ["ignore_handwriting", "忽略手写"],
  ];
}

function pickCompareFile(kind) {
  const input = el("input", { type: "file", class: "hidden", accept: ".pdf,.docx,.xlsx" });
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (kind === "base") compareState.baseFile = file;
    else compareState.compareFile = file;
    renderCompareFiles();
  });
  input.click();
}

function renderCompareFiles() {
  const baseHint = $("#compare-base-hint");
  const compareHint = $("#compare-compare-hint");
  if (baseHint) baseHint.textContent = compareState.baseFile ? compareState.baseFile.name : "未选择基准文档";
  if (compareHint) compareHint.textContent = compareState.compareFile ? compareState.compareFile.name : "未选择比对文档";
}

/** 当前页面已选中的文件池：合同拟定页用表单附件，合同审查页用待审文件。 */
function currentFilePool() {
  return currentPage === "draft" ? elementsState.files : reviewState.files;
}

async function openCompareDialog() {
  compareState.result = null;
  const pool = currentFilePool();
  if (!compareState.baseFile && pool[0]) compareState.baseFile = pool[0];
  const body = el("div", { class: "compare-dialog" }, [
    el("p", { class: "muted", text: "支持 Word、PDF 对比和相似度提醒。上传基准文档与比对文档后查看差异列表。" }),
    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", {}, [el("span", { class: "req", text: "*" }), " 基准文档"]),
        el("div", { class: "flex" }, [
          el("button", { class: "btn btn-ghost btn-sm", text: "选择文件", onclick: () => pickCompareFile("base") }),
          el("span", { class: "muted", id: "compare-base-hint", text: compareState.baseFile ? compareState.baseFile.name : "未选择基准文档" }),
        ]),
      ]),
      el("div", { class: "form-row" }, [
        el("label", {}, [el("span", { class: "req", text: "*" }), " 比对文档"]),
        el("div", { class: "flex" }, [
          el("button", { class: "btn btn-ghost btn-sm", text: "选择文件", onclick: () => pickCompareFile("compare") }),
          el("span", { class: "muted", id: "compare-compare-hint", text: compareState.compareFile ? compareState.compareFile.name : "未选择比对文档" }),
        ]),
      ]),
    ]),
    el("p", { class: "muted", text: reviewState.result?.review_result
      ? "版本差异会挂到当前审查结果上；基准文档需是本合同包里已审过的那一份。"
      : "当前没有审查结果：将进行纯文档对比（无需先审查）；基准文档若恰好审过，差异会自动挂到该结果上。" }),
    el("div", { class: "compare-ignores" }, compareIgnoreFlags().map(([key, label]) =>
      el("label", { class: "compare-ignore" }, [
        el("input", {
          type: "checkbox",
          onchange: (e) => { compareState.options[key] = e.target.checked; },
        }),
        el("span", { text: label }),
      ])
    )),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", id: "btn-compare", text: "开始对比", onclick: submitCompare }),
      el("button", { class: "btn btn-ghost", text: "关闭", onclick: closeModal }),
    ]),
    el("div", { id: "compare-result" }),
  ]);
  openModal("文档对比", body, { wide: true });
  renderCompareFiles();
}

async function submitCompare() {
  if (!compareState.baseFile || !compareState.compareFile) {
    toast("请先选择基准文档和比对文档", "warn");
    return;
  }
  const btn = $("#btn-compare");
  if (btn) btn.disabled = true;
  const host = $("#compare-result");
  if (host) {
    host.innerHTML = "";
    host.appendChild(el("p", { class: "muted", text: "正在对比文档…" }));
  }
  try {
    const fd = new FormData();
    fd.append("base_file", compareState.baseFile);
    fd.append("compare_file", compareState.compareFile);
    compareIgnoreFlags().forEach(([key]) => {
      if (compareState.options[key]) fd.append(key, "true");
    });
    if (reviewState.result?.review_result) {
      // 有审查结果才挂载（可选）：以文件部件上传，完整 ReviewResult 常超
      // 1MB，普通表单字段会撞 Starlette 的 max_part_size=1MB。
      fd.append(
        "ReviewResultPayload",
        new Blob([JSON.stringify(reviewState.result.review_result)], { type: "application/json" }),
        "review-result.json",
      );
    }
    const resp = await api.upload("/contract-compare", fd);
    compareState.result = resp;
    if (resp.review_result) reviewState.result = { review_result: resp.review_result };
    renderCompareResult(resp);
  } catch (e) {
    if (host) host.innerHTML = "";
    toast(`文档对比失败: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderCompareResult(resp) {
  const host = $("#compare-result");
  if (!host) return;
  host.innerHTML = "";
  const labels = { added: "新增", deleted: "删除", modified: "修改" };
  host.appendChild(el("div", { class: "grid grid-3" }, [
    stat("相似度", resp.similarity_label || "-", "blue"),
    stat("新增", String(resp.added || 0), "green"),
    stat("删除 / 修改", `${resp.deleted || 0} / ${resp.modified || 0}`, "orange"),
  ]));
  host.appendChild(el("div", { class: "compare-workspace" }, [
    el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "基准文档" }, [el("span", { class: "hint", text: resp.base_filename })]),
      el("div", { class: "compare-doc", html: resp.base?.html || "" }),
    ]),
    el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "比对文档" }, [el("span", { class: "hint", text: resp.compare_filename })]),
      el("div", { class: "compare-doc", html: resp.compare?.html || "" }),
    ]),
    el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "差异列表" }, [
        el("span", { class: "hint", text: `${(resp.changes || []).length} 处` }),
        el("span", { class: "grow" }),
        el("button", { class: "link-btn", text: "导出差异报告", onclick: () => downloadCompareReport(resp) }),
      ]),
      (resp.changes || []).length
        ? el("div", { class: "compare-changes" }, (resp.changes || []).map((change, index) =>
            el("div", { class: `compare-change ${change.kind}` }, [
              el("div", { class: "compare-change-head", text: `${index + 1}. ${labels[change.kind] || change.kind}` }),
              change.base_text ? el("p", { class: "muted", text: `基准：${change.base_text}` }) : null,
              change.compare_text ? el("p", { text: `比对：${change.compare_text}` }) : null,
            ])
          ))
        : el("p", { class: "muted", text: "未发现差异。" }),
    ]),
  ]));
}

function downloadCompareReport(resp) {
  const blob = new Blob([resp.report || ""], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: "文档对比差异报告.txt" });
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  toast("差异报告已导出", "ok");
}

function buildReviewDropzone() {
  const dz = el("div", { class: "dropzone", id: "review-dropzone" }, [
    el("div", { class: "dz-icon", text: "↑" }),
    el("div", { id: "review-dz-hint", text: "点击选择或拖拽多个合同文件到此处" }),
    el("div", { class: "muted", text: "支持 PDF / DOCX / XLSX" }),
  ]);
  const input = el("input", { type: "file", class: "hidden", multiple: true, onchange: (e) => {
    setReviewFiles([...e.target.files]);
    e.target.value = "";
  } });
  dz.appendChild(input);
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    setReviewFiles([...e.dataTransfer.files]);
  });
  return dz;
}

function setReviewFiles(files, options = {}) {
  const incoming = [...files];
  const clearing = incoming.length === 0;
  const append = !clearing && options.append !== false && reviewState.files.length > 0;
  reviewState.files = append ? mergeFiles(reviewState.files, incoming) : incoming;
  if (!append) {
    reviewState.packageId = newPackageId();
    const pkgInput = $("#review-pkg");
    if (pkgInput) pkgInput.value = reviewState.packageId;
  }
  reviewState.result = null;
  const wrap = $("#review-result");
  if (wrap) wrap.innerHTML = "";
  renderReviewFiles();
  if (clearing) toast("已清空合同文件", "ok");
  else if (append) toast(`已追加附件，当前共 ${reviewState.files.length} 个文件`, "ok");
  else toast(`已选择 ${reviewState.files.length} 个文件，开始新的审查`, "ok");
}

function renderReviewFiles() {
  const wrap = $("#review-files");
  if (!wrap) return;
  wrap.innerHTML = "";
  const dz = $("#review-dropzone");
  const hint = $("#review-dz-hint");
  const count = reviewState.files.length;
  if (hint) {
    hint.textContent = count
      ? `已添加 ${count} 个文件，点击或拖拽可继续添加`
      : "点击选择或拖拽多个合同文件到此处";
  }
  if (dz) dz.classList.toggle("has-files", count > 0);
  if (!count) return;
  wrap.appendChild(el("div", { class: "upload-ok" }, [
    el("span", { text: `✓ 已选择 ${count} 个文件` }),
    el("span", { class: "muted", text: `（共 ${fmtBytes(reviewState.files.reduce((s, f) => s + f.size, 0))}）` }),
    el("button", { class: "link-btn", text: "清空", onclick: (event) => { event.preventDefault(); event.stopPropagation(); setReviewFiles([], { replace: true }); } }),
  ]));
  for (const f of reviewState.files) {
    wrap.appendChild(el("span", { class: "file-chip" }, [
      el("span", { text: `${f.name}（${fmtBytes(f.size)}）` }),
      el("span", { class: "remove", text: "✕", onclick: (event) => {
        event.preventDefault();
        event.stopPropagation();
        setReviewFiles(
          reviewState.files.filter((item) => fileKey(item) !== fileKey(f)),
          { replace: true },
        );
      } }),
    ]));
  }
}

function validateReview() {
  if (!reviewState.files.length) { toast("请至少选择一个合同文件", "warn"); return false; }
  if (!reviewState.packageId) { toast("请填写合同包 ID", "warn"); return false; }
  return true;
}

function appendReviewContext(formData) {
  const fields = [
    ["PartyPosition", reviewState.partyPosition],
    ["Jurisdiction", reviewState.jurisdiction],
    ["TransactionContext", reviewState.transactionContext],
    ["ReviewScope", reviewState.reviewScope],
  ];
  fields.forEach(([name, value]) => {
    if (value) formData.append(name, value);
  });
}

async function submitReviewSync() {
  if (!validateReview()) return;
  const btn = $("#btn-review-sync");
  btn.disabled = true;
  const wrap = $("#review-result");
  wrap.innerHTML = "";
  const progressCard = el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "审查中…" }),
    el("div", { class: "progress" }, [el("div", { style: "width:100%;animation:shimmer 1.2s infinite" })]),
    el("p", { class: "muted mt-8", text: "正在解析合同、检测印章、召回规则并调用语义模型，耗时取决于文件大小与页数" }),
  ]);
  wrap.appendChild(progressCard);
  try {
    const fd = new FormData();
    reviewState.files.forEach((f) => fd.append("files", f));
    fd.append("PackageId", reviewState.packageId);
    if (reviewState.contractType) fd.append("ContractType", reviewState.contractType);
    appendReviewContext(fd);
    const resp = await api.upload("/contract-review", fd);
    reviewState.result = resp;
    wrap.innerHTML = "";
    renderReviewResult(resp);
  } catch (e) {
    wrap.innerHTML = "";
    toast(`审查失败: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function submitReviewAsync() {
  if (!validateReview()) return;
  const btn = $("#btn-review-async");
  btn.disabled = true;
  try {
    const fd = new FormData();
    reviewState.files.forEach((f) => fd.append("files", f));
    fd.append("PackageId", reviewState.packageId);
    if (reviewState.contractType) fd.append("ContractType", reviewState.contractType);
    appendReviewContext(fd);
    const resp = await api.upload("/contract-review-async", fd);
    const taskId = resp?.Response?.task_id;
    toast(`异步审查任务已创建: ${taskId}`, "ok");
    openTaskDetail(taskId);
  } catch (e) {
    toast(`创建任务失败: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

function renderReviewResult(resp) {
  const wrap = $("#review-result");
  if (!wrap) return;
  wrap.innerHTML = "";
  renderReviewViews(wrap, resp);
}

/** 把审查结果渲染进指定容器（主页面 / 任务结果模态框共用） */
function renderReviewViews(wrap, resp) {
  const rr = resp?.review_result;
  if (!rr) {
    wrap.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "审查结果" }),
      el("div", { class: "json-view", html: escapeHtml(JSON.stringify(resp, null, 2)) }),
    ]));
    return;
  }
  reviewState.result = resp;

  // 缓存命中提示：输入未变化时后端直接复用上次结果，未重新分析
  if (resp?.cached) {
    wrap.appendChild(el("div", { class: "cache-hit" }, [
      el("span", { text: "✓ 输入未变化，已直接复用上次审查结果（未重新分析，结果与上次一致）" }),
    ]));
  }

  const report = rr.report || {};
  const run = rr.run || {};
  const evidence = rr.evidence || [];
  const documents = rr.documents || [];
  const overall = report.overall_status || "UNKNOWN";
  const fm = FINDING_META[overall] || FINDING_META.UNKNOWN;

  // 单一核心清单：所有展示项都由 ReviewResult.findings 派生而来。
  const items = reviewItems(resp);
  const blocked = items.some((it) => itemVerdict(it) === "不符");
  const verdictCounts = { 符合: 0, 不符: 0, 待确认: 0 };
  items.forEach((item) => { verdictCounts[itemVerdict(item)] += 1; });

  // 摘要
  const overallCls = fm.cls === "red" ? "red" : fm.cls === "yellow" ? "yellow" : fm.cls === "green" ? "green" : "gray";
  const leadSub = {
    red: "存在拦截级风险，建议人工复核后再签署",
    yellow: "存在需关注项，建议逐条核对",
    green: "整体通过，未发现风险项",
    gray: "部分项无法判定，需人工确认",
  }[overallCls] || "";
  const lead = stat("总体结论", fm.label, overallCls, el("span", { class: "lead-sub", text: leadSub }));
  lead.classList.add("stat-lead", overallCls);
  const summary = el("div", { class: "grid grid-4" }, [
    lead,
    stat("规则项", String(items.length), "blue", el("span", { class: "lead-sub", text: `不符 ${verdictCounts["不符"]} · 待确认 ${verdictCounts["待确认"]} · 符合 ${verdictCounts["符合"]}` })),
    stat("需人工复核", blocked ? "是" : "否", blocked ? "orange" : "green"),
    stat("审查指纹", run.result_fingerprint ? shortId(run.result_fingerprint) : "-", "gray", el("span", { class: "muted", text: `规则 ${run.rule_version || "-"} · 模型 ${run.model_version || "未启用"}` })),
  ]);
  wrap.appendChild(summary);

  // 模型判定合同类型：仅在用户未声明类型时展示（用户输入为主），
  // 判定值不参与本次审查的规则筛选，提示用户补录后重新审查。
  const detectedType = rr.risk_analysis_response?.contract_type;
  const declaredType = (rr.review_context?.contract_type || "").trim();
  if (detectedType?.name && !declaredType) {
    wrap.appendChild(el("div", { class: "cache-hit" }, [
      el("span", { text: `模型判定合同类型：${detectedType.name}（依据：${detectedType.basis || "合同摘录"}）。本次审查未按类型筛选规则；填写合同类型后重新审查，结论会更精确。` }),
    ]));
  }

  // 标准要素回填入口：字段值全部来自本次审查已产生的 contract_element:* 事实。
  wrap.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-title" }, [
      el("span", { text: "标准要素回填" }),
      el("span", { class: "hint", text: "在「合同拟定」页填写、复制字段值" }),
    ]),
    el("div", { class: "action-bar" }, [
      el("button", {
        class: "btn btn-secondary",
        text: "在合同拟定中打开要素表单",
        onclick: () => {
          closeModal();
          navigate("draft");
          loadElementForm(rr);
        },
      }),
    ]),
  ]));

  // 文件清单
  if (documents.length) {
    const parseBadge = (status) => {
      const cls = status === "parsed" ? "green" : status === "needs_ocr" ? "orange" : "red";
      const label = status === "parsed" ? "已解析" : status === "needs_ocr" ? "需 OCR" : (status || "-");
      return el("span", { class: `badge ${cls}`, text: label });
    };
    const docCard = el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "合同文件" }, [el("span", { class: "hint", text: `${documents.length} 份` })]),
      el("div", { class: "doc-list" }, documents.map((d) => el("div", { class: "doc-row" }, [
        el("div", {}, [
          el("div", { class: "doc-name", text: d.filename || "-" }),
          el("div", { class: "doc-meta" }, [
            el("span", { class: "badge gray", text: fileFormat(d.filename) }),
            parseBadge(d.parse_status),
            el("span", { class: "muted", text: d.page_count ? `${d.page_count} 页` : "页数未知" }),
          ]),
        ]),
        el("span", { class: "mono muted", text: d.source_sha256 ? d.source_sha256.slice(0, 12) + "…" : "-" }),
      ]))),
    ]);
    wrap.appendChild(docCard);
  }

  wrap.appendChild(buildReviewPanels(items, rr, run, report, evidence, documents));
}

const REVIEW_PANELS = [
  ["内控", "对照企业合规阈值拦截缺失或不合规条款，可插入调整建议"],
  ["合理性", "按审查等级分成需要明确的点、需要关注点、风险点与陷阱"],
  ["风险点", "财务风险与项目风险指标，如利润率、资金、预算、收款进度"],
  ["资信", "对方经营状况、合作历史、当前合同与征信提示"],
];

function itemText(item) {
  return [item.module, item.category, item.metric, item.title, item.reason, item.section].filter(Boolean).join(" ");
}

function itemModule(item) {
  if (REVIEW_PANELS.some(([name]) => name === item.module)) return item.module;
  const text = itemText(item);
  if (/利润率|资金要求|项目预算|收款进度|财务风险|项目风险/.test(text)) return "风险点";
  if (/资信|征信|注册资本|合作历史|经营状况|诉讼/.test(text)) return "资信";
  if (/采购需求|需要明确|需要关注|风险陷阱|风险点与陷阱|信创|国产化|云架构|数据治理|范围膨胀/.test(text)) return "合理性";
  if (/质保|检验期限|发票类型|强制性标准|合法性|骑缝章/.test(text)) return "内控";
  if (/合同类型判定/.test(text)) return "风险点";
  return "内控";
}

const reviewWorkspace = {
  showPanel() {},
  openControl() {},
  controlVerdict: "不符",
};

function buildReviewPanels(items, rr, run, report, evidence, documents, options = {}) {
  const grouped = {};
  REVIEW_PANELS.forEach(([name]) => { grouped[name] = []; });
  items.forEach((item) => grouped[itemModule(item)].push(item));
  let current = "内控";
  const host = el("div", { class: "review-panel-body ppt-panel-body" });
  const tabs = el("div", { class: "tabs review-tabs ppt-tabs" }, REVIEW_PANELS.map(([name]) =>
    el("button", { type: "button", class: "tab", "data-panel": name, text: name })
  ));

  const renderPanel = (name) => {
    current = name;
    tabs.querySelectorAll(".tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-panel") === current);
    });
    host.innerHTML = "";
    // v1 同款：内控是全量规则的核对清单（含符合项），合理性/风险点用全量
    // 做指标卡片，资信只看归入该栏的条目。
    const panelItems = (name === "内控" || name === "合理性" || name === "风险点")
      ? items
      : (grouped[name] || []);
    host.appendChild(buildPanelContent(name, panelItems, rr, run, report, evidence, documents));
  };

  reviewWorkspace.showPanel = renderPanel;

  tabs.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => renderPanel(btn.getAttribute("data-panel")));
  });
  renderPanel(current);

  if (options.compact) {
    return el("div", { class: "ppt-review-shell" }, [tabs, host]);
  }
  return el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "AI 审查" }, [
      el("span", { class: "hint", text: `共 ${items.length} 条规则` }),
    ]),
    tabs,
    host,
    el("div", { class: "muted mt-8", text: `合同包: ${rr.package?.package_id || "-"} · 审查状态: ${statusText(run.status)} · 生成时间: ${fmtTime(report.generated_at)}` }),
  ]);
}

function controlItemDomId(item) {
  const key = item.rule_id || item.risk_id || controlNavTitle(item);
  return `control-item-${String(key).replace(/[^\w\u4e00-\u9fff-]+/g, "_")}`;
}

function buildPanelContent(name, items, rr, run, report, evidence, documents) {
  if (name === "内控") return buildControlPanel(items);
  if (name === "合理性") return buildReasonablenessPanel(items);
  if (name === "风险点") return buildRiskPointPanel(items);
  if (name === "资信") return buildCreditPanel(items);
  if (!items.length) return el("p", { class: "muted", text: `「${name}」暂无命中项` });
  const evById = {};
  (evidence || []).forEach((ev) => (evById[ev.evidence_id] = ev));
  const docById = {};
  (documents || []).forEach((d) => (docById[d.document_id] = d));
  return el("div", {}, items.map((item) => buildFinding(item, evById, docById)));
}

function groupByCategory(items, fallback) {
  const grouped = {};
  items.forEach((item) => {
    const key = item.category || fallback;
    grouped[key] = grouped[key] || [];
    grouped[key].push(item);
  });
  return grouped;
}

function controlNavTitle(item) {
  return (item.title || "未命名条款")
    .replace(/^合同类型判定[:：]\s*/, "")
    .replace(/^(?:第?\d+(?:\.\d+)*[、.\s]*)+/, "")
    .trim() || "未命名条款";
}

function isContractQuote(text) {
  const value = String(text || "").trim();
  if (!value) return false;
  if (/^未(定位到合同原文|检索到相关原文|明确约定该项)/.test(value)) return false;
  if (/需结合报价和履约安排补充/.test(value)) return false;
  if (/AI 依据合同原文判定本合同类型/.test(value)) return false;
  if (/^规则来源[：:]/.test(value)) return false;
  return true;
}

function controlQuote(item) {
  return isContractQuote(item.quote) ? String(item.quote).trim() : "";
}

function controlAdvice(item) {
  return String(item.suggested_action || item.recommended_action || "").trim() || "暂无调整建议";
}

/** 当前语境下的"重新审查"：拟定页弹窗用表单附件，审查页用页面附件。 */
function rerunActiveReview() {
  if (currentPage === "draft" && typeof submitFillReview === "function" && elementsState.files.length) {
    submitFillReview();
    return;
  }
  submitReviewSync();
}

function buildControlBody(item) {
  return el("div", { class: "ppt-acc-body" }, [
    el("div", { class: "ppt-field" }, [
      el("div", { class: "ppt-field-label", text: "原文" }),
      el("div", { class: "ppt-quote", text: controlQuote(item) || "" }),
    ]),
    el("div", { class: "ppt-field" }, [
      el("div", { class: "ppt-field-label", text: "建议" }),
      el("div", { class: "ppt-advice", text: controlAdvice(item) }),
    ]),
    el("div", { class: "ppt-control-actions" }, [
      el("button", { class: "ppt-btn primary", text: "插入调整", onclick: () => copyText(controlAdvice(item)) }),
      el("button", { class: "ppt-btn", text: "插入评论", onclick: () => copyText(`【内控】${controlNavTitle(item)}\n原文：${controlQuote(item)}\n建议：${controlAdvice(item)}`) }),
      el("button", { class: "ppt-btn", text: "重新审查", onclick: rerunActiveReview }),
    ]),
  ]);
}

function controlItems(items) {
  return items.filter((item) => !/^合同类型判定/.test(item.title || ""));
}

function buildControlRuleList(items) {
  if (!items.length) return el("p", { class: "muted", text: "该分类暂无规则。" });
  const list = el("div", { class: "ppt-acc" });
  let opened = -1;
  const rows = items.map((item, index) => {
    const verdict = itemVerdict(item);
    const vm = VERDICT_META[verdict] || VERDICT_META["待确认"];
    const head = el("button", {
      type: "button",
      class: "ppt-acc-head",
      onclick: () => {
        opened = opened === index ? -1 : index;
        rows.forEach((row, i) => row.classList.toggle("open", i === opened));
      },
    }, [
      el("span", { class: "ppt-acc-caret", text: "▸" }),
      el("span", { text: `${index + 1}. ${controlNavTitle(item)}` }),
    ]);
    const row = el("div", { class: `ppt-acc-item verdict-${vm.cls}`, id: controlItemDomId(item) }, [head, buildControlBody(item)]);
    return row;
  });
  rows.forEach((row) => list.appendChild(row));
  if (rows[0]) rows[0].classList.add("open");
  return list;
}

function buildControlPanel(items) {
  const source = controlItems(items);
  if (!source.length) return el("p", { class: "muted", text: "本次审查没有规则。" });
  const grouped = {};
  VERDICT_ORDER.forEach((name) => { grouped[name] = []; });
  source.forEach((item) => grouped[itemVerdict(item)].push(item));
  let current = reviewWorkspace.controlVerdict;
  if (!VERDICT_META[current] || !grouped[current]?.length) {
    current = VERDICT_ORDER.find((name) => grouped[name].length) || VERDICT_ORDER[0];
  }
  const listHost = el("div", { class: "ppt-control-list" });
  const tabs = el("div", { class: "tabs ppt-tabs ppt-control-tabs" }, VERDICT_ORDER.map((name) => {
    const vm = VERDICT_META[name];
    return el("button", {
      type: "button",
      class: `tab verdict-tab ${vm.cls}`,
      "data-verdict": name,
      text: `${name} ${grouped[name].length}`,
    });
  }));

  const renderVerdict = (name) => {
    current = name;
    reviewWorkspace.controlVerdict = name;
    tabs.querySelectorAll(".tab").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-verdict") === current);
    });
    listHost.innerHTML = "";
    listHost.appendChild(buildControlRuleList(grouped[current] || []));
  };

  tabs.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => renderVerdict(btn.getAttribute("data-verdict")));
  });
  renderVerdict(current);
  return el("div", { class: "ppt-control-groups" }, [tabs, listHost]);
}

const RISK_POINT_KEYS = ["利润率", "资金要求", "项目预算", "收款进度"];
const CREDIT_METRIC_KEYS = ["注册资本", "合作历史", "当前合同", "经营状况"];

function extraRiskItems(items) {
  const seen = new Set(RISK_POINT_KEYS);
  return items.filter((item) => {
    if ((item.risk_level || item.status) === "UNKNOWN") return false;
    if (/^合同类型判定/.test(item.title || "")) return false;
    const key = metricKey(item);
    if (seen.has(key) || CREDIT_METRIC_KEYS.includes(key)) return false;
    const text = itemText(item);
    const extra = key === "履行期限" || key === "权属范围"
      || /期限|签订时间|权属|既有软件|知识产权|履约|违约金|陷阱/.test(text);
    if (!extra && (item.risk_level || "").toUpperCase() !== "BLOCK") return false;
    seen.add(key);
    return true;
  });
}

function riskPointEntries(items) {
  return [
    ...RISK_POINT_KEYS.map((key) => ({ key, item: pickMetric(items, key) })),
    ...extraRiskItems(items).map((item) => ({ key: metricKey(item), item })),
  ];
}

function reasonablenessBucket(item) {
  if (isRiskPointItem(item)) return "风险点与陷阱";
  const level = (item.risk_level || item.status || "INFO").toUpperCase();
  if (level === "WARN") return "需要关注点";
  return "需要明确的点";
}

function isRiskPointItem(item) {
  return RISK_POINT_KEYS.includes(metricKey(item)) || extraRiskItems([item]).length > 0;
}

function linkedControlItem(item, allItems) {
  if (!item) return null;
  const keys = new Set([item.rule_id, item.risk_id, controlNavTitle(item)].filter(Boolean));
  return controlItems(allItems || []).find((candidate) => {
    const candidateKeys = [candidate.rule_id, candidate.risk_id, controlNavTitle(candidate)].filter(Boolean);
    return candidateKeys.some((key) => keys.has(key));
  }) || (itemModule(item) === "内控" ? item : null);
}

function reasonablenessLine(item, title, allItems) {
  const name = title || item.metric || item.title || "";
  const text = item.value && item.value !== name ? `${item.value}。${item.reason || ""}` : (item.reason || "");
  const control = linkedControlItem(item, allItems || []);
  const line = el("li", { class: control ? "ppt-reason-link" : "" }, [
    el("b", { text: name.replace(/^[\d.、]+\s*/, "") }),
    el("span", { text }),
    control ? el("span", { class: "ppt-link-hint", text: `内控 · ${itemVerdict(control)}` }) : null,
  ]);
  if (control) {
    line.addEventListener("click", () => reviewWorkspace.openControl(control));
  }
  return line;
}

function buildReasonablenessPanel(items) {
  const order = ["需要明确的点", "需要关注点", "风险点与陷阱"];
  const grouped = { 需要明确的点: [], 需要关注点: [], 风险点与陷阱: [] };
  const riskKeys = new Set(riskPointEntries(items).map((entry) => entry.key));
  items
    .filter((item) => (item.risk_level || item.status) !== "UNKNOWN")
    .filter((item) => !/^合同类型判定/.test(item.title || ""))
    .filter((item) => !riskKeys.has(metricKey(item)))
    .forEach((item) => grouped[reasonablenessBucket(item)].push(item));
  return el("div", { class: "ppt-reason" }, order.map((name) =>
    el("div", { class: "ppt-reason-col" }, [
      el("div", { class: "ppt-reason-head", text: name }),
      el("ol", { class: "ppt-reason-ol" }, name === "风险点与陷阱"
        ? riskPointEntries(items).map(({ key, item }) => reasonablenessLine(item, key, items))
        : grouped[name].map((item) => reasonablenessLine(item, undefined, items))),
    ])
  ));
}

function metricKey(item) {
  const text = `${item.metric || ""}${item.title || ""}${item.reason || ""}`;
  if (/利润/.test(text)) return "利润率";
  if (/资金/.test(text)) return "资金要求";
  if (/预算/.test(text)) return "项目预算";
  if (/收款|进度/.test(text)) return "收款进度";
  if (/期限|签订时间|履约/.test(text)) return "履行期限";
  if (/权属|知识产权|既有软件|源代码/.test(text)) return "权属范围";
  if (/注册资本/.test(text)) return "注册资本";
  if (/合作历史/.test(text)) return "合作历史";
  if (/当前合同/.test(text)) return "当前合同";
  if (/经营/.test(text)) return "经营状况";
  return item.metric || item.title || "风险项";
}

function metricIconSvg(name) {
  const svgs = {
    利润率: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3a9 9 0 1 1-9 9h9V3z"/><path d="M12 3a9 9 0 0 1 9 9"/></svg>',
    资金要求: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18"/><circle cx="8" cy="14" r="1" fill="currentColor"/><circle cx="12" cy="14" r="1" fill="currentColor"/></svg>',
    项目预算: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 19h16M7 16V10M12 16V7M17 16v-4"/></svg>',
    收款进度: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 16l5-4 4 3 7-7"/><path d="M15 8h5v5"/></svg>',
    履行期限: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="8"/><path d="M12 8v5l3 2"/></svg>',
    权属范围: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3l8 4v6c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V7z"/></svg>',
    注册资本: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="8" r="3"/><path d="M5 19c1.5-3 4-5 7-5s5.5 2 7 5"/></svg>',
    合作历史: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="8" cy="8" r="3"/><circle cx="16" cy="8" r="3"/><path d="M3 19c1-3 3.5-5 5-5M21 19c-1-3-3.5-5-5-5M8 14c1.2 0 3 .6 4 2"/></svg>',
    当前合同: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M7 3h8l4 4v14H7z"/><path d="M15 3v5h5"/></svg>',
    经营状况: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 19h16M6 16V9l4 3 4-6 4 5v5"/></svg>',
  };
  const wrap = el("span", { class: "ppt-metric-icon" });
  wrap.innerHTML = svgs[name] || svgs["当前合同"];
  return wrap;
}

function pickMetric(items, key) {
  return items.find((item) => metricKey(item) === key) || { title: key, metric: key, reason: "合同未明确约定该项，需结合报价和履约安排补充。" };
}

function metricCardText(item, name) {
  const value = (item.value || "").trim();
  const reason = (item.reason || "").trim();
  if (value && reason && value !== name && !reason.includes(value)) return `${value}。${reason}`;
  return reason || value || "";
}

function buildInsightCard(item, key, allItems) {
  const name = key || metricKey(item);
  const control = linkedControlItem(item, allItems || []);
  const card = el("div", { class: `ppt-metric-card${control ? " ppt-metric-link" : ""}` }, [
    el("div", { class: "ppt-metric-head" }, [
      metricIconSvg(name),
      el("span", { text: name }),
      control ? el("span", { class: `badge ${VERDICT_META[itemVerdict(control)].cls}`, text: itemVerdict(control) }) : null,
    ]),
    el("p", { text: metricCardText(item, name) }),
    control ? el("div", { class: "ppt-link-hint", text: `对应内控：${controlNavTitle(control)}` }) : null,
  ]);
  if (control) {
    card.addEventListener("click", () => reviewWorkspace.openControl(control));
  }
  return card;
}

function donutChart(slices) {
  const colors = ["#73a0fa", "#67e0a3"];
  const total = slices.reduce((sum, item) => sum + item.value, 0) || 1;
  const r = 54;
  const c = 2 * Math.PI * r;
  let offset = 0;
  const rings = slices.map((slice, i) => {
    const len = (slice.value / total) * c;
    const circle = `<circle cx="70" cy="70" r="${r}" fill="none" stroke="${colors[i % colors.length]}" stroke-width="22" stroke-dasharray="${len} ${c - len}" stroke-dashoffset="${-offset}" transform="rotate(-90 70 70)"/>`;
    offset += len;
    return circle;
  }).join("");
  const chart = el("div", { class: "ppt-donut" });
  chart.innerHTML = `<svg viewBox="0 0 140 140">${rings}</svg>`;
  const legend = el("div", { class: "ppt-donut-legend" }, slices.map((slice, i) =>
    el("div", { class: "ppt-legend-item" }, [
      el("span", { class: "ppt-swatch", style: `background:${colors[i % colors.length]}` }),
      el("span", { text: slice.label }),
    ])
  ));
  return el("div", { class: "ppt-donut-wrap" }, [chart, legend]);
}

function buildRiskPointPanel(items) {
  const financeKeys = ["利润率", "资金要求"];
  const projectKeys = ["项目预算", "收款进度"];
  const finance = financeKeys.map((key) => pickMetric(items, key));
  const project = projectKeys.map((key) => pickMetric(items, key));
  const extra = extraRiskItems(items);
  return el("div", { class: "ppt-risk" }, [
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "财务风险" }),
      el("div", { class: "ppt-metric-row" }, finance.map((item, i) => buildInsightCard(item, financeKeys[i], items))),
    ]),
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "项目风险" }),
      el("div", { class: "ppt-risk-split" }, [
        el("div", { class: "ppt-metric-col" }, [
          ...project.map((item, i) => buildInsightCard(item, projectKeys[i], items)),
          ...extra.map((item) => buildInsightCard(item, metricKey(item), items)),
        ]),
        donutChart([
          { label: "项目预算", value: 58 },
          { label: "收款进度", value: 42 },
        ]),
      ]),
    ]),
  ]);
}

function buildCreditPanel(items) {
  const creditKeys = ["合作历史", "当前合同"];
  const businessKeys = ["注册资本", "经营状况"];
  const credit = creditKeys.map((key) => pickMetric(items, key));
  const business = businessKeys.map((key) => pickMetric(items, key));
  return el("div", { class: "ppt-credit" }, [
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "客户资信风险" }),
      el("div", { class: "ppt-metric-row" }, credit.map((item, i) => buildInsightCard(item, creditKeys[i], items))),
    ]),
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "经营风险" }),
      el("div", { class: "ppt-risk-split" }, [
        el("div", { class: "ppt-metric-col" }, business.map((item, i) => buildInsightCard(item, businessKeys[i], items))),
        donutChart([
          { label: "注册资本", value: 58 },
          { label: "经营状况", value: 42 },
        ]),
      ]),
    ]),
  ]);
}

function buildAiRiskItem(item, index) {
  const lv = AI_LEVEL_META[item.risk_level] || AI_LEVEL_META.INFO;
  return el("div", { class: `ai-item ${(item.risk_level || "info").toLowerCase()}` }, [
    el("div", { class: "ai-title" }, [
      el("span", { class: "ai-index", text: String(index + 1) }),
      el("span", { class: `badge ${lv.cls}`, text: lv.label }),
      el("span", { text: item.section ? `${item.section} ${item.title}` : item.title }),
    ]),
    el("div", { class: "ai-reason", text: item.reason }),
  ]);
}

function buildFilteredRiskCard(items, rr, run, report, options = {}) {
  const levelOrder = [["BLOCK", "重大风险", "red"], ["WARN", "需关注", "yellow"], ["INFO", "提示", "blue"], ["UNKNOWN", "未知", "gray"]];
  const levelCounts = {};
  items.forEach((it) => { const k = it.risk_level || "INFO"; levelCounts[k] = (levelCounts[k] || 0) + 1; });
  const present = levelOrder.filter(([k]) => levelCounts[k]);
  let currentFilter = present[0] ? present[0][0] : "";

  const hint = el("span", { class: "hint" });
  const listHost = el("div", { class: "risk-list" });
  const chips = el("div", { class: "risk-bar" }, [
    el("button", { type: "button", class: "risk-bar-item gray", "data-level": "" }, [
      el("b", { text: String(items.length) }),
      el("span", { text: "全部" }),
    ]),
    ...present.map(([k, label, cls]) => el("button", { type: "button", class: `risk-bar-item ${cls}`, "data-level": k }, [
      el("b", { text: String(levelCounts[k]) }),
      el("span", { text: label }),
    ])),
  ]);

  const applyFilter = (next) => {
    currentFilter = next;
    const shown = currentFilter ? items.filter((it) => (it.risk_level || "INFO") === currentFilter) : items;
    const label = present.find(([k]) => k === currentFilter)?.[1];
    hint.textContent = currentFilter
      ? `${label} ${shown.length} / ${items.length} 项`
      : `共 ${items.length} 项`;
    chips.querySelectorAll(".risk-bar-item").forEach((btn) => {
      btn.classList.toggle("active", (btn.getAttribute("data-level") || "") === currentFilter);
    });
    listHost.innerHTML = "";
    if (!shown.length) {
      listHost.appendChild(el("p", { class: "muted", text: options.emptyText || "该等级暂无风险项" }));
      return;
    }
    shown.forEach((item, index) => listHost.appendChild(buildAiRiskItem(item, index)));
  };

  chips.querySelectorAll(".risk-bar-item").forEach((btn) => {
    btn.addEventListener("click", () => applyFilter(btn.getAttribute("data-level") || ""));
  });
  applyFilter(currentFilter);

  const card = el("div", { class: options.hideMeta ? "panel-risks" : "card" }, [
    options.hideMeta ? null : el("div", { class: "card-title", text: "风险清单" }, [hint]),
    chips,
    listHost,
    options.hideMeta ? null : el("div", { class: "muted mt-8", text: `合同包: ${rr.package?.package_id || "-"} · 审查状态: ${statusText(run.status)} · 生成时间: ${fmtTime(report.generated_at)}` }),
  ]);
  return card;
}

function buildFinding(f, evById, docById) {
  const statusCls = (f.status || "UNKNOWN").toLowerCase().replace(/_/g, "_");
  const div = el("div", { class: `finding ${statusCls}` }, [
    el("div", { class: "finding-head" }, [
      badge(f.status, FINDING_META),
      badge(f.risk_level, RISK_META),
      el("span", { class: "finding-title", text: f.title }),
      el("span", { class: "finding-rule", text: f.rule_id }),
    ]),
    el("div", { class: "finding-reason", text: f.reason }),
    f.recommended_action ? el("div", { class: "finding-meta", style: "margin-bottom:6px" }, [
      el("span", { text: "建议: " + f.recommended_action }),
    ]) : null,
    el("div", { class: "finding-meta" }, [
      el("span", { text: `置信度: ${f.confidence != null ? Math.round(f.confidence * 100) + "%" : "-"}` }),
      el("span", { text: `证据 ${(f.evidence_ids || []).length} 条` }),
      el("span", { text: `规则版本: ${f.rule_version || "-"}` }),
    ]),
    (f.evidence_ids || []).map((eid) => {
      const ev = evById[eid];
      if (!ev) return el("div", { class: "evidence-box" }, [el("div", { class: "ev-head", text: eid }), el("p", { class: "muted", text: "（证据不存在）" })]);
      const doc = ev.document_id ? docById[ev.document_id] : null;
      const loc = ev.locator || {};
      const pos = [];
      if (doc) pos.push(doc.filename);
      if (loc.page_number) pos.push(`第 ${loc.page_number} 页`);
      if (loc.bbox) pos.push(`坐标 (${Math.round(loc.bbox.x1)}, ${Math.round(loc.bbox.y1)})`);
      return el("div", { class: "evidence-box" }, [
        el("div", { class: "ev-head" }, [
          el("span", { text: `${ev.evidence_type || "evidence"} · ${eid.slice(0, 8)}` }),
          pos.length ? el("span", { text: " · " + pos.join(" · ") }) : null,
        ]),
        ev.display_excerpt ? el("blockquote", { text: ev.display_excerpt }) : null,
        ev.raw_excerpt && ev.raw_excerpt !== ev.display_excerpt ? el("blockquote", { text: ev.raw_excerpt.slice(0, 300) + (ev.raw_excerpt.length > 300 ? "…" : "") }) : null,
      ]);
    }),
  ]);
  return div;
}

/* ============================================================
 * 合同拟定（v1 版式：合同附件 + 要素表单 + 抽取弹窗）
 * ============================================================ */

const ELEMENT_SOURCE_META = {
  rule: { label: "规则抽到", cls: "green" },
  ai: { label: "AI 补全", cls: "blue" },
  merged: { label: "规则+AI", cls: "orange" },
  empty: { label: "未抽到", cls: "gray" },
};

// v1「合同拟定」页的状态：附件、抽取结果、字段目录与原文预览。
let elementsState = {
  files: [],
  packageId: newPackageId(),
  form: null,
  catalog: null,
  overrides: {},
  previewUrl: null,
  previewHtml: "",
  previewText: "",
  previewKind: "",
  previewMessage: "",
};

function renderDraftPage(content) {
  content.appendChild(pageHead("CONTRACT FORM", "合同拟定", "按标准版合同管理：先上传合同附件，再做要素抽取、合同审查和文档对比"));
  content.appendChild(el("div", { class: "card" }, [
    el("div", { class: "flex", style: "flex-wrap:wrap;gap:8px;align-items:center" }, [
      el("button", { class: "btn btn-primary", text: "要素抽取", onclick: openExtractDialog }),
      el("button", { class: "btn btn-secondary", text: "合同审查", onclick: openFillReviewDialog }),
      el("button", { class: "btn btn-ghost", text: "客商风险", onclick: openCreditRiskDialog }),
      el("button", { class: "btn btn-ghost", text: "文档对比", onclick: openCompareDialog }),
      el("span", { class: "muted", id: "elements-status", text: elementsStatusText() }),
    ]),
  ]));
  content.appendChild(el("div", { id: "contract-fill-form" }, [
    el("div", { class: "card" }, [el("p", { class: "muted", text: "加载合同表单…" })]),
  ]));
  renderElementForm();
  ensureElementCatalog();
}

function elementsStatusText() {
  const fields = elementsState.form?.fields || [];
  if (!fields.length) return "先添加合同附件，再点功能按钮";
  const filled = fields.filter((item) => item.value).length;
  return `共 ${fields.length} 项要素，已抽到 ${filled} 项`;
}

function updateElementsStatus() {
  const node = $("#elements-status");
  if (node) node.textContent = elementsStatusText();
}

/* ---------------- 合同信息表单 ---------------- */

function renderElementForm() {
  const host = $("#contract-fill-form");
  if (!host) return;
  host.innerHTML = "";
  const items = elementFormItems();
  host.appendChild(el("div", { class: "card contract-form-card" }, [
    el("div", { class: "card-title", text: "合同信息" }, [
      el("span", { class: "hint", text: "点开输入框后，抽出的内容在下方下拉列表中竖排显示" }),
      el("span", { class: "grow" }),
      items.some((item) => item.value)
        ? el("button", { class: "link-btn", text: "填入全部抽取结果", onclick: fillAllExtracted })
        : null,
    ]),
    el("div", { class: "contract-form-grid" }, [
      buildAttachmentField(),
      ...items.map(buildInlineFillField),
    ]),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", text: "保存当前填写", onclick: confirmElementsFill }),
      el("button", { class: "btn btn-ghost", text: "复制 JSON", onclick: () => copyText(JSON.stringify(collectElementValues(), null, 2)) }),
    ]),
  ]));
  renderElementsFiles();
  updateElementsStatus();
}

/** 有抽取结果用结果字段；没有就按字段目录生成空表单（v1 行为）。 */
function elementFormItems() {
  const fields = elementsState.form?.fields || [];
  if (fields.length) {
    return fields.map((item) => ({
      key: item.key,
      label: item.label,
      value: item.value || "",
      source: item.source || "empty",
      confidence: typeof item.confidence === "number" ? item.confidence : null,
      quote: item.quote || "",
      hint: item.hint || "",
      required: !!item.required,
      candidates: (item.candidates || []).filter((value) => value && value !== item.value),
    }));
  }
  return (elementsState.catalog?.fields || [])
    .filter((item) => item.enabled !== false)
    .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
    .map((item) => ({
      key: item.key,
      label: item.label,
      value: "",
      source: "empty",
      confidence: null,
      quote: "",
      hint: item.hint || "",
      required: !!item.required,
      candidates: [],
    }));
}

async function ensureElementCatalog() {
  if (elementsState.catalog) return;
  try {
    elementsState.catalog = await api.get("/contract-review/element-fields");
  } catch {
    // 目录拿不到时保持"加载合同表单…"占位，不阻塞已抽取结果的渲染。
    return;
  }
  if (!elementsState.form) renderElementForm();
}

function buildAttachmentField() {
  const input = el("input", {
    type: "file",
    class: "hidden",
    id: "elements-file-input",
    multiple: true,
    accept: ".pdf,.doc,.docx,.xlsx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    onchange: (e) => {
      setElementFiles(e.target.files);
      e.target.value = "";
    },
  });
  const picker = el("div", { class: "attachment-control", id: "elements-dropzone" }, [
    el("button", {
      type: "button",
      class: "btn btn-ghost btn-sm",
      text: "添加附件",
      onclick: (event) => { event.preventDefault(); input.click(); },
    }),
    el("span", { class: "muted", id: "elements-dz-hint", text: "支持 PDF / DOCX / XLSX" }),
    input,
    el("div", { class: "attachment-files", id: "elements-files" }),
  ]);
  picker.addEventListener("dragover", (e) => { e.preventDefault(); picker.classList.add("dragover"); });
  picker.addEventListener("dragleave", () => picker.classList.remove("dragover"));
  picker.addEventListener("drop", (e) => {
    e.preventDefault();
    picker.classList.remove("dragover");
    if (e.dataTransfer.files.length) setElementFiles(e.dataTransfer.files);
  });
  return el("div", { class: "contract-field" }, [
    el("label", { class: "contract-label" }, [el("span", { class: "req", text: "*" }), " 合同附件"]),
    picker,
  ]);
}

function setElementFiles(files, options = {}) {
  const incoming = [...files];
  const replace = options.replace === true || incoming.length === 0;
  const append = !replace && options.append !== false && elementsState.files.length > 0;
  elementsState.files = append ? mergeFiles(elementsState.files, incoming) : incoming;
  if (!append) elementsState.packageId = newPackageId();
  resetElementPreview();
  renderElementsFiles();
}

function renderElementsFiles() {
  const wrap = $("#elements-files");
  if (!wrap) return;
  wrap.innerHTML = "";
  const hint = $("#elements-dz-hint");
  const dz = $("#elements-dropzone");
  const count = elementsState.files.length;
  if (hint) hint.textContent = count ? `已添加 ${count} 个文件` : "支持 PDF / DOCX / XLSX";
  if (dz) dz.classList.toggle("has-files", count > 0);
  if (!count) return;
  for (const file of elementsState.files) {
    wrap.appendChild(el("span", { class: "file-chip" }, [
      el("span", { text: `${file.name}（${fmtBytes(file.size)}）` }),
      el("span", {
        class: "remove",
        text: "✕",
        onclick: (event) => {
          event.preventDefault();
          event.stopPropagation();
          setElementFiles(
            elementsState.files.filter((item) => fileKey(item) !== fileKey(file)),
            { replace: true },
          );
        },
      }),
    ]));
  }
}

/** v1 的候选取值：本次抽到的值排最前，其余候选值依次排在后面。 */
function candidateValues(item) {
  const values = [];
  const seen = new Set();
  if (item.value) {
    values.push(item.value);
    seen.add(item.value);
  }
  for (const value of item.candidates || []) {
    const text = String(value || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    values.push(text);
  }
  return values;
}

/** 抽取值来源说明，挂在输入框与候选项的悬浮提示上，不改变 v1 的版式。 */
function elementFieldHint(item, value) {
  if (!value) return "";
  const meta = ELEMENT_SOURCE_META[item.source] || ELEMENT_SOURCE_META.empty;
  const parts = [`来源：${meta.label}`];
  if (typeof item.confidence === "number") parts.push(`置信度：${item.confidence.toFixed(2)}`);
  if (item.quote) parts.push(`依据：${item.quote}`);
  return parts.join("\n");
}

function buildInlineFillField(item) {
  const suggestions = candidateValues(item);
  const input = el("input", {
    class: "input fill-input",
    "data-element-key": item.key,
    value: elementsState.overrides[item.key] ?? "",
    placeholder: "请输入",
    title: suggestions.length ? elementFieldHint(item, suggestions[0]) : "",
    oninput: (e) => { elementsState.overrides[item.key] = e.target.value; },
  });
  const menu = suggestions.length
    ? el("div", { class: "fill-dropdown hidden" }, suggestions.map((value) =>
        el("button", {
          type: "button",
          class: "fill-dropdown-item",
          text: value,
          title: elementFieldHint(item, value),
          onmousedown: (event) => event.preventDefault(),
          onclick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            input.value = value;
            elementsState.overrides[item.key] = value;
            menu.classList.add("hidden");
            input.focus();
          },
        })
      ))
    : null;
  const hideMenu = () => { if (menu) menu.classList.add("hidden"); };
  const showMenu = () => { if (menu) menu.classList.remove("hidden"); };
  if (menu) {
    input.addEventListener("focus", showMenu);
    input.addEventListener("click", showMenu);
    input.addEventListener("blur", () => setTimeout(hideMenu, 120));
  }
  const box = el("div", { class: "in-input-box" }, [input, menu]);
  box.addEventListener("mousedown", (event) => {
    if (event.target === box) {
      event.preventDefault();
      input.focus();
    }
  });
  return el("div", { class: "contract-field" }, [
    el("label", { class: "contract-label", text: item.label }),
    box,
  ]);
}

function collectElementValues() {
  const values = {};
  document.querySelectorAll("[data-element-key]").forEach((node) => {
    values[node.getAttribute("data-element-key")] = node.value.trim();
  });
  return values;
}

function confirmElementsFill() {
  const values = collectElementValues();
  copyText(JSON.stringify(values, null, 2));
  toast("已确认要素，JSON 已复制，可填充到合同模块", "ok");
}

/** 一键把抽到的值填进输入框，避免 17 个字段逐个点开下拉。 */
function fillAllExtracted() {
  const items = elementFormItems().filter((item) => item.value || item.candidates.length);
  for (const item of items) {
    const value = item.value || item.candidates[0];
    elementsState.overrides[item.key] = value;
    const node = document.querySelector(`[data-element-key="${item.key}"]`);
    if (node) node.value = value;
  }
  toast(`已填入 ${items.length} 个字段，可继续手工修改`, "ok");
}

/* ---------------- 合同审查（页内完成，用表单里已添加的附件） ---------------- */

function requireContractFiles(action) {
  if (!elementsState.files.length) {
    toast(`请先在合同信息中添加合同附件后再${action}`, "warn");
    return false;
  }
  return true;
}

function openFillReviewDialog() {
  if (!requireContractFiles("合同审查")) return;
  const body = el("div", { class: "review-dialog" }, [
    el("div", { id: "review-result" }),
  ]);
  openModal("合同审查", body, { wide: true });
  if (reviewState.result?.review_result) {
    renderReviewViews($("#review-result"), reviewState.result);
  } else {
    renderFillReviewStart();
  }
}

/** v1 的「开始审查」表单：合同包 ID + 合同类型，直接用已添加的附件。 */
function renderFillReviewStart() {
  const wrap = $("#review-result");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("div", {}, [
    el("p", { class: "muted", text: "对照正式规则包审查本合同，结果按内控 / 合理性 / 风险点 / 资信展示；要素字段同时会回填到合同拟定表单。" }),
    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "合同包 ID" }),
        el("input", {
          class: "input",
          id: "fill-review-pkg",
          value: elementsState.packageId,
          oninput: (e) => (elementsState.packageId = e.target.value.trim()),
        }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "合同类型" }),
        el("select", { class: "select", onchange: (e) => (reviewState.contractType = e.target.value) },
          CONTRACT_TYPES.map((type) => el("option", { value: type, text: type || "— 不指定 —", selected: type === reviewState.contractType ? "" : null }))),
      ]),
    ]),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", id: "btn-fill-review", text: "开始审查", onclick: submitFillReview }),
    ]),
  ]));
}

async function submitFillReview() {
  if (!requireContractFiles("合同审查")) return;
  const btn = $("#btn-fill-review");
  if (btn) btn.disabled = true;
  const wrap = $("#review-result");
  if (wrap) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "审查中…" }),
      el("p", { class: "muted", text: "正在解析合同、检测印章、召回规则并调用模型，耗时取决于文件大小与页数" }),
    ]));
  }
  try {
    const resp = await runDraftReview();
    renderReviewViews(wrap, resp);
    toast("审查完成，要素字段已回填到合同信息", "ok");
  } catch (e) {
    if (wrap) wrap.innerHTML = "";
    renderReviewError(wrap, e.message);
    toast(`审查失败: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderReviewError(wrap, message, title = "审查失败") {
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-title", text: title }),
    el("p", { class: "muted", text: message || "未知错误" }),
  ]));
}

/** 用表单里的附件跑一次审查，并同步刷新要素表单（合同审查 / 客商风险共用）。 */
async function runDraftReview() {
  const fd = new FormData();
  elementsState.files.forEach((file) => fd.append("files", file));
  fd.append("PackageId", elementsState.packageId);
  if (reviewState.contractType) fd.append("ContractType", reviewState.contractType);
  const resp = await api.upload("/contract-review", fd);
  reviewState.result = resp;
  elementsState.form = await api.post("/contract-review/element-form", {
    review_result: resp.review_result,
  });
  elementsState.overrides = {};
  renderElementForm();
  return resp;
}

/* ---------------- 客商风险 ---------------- */

const CREDIT_CHECK_META = {
  RISK: { label: "有风险", cls: "red" },
  CLEAR: { label: "已核验通过", cls: "green" },
  UNKNOWN: { label: "待人工确认", cls: "yellow" },
  UNAVAILABLE: { label: "待接入数据源", cls: "gray" },
};

const ELEMENT_SOURCE_LABEL = {
  rule: "规则抽到",
  ai: "AI 补全",
  merged: "规则+AI",
  empty: "未抽到",
};

async function openCreditRiskDialog() {
  // 合同拟定页没有审查结果时，用表单里的附件直接跑一次（v1 行为）；
  // 合同审查页则要求先完成审查，避免在这里重复触发一次耗时审查。
  if (!reviewState.result?.review_result) {
    if (currentPage === "draft") {
      if (!requireContractFiles("查看客商风险")) return;
    } else if (!requireReviewResult("查看客商风险")) {
      return;
    }
  }
  const body = el("div", { class: "review-dialog" }, [
    el("p", { class: "muted", text: "客商风险对接合同主体与资信规则。有企业征信平台时，可将甲方/乙方送去核验。" }),
    el("div", { id: "credit-dialog-body" }, [el("p", { class: "muted", text: "正在汇总客商风险…" })]),
  ]);
  openModal("客商风险", body, { wide: true });
  const host = $("#credit-dialog-body");
  try {
    if (!reviewState.result?.review_result) await runDraftReview();
    const view = await api.post("/contract-review/credit-risk", {
      review_result: reviewState.result.review_result,
    });
    renderCreditRiskView(host, view);
  } catch (e) {
    if (host) {
      host.innerHTML = "";
      host.appendChild(el("p", { class: "muted", text: "客商风险分析失败：" + (e?.message || e) }));
    }
    toast(`客商风险分析失败: ${e?.message || e}`, "err");
  }
}

function renderCreditRiskView(host, view) {
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(el("div", { class: "cache-hit", text: view?.data_source_message || "" }));

  const subjects = view?.subjects || [];
  if (subjects.length) {
    host.appendChild(el("div", { class: "ocr-field-grid mt-8" }, subjects.map((item) => {
      const meta = ELEMENT_SOURCE_LABEL[item.source] || item.source;
      const confidence = typeof item.confidence === "number" ? ` · 置信度 ${item.confidence.toFixed(2)}` : "";
      return el("div", { class: "ocr-field" + (item.value ? "" : " empty") }, [
        el("div", { class: "k", text: item.role }),
        el("div", { class: "v", text: item.value || "未从合同正文识别" }),
        el("div", { class: "muted", text: item.value ? `${meta}${confidence}` : "可在合同信息中人工补录" }),
      ]);
    })));
  }

  const findings = view?.subject_findings || [];
  host.appendChild(el("div", { class: "card-title mt-8" }, [
    el("span", { text: "主体类审查发现" }),
    el("span", { class: "hint", text: `${findings.length} 条` }),
  ]));
  host.appendChild(findings.length
    ? el("div", {}, findings.map((item) => el("div", { class: "credit-finding" }, [
        el("div", { class: "credit-finding-head" }, [
          badge(String(item.status || "UNKNOWN").toUpperCase(), FINDING_META),
          badge(String(item.risk_level || "unclassified"), RISK_META),
          el("span", { text: item.title || item.rule_id }),
        ]),
        el("p", { text: item.reason || "" }),
        item.recommended_action ? el("p", { class: "muted", text: `建议动作：${item.recommended_action}` }) : null,
      ])))
    : el("p", { class: "muted", text: "本次审查未产生合同主体类发现。当前规则包里没有资信/征信类规则。" }));

  const checks = view?.checks || [];
  host.appendChild(el("div", { class: "card-title mt-8" }, [
    el("span", { text: "外部核验项" }),
    el("span", { class: "hint", text: view?.data_source_connected ? "已接入征信数据源" : "企业征信数据源未接入" }),
  ]));
  host.appendChild(el("div", { class: "table-wrap" }, [
    el("table", { class: "table" }, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "核验项" }),
        el("th", { text: "状态" }),
        el("th", { text: "说明" }),
      ])]),
      el("tbody", {}, checks.map((item) => el("tr", {}, [
        el("td", { class: "rule-title", text: item.label }),
        el("td", {}, [badge(String(item.status || "UNAVAILABLE"), CREDIT_CHECK_META)]),
        el("td", { class: "muted", text: item.detail || "" }),
      ]))),
    ]),
  ]));
  host.appendChild(el("p", { class: "muted", text: view?.summary || "" }));
}

/* ---------------- 要素抽取（跑一次审查并回填表单） ---------------- */

function openExtractDialog() {
  if (!elementsState.files.length) {
    toast("请先在合同信息中添加合同附件", "warn");
    return;
  }
  const body = el("div", { class: "extract-workspace" }, [
    el("div", { id: "extract-preview" }),
    el("div", { class: "extract-dialog" }, [
      el("div", { id: "element-schema" }),
      el("div", { class: "form-row" }, [
        el("label", {}, [el("span", { class: "req", text: "*" }), " 合同包 ID"]),
        el("input", { class: "input", id: "elements-pkg", value: elementsState.packageId, oninput: (e) => (elementsState.packageId = e.target.value.trim()) }),
      ]),
      el("div", { class: "action-bar mt-8" }, [
        el("button", { class: "btn btn-primary", id: "btn-elements-sync", text: "开始抽取", onclick: submitElementsSync }),
        el("button", { class: "btn btn-ghost", text: "取消", onclick: closeModal }),
      ]),
    ]),
  ]);
  openModal("要素抽取", body, { wide: true });
  loadElementSchema();
  loadExtractPreview(elementsState.files[0]);
}

/** v1 的可编辑要素口径表：新增 / 编辑 / 停用 / 删除都直接写回目录快照。 */
async function loadElementSchema() {
  const wrap = $("#element-schema");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("p", { class: "muted", text: "加载字段目录…" }));
  let catalog;
  try {
    catalog = await api.get("/contract-review/element-fields");
    elementsState.catalog = catalog;
  } catch (e) {
    wrap.innerHTML = "";
    wrap.appendChild(el("p", { class: "muted", text: "要素定义加载失败：" + (e?.message || e) }));
    return;
  }
  const fields = catalog.fields || [];
  const editable = catalog.editable !== false;
  wrap.innerHTML = "";
  wrap.appendChild(el("div", { class: "schema-block" }, [
    el("div", { class: "card-title" }, [
      el("span", { text: "自定义抽取要素" }),
      el("span", { class: "hint", text: `共 ${fields.length} 项 · 启用后才会抽取` }),
      el("span", { class: "grow" }),
      editable
        ? el("button", { class: "btn btn-primary btn-sm", text: "新增要素", onclick: () => openElementFieldForm(null) })
        : el("span", { class: "badge gray", text: "只读" }),
    ]),
    editable
      ? null
      : el("p", { class: "muted", text: "当前使用内置要素定义（未配置 CONTRACT_ELEMENT_FIELDS_PATH），无法在线编辑。" }),
    el("div", { class: "table-wrap" }, [
      el("table", { class: "table" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "要素名称" }),
          el("th", { text: "字段键" }),
          el("th", { text: "别名/提示词" }),
          el("th", { text: "必填" }),
          el("th", { text: "是否启用" }),
          el("th", { text: "操作" }),
        ])]),
        el("tbody", {}, fields.map((field) => el("tr", {}, [
          el("td", { class: "rule-title", text: field.label }),
          el("td", { class: "mono", text: field.key }),
          el("td", { class: "muted", text: (field.aliases || []).join("、") || "-" }),
          el("td", {}, [field.required ? el("span", { class: "badge orange", text: "是" }) : el("span", { class: "muted", text: "否" })]),
          el("td", {}, [
            el("button", {
              class: "enable-toggle" + (field.enabled ? " on" : ""),
              text: field.enabled ? "是" : "否",
              disabled: editable ? null : "",
              onclick: () => toggleElementField(field, !field.enabled),
            }),
          ]),
          el("td", {}, editable ? [
            el("button", { class: "link-btn", text: "编辑", onclick: () => openElementFieldForm(field) }),
            el("button", { class: "link-btn danger", text: "删除", onclick: () => deleteElementField(field) }),
          ] : [el("span", { class: "muted", text: "-" })]),
        ]))),
      ]),
    ]),
    el("p", { class: "muted", text: `目录 ${catalog.catalog_id} · 抽取器 ${catalog.extractor_version} · 指纹 ${shortId(catalog.fingerprint)}` }),
  ]));
  // 口径变了，主表单的字段清单也要跟着变。
  renderElementForm();
}

function openElementFieldForm(field) {
  const editing = !!(field && field.key);
  const form = el("div", { class: "rule-form" }, [
    el("div", { class: "form-row" }, [
      el("label", {}, [el("span", { class: "req", text: "*" }), " 要素名称"]),
      el("input", { class: "input", id: "el-label", value: field?.label || "", placeholder: "如 质保期" }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "字段键（可空，自动生成）" }),
      el("input", { class: "input", id: "el-key", value: field?.key || "", placeholder: "如 warranty_period", disabled: editing ? "" : null }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "别名/提示词（逗号分隔）" }),
      el("input", { class: "input", id: "el-aliases", value: (field?.aliases || []).join("、"), placeholder: "如 质保期,质量保证期" }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "自定义正则（每行一条，可空）" }),
      el("textarea", { class: "input", id: "el-patterns", placeholder: "如 质保期[:：]\\s*([^\\n]{2,40})" }, (field?.patterns || []).join("\n")),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "填写提示（可空）" }),
      el("input", { class: "input", id: "el-hint", value: field?.hint || "", placeholder: "表单里显示给填写人的说明" }),
    ]),
    el("div", { class: "flex", style: "gap:20px" }, [
      el("label", { class: "muted" }, [
        el("input", { type: "checkbox", id: "el-required", checked: field?.required ? "" : null }),
        " 必填（缺失时列入 missing_required）",
      ]),
      el("label", { class: "muted" }, [
        el("input", { type: "checkbox", id: "el-enabled", checked: !field || field.enabled !== false ? "" : null }),
        " 启用抽取",
      ]),
    ]),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", text: editing ? "保存" : "新增", onclick: () => saveElementField(field) }),
      el("button", { class: "btn btn-ghost", text: "取消", onclick: closeModal }),
    ]),
  ]);
  openModal(editing ? "编辑要素" : "新增要素", form);
}

function elementFieldFormValues() {
  return {
    label: $("#el-label")?.value?.trim() || "",
    key: $("#el-key")?.value?.trim() || "",
    aliases: ($("#el-aliases")?.value || "").split(/[,，、]/).map((item) => item.trim()).filter(Boolean),
    patterns: ($("#el-patterns")?.value || "").split("\n").map((item) => item.trim()).filter(Boolean),
    hint: $("#el-hint")?.value?.trim() || "",
    required: !!$("#el-required")?.checked,
    enabled: !!$("#el-enabled")?.checked,
  };
}

async function saveElementField(field) {
  const values = elementFieldFormValues();
  if (!values.label) { toast("请填写要素名称", "warn"); return; }
  if (!values.aliases.length && !values.patterns.length) {
    toast("至少填写一个别名或一条正则，否则该字段抽不到任何内容", "warn");
    return;
  }
  try {
    if (field?.key) {
      await api.put(`/contract-review/element-fields/${encodeURIComponent(field.key)}`, values);
      toast("要素已更新", "ok");
    } else {
      await api.post("/contract-review/element-fields", values);
      toast("要素已新增", "ok");
    }
    closeModal();
    await loadElementSchema();
  } catch (e) {
    toast("保存失败：" + (e?.message || e), "err");
  }
}

async function toggleElementField(field, enabled) {
  try {
    await api.put(`/contract-review/element-fields/${encodeURIComponent(field.key)}`, { enabled });
    await loadElementSchema();
  } catch (e) {
    toast("操作失败：" + (e?.message || e), "err");
  }
}

async function deleteElementField(field) {
  if (!window.confirm(`确定删除要素「${field.label}」？之后将不再抽取该字段。`)) return;
  try {
    await api.del(`/contract-review/element-fields/${encodeURIComponent(field.key)}`);
    toast("要素已删除", "ok");
    await loadElementSchema();
  } catch (e) {
    toast("删除失败：" + (e?.message || e), "err");
  }
}

async function submitElementsSync() {
  if (!elementsState.files.length) { toast("请至少选择一个合同文件", "warn"); return; }
  if (!elementsState.packageId) { toast("请填写合同包 ID", "warn"); return; }
  const btn = $("#btn-elements-sync");
  if (btn) btn.disabled = true;
  try {
    await runDraftReview();
    closeModal();
    navigate("draft");
    toast("抽取完成，点开输入框可选择填充", "ok");
  } catch (e) {
    toast(`提取失败: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadElementForm(reviewResult) {
  const host = $("#contract-fill-form");
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(el("div", { class: "card" }, [el("p", { class: "muted", text: "正在投影要素表单…" })]));
  try {
    elementsState.form = await api.post("/contract-review/element-form", { review_result: reviewResult });
    elementsState.overrides = {};
    renderElementForm();
  } catch (e) {
    host.innerHTML = "";
    host.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "载入失败" }),
      el("p", { class: "muted", text: e?.message || String(e) }),
      el("p", { class: "muted", text: "若提示结果未登记，请重新做一次要素抽取；服务重启会清空内存中的结果登记。" }),
    ]));
  }
}

/* ---------------- 合同原文预览 ---------------- */

function revokeElementPreview() {
  if (elementsState.previewUrl) {
    URL.revokeObjectURL(elementsState.previewUrl);
    elementsState.previewUrl = null;
  }
}

function resetElementPreview() {
  revokeElementPreview();
  elementsState.previewHtml = "";
  elementsState.previewText = "";
  elementsState.previewKind = "";
  elementsState.previewMessage = "";
}

function setElementPreview(file) {
  resetElementPreview();
  if (file) elementsState.previewUrl = URL.createObjectURL(file);
}

function nativePreviewKind(file) {
  if (!file) return "";
  const name = file.name || "";
  const type = file.type || "";
  if (type === "application/pdf" || /\.pdf$/i.test(name)) return "pdf";
  if (type.startsWith("image/") || /\.(png|jpe?g|gif|bmp|webp)$/i.test(name)) return "image";
  return "";
}

async function loadExtractPreview(file) {
  setElementPreview(file);
  renderExtractPreview();
  if (!file) return;
  const nativeKind = nativePreviewKind(file);
  if (nativeKind) {
    elementsState.previewKind = nativeKind;
    renderExtractPreview();
    return;
  }
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await api.upload("/contract-preview", fd);
    elementsState.previewHtml = resp.html || "";
    elementsState.previewText = resp.text || "";
    elementsState.previewKind = resp.kind || (resp.html ? "html" : "text");
    elementsState.previewMessage = resp.message || "";
    renderExtractPreview();
  } catch (e) {
    elementsState.previewKind = "error";
    elementsState.previewMessage = e.message || "打开合同原文失败";
    renderExtractPreview();
  }
}

function renderExtractPreview() {
  const host = $("#extract-preview");
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(buildContractPreviewPane());
}

function buildContractPreviewPane() {
  const file = elementsState.files[0];
  const url = elementsState.previewUrl;
  const name = file ? file.name : "";
  const kind = elementsState.previewKind || nativePreviewKind(file);
  let viewer;
  if (url && kind === "pdf") {
    viewer = el("iframe", { class: "contract-preview-frame", src: url, title: name || "合同预览" });
  } else if (url && kind === "image") {
    viewer = el("img", { class: "contract-preview-image", src: url, alt: name || "合同预览" });
  } else if (kind === "html" && elementsState.previewHtml) {
    viewer = el("iframe", {
      class: "contract-preview-frame",
      srcdoc: elementsState.previewHtml,
      title: name || "合同预览",
      sandbox: "allow-same-origin",
    });
  } else if (elementsState.previewText) {
    viewer = el("pre", { class: "contract-preview-text", text: elementsState.previewText });
  } else if (file && !kind) {
    viewer = el("div", { class: "contract-preview-empty" }, [
      el("p", { class: "muted", text: "正在打开合同原文…" }),
    ]);
  } else if (elementsState.previewMessage) {
    viewer = el("div", { class: "contract-preview-fallback" }, [
      el("p", { class: "muted", text: elementsState.previewMessage }),
    ]);
  } else {
    viewer = el("div", { class: "contract-preview-empty" }, [
      el("p", { class: "muted", text: "选择合同文件后，原文会在这里打开。" }),
    ]);
  }
  return el("div", { class: "card contract-preview-pane" }, [
    el("div", { class: "card-title", text: "合同原文" }, [
      el("span", { class: "hint", text: name || "未选择文件" }),
    ]),
    viewer,
  ]);
}

/* ============================================================
 * 正式规则包（只读）
 * ============================================================ */

/* ============================================================
 * 规则引擎库（合同检查标准 + AI 自进化规则）
 * ============================================================ */

const RULE_TOPICS = ["合同类型", "金额", "付款", "发票", "源代码相关（按关键字搜索）", "知识产权", "新技术架构描述相关", "合同主体", "合规性/交付问题", "软件开发服务合同（0税率）重点检查项"];
const RULE_PAGE_SIZES = [10, 20, 50];
let aiRulesState = { topics: RULE_TOPICS, pager: {} };
const RULE_LEVEL_TO_V2 = { WARN: "medium", BLOCK: "high", INFO: "low" };
const RULE_LEVEL_FROM_V2 = { medium: "WARN", high: "BLOCK", low: "INFO", critical: "BLOCK" };

async function renderRulesPage(content) {
  content.appendChild(pageHead("RULE ENGINE", "规则引擎库", "每套规则都有两部分：上方规则列表，下方规则引擎按 PPT 评分矩阵展示（权重 + 高/中/低分标准）"));
  content.appendChild(el("div", { id: "ai-rules-list" }));
  await loadAiRules();
}

async function loadAiRules() {
  const wrap = $("#ai-rules-list");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("p", { class: "muted", text: "加载中…" }));
  try {
    const data = await api.get("/contract-review/rules-engine");
    aiRulesState.topics = data.topics && data.topics.length ? data.topics : RULE_TOPICS;
    const packs = data.packs || {};
    const approval = (packs.approval && packs.approval.rules) || [];
    const aiRules = (packs.ai && packs.ai.rules) || [];
    wrap.innerHTML = "";
    wrap.appendChild(buildRulePack({
      mark: "HT",
      title: "合同检查标准",
      hint: `共 ${approval.length} 条`,
      empty: "暂无合同审批规则。服务启动后会写入检查标准，也可手动新增。",
      rules: approval,
      groups: (packs.approval && packs.approval.groups) || groupRulesByTopic(approval),
      engineTitle: "规则引擎",
      engineHint: "按检查维度分类，展示权重和高中低分标准",
      guide: "列表中新增、编辑、启用或删除后立即生效，规则引擎评分矩阵同步更新。",
      defaultTopic: "合规性/交付问题",
      // 固定规则池隐藏「规则内容」列：50/57 条 condition 为空，列内常年是 "-"。
      conditionColumn: null,
    }));
    wrap.appendChild(buildRulePack({
      mark: "AI",
      title: "AI 自进化规则",
      hint: `共 ${aiRules.length} 条 · 审查后由模型提炼，待确认后生效`,
      empty: "暂无 AI 规则。完成一次合同审查后，模型提炼的检查点会出现在这里。",
      rules: aiRules,
      groups: (packs.ai && packs.ai.groups) || groupRulesByTopic(aiRules),
      engineTitle: "AI 规则引擎",
      engineHint: "与上方同一批 AI 规则，按维度展示评分标准",
      guide: "确认启用后进入审查提示池；规则引擎按同一批规则分维度展示。",
      defaultTopic: "其他检查",
      hideCreate: true,
      // AI 规则池保留该列并改名：condition 即模型提炼该检查点的理由，
      // 是审批「是否确认启用」的依据，不能省。
      conditionColumn: "提炼依据",
    }));
  } catch (e) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("p", { class: "muted", text: "加载失败：" + (e?.message || e) }),
    ]));
  }
}

function groupRulesByTopic(rules) {
  const buckets = {};
  for (const rule of rules) {
    const topic = rule.topic || "其他检查";
    buckets[topic] = buckets[topic] || [];
    buckets[topic].push(rule);
  }
  const names = [...aiRulesState.topics.filter((name) => buckets[name]), ...Object.keys(buckets).filter((name) => !aiRulesState.topics.includes(name))];
  return names.map((name) => ({ name, count: buckets[name].length, rules: buckets[name] }));
}

function isRuleEnabled(rule) {
  return rule.enabled !== false;
}

function rulePager(key) {
  if (!aiRulesState.pager[key]) aiRulesState.pager[key] = { page: 1, size: 10 };
  return aiRulesState.pager[key];
}

function pagedSlice(items, pager) {
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / pager.size) || 1);
  if (pager.page > pages) pager.page = pages;
  if (pager.page < 1) pager.page = 1;
  const start = (pager.page - 1) * pager.size;
  return { total, pages, start, rows: items.slice(start, start + pager.size) };
}

function buildTablePager(pager, total, onChange) {
  const pages = Math.max(1, Math.ceil(total / pager.size) || 1);
  const from = total ? (pager.page - 1) * pager.size + 1 : 0;
  const to = Math.min(total, pager.page * pager.size);
  const numbers = [];
  const windowStart = Math.max(1, Math.min(pager.page - 2, pages - 4));
  const windowEnd = Math.min(pages, windowStart + 4);
  for (let i = windowStart; i <= windowEnd; i++) numbers.push(i);
  return el("div", { class: "table-pager" }, [
    el("span", { class: "pager-info", text: `显示 ${from} 到 ${to} 条，共 ${total} 条` }),
    el("div", { class: "pager-controls" }, [
      el("select", {
        class: "pager-size",
        onchange: (e) => { pager.size = Number(e.target.value) || 10; pager.page = 1; onChange(); },
      }, RULE_PAGE_SIZES.map((size) => el("option", { value: String(size), text: `${size}条/页`, selected: pager.size === size ? "" : null }))),
      el("button", { class: "pager-btn", text: "‹", disabled: pager.page <= 1 ? "" : null, onclick: () => { if (pager.page > 1) { pager.page -= 1; onChange(); } } }),
      ...numbers.map((num) => el("button", {
        class: "pager-btn" + (num === pager.page ? " active" : ""),
        text: String(num),
        onclick: () => { pager.page = num; onChange(); },
      })),
      el("button", { class: "pager-btn", text: "›", disabled: pager.page >= pages ? "" : null, onclick: () => { if (pager.page < pages) { pager.page += 1; onChange(); } } }),
    ]),
  ]);
}

function buildRulePack(pack) {
  const listHost = el("div", { class: "rule-table-host" });
  const renderList = () => {
    const pager = rulePager(`${pack.mark}-list`);
    const slice = pagedSlice(pack.rules, pager);
    listHost.innerHTML = "";
    if (!pack.rules.length) {
      listHost.appendChild(el("p", { class: "muted", text: pack.empty }));
      return;
    }
    listHost.appendChild(el("div", { class: "table-wrap" }, [buildApprovalRuleTable(slice.rows, slice.start, pack.conditionColumn)]));
    listHost.appendChild(buildTablePager(pager, slice.total, renderList));
  };
  renderList();
  return el("div", { class: "rule-pack" }, [
    el("div", { class: "card" }, [
      el("div", { class: "card-title" }, [
        el("span", { class: "rule-pack-mark", text: pack.mark }),
        el("span", { text: pack.title }),
        el("span", { class: "hint", text: pack.hint }),
        el("span", { class: "grow" }),
        pack.hideCreate ? null : el("button", { class: "btn btn-primary btn-sm", text: "新增规则", onclick: () => openRuleForm({ topic: pack.defaultTopic }, pack.conditionColumn) }),
        el("button", { class: "btn btn-secondary btn-sm", text: "刷新", onclick: loadAiRules }),
      ]),
      listHost,
    ]),
    buildScoreMatrixCard(pack),
  ]);
}

function buildApprovalRuleTable(rules, start = 0, conditionColumn = "规则内容") {
  // conditionColumn 传假值时整列不渲染：固定规则池 57 条里有 50 条 condition
  // 为 null，该列只会显示 "-"，白占 420px 列宽。AI 规则池必须保留此列，
  // 那里的 condition 是模型提炼该检查点的理由（rule_edits.condition =
  // item.reason 回退 title），是审批「是否确认启用」的唯一依据，故换个列名。
  const head = el("tr", {}, [
    el("th", { class: "col-index", text: "" }),
    el("th", { text: "规则编号" }),
    el("th", { text: "规则名称" }),
    conditionColumn ? el("th", { text: conditionColumn }) : null,
    el("th", { text: "是否启用" }),
    el("th", { text: "操作" }),
  ]);
  const body = rules.map((rule, index) => {
    const enabled = isRuleEnabled(rule);
    return el("tr", {}, [
      el("td", { class: "muted", text: String(start + index + 1) }),
      el("td", { class: "mono", text: rule.code || "-" }),
      el("td", {}, [
        el("div", { class: "rule-title", text: rule.title }),
        rule.status === "draft" ? el("span", { class: "badge orange", text: "待确认" }) : null,
      ]),
      conditionColumn ? el("td", { class: "rule-condition", text: rule.condition || "-" }) : null,
      el("td", {}, [
        el("button", {
          class: "enable-toggle" + (enabled ? " on" : ""),
          text: enabled ? "是" : "否",
          onclick: () => toggleRuleEnabled(rule, !enabled),
        }),
      ]),
      el("td", {}, [
        el("button", { class: "link-btn", text: "编辑", onclick: () => openRuleForm(rule, conditionColumn) }),
        el("button", { class: "link-btn danger", text: "删除", onclick: () => deleteRule(rule) }),
      ]),
    ]);
  });
  return el("table", { class: "table rule-engine-table" }, [
    el("thead", {}, [head]),
    el("tbody", {}, body),
  ]);
}

function flattenScoreRows(groups) {
  const rows = [];
  (groups || []).forEach((group) => {
    (group.rules || []).forEach((rule) => rows.push({ group: group.name, rule }));
  });
  return rows;
}

function regroupScoreRows(rows) {
  const groups = [];
  rows.forEach((row) => {
    const last = groups[groups.length - 1];
    if (!last || last.name !== row.group) groups.push({ name: row.group, rules: [row.rule] });
    else last.rules.push(row.rule);
  });
  return groups;
}

function buildScoreMatrixCard(pack) {
  const host = el("div", { class: "score-matrix-host" });
  const allRows = flattenScoreRows(pack.groups);
  const renderMatrix = () => {
    const pager = rulePager(`${pack.mark}-matrix`);
    const slice = pagedSlice(allRows, pager);
    const groups = regroupScoreRows(slice.rows);
    const rows = [];
    let rowNo = slice.start + 2;
    groups.forEach((group) => {
      (group.rules || []).forEach((rule, index) => {
        const cells = [el("td", { class: "muted col-index", text: String(rowNo) })];
        if (index === 0) {
          cells.push(el("td", { class: "score-group", rowspan: String(group.rules.length), text: group.name }));
        }
        cells.push(
          el("td", { text: rule.title }),
          el("td", { class: "score-weight", text: String(rule.weight ?? 10) }),
          el("td", { class: "score-high", text: rule.high_standard || "-" }),
          el("td", { class: "score-mid", text: rule.mid_standard || "-" }),
          el("td", { class: "score-low", text: rule.low_standard || "-" }),
        );
        rows.push(el("tr", {}, cells));
        rowNo += 1;
      });
    });
    host.innerHTML = "";
    if (!allRows.length) {
      host.appendChild(el("p", { class: "muted", text: "暂无规则可展示。" }));
      return;
    }
    host.appendChild(el("div", { class: "table-wrap" }, [
      el("table", { class: "table score-matrix" }, [
        el("thead", {}, [
          el("tr", { class: "score-letters" }, [
            el("th", { class: "col-index" }),
            el("th", { text: "A" }),
            el("th", { text: "B" }),
            el("th", { text: "C" }),
            el("th", { text: "D" }),
            el("th", { text: "E" }),
            el("th", { text: "F" }),
          ]),
          el("tr", {}, [
            el("th", { class: "col-index", text: "1" }),
            el("th", { text: "检查维度" }),
            el("th", { text: "规则名称" }),
            el("th", { text: "权重" }),
            el("th", { text: "高分标准 (8-10 分)" }),
            el("th", { text: "中等标准 (4-7 分)" }),
            el("th", { text: "低分标准 (0-3 分)" }),
          ]),
        ]),
        el("tbody", {}, rows),
      ]),
    ]));
    host.appendChild(buildTablePager(pager, slice.total, renderMatrix));
  };
  renderMatrix();
  return el("div", { class: "card" }, [
    el("div", { class: "card-title" }, [
      el("span", { text: pack.engineTitle }),
      el("span", { class: "hint", text: `${pack.engineHint} · 共 ${pack.rules.length} 条` }),
    ]),
    el("div", { class: "score-guide" }, [
      el("b", { text: "操作指引" }),
      el("span", { text: pack.guide }),
    ]),
    host,
  ]);
}

// conditionLabel 由规则池决定：AI 池传「提炼依据」，与上方列表列名保持一致；
// 固定规则池列表不显示该列、传 null，弹窗回退默认「规则内容」。
function openRuleForm(rule, conditionLabel) {
  const editing = !!(rule && rule.id);
  const topics = aiRulesState.topics.length ? aiRulesState.topics : RULE_TOPICS;
  const currentTopic = rule?.topic || topics[0];
  const form = el("div", { class: "rule-form" }, [
    el("div", { class: "form-row" }, [
      el("label", { text: "规则名称" }),
      el("input", { class: "input", id: "rule-title", value: rule?.title || "", placeholder: "如 金额大小写一致" }),
    ]),
    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "规则编号（可空，自动生成）" }),
        el("input", { class: "input", id: "rule-code", value: rule?.code || "", placeholder: "如 HTSP-202511-006" }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "检查维度" }),
        el("select", { class: "select", id: "rule-topic" },
          topics.map((name) => el("option", { value: name, text: name, selected: currentTopic === name ? "" : null }))),
      ]),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "风险等级" }),
      el("select", { class: "select", id: "rule-level" },
        [["WARN", "需关注"], ["BLOCK", "重大风险"], ["INFO", "提示"]].map(([v, t]) =>
          el("option", { value: v, text: t, selected: (rule?.risk_level || "WARN") === v ? "" : null }))),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: conditionLabel || "规则内容" }),
      el("textarea", { class: "input", id: "rule-condition", rows: "4", placeholder: "判定条件，例如：合同金额大小写必须一致", text: rule?.condition || "" }),
    ]),
    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "权重" }),
        el("input", { class: "input", id: "rule-weight", type: "number", min: "1", max: "100", value: String(rule?.weight || 10) }),
      ]),
      el("div", { class: "form-row" }, [
        el("label", { text: "建议动作（可空）" }),
        el("input", { class: "input", id: "rule-action", value: rule?.suggested_action || "", placeholder: "如 核对金额大小写" }),
      ]),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "高分标准 (8-10 分)" }),
      el("input", { class: "input", id: "rule-high", value: rule?.high_standard || "", placeholder: "如 约定完整、口径一致" }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "中等标准 (4-7 分)" }),
      el("input", { class: "input", id: "rule-mid", value: rule?.mid_standard || "", placeholder: "如 约定不完整或口径不清" }),
    ]),
    el("div", { class: "form-row" }, [
      el("label", { text: "低分标准 (0-3 分)" }),
      el("input", { class: "input", id: "rule-low", value: rule?.low_standard || "", placeholder: "如 未约定或明显不符" }),
    ]),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", text: editing ? "保存修改" : "创建并启用", onclick: () => saveRuleForm(rule) }),
      el("button", { class: "btn btn-ghost", text: "取消", onclick: closeModal }),
    ]),
  ]);
  openModal(editing ? "编辑规则" : "新增规则", form);
}

async function saveRuleForm(rule) {
  const level = $("#rule-level")?.value || "WARN";
  const payload = {
    rule_id: $("#rule-code")?.value?.trim() || undefined,
    title: $("#rule-title")?.value?.trim(),
    category: $("#rule-topic")?.value,
    check_method: "keyword",
    risk_level: RULE_LEVEL_TO_V2[level] || null,
    condition: $("#rule-condition")?.value?.trim() || null,
    suggested_action: $("#rule-action")?.value?.trim() || null,
    weight: Number($("#rule-weight")?.value || 10),
    high_standard: $("#rule-high")?.value?.trim() || null,
    mid_standard: $("#rule-mid")?.value?.trim() || null,
    low_standard: $("#rule-low")?.value?.trim() || null,
  };
  if (!payload.title) { toast("请填写规则名称", "warn"); return; }
  try {
    if (rule?.id) {
      await api.put(`/contract-review/rules/${encodeURIComponent(rule.id)}`, { payload });
      toast("规则已更新", "ok");
    } else {
      await api.post("/contract-review/rules", { payload });
      toast("规则已创建并启用", "ok");
    }
    closeModal();
    await loadAiRules();
  } catch (e) {
    toast("保存失败：" + (e?.message || e), "err");
  }
}

async function toggleRuleEnabled(rule, enabled) {
  try {
    if (rule.status === "draft") {
      // AI 自进化候选行：开关 = 确认启用（v1 的确认流程）。
      if (!enabled) return;
      await api.post(`/contract-review/rules/${encodeURIComponent(rule.id)}/confirm`, {});
      toast("规则已确认启用，进入合同检查标准", "ok");
    } else {
      await api.post(`/contract-review/rules/${encodeURIComponent(rule.id)}/${enabled ? "enable" : "disable"}`, {});
      toast(enabled ? "规则已启用，下次审查生效" : "规则已停用", "ok");
    }
    await loadAiRules();
  } catch (e) {
    toast("操作失败：" + (e?.message || e), "err");
  }
}

async function deleteRule(rule) {
  if (!window.confirm(`确定删除规则「${rule.title}」？列表和规则引擎会同步移除。`)) return;
  try {
    await api.request("DELETE", `/contract-review/rules/${encodeURIComponent(rule.id)}`);
    toast("规则已删除", "ok");
    await loadAiRules();
  } catch (e) {
    toast("删除失败：" + (e?.message || e), "err");
  }
}

/* ============================================================
 * 任务中心
 * ============================================================ */
let tasksState = { page: 1, size: 15, status: "", taskType: "", timer: null };

function renderTasksPage(content) {
  content.appendChild(pageHead("ASYNC QUEUE", "任务中心", "异步合同审查队列，可按状态过滤并查看核心 ReviewResult"));

  const filterCard = el("div", { class: "card" }, [
    el("div", { class: "flex", style: "flex-wrap:wrap" }, [
      el("select", { class: "select", style: "width:180px", id: "task-status-filter", onchange: (e) => { tasksState.status = e.target.value; tasksState.page = 1; loadTasks(); } },
        [["", "全部状态"], ["PENDING", "排队中"], ["RUNNING", "执行中"], ["SUCCEEDED", "成功"], ["FAILED", "失败"], ["CANCELED", "已取消"], ["EXPIRED", "已过期"]].map(([v, t]) => el("option", { value: v, text: t }))),
      el("select", { class: "select", style: "width:200px", id: "task-type-filter", onchange: (e) => { tasksState.taskType = e.target.value; tasksState.page = 1; loadTasks(); } },
        [["", "全部类型"], ...Object.entries(TASK_TYPE_LABELS)].map(([v, t]) => el("option", { value: v, text: t }))),
      el("span", { class: "grow" }),
      el("button", { class: "btn btn-secondary btn-sm", text: "刷新", onclick: loadTasks }),
    ]),
  ]);
  content.appendChild(filterCard);

  const tableCard = el("div", { class: "card" }, [
    el("div", { class: "table-wrap", id: "tasks-table" }),
    el("div", { class: "flex-between mt-8", id: "tasks-pager" }),
  ]);
  content.appendChild(tableCard);
  loadTasks();
}

async function loadTasks() {
  const tableWrap = $("#tasks-table");
  if (!tableWrap) return;
  tableWrap.innerHTML = Array.from({ length: 5 }, () => el("div", { class: "skeleton" }));
  const pager = $("#tasks-pager");
  if (pager) pager.innerHTML = "";
  try {
    const params = new URLSearchParams({ page: tasksState.page, size: tasksState.size });
    if (tasksState.status) params.set("status", tasksState.status);
    if (tasksState.taskType) params.set("task_type", tasksState.taskType);
    const resp = await api.get(`/tasks?${params}`);
    const data = resp?.Response || {};
    const tasks = data.tasks || [];
    const total = data.total || 0;

    tableWrap.innerHTML = "";
    if (!tasks.length) {
      tableWrap.appendChild(el("div", { class: "empty-state" }, [el("div", { class: "icon", text: "🗂" }), el("div", { text: "暂无任务" })]));
    } else {
      tableWrap.appendChild(el("table", { class: "table" }, [
        el("thead", {}, [el("tr", {}, ["任务 ID", "类型", "状态", "阶段", "进度", "创建时间", "操作"].map((h) => el("th", { text: h })))]),
        el("tbody", {}, tasks.map((t) => {
          const sm = STATUS_META[t.status] || STATUS_META.PENDING;
          const rowCls = t.status === "RUNNING" ? "row-running" : t.status === "FAILED" ? "row-failed" : t.status === "SUCCEEDED" ? "row-succeeded" : "";
          return el("tr", { class: rowCls }, [
            el("td", { class: "mono" }, [el("span", { class: "link-btn", text: shortId(t.task_id), onclick: () => openTaskDetail(t.task_id) })]),
            el("td", { text: TASK_TYPE_LABELS[t.task_type] || t.task_type }),
            el("td", {}, [badge(t.status, STATUS_META)]),
            el("td", { class: "muted text-sm", text: t.stage || "-" }),
            el("td", { style: "min-width:110px" }, [
              el("div", { class: "flex", style: "gap:6px" }, [
                el("div", { class: "progress grow mb-0", style: "margin:0" }, [el("div", { style: `width:${t.progress || 0}%` })]),
                el("span", { class: "muted text-sm nowrap", text: (t.progress || 0) + "%" }),
              ]),
            ]),
            el("td", { class: "muted text-sm nowrap", text: fmtTime(t.created_at) }),
            el("td", {}, [
              el("button", { class: "link-btn", text: "详情", onclick: () => openTaskDetail(t.task_id) }),
              t.status === "SUCCEEDED" ? el("span", {}, [el("span", { class: "muted", text: " · " }), el("button", { class: "link-btn", text: "结果", onclick: () => openTaskResult(t.task_id) })]) : null,
            ]),
          ]);
        })),
      ]));
    }

    // 分页
    pager.innerHTML = "";
    const pages = Math.max(1, Math.ceil(total / tasksState.size));
    pager.appendChild(el("span", { class: "muted text-sm", text: `共 ${total} 条 · 第 ${tasksState.page} / ${pages} 页` }));
    pager.appendChild(el("div", { class: "flex" }, [
      el("button", { class: "btn btn-ghost btn-sm", text: "上一页", disabled: tasksState.page <= 1 ? "" : null, onclick: () => { if (tasksState.page > 1) { tasksState.page--; loadTasks(); } } }),
      el("button", { class: "btn btn-ghost btn-sm", text: "下一页", disabled: tasksState.page >= pages ? "" : null, onclick: () => { if (tasksState.page < pages) { tasksState.page++; loadTasks(); } } }),
    ]));
  } catch (e) {
    tableWrap.innerHTML = "";
    tableWrap.appendChild(el("div", { class: "empty-state" }, [
      el("div", { class: "icon", text: "⚠️" }),
      el("div", { text: "加载失败" }),
      el("p", { class: "muted", text: e.message }),
    ]));
  }
}

let detailTimer = null;

function openTaskDetail(taskId) {
  if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
  const body = el("div", {}, [el("div", { class: "skeleton" }), el("div", { class: "skeleton" })]);
  openModal("任务详情", body);

  const render = (t) => {
    body.innerHTML = "";
    const d = t.Response || t || {};
    const sm = STATUS_META[d.status] || STATUS_META.PENDING;
    const done = ["SUCCEEDED", "FAILED", "CANCELED", "EXPIRED"].includes(d.status);
    body.appendChild(el("div", { class: "kv" }, [
      ["任务 ID", d.task_id], ["类型", TASK_TYPE_LABELS[d.task_type] || d.task_type], ["状态", sm.label],
      ["阶段", d.stage || "-"], ["队列", d.queue_name || "-"],
      ["创建时间", fmtTime(d.created_at)], ["开始时间", fmtTime(d.started_at)], ["完成时间", fmtTime(d.finished_at)],
    ].map(([k, v]) => el("div", { class: "kv-item" }, [el("div", { class: "k", text: k }), el("div", { class: "v", text: String(v ?? "-") })]))));
    body.appendChild(el("div", { class: "progress mt-8" + (d.status === "FAILED" ? " danger" : d.status === "SUCCEEDED" ? " success" : ""), style: "margin-bottom:4px" }, [el("div", { style: `width:${d.progress || 0}%` })]));
    body.appendChild(el("div", { class: "muted text-sm", text: `进度 ${d.progress || 0}%` }));

    if (d.error_message) {
      body.appendChild(el("div", { class: "evidence-box mt-8", style: "border-left:3px solid var(--red)" }, [
        el("div", { class: "ev-head", text: `错误 ${d.error_code || ""}` }),
        el("p", { class: "text-sm", text: d.error_message }),
      ]));
    }
    if (done) {
      body.appendChild(el("div", { class: "flex mt-16" }, [
        d.status === "SUCCEEDED" ? el("button", { class: "btn btn-primary btn-sm", text: "查看结果", onclick: () => openTaskResult(d.task_id) }) : null,
        el("button", { class: "btn btn-secondary btn-sm", text: "复制任务 ID", onclick: () => copyText(d.task_id) }),
        el("button", { class: "btn btn-ghost btn-sm", text: "关闭", onclick: closeModal }),
      ]));
      if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
    } else {
      body.appendChild(el("div", { class: "muted text-sm mt-8", text: "自动刷新中…" }));
      if (!detailTimer) {
        detailTimer = setInterval(async () => {
          try {
            const r = await api.get(`/tasks/${d.task_id}`);
            render(r);
          } catch (e) {
            toast(`查询任务失败: ${e.message}`, "err");
            clearInterval(detailTimer);
            detailTimer = null;
          }
        }, 2000);
      }
    }
  };

  api.get(`/tasks/${taskId}`).then(render).catch((e) => {
    body.innerHTML = "";
    body.appendChild(el("p", { class: "text-sm", text: "查询失败: " + e.message }));
  });
}

async function openTaskResult(taskId) {
  try {
    const resp = await api.get(`/tasks/${taskId}/result`);
    if (resp && resp.review_result) {
      const body = el("div", {});
      openModal("任务结果（合同审查）", body);
      renderReviewViews(body, resp);
      return;
    }
    openModal("任务结果", jsonView(resp));
  } catch (e) {
    toast(`获取结果失败: ${e.message}`, "err");
  }
}

/* ============================================================
 * 初始化
 * ============================================================ */
async function refreshHealth() {
  const dot = $("#health-dot");
  const text = $("#health-text");
  try {
    const h = await api.get("/health");
    api.authHeaderName = h.auth_header_name || DEFAULT_AUTH_HEADER_NAME;
    const ok = h.status === "healthy";
    dot.className = "health-dot " + (ok ? "ok" : "bad");
    text.textContent = `${ok ? "服务正常" : "服务降级"} · ${h.service || ""} v${h.version || ""} · OCR 网关: ${h.ocr_gateway === "connected" ? "已连接" : "未连接"}`;
    $("#brand-version").textContent = "v" + (h.version || "-");
  } catch (e) {
    dot.className = "health-dot bad";
    text.textContent = "无法连接服务: " + e.message;
  }
}

function init() {
  try {
    loadConfigUI();
  } catch (e) {
    console.warn("loadConfigUI failed", e);
  }

  document.querySelectorAll(".nav-item[data-page]").forEach((btn) => {
    btn.addEventListener("click", () => navigate(btn.dataset.page));
  });

  const saveBtn = $("#save-config");
  if (saveBtn) saveBtn.addEventListener("click", saveConfig);
  const apiBase = $("#api-base");
  if (apiBase) apiBase.addEventListener("keydown", (e) => { if (e.key === "Enter") saveConfig(); });
  const tokenInput = $("#token-input");
  if (tokenInput) tokenInput.addEventListener("keydown", (e) => { if (e.key === "Enter") saveConfig(); });

  const closeBtn = $("#modal-close");
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  const mask = $("#modal-mask");
  if (mask) mask.addEventListener("click", (e) => { if (e.target.id === "modal-mask") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

  refreshHealth();
  setInterval(refreshHealth, 30000);
  navigate("review");
}

document.addEventListener("DOMContentLoaded", init);
