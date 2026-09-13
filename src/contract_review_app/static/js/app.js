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
};

function badge(key, meta) {
  const m = meta[key];
  return el("span", { class: `badge ${m ? m.cls : "gray"}`, text: m ? m.label : key });
}

const TASK_TYPE_LABELS = {
  "contract-review": "合同审查",
};

/* ---------------- 导航 ---------------- */
const PAGES = { review: renderReviewPage, rules: renderRulesPage, tasks: renderTasksPage };
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
        el("button", { class: "btn btn-ghost", text: "查看资信风险", onclick: openCreditRiskDialog }),
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


function reviewItems(resp) {
  const rr = resp?.review_result;
  return (rr?.findings || [])
    .filter((finding) => finding.status !== "PASS" && finding.status !== "NOT_APPLICABLE")
    .map((finding) => projectReviewFinding(finding, rr))
    .sort((left, right) => findingSeverity(right.risk_level) - findingSeverity(left.risk_level));
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
  return { BLOCK: 5, WARN: 4, INFO: 3, UNKNOWN: 2, PASS: 1 }[status] || 0;
}

function itemsByModule(resp, module) {
  return reviewItems(resp).filter((item) => itemModule(item) === module);
}

function reviewFactValue(resp, key) {
  return (resp?.review_result?.facts || [])
    .filter((fact) => fact.fact_type === `contract_element:${key}`)
    .map((fact) => fact.value)
    .find((value) => value !== undefined && value !== null && value !== "") || "";
}

function requireReviewResult(action) {
  if (reviewState.result?.review_result) return true;
  toast(`请先完成合同审查后再${action}`, "warn");
  return false;
}

function openCreditRiskDialog() {
  if (!requireReviewResult("查看资信风险")) return;
  const resp = reviewState.result;
  const body = el("div", { class: "review-dialog" }, [
    el("p", { class: "muted", text: "客商主体与资信风险均来自当前 ReviewResult 的事实、发现和证据。" }),
    el("div", { id: "credit-dialog-body" }),
  ]);
  openModal("客商风险", body, { wide: true });
  const host = $("#credit-dialog-body");
  const items = itemsByModule(resp, "资信");
  host.appendChild(el("div", { class: "grid grid-3" }, [
    el("div", { class: "review-fact-field" }, [el("div", { class: "k", text: "甲方" }), el("div", { class: "v", text: reviewFactValue(resp, "party_a") || "未抽取" })]),
    el("div", { class: "review-fact-field" }, [el("div", { class: "k", text: "乙方" }), el("div", { class: "v", text: reviewFactValue(resp, "party_b") || "未抽取" })]),
    el("div", { class: "review-fact-field" }, [el("div", { class: "k", text: "资信风险项" }), el("div", { class: "v", text: String(items.length) })]),
  ]));
  host.appendChild(items.length
    ? el("div", { class: "risk-list mt-8" }, items.map((item, index) => buildAiRiskItem(item, index)))
    : el("p", { class: "muted mt-8", text: "当前 ReviewResult 未发现资信风险提示。" }));
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

function openCompareDialog() {
  if (!requireReviewResult("进行版本比对")) return;
  compareState.result = null;
  if (!compareState.baseFile && reviewState.files[0]) compareState.baseFile = reviewState.files[0];
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
    fd.append("ReviewResultPayload", JSON.stringify(reviewState.result.review_result));
    const resp = await api.upload("/contract-compare", fd);
    compareState.result = resp;
    reviewState.result = { review_result: resp.review_result };
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
  const blocked = items.some((it) => (it.risk_level || it.status) === "BLOCK");

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
    stat("风险项", String(items.length), "blue"),
    stat("需人工复核", blocked ? "是" : "否", blocked ? "orange" : "green"),
    stat("审查指纹", run.result_fingerprint ? shortId(run.result_fingerprint) : "-", "gray", el("span", { class: "muted", text: `规则 ${run.rule_version || "-"} · 模型 ${run.model_version || "未启用"}` })),
  ]);
  wrap.appendChild(summary);

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
    const panelItems = (name === "合理性" || name === "风险点") ? items : (grouped[name] || []);
    host.appendChild(buildPanelContent(name, panelItems, rr, run, report, evidence, documents));
  };

  tabs.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => renderPanel(btn.getAttribute("data-panel")));
  });
  renderPanel(current);

  if (options.compact) {
    return el("div", { class: "ppt-review-shell" }, [tabs, host]);
  }
  return el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "审查发现" }, [
      el("span", { class: "hint", text: `共 ${items.length} 项` }),
    ]),
    tabs,
    host,
    el("div", { class: "muted mt-8", text: `合同包: ${rr.package?.package_id || "-"} · 审查状态: ${run.status || "-"} · 生成时间: ${fmtTime(report.generated_at)}` }),
  ]);
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

function buildControlBody(item) {
  return el("div", { class: "ppt-acc-body" }, [
    el("div", { class: "ppt-field" }, [
      el("div", { class: "ppt-field-label", text: "原文" }),
      el("div", { class: "ppt-quote", text: item.quote || item.reason || "未定位到合同原文" }),
    ]),
    el("div", { class: "ppt-field" }, [
      el("div", { class: "ppt-field-label", text: "建议" }),
      el("div", { class: "ppt-advice", text: item.suggested_action || "暂无调整建议" }),
    ]),
    el("div", { class: "ppt-control-actions" }, [
      el("button", { class: "ppt-btn primary", text: "插入调整", onclick: () => copyText(item.suggested_action || item.reason || "") }),
      el("button", { class: "ppt-btn", text: "插入评论", onclick: () => copyText(`【内控】${controlNavTitle(item)}\n原文：${item.quote || ""}\n建议：${item.suggested_action || item.reason || ""}`) }),
    ]),
  ]);
}

function buildControlPanel(items) {
  if (!items.length) return el("p", { class: "muted", text: "本次审查没有内控命中项。" });
  const list = el("div", { class: "ppt-acc" });
  let opened = -1;
  const rows = items.map((item, index) => {
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
    const row = el("div", { class: "ppt-acc-item" }, [head, buildControlBody(item)]);
    return row;
  });
  rows.forEach((row) => list.appendChild(row));
  return list;
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

function reasonablenessLine(item, title) {
  const name = title || item.metric || item.title || "";
  const text = item.value && item.value !== name ? `${item.value}。${item.reason || ""}` : (item.reason || "");
  return el("li", {}, [
    el("b", { text: name.replace(/^[\d.、]+\s*/, "") }),
    el("span", { text }),
  ]);
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
        ? riskPointEntries(items).map(({ key, item }) => reasonablenessLine(item, key))
        : grouped[name].map((item) => reasonablenessLine(item))),
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

function buildInsightCard(item, key) {
  const name = key || metricKey(item);
  return el("div", { class: "ppt-metric-card" }, [
    el("div", { class: "ppt-metric-head" }, [
      metricIconSvg(name),
      el("span", { text: name }),
    ]),
    el("p", { text: metricCardText(item, name) }),
  ]);
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
      el("div", { class: "ppt-metric-row" }, finance.map((item, i) => buildInsightCard(item, financeKeys[i]))),
    ]),
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "项目风险" }),
      el("div", { class: "ppt-risk-split" }, [
        el("div", { class: "ppt-metric-col" }, [
          ...project.map((item, i) => buildInsightCard(item, projectKeys[i])),
          ...extra.map((item) => buildInsightCard(item, metricKey(item))),
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
      el("div", { class: "ppt-metric-row" }, credit.map((item, i) => buildInsightCard(item, creditKeys[i]))),
    ]),
    el("div", { class: "ppt-risk-block" }, [
      el("div", { class: "ppt-block-title", text: "经营风险" }),
      el("div", { class: "ppt-risk-split" }, [
        el("div", { class: "ppt-metric-col" }, business.map((item, i) => buildInsightCard(item, businessKeys[i]))),
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
    options.hideMeta ? null : el("div", { class: "muted mt-8", text: `合同包: ${rr.package?.package_id || "-"} · 审查状态: ${run.status || "-"} · 生成时间: ${fmtTime(report.generated_at)}` }),
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
 * 正式规则包（只读）
 * ============================================================ */

async function renderRulesPage(content) {
  content.appendChild(pageHead("RULE BUNDLE", "正式规则包", "当前审查使用的版本化 RuleBundle；规则随 ReviewResult 固化，应用层不再维护第二套规则库。"));
  content.appendChild(el("div", { id: "rules-list" }));
  await loadRuleCatalog();
}

async function loadRuleCatalog() {
  const wrap = $("#rules-list");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("p", { class: "muted", text: "加载中…" }));
  try {
    const data = await api.get("/contract-review/rule-bundle");
    const rules = data.rules || [];
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title" }, [
        el("span", { text: data.bundle_id || "正式规则包" }),
        el("span", { class: "hint", text: `共 ${rules.length} 条` }),
        el("span", { class: "grow" }),
        el("span", { class: "badge gray", text: "只读" }),
        el("button", { class: "btn btn-secondary btn-sm", text: "刷新", onclick: loadRuleCatalog }),
      ]),
      data.source_filename ? el("p", { class: "muted", text: `来源：${data.source_filename} · 版本：${data.bundle_id || "-"}` }) : null,
      rules.length ? el("div", { class: "table-wrap" }, [buildFormalRuleTable(rules)]) : el("p", { class: "muted", text: "暂无正式规则。" }),
    ]));
  } catch (e) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("p", { class: "muted", text: "加载失败：" + (e?.message || e) }),
    ]));
  }
}

function buildFormalRuleTable(rules) {
  return el("table", { class: "table rule-engine-table" }, [
    el("thead", {}, [el("tr", {}, [
      el("th", { text: "规则编号" }),
      el("th", { text: "规则名称" }),
      el("th", { text: "分类" }),
      el("th", { text: "检查方式" }),
      el("th", { text: "风险等级" }),
      el("th", { text: "适用范围" }),
      el("th", { text: "人工复核" }),
    ])]),
    el("tbody", {}, rules.map((rule) => el("tr", {}, [
      el("td", { class: "mono", text: rule.rule_id || rule.code || "-" }),
      el("td", {}, [el("div", { class: "rule-title", text: rule.title || "-" }), el("div", { class: "muted", text: rule.condition || "" })]),
      el("td", { text: rule.category || "-" }),
      el("td", { class: "mono", text: rule.check_method || "-" }),
      el("td", { text: rule.risk_level || "UNKNOWN" }),
      el("td", { class: "muted", text: (rule.applies_to || []).join("、") || "全部" }),
      el("td", {}, [rule.human_review ? el("span", { class: "badge orange", text: "是" }) : el("span", { class: "muted", text: "否" })]),
    ]))),
  ]);
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
