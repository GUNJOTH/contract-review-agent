/* ============================================================
 * 合同审查控制台 — 前端逻辑
 * 依赖后端：合同审查智能体（/api/v1），扫描页走 OCR 网关
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

const api = {
  get base() {
    return localStorage.getItem("contract_api_base") || DEFAULT_API_BASE;
  },
  headers(extra = {}) {
    return { ...extra };
  },
  async request(method, path, options = {}) {
    const url = api.base.replace(/\/$/, "") + path;
    const res = await fetch(url, { method, headers: api.headers(options.headers), body: options.body });
    return api.unwrap(res);
  },
  get(path) {
    return api.request("GET", path);
  },
  postJson(path, body) {
    return api.request("POST", path, {
      body: JSON.stringify(body || {}),
      headers: { "Content-Type": "application/json" },
    });
  },
  postForm(path, formData) {
    return api.request("POST", path, { body: formData });
  },
  /** 带上传进度的 multipart 请求（XHR） */
  upload(path, formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", api.base.replace(/\/$/, "") + path);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
      };
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
}

function loadConfigUI() {
  const input = $("#api-base");
  if (!input) return;
  input.value = api.base === DEFAULT_API_BASE ? "" : api.base;
  input.addEventListener("input", applyConfig);
}
function saveConfig() {
  applyConfig();
  toast("配置已保存", "ok");
  refreshHealth();
}

/* ---------------- 轻量 Markdown 渲染 ---------------- */
function renderMarkdown(md) {
  if (!md) return "<p class='muted'>（无内容）</p>";
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let inCode = false;
  let codeBuf = [];
  let inTable = false;
  let tableBuf = [];
  let listType = null;

  const inline = (s) =>
    escapeHtml(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  const closeTable = () => {
    if (!inTable) return;
    const [head, ...rows] = tableBuf;
    const thead = head ? `<thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>` : "";
    const tbody = rows.length ? `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody>` : "";
    html += `<table>${thead}${tbody}</table>`;
    inTable = false;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith("```")) {
      if (inCode) {
        html += `<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`;
        codeBuf = [];
        inCode = false;
      } else {
        closeList(); closeTable();
        inCode = true;
      }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)/);
    if (h) {
      closeList(); closeTable();
      const level = Math.min(h[1].length, 6);
      html += `<h${level}>${inline(h[2])}</h${level}>`;
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      closeTable();
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(line.replace(/^\s*[-*+]\s+/, ""))}</li>`;
      continue;
    }
    if (/^\s*\d+[.、)]\s+/.test(line)) {
      closeTable();
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(line.replace(/^\s*\d+[.、)]\s+/, ""))}</li>`;
      continue;
    }

    if (line.startsWith("|") && line.endsWith("|")) {
      closeList();
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue; // 分隔行
      if (!inTable) { inTable = true; tableBuf = []; }
      tableBuf.push(cells);
      continue;
    }
    closeTable();

    if (/^>+\s?/.test(line)) { html += `<blockquote>${inline(line.replace(/^>+\s?/, ""))}</blockquote>`; continue; }
    if (/^\s*---+$/.test(line)) { html += "<hr/>"; continue; }
    if (line === "") { closeList(); continue; }
    html += `<p>${inline(line)}</p>`;
  }
  closeList(); closeTable();
  if (inCode) html += `<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`;
  return html;
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

function jsonModal(title, data) {
  openModal(title, jsonView(data));
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
  "contract-elements": "合同要素提取",
};

/* ---------------- 导航 ---------------- */
const PAGES = { review: renderReviewPage, "element-fill": renderElementFillPage, "ai-rules": renderAiRulesPage, tasks: renderTasksPage };
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

/* ============================================================
 * 概览页
 * ============================================================ */
async function renderDashboard(content) {
  content.appendChild(pageHead("CONTROL PLANE", "服务概览", "一眼看清网关健康、OCR 引擎连通性和本机算力占用"));

  const hero = el("div", { class: "dash-hero" }, [
    el("div", { class: "dash-hero-left" }, [
      el("span", { class: "hero-pulse" }),
      el("div", {}, [
        el("div", { class: "hero-title", text: "正在探测服务状态…" }),
        el("div", { class: "hero-sub", text: "读取 /health 与硬件指标" }),
      ]),
    ]),
  ]);
  const statGrid = el("div", { class: "grid grid-4" });
  ["状态", "服务版本", "OCR 服务", "请求地址"].forEach(() => statGrid.appendChild(el("div", { class: "skeleton", style: "height:118px;border-radius:14px" })));
  const hwCard = el("div", { class: "card hw-card" }, [el("div", { class: "card-title", text: "硬件资源" }), el("div", { class: "skeleton", style: "height:140px" })]);
  content.appendChild(hero);
  content.appendChild(statGrid);
  content.appendChild(hwCard);

  try {
    const health = await api.get("/health");
    const ok = health.status === "healthy";
    const ocrOk = health.ocr_gateway === "connected";
    hero.classList.toggle("ok", ok);
    hero.classList.toggle("bad", !ok);
    hero.innerHTML = "";
    hero.appendChild(el("div", { class: "dash-hero-left" }, [
      el("span", { class: "hero-pulse" }),
      el("div", {}, [
        el("div", { class: "hero-title", text: ok ? "服务运行正常" : "服务处于降级状态" }),
        el("div", { class: "hero-sub", text: ok
          ? `${health.service || "合同审查智能体"} · OCR 网关 ${ocrOk ? "已连接" : "未连接"}`
          : "OCR 网关未连通，扫描页识别可能不可用" }),
      ]),
    ]));
    hero.appendChild(el("div", { class: "dash-hero-right hero-meta" }, [
      el("span", { class: "muted", text: "最近检查" }),
      el("b", { text: fmtTime(new Date().toISOString()) }),
    ]));

    statGrid.innerHTML = "";
    statGrid.appendChild(stat("服务状态", ok ? "正常" : "降级", ok ? "green" : "red", el("span", { class: `badge ${ok ? "green" : "red"}`, text: ok ? "healthy" : "degraded" })));
    statGrid.appendChild(stat("服务版本", health.version || "-", "blue", el("span", { class: "muted", text: health.service || "合同审查智能体" })));
    statGrid.appendChild(stat("OCR 网关", ocrOk ? "已连接" : "未连接", ocrOk ? "green" : "red", el("span", { class: "muted", text: health.ocr_gateway_url || "OCR Gateway" })));
    statGrid.appendChild(stat("API 地址", api.base, "gray", el("a", { class: "link-btn", href: "/docs", target: "_blank", text: "打开 API 文档" })));

    const hw = await api.get("/hardware");
    renderHardware(hwCard, hw);
  } catch (e) {
    hero.classList.add("bad");
    hero.innerHTML = "";
    hero.appendChild(el("div", { class: "dash-hero-left" }, [
      el("span", { class: "hero-pulse" }),
      el("div", {}, [
        el("div", { class: "hero-title", text: "无法获取服务状态" }),
        el("div", { class: "hero-sub", text: e.message }),
      ]),
    ]));
    statGrid.innerHTML = "";
    hwCard.innerHTML = "";
    hwCard.appendChild(el("div", { class: "card-title", text: "硬件资源" }));
    hwCard.appendChild(el("p", { class: "muted", text: "请检查 API 地址与 Token 配置（右上角），并确认服务已启动。" }));
  }
}

function stat(label, value, color = "blue", sub = null) {
  return el("div", { class: `stat tone-${color}` }, [
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value", style: `color: var(--${color})`, text: value }),
    sub ? el("div", { class: "stat-sub" }, [sub]) : null,
  ]);
}

function kvGrid(rows) {
  return el("div", { class: "kv" }, rows.map(([k, v]) =>
    el("div", { class: "kv-item" }, [
      el("div", { class: "k", text: k }),
      el("div", { class: "v" + (v == null || v === "" ? " empty" : ""), text: v == null || v === "" ? "未获取" : String(v) }),
    ])
  ));
}

function usageTone(pct) {
  if (pct > 85) return "red";
  if (pct > 60) return "yellow";
  return "green";
}

function gauge(pct, tone) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const color = { red: "var(--red)", yellow: "var(--yellow)", green: "var(--green)", blue: "var(--blue)" }[tone] || "var(--green)";
  const node = el("div", { class: "gauge" }, [
    el("div", { class: "gauge-ring" }),
    el("div", { class: "gauge-center", text: Math.round(p) + "%" }),
  ]);
  node.style.setProperty("--p", String(p));
  node.style.setProperty("--tone", color);
  return node;
}

function hwTile(title, pct, lines) {
  const tone = usageTone(pct);
  return el("div", { class: "hw-tile" }, [
    gauge(pct, tone),
    el("div", { class: "hw-tile-body" }, [
      el("div", { class: "hw-tile-title", text: title }),
      ...lines.map((line) => el("div", { class: "muted", text: line })),
    ]),
  ]);
}

function renderHardware(card, res) {
  card.innerHTML = "";
  card.appendChild(el("div", { class: "card-title", text: "硬件资源" }, [el("span", { class: "hint", text: "实时占用" })]));
  const hw = res?.hardware || {};
  if (!hw || Object.keys(hw).length === 0) {
    card.appendChild(el("p", { class: "muted", text: "暂无硬件数据" }));
    return;
  }
  const grid = el("div", { class: "hw-grid" });
  card.appendChild(grid);

  const cpu = hw.cpu || {};
  const cpuPct = Number(cpu.usage_percent ?? 0);
  const cpuLines = [`${cpu.count ?? "-"} 逻辑核 / ${cpu.physical_count ?? "-"} 物理核`];
  if (cpu.frequency_mhz?.current) cpuLines.push(`${(cpu.frequency_mhz.current / 1000).toFixed(2)} GHz`);
  grid.appendChild(hwTile("CPU", cpuPct, cpuLines));

  const mem = hw.memory || {};
  const memPct = mem.total_gb ? Math.round((mem.used_gb / mem.total_gb) * 100) : 0;
  grid.appendChild(hwTile("内存", memPct, [`已用 ${mem.used_gb ?? "-"} GB`, `共 ${mem.total_gb ?? "-"} GB`]));

  (hw.gpu || []).forEach((g) => {
    const use = g.utilization_percent ?? 0;
    const lines = [`显存 ${g.memory?.used_gb ?? "-"} / ${g.memory?.total_gb ?? "-"} GB`];
    if (g.temperature_celsius != null) lines.push(`温度 ${g.temperature_celsius}°C`);
    grid.appendChild(hwTile(g.name || `GPU ${g.id ?? "-"}`, use, lines));
  });

  const disk = hw.disk;
  if (disk && Object.keys(disk).length) {
    const pct = disk.usage_percent ?? 0;
    grid.appendChild(hwTile("磁盘 /", pct, [`已用 ${disk.used_gb ?? "-"} GB`, `共 ${disk.total_gb ?? "-"} GB`]));
  }
}

/* ============================================================
 * OCR 识别页
 * ============================================================ */
const OCR_TYPES = {
  "id-card": {
    label: "身份证识别",
    short: "证",
    tone: "blue",
    desc: "姓名/性别/民族/出生日期/身份证号/地址/有效期",
    fields: [["Name", "姓名"], ["Sex", "性别"], ["Nation", "民族"], ["Birth", "出生日期"], ["IdNum", "身份证号"], ["Address", "地址"], ["Authority", "发证机关"], ["ValidDate", "有效期"]],
    endpoint: "/id-card",
    taskType: "id-card",
  },
  "business-license": {
    label: "营业执照识别",
    short: "照",
    tone: "purple",
    desc: "公司名称/统一社会信用代码/法定代表人/注册资本/经营范围",
    fields: [["RegNum", "统一社会信用代码"], ["Name", "公司名称"], ["Person", "法定代表人"], ["Capital", "注册资本"], ["Type", "主体类型"], ["Period", "营业期限"], ["SetDate", "成立日期"], ["Address", "地址"], ["Business", "经营范围"], ["RegistrationAuthority", "登记机关"], ["RegistrationDate", "登记日期"], ["SerialNumber", "编号"]],
    endpoint: "/business-license",
    taskType: "business-license",
  },
  "law-firm-license": {
    label: "律所执业许可证",
    short: "所",
    tone: "cyan",
    desc: "律师事务所名称/信用代码/负责人/合伙人/组织形式",
    fields: [["RegNum", "统一社会信用代码"], ["Name", "律所名称"], ["Person", "负责人"], ["Partner", "合伙人"], ["OrgType", "组织形式"], ["Capital", "设立资产"], ["Address", "住所"], ["Authority", "主管机关"], ["ApprovalNumber", "批准文号"], ["ApprovalDate", "批准日期"], ["IssuingAuthority", "发证机关"], ["IssueDate", "发证日期"]],
    endpoint: "/law-firm-license",
    taskType: "law-firm-license",
  },
  "mainland-permit": {
    label: "港澳台通行证",
    short: "通",
    tone: "orange",
    desc: "姓名/证件号/证件类别/签发机关/有效期",
    fields: [["Name", "姓名"], ["EnglishName", "英文姓名"], ["Sex", "性别"], ["Birthday", "出生日期"], ["Number", "证件号"], ["Type", "证件类别"], ["IssueAuthority", "签发机关"], ["ValidDate", "有效期"], ["IssueAddress", "签发地点"], ["IssueNumber", "签发次数"], ["Nationality", "国籍"]],
    endpoint: "/mainland-permit",
    taskType: "mainland-permit",
  },
  "seal": {
    label: "印章识别",
    short: "印",
    tone: "red",
    desc: "公司名称/印章类型/防伪编码/形状（VL 优先，自动降级）",
    fields: [["CompanyName", "公司名称"], ["SealBody", "印章主体"], ["SealType", "印章类型"], ["SerialNumber", "防伪编码"], ["SealShape", "形状"], ["IsSquare", "是否方形"], ["Source", "识别来源"]],
    endpoint: "/seal",
    taskType: "seal",
    seal: true,
  },
  "general-ocr": {
    label: "通用 OCR",
    short: "文",
    tone: "green",
    desc: "自由文本识别，返回全部文本行",
    fields: [],
    endpoint: "/general-ocr",
    taskType: "general-basic-ocr",
    raw: true,
  },
  "general-basic-ocr": {
    label: "通用印刷体",
    short: "体",
    tone: "gray",
    desc: "腾讯云格式：文本行 + 置信度 + 坐标",
    fields: [],
    endpoint: "/general-basic-ocr",
    taskType: "general-basic-ocr",
    textDetections: true,
  },
  "license-plate": {
    label: "车牌识别",
    short: "牌",
    tone: "yellow",
    desc: "车牌号码/颜色/字符置信度",
    fields: [],
    endpoint: "/license-plate",
    taskType: "license-plate",
    plate: true,
  },
  "pdf-extract": {
    label: "PDF 智能提取",
    short: "PDF",
    tone: "blue",
    desc: "文字版 PDF 直接提取 Markdown；扫描版标记需 OCR 页码",
    fields: [],
    endpoint: "/pdf-extract",
    taskType: null,
    pdf: true,
  },
};

const COMMON_OPTIONS = ["EnablePdf", "PdfPageNumber", "MergeSplitPages", "UseVL"];
const OPTION_META = {
  EnablePdf: { label: "开启 PDF 识别", default: true },
  PdfPageNumber: { label: "PDF 页码", default: 1 },
  MergeSplitPages: { label: "自动拼接拆分页", default: true },
  UseVL: { label: "VL 优先", default: true },
};

let ocrState = {
  type: "id-card",
  files: [],
  mode: "file", // file | url | base64
  url: "",
  base64: "",
  options: { EnablePdf: true, PdfPageNumber: 1, MergeSplitPages: true, UseVL: true },
};

function renderOcrPage(content) {
  content.appendChild(pageHead("VISION PIPELINE", "OCR 识别", "选类型、丢文件，同步拿结果或丢进任务中心排队"));

  // 类型选择
  const typeCard = el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "识别类型" }, [el("span", { class: "hint", text: "点一张卡片开始" })]),
    el("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px" }, Object.entries(OCR_TYPES).map(([key, t]) =>
      el("button", {
        class: `type-card ${ocrState.type === key ? "selected" : ""}`,
        "data-type": key,
        onclick: () => selectType(key),
      }, [
        el("div", { class: `type-mark tone-${t.tone || "blue"}`, text: t.short || t.label.slice(0, 1) }),
        el("div", {}, [
          el("div", { class: "type-label", text: t.label }),
          el("div", { class: "type-desc", text: t.desc }),
        ]),
      ])
    )),
  ]);
  content.appendChild(typeCard);

  // 输入区
  const inputCard = el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "输入文件" }),
    buildDropzone(),
    el("div", { class: "tabs" }, [
      el("button", { class: `tab ${ocrState.mode === "file" ? "active" : ""}`, text: "本地上传", onclick: () => setInputMode("file") }),
      el("button", { class: `tab ${ocrState.mode === "url" ? "active" : ""}`, text: "图片 URL", onclick: () => setInputMode("url") }),
      el("button", { class: `tab ${ocrState.mode === "base64" ? "active" : ""}`, text: "Base64", onclick: () => setInputMode("base64") }),
    ]),
    el("div", { id: "ocr-url-wrap", class: "hidden" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "图片 / PDF / DOC 的 URL 地址" }),
        el("input", { class: "input", id: "ocr-url", placeholder: "https://example.com/image.jpg", value: ocrState.url, oninput: (e) => (ocrState.url = e.target.value) }),
      ]),
    ]),
    el("div", { id: "ocr-b64-wrap", class: "hidden" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "Base64 编码内容（可含 data: 前缀）" }),
        el("textarea", { class: "textarea", id: "ocr-b64", rows: 5, placeholder: "粘贴 Base64…", oninput: (e) => (ocrState.base64 = e.target.value.trim()) }),
      ]),
    ]),
    el("div", { id: "ocr-preview", class: "mt-8" }),
    el("div", { id: "ocr-options", class: "mt-16" }),
  ]);
  content.appendChild(inputCard);

  // 提交区
  const submitCard = el("div", { class: "card" }, [
    el("div", { class: "action-bar" }, [
      el("div", { class: "flex", style: "flex-wrap:wrap" }, [
        el("button", { class: "btn btn-primary", id: "btn-ocr-sync", text: "同步识别", onclick: submitOcrSync }),
        el("button", { class: "btn btn-secondary", id: "btn-ocr-async", text: "提交异步任务", onclick: submitOcrAsync }),
        el("span", { class: "muted", text: "同步保持连接直到完成；异步经 Celery 执行" }),
      ]),
      el("span", { class: "muted", id: "ocr-upload-progress" }),
    ]),
  ]);
  content.appendChild(submitCard);

  // 结果区
  content.appendChild(el("div", { id: "ocr-result" }));

  renderOcrOptions();
  renderPreview();
}

function selectType(key) {
  ocrState.type = key;
  document.querySelectorAll(".type-card").forEach((c) => c.classList.toggle("selected", c.dataset.type === key));
  renderOcrOptions();
  $("#ocr-result").innerHTML = "";
}

function renderOcrOptions() {
  const type = OCR_TYPES[ocrState.type];
  const wrap = $("#ocr-options");
  if (!wrap) return;
  wrap.innerHTML = "";
  const meta = OPTION_META;
  const keys = COMMON_OPTIONS.filter((k) => k in meta && type.endpoint !== "/pdf-extract");
  // 按类型裁剪适用项
  let applicable = keys;
  if (ocrState.type === "seal") applicable = ["EnablePdf", "PdfPageNumber", "UseVL"];
  if (ocrState.type === "id-card" || ocrState.type === "mainland-permit" || ocrState.type === "general-ocr" || ocrState.type === "general-basic-ocr" || ocrState.type === "license-plate") applicable = ["EnablePdf", "PdfPageNumber"];
  if (ocrState.type === "pdf-extract") applicable = [];

  wrap.appendChild(el("div", { class: "card-title", text: "识别参数" }));
  if (!applicable.length) {
    wrap.appendChild(el("p", { class: "muted", text: "该接口无额外参数。" }));
    return;
  }
  const grid = el("div", { class: "grid", style: "grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px" });
  wrap.appendChild(grid);
  for (const k of applicable) {
    const m = meta[k];
    if (k === "PdfPageNumber") {
      const row = el("div", {}, [
        el("label", { class: "text-sm", text: m.label }),
        el("input", {
          class: "input", type: "number", min: 1, value: ocrState.options[k] ?? 1, style: "margin-top:4px",
          oninput: (e) => (ocrState.options[k] = Math.max(1, parseInt(e.target.value) || 1)),
        }),
      ]);
      grid.appendChild(row);
    } else {
      const row = el("div", {}, [
        el("label", { class: "checkbox-row", style: "cursor:pointer" }, [
          el("input", { type: "checkbox", checked: ocrState.options[k] !== false, onchange: (e) => (ocrState.options[k] = e.target.checked) }),
          el("span", { text: m.label }),
        ]),
      ]);
      grid.appendChild(row);
    }
  }
}

function buildDropzone() {
  const dz = el("div", { class: "dropzone", id: "ocr-dropzone" }, [
    el("div", { class: "dz-icon", text: "↑" }),
    el("div", { id: "ocr-dz-hint", text: "点击选择或拖拽文件到此处" }),
    el("div", { class: "muted", text: "支持 JPG / PNG / BMP / PDF / DOC / DOCX（合同审查除外）" }),
    el("div", { class: "dz-files" }),
  ]);
  const input = el("input", { type: "file", class: "hidden", onchange: (e) => { ocrState.files = [...e.target.files]; renderPreview(); toast(`已选择 ${ocrState.files.length} 个文件`, "ok"); } });
  dz.appendChild(input);
  dz.addEventListener("click", () => input.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    ocrState.files = [...e.dataTransfer.files];
    renderPreview();
    toast(`已选择 ${ocrState.files.length} 个文件`, "ok");
  });
  return dz;
}

function renderPreview() {
  const wrap = $("#ocr-preview");
  if (!wrap) return;
  wrap.innerHTML = "";
  const dz = $("#ocr-dropzone");
  const hint = $("#ocr-dz-hint");
  const { files } = ocrState;
  if (hint) {
    hint.textContent = files.length
      ? `已添加 ${files.length} 个文件，点击或拖拽可继续添加`
      : "点击选择或拖拽文件到此处";
  }
  if (dz) dz.classList.toggle("has-files", files.length > 0);
  if (!files.length) return;

  wrap.appendChild(el("div", { class: "upload-ok" }, [
    el("span", { text: `✓ 已选择 ${files.length} 个文件` }),
    el("span", { class: "muted", text: `（共 ${fmtBytes(files.reduce((s, f) => s + f.size, 0))}）` }),
    el("button", { class: "link-btn", text: "清空", onclick: () => { ocrState.files = []; renderPreview(); } }),
  ]));

  for (const f of files) {
    const chip = el("span", { class: "file-chip" }, [
      el("span", { text: `${f.name}（${fmtBytes(f.size)}）` }),
      el("span", { class: "remove", text: "✕", onclick: () => { ocrState.files = ocrState.files.filter((x) => x !== f); renderPreview(); } }),
    ]);
    wrap.appendChild(chip);

    // 图片预览
    if (f.type.startsWith("image/")) {
      const reader = new FileReader();
      reader.onload = (e) => wrap.appendChild(el("img", { class: "preview-img mt-8", src: e.target.result }));
      reader.readAsDataURL(f);
    }
  }
}

function setInputMode(mode) {
  ocrState.mode = mode;
  document.querySelectorAll("#ocr-url-wrap, #ocr-b64-wrap").forEach((w) => w.classList.add("hidden"));
  document.querySelectorAll(".tabs .tab").forEach((t, i) => t.classList.toggle("active", i === (mode === "file" ? 0 : mode === "url" ? 1 : 2)));
  if (mode === "url") $("#ocr-url-wrap").classList.remove("hidden");
  if (mode === "base64") $("#ocr-b64-wrap").classList.remove("hidden");
}

function buildForm(extra = {}) {
  const type = OCR_TYPES[ocrState.type];
  const fd = new FormData();
  if (ocrState.mode === "file" && ocrState.files.length) fd.append("file", ocrState.files[0]);
  if (ocrState.mode === "url" && ocrState.url) fd.append("ImageUrl", ocrState.url);
  if (ocrState.mode === "base64" && ocrState.base64) fd.append("ImageBase64", ocrState.base64);
  const opts = ocrState.options;
  if (type.endpoint !== "/pdf-extract") {
    if ("EnablePdf" in opts) fd.append("EnablePdf", opts.EnablePdf ? "true" : "false");
    if (opts.PdfPageNumber) fd.append("PdfPageNumber", String(opts.PdfPageNumber));
  }
  if (ocrState.type === "business-license" || ocrState.type === "law-firm-license") fd.append("MergeSplitPages", opts.MergeSplitPages ? "true" : "false");
  if (ocrState.type === "seal") fd.append("UseVL", opts.UseVL ? "true" : "false");
  for (const [k, v] of Object.entries(extra)) fd.append(k, v);
  return fd;
}

function validateInput() {
  if (ocrState.mode === "file" && !ocrState.files.length) { toast("请先选择文件", "warn"); return false; }
  if (ocrState.mode === "url" && !ocrState.url.trim()) { toast("请输入图片 URL", "warn"); return false; }
  if (ocrState.mode === "base64" && !ocrState.base64.trim()) { toast("请输入 Base64 内容", "warn"); return false; }
  return true;
}

async function submitOcrSync() {
  if (!validateInput()) return;
  const type = OCR_TYPES[ocrState.type];
  const btn = $("#btn-ocr-sync");
  const prog = $("#ocr-upload-progress");
  btn.disabled = true;
  prog.textContent = "上传中…";
  const resultWrap = $("#ocr-result");
  resultWrap.innerHTML = "";
  try {
    const resp = await api.upload(type.endpoint, buildForm(), (p) => (prog.textContent = `上传中 ${p}%`));
    prog.textContent = "";
    renderOcrResult(resp);
  } catch (e) {
    prog.textContent = "";
    toast(`识别失败: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function submitOcrAsync() {
  if (!validateInput()) return;
  const type = OCR_TYPES[ocrState.type];
  if (!type.taskType) { toast("该接口不支持异步任务", "warn"); return; }
  const btn = $("#btn-ocr-async");
  btn.disabled = true;
  try {
    const fd = buildForm({ task_type: type.taskType });
    fd.delete("file");
    if (ocrState.mode === "file" && ocrState.files.length) fd.append("file", ocrState.files[0]);
    const options = {};
    if (ocrState.options.EnablePdf !== undefined) options.EnablePdf = ocrState.options.EnablePdf;
    if (ocrState.options.PdfPageNumber) options.PdfPageNumber = ocrState.options.PdfPageNumber;
    if (ocrState.type === "seal") options.UseVL = ocrState.options.UseVL;
    fd.append("options", JSON.stringify(options));
    const resp = await api.upload("/tasks", fd);
    const taskId = resp?.Response?.task_id;
    toast(`任务已创建: ${taskId}`, "ok");
    openTaskDetail(taskId);
  } catch (e) {
    toast(`创建任务失败: ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

function fmtConfidence(v) {
  if (v == null || v === "") return "-";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return (n <= 1 ? Math.round(n * 100) : Math.round(n)) + "%";
}

function ocrMetric(label, value) {
  return el("div", { class: "ocr-metric" }, [
    el("div", { class: "k", text: label }),
    el("div", { class: "v", text: value }),
  ]);
}

function ocrResultToolbar(resp, extra = []) {
  return el("div", { class: "ocr-toolbar" }, [
    ...extra,
    el("button", { class: "btn btn-secondary btn-sm", text: "查看 JSON", onclick: () => openModal("原始 JSON", jsonView(resp)) }),
    el("button", { class: "btn btn-ghost btn-sm", text: "复制 JSON", onclick: () => copyText(JSON.stringify(resp, null, 2)) }),
  ]);
}

function renderOcrFields(fields, data) {
  const filled = fields.filter(([k]) => data[k] != null && data[k] !== "");
  const empty = fields.length - filled.length;
  return el("div", {}, [
    el("div", { class: "ocr-field-grid" }, fields.map(([k, label]) => {
      const value = data[k];
      const vacant = value == null || value === "";
      return el("div", { class: "ocr-field" + (vacant ? " empty" : "") }, [
        el("div", { class: "k", text: label }),
        el("div", { class: "v", text: vacant ? "未识别" : String(value) }),
      ]);
    })),
    empty ? el("div", { class: "muted mt-8", text: `${filled.length} 项已识别 · ${empty} 项为空` }) : null,
  ]);
}

function renderOcrResult(resp) {
  const type = OCR_TYPES[ocrState.type];
  const wrap = $("#ocr-result");
  wrap.innerHTML = "";
  const r = resp?.Response || resp?.data || resp || {};
  const card = el("div", { class: "card ocr-result-card" });
  wrap.appendChild(card);

  const hero = el("div", { class: "ocr-hero" }, [
    el("div", { class: "ocr-hero-main" }, [
      el("div", { class: `type-mark tone-${type.tone || "blue"}`, text: type.short || type.label.slice(0, 1) }),
      el("div", {}, [
        el("div", { class: "ocr-hero-title", text: type.label }),
        el("div", { class: "ocr-hero-sub", text: "识别完成，结果已按字段结构化展示" }),
      ]),
    ]),
    ocrResultToolbar(resp),
  ]);
  card.appendChild(hero);

  if (type.seal) {
    hero.querySelector(".ocr-hero-sub").textContent = r.CompanyName || r.SealBody || "已提取印章主体与类型";
    card.appendChild(el("div", { class: "ocr-metrics" }, [
      ocrMetric("印章类型", r.SealType || "-"),
      ocrMetric("形状", r.SealShape || "-"),
      ocrMetric("防伪编码", r.SerialNumber || "-"),
      ocrMetric("来源", r.Source || "-"),
    ]));
    card.appendChild(renderOcrFields(type.fields, r));
    const infos = r.SealInfos || [];
    if (infos.length) {
      card.appendChild(el("div", { class: "card-title mt-16", text: "印章明细" }, [el("span", { class: "hint", text: `${infos.length} 枚` })]));
      for (const [i, info] of infos.entries()) {
        card.appendChild(el("div", { class: "evidence-box" }, [
          el("div", { class: "ev-head", text: `#${i + 1}` }),
          el("div", { class: "kv" }, Object.entries(info).filter(([, v]) => v != null && v !== "").map(([k, v]) =>
            el("div", { class: "kv-item" }, [el("div", { class: "k", text: k }), el("div", { class: "v", text: typeof v === "object" ? JSON.stringify(v) : String(v) })])
          )),
        ]));
      }
    }
  } else if (type.pdf) {
    const pdfLabel = { text_based: "文字版", scanned: "扫描件", image_based: "图片型", mixed: "混合" }[r.pdf_type] || (r.pdf_type || "-");
    const pdfCls = { text_based: "green", scanned: "orange", image_based: "blue", mixed: "purple" }[r.pdf_type] || "gray";
    hero.querySelector(".ocr-hero-sub").textContent = r.title || "已提取文档正文，可复制 Markdown";
    hero.querySelector(".ocr-hero-main").appendChild(el("span", { class: `badge ${pdfCls}`, text: pdfLabel }));
    card.appendChild(el("div", { class: "ocr-metrics" }, [
      ocrMetric("页数", String(r.page_count ?? "-")),
      ocrMetric("置信度", fmtConfidence(r.confidence)),
      ocrMetric("耗时", r.time_ms != null ? `${r.time_ms} ms` : "-"),
      ocrMetric("标题", r.title || "-"),
    ]));
    if (r.pages_needing_ocr && r.pages_needing_ocr.length) {
      card.appendChild(el("div", { class: "ocr-note" }, [
        el("span", { class: "badge orange", text: "需 OCR" }),
        el("span", { class: "text-sm", text: `第 ${r.pages_needing_ocr.join("、")} 页` }),
      ]));
    }
    const toolbar = hero.querySelector(".ocr-toolbar");
    toolbar.prepend(el("button", { class: "btn btn-secondary btn-sm", text: "复制 Markdown", onclick: () => copyText(r.markdown || "") }));
    card.appendChild(el("div", { class: "ocr-doc" }, [
      el("div", { class: "ocr-doc-head" }, [
        el("div", { class: "card-title mb-0", text: "提取正文" }),
        el("span", { class: "hint", text: "Markdown 预览" }),
      ]),
      el("div", { class: "md-body", html: renderMarkdown(r.markdown) }),
    ]));
  } else if (type.textDetections) {
    const dets = r.TextDetections || [];
    hero.querySelector(".ocr-hero-sub").textContent = `共 ${dets.length} 行印刷体文本`;
    card.appendChild(el("div", { class: "ocr-metrics" }, [
      ocrMetric("文本行", String(dets.length)),
      ocrMetric("旋转角", r.Angle != null ? Number(r.Angle).toFixed(2) + "°" : "-"),
      ocrMetric("RequestId", r.RequestId ? shortId(r.RequestId) : "-"),
    ]));
    if (dets.length) {
      card.appendChild(el("div", { class: "ocr-lines" }, dets.map((d, i) => el("div", { class: "ocr-line" }, [
        el("span", { class: "ai-index", text: String(i + 1) }),
        el("div", { class: "ocr-line-text", text: d.DetectedText }),
        el("span", { class: `badge ${d.Confidence >= 90 ? "green" : d.Confidence >= 60 ? "yellow" : "red"}`, text: fmtConfidence(d.Confidence) }),
      ]))));
    }
  } else if (type.plate) {
    const words = r.words_result || [];
    hero.querySelector(".ocr-hero-sub").textContent = words.length ? `识别到 ${words.length} 个车牌` : "未识别到车牌";
    if (!words.length) {
      card.appendChild(el("p", { class: "muted", text: "未识别到车牌" }));
    } else {
      card.appendChild(el("div", { class: "plate-grid" }, words.map((w) => el("div", { class: "plate-card" }, [
        el("div", { class: "plate-number", text: w.number }),
        el("div", { class: "plate-meta" }, [
          el("span", { class: "badge blue", text: w.color || "-" }),
          el("span", { class: "muted", text: w.cover_info === "incomplete" ? "被遮挡" : "完整" }),
        ]),
      ]))));
    }
  } else if (type.raw) {
    const texts = r.rec_texts || [];
    hero.querySelector(".ocr-hero-sub").textContent = `共 ${texts.length} 行自由文本`;
    if (texts.length) {
      const joined = texts.join("\n");
      hero.querySelector(".ocr-toolbar").prepend(el("button", { class: "btn btn-secondary btn-sm", text: "复制全文", onclick: () => copyText(joined) }));
      card.appendChild(el("div", { class: "ocr-doc" }, texts.map((t) => el("p", { text: t }))));
    } else {
      card.appendChild(el("p", { class: "muted", text: "未识别到文本" }));
    }
  } else if (type.fields.length) {
    const filled = type.fields.filter(([k]) => r[k] != null && r[k] !== "").length;
    hero.querySelector(".ocr-hero-sub").textContent = `已识别 ${filled} / ${type.fields.length} 个字段`;
    card.appendChild(renderOcrFields(type.fields, r));
    if (r.RequestId) card.appendChild(el("div", { class: "muted mt-8", text: `RequestId: ${r.RequestId}` }));
  } else {
    card.appendChild(el("p", { class: "muted", text: "接口返回空结果" }));
  }
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

let reviewState = { files: [], packageId: newPackageId(), contractType: "" };

function renderReviewPage(content) {
  content.appendChild(pageHead("CONTRACT REVIEW", "合同审查", "上传合同后按风险点 / 合理性 / 内控 / 资信四栏展示，规则来自规则引擎库（内置提示词 + 用户自定义）"));

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

    el("div", { class: "action-bar mt-8" }, [
      el("div", { class: "flex", style: "flex-wrap:wrap" }, [
        el("button", { class: "btn btn-primary", id: "btn-review-sync", text: "同步审查", onclick: submitReviewSync }),
        el("button", { class: "btn btn-secondary", id: "btn-review-async", text: "异步审查", onclick: submitReviewAsync }),
        el("span", { class: "muted", text: "大批量请走异步，结果可在任务中心查看" }),
      ]),
    ]),
  ]);
  content.appendChild(card);
  content.appendChild(el("div", { id: "review-result" }));
}

/* ============================================================
 * 合同要素提取
 * ============================================================ */
let elementsState = { files: [], packageId: newPackageId(), result: null, previewUrl: null, previewHtml: "", previewText: "", previewKind: "", previewMessage: "", reviewResult: null };
let compareState = { baseFile: null, compareFile: null, result: null, options: {} };

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

function setElementFiles(files, options = {}) {
  const incoming = [...files];
  const replace = options.replace === true || incoming.length === 0;
  const append = !replace && options.append !== false && elementsState.files.length > 0;
  elementsState.files = append ? mergeFiles(elementsState.files, incoming) : incoming;
  if (!append) {
    elementsState.packageId = newPackageId();
    const pkgInput = $("#elements-pkg");
    if (pkgInput) pkgInput.value = elementsState.packageId;
  }
  elementsState.result = null;
  elementsState.reviewResult = null;
  resetElementPreview();
  renderElementsFiles();
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

async function loadElementSchema() {
  const wrap = $("#element-schema");
  if (!wrap) return;
  wrap.innerHTML = "";
  try {
    const data = await api.get("/contract-element-fields");
    const fields = data.fields || [];
    wrap.appendChild(el("div", { class: "schema-block" }, [
      el("div", { class: "card-title" }, [
        el("span", { text: "自定义抽取要素" }),
        el("span", { class: "hint", text: `共 ${fields.length} 项 · 启用后才会抽取` }),
        el("span", { class: "grow" }),
        el("button", { class: "btn btn-primary btn-sm", text: "新增要素", onclick: () => openElementFieldForm() }),
      ]),
      el("div", { class: "table-wrap" }, [
        el("table", { class: "table" }, [
          el("thead", {}, [el("tr", {}, [
            el("th", { text: "要素名称" }),
            el("th", { text: "字段键" }),
            el("th", { text: "别名/提示词" }),
            el("th", { text: "是否启用" }),
            el("th", { text: "操作" }),
          ])]),
          el("tbody", {}, fields.map((field) => el("tr", {}, [
            el("td", { class: "rule-title", text: field.label }),
            el("td", { class: "mono", text: field.key }),
            el("td", { class: "muted", text: (field.aliases || []).join("、") || "-" }),
            el("td", {}, [
              el("button", {
                class: "enable-toggle" + (field.enabled ? " on" : ""),
                text: field.enabled ? "是" : "否",
                onclick: () => toggleElementField(field, !field.enabled),
              }),
            ]),
            el("td", {}, [
              el("button", { class: "link-btn", text: "编辑", onclick: () => openElementFieldForm(field) }),
              el("button", { class: "link-btn danger", text: "删除", onclick: () => deleteElementField(field) }),
            ]),
          ]))),
        ]),
      ]),
    ]));
  } catch (e) {
    wrap.appendChild(el("div", { class: "card" }, [
      el("p", { class: "muted", text: "加载要素定义失败：" + (e?.message || e) }),
    ]));
  }
}

function openElementFieldForm(field) {
  const editing = !!(field && field.key);
  const form = el("div", { class: "rule-form" }, [
    el("div", { class: "form-row" }, [
      el("label", { text: "要素名称" }),
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
      el("label", { text: "自定义正则（可空）" }),
      el("input", { class: "input", id: "el-pattern", value: field?.pattern || "", placeholder: "如 质保期[:：]\\s*([^\\n]{2,40})" }),
    ]),
    el("div", { class: "action-bar mt-8" }, [
      el("button", { class: "btn btn-primary", text: editing ? "保存" : "新增", onclick: () => saveElementField(field) }),
      el("button", { class: "btn btn-ghost", text: "取消", onclick: closeModal }),
    ]),
  ]);
  openModal(editing ? "编辑要素" : "新增要素", form);
}

async function saveElementField(field) {
  const payload = {
    label: $("#el-label")?.value?.trim(),
    key: $("#el-key")?.value?.trim() || undefined,
    aliases: ($("#el-aliases")?.value || "").split(/[,，、]/).map((item) => item.trim()).filter(Boolean),
    pattern: $("#el-pattern")?.value?.trim() || "",
    enabled: true,
  };
  if (!payload.label) { toast("请填写要素名称", "warn"); return; }
  try {
    if (field?.key) {
      await api.request("PUT", `/contract-element-fields/${field.key}`, {
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      toast("要素已更新", "ok");
    } else {
      await api.postJson("/contract-element-fields", payload);
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
    await api.request("PUT", `/contract-element-fields/${field.key}`, {
      body: JSON.stringify({ enabled }),
      headers: { "Content-Type": "application/json" },
    });
    await loadElementSchema();
  } catch (e) {
    toast("操作失败：" + (e?.message || e), "err");
  }
}

async function deleteElementField(field) {
  if (!window.confirm(`确定删除要素「${field.label}」？之后将不再抽取该字段。`)) return;
  try {
    await api.request("DELETE", `/contract-element-fields/${field.key}`);
    toast("要素已删除", "ok");
    await loadElementSchema();
  } catch (e) {
    toast("删除失败：" + (e?.message || e), "err");
  }
}

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

async function submitElementsSync() {
  if (!elementsState.files.length) { toast("请至少选择一个合同文件", "warn"); return; }
  if (!elementsState.packageId) { toast("请填写合同包 ID", "warn"); return; }
  const btn = $("#btn-elements-sync");
  if (btn) btn.disabled = true;
  try {
    const fd = new FormData();
    elementsState.files.forEach((f) => fd.append("files", f));
    fd.append("PackageId", elementsState.packageId);
    const resp = await api.upload("/contract-elements", fd);
    elementsState.result = resp;
    closeModal();
    navigate("element-fill");
    toast("抽取完成，点开输入框可选择填充", "ok");
  } catch (e) {
    toast(`提取失败: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderExtractPreview() {
  const host = $("#extract-preview");
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(buildContractPreviewPane());
}

function requireContractFiles(action) {
  if (!elementsState.files.length) {
    toast(`请先在合同信息中添加合同附件后再${action}`, "warn");
    return false;
  }
  return true;
}

function renderElementFillPage(content) {
  content.appendChild(pageHead("CONTRACT FORM", "合同拟定", "按标准版合同管理：先上传合同附件，再做要素抽取、AI 审查、客商风险和文档对比"));
  content.appendChild(el("div", { class: "card" }, [
    el("div", { class: "flex", style: "flex-wrap:wrap;gap:8px;align-items:center" }, [
      el("button", { class: "btn btn-primary", text: "要素抽取", onclick: openExtractDialog }),
      el("button", { class: "btn btn-secondary", text: "AI审查", onclick: openAiReviewDialog }),
      el("button", { class: "btn btn-ghost", text: "客商风险", onclick: openCreditRiskDialog }),
      el("button", { class: "btn btn-ghost", text: "文档对比", onclick: openCompareDialog }),
      el("span", { class: "muted", text: elementsState.result ? `已抽取 ${elementsState.result.fields?.length || 0} 个字段` : "先添加合同附件，再点功能按钮" }),
    ]),
  ]));
  const formHost = el("div", { id: "contract-fill-form" }, [
    el("div", { class: "card" }, [el("p", { class: "muted", text: "加载合同表单…" })]),
  ]);
  content.appendChild(formHost);
  content.appendChild(el("div", { id: "credit-risk-panel" }));
  renderCreditRiskPanel();
  const extracted = (elementsState.result && elementsState.result.fields) || [];
  const loadFields = extracted.length
    ? Promise.resolve(extracted)
    : api.get("/contract-element-fields").then((data) =>
        (data.fields || []).filter((item) => item.enabled !== false).map((item) => ({
          key: item.key,
          label: item.label,
          candidates: [],
        }))
      ).catch(() => []);
  loadFields.then((items) => {
    const formHost = $("#contract-fill-form");
    if (!formHost) return;
    formHost.innerHTML = "";
    formHost.appendChild(el("div", { class: "card contract-form-card" }, [
      el("div", { class: "card-title", text: "合同信息" }, [
        el("span", { class: "hint", text: "点开输入框后，抽出的内容在下方下拉列表中竖排显示" }),
      ]),
      el("div", { class: "contract-form-grid" }, [
        buildAttachmentField(),
        ...items.map((item) => buildInlineFillField(item)),
      ]),
      el("div", { class: "action-bar mt-8" }, [
        el("button", { class: "btn btn-primary", text: "保存当前填写", onclick: confirmElementsFill }),
        el("button", { class: "btn btn-ghost", text: "复制 JSON", onclick: () => copyText(JSON.stringify(collectElementValues(), null, 2)) }),
      ]),
    ]));
    renderElementsFiles();
    renderCreditRiskPanel();
  });
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

function candidateValues(item) {
  const values = [];
  const seen = new Set();
  for (const candidate of item.candidates || []) {
    const value = (candidate.value || candidate || "").toString().trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    values.push(value);
  }
  if (item.value && !seen.has(item.value)) values.unshift(item.value);
  return values;
}

function buildInlineFillField(item) {
  const suggestions = candidateValues(item);
  const input = el("input", {
    class: "input fill-input",
    "data-element-key": item.key,
    value: "",
    placeholder: "请输入",
  });
  const menu = suggestions.length
    ? el("div", { class: "fill-dropdown hidden" }, suggestions.map((value) =>
        el("button", {
          type: "button",
          class: "fill-dropdown-item",
          text: value,
          onmousedown: (event) => event.preventDefault(),
          onclick: (event) => {
            event.preventDefault();
            event.stopPropagation();
            input.value = value;
            menu.classList.add("hidden");
            input.focus();
          },
        })
      ))
    : null;
  const hideMenu = () => { if (menu) menu.classList.add("hidden"); };
  const showMenu = () => { if (menu) menu.classList.remove("hidden"); };
  input.addEventListener("focus", showMenu);
  input.addEventListener("click", showMenu);
  input.addEventListener("blur", () => setTimeout(hideMenu, 120));
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

function partyValue(key) {
  const filled = collectElementValues()[key];
  if (filled) return filled;
  const field = (elementsState.result?.fields || []).find((item) => item.key === key);
  return (field && field.value) || "";
}

function reviewItems(resp) {
  const ai = resp?.ai_analysis;
  const findings = resp?.review_result?.findings || [];
  if (ai && ai.items && ai.items.length) return ai.items;
  return findings.filter((item) => item.status !== "PASS" && item.status !== "NOT_APPLICABLE");
}

function itemsByModule(resp, module) {
  return reviewItems(resp).filter((item) => itemModule(item) === module);
}

function renderCreditRiskPanel() {
  const host = $("#credit-risk-panel");
  if (!host) return;
  host.innerHTML = "";
  const partyA = partyValue("party_a");
  const partyB = partyValue("party_b");
  const items = itemsByModule(elementsState.reviewResult, "资信");
  if (!partyA && !partyB && !items.length) return;
  host.appendChild(el("div", { class: "card" }, [
    el("div", { class: "card-title", text: "客商风险" }, [
      el("span", { class: "hint", text: "依据合同主体与资信审查结果提示，可对接企业征信平台" }),
    ]),
    el("div", { class: "grid grid-3" }, [
      el("div", { class: "ocr-field" }, [el("div", { class: "k", text: "甲方" }), el("div", { class: "v", text: partyA || "未抽取" })]),
      el("div", { class: "ocr-field" }, [el("div", { class: "k", text: "乙方" }), el("div", { class: "v", text: partyB || "未抽取" })]),
      el("div", { class: "ocr-field" }, [el("div", { class: "k", text: "资信风险项" }), el("div", { class: "v", text: String(items.length) })]),
    ]),
    items.length
      ? el("div", { class: "risk-list mt-8" }, items.map((item, index) => buildAiRiskItem(item, index)))
      : el("p", { class: "muted mt-8", text: "暂无资信风险命中。点「客商风险」或「AI审查」后会按规则引擎库刷新。" }),
  ]));
}

async function runContractReviewForFill() {
  reviewState.files = [...elementsState.files];
  reviewState.packageId = elementsState.packageId || reviewState.packageId;
  const fd = new FormData();
  reviewState.files.forEach((file) => fd.append("files", file));
  fd.append("PackageId", reviewState.packageId);
  if (reviewState.contractType) fd.append("ContractType", reviewState.contractType);
  const resp = await api.upload("/contract-review", fd);
  elementsState.reviewResult = resp;
  renderCreditRiskPanel();
  return resp;
}

function openAiReviewDialog() {
  if (!requireContractFiles("AI审查")) return;
  const body = el("div", { class: "review-dialog ppt-review" }, [
    el("div", { id: "review-result" }),
  ]);
  openModal("AI审查", body, { wide: true });
  if (elementsState.reviewResult) {
    renderAiReviewWorkspace(elementsState.reviewResult);
  } else {
    renderAiReviewStart();
  }
}

function renderAiReviewStart() {
  const wrap = $("#review-result");
  if (!wrap) return;
  wrap.innerHTML = "";
  wrap.appendChild(el("div", { class: "ppt-review-start" }, [
    el("p", { class: "muted", text: "对照规则引擎库审查本合同，结果按内控 / 合理性 / 风险点 / 资信展示。" }),
    el("div", { class: "grid grid-2" }, [
      el("div", { class: "form-row" }, [
        el("label", { text: "合同包 ID" }),
        el("input", { class: "input", value: elementsState.packageId, oninput: (e) => (elementsState.packageId = e.target.value.trim()) }),
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

function renderAiReviewWorkspace(resp) {
  const wrap = $("#review-result");
  if (!wrap) return;
  wrap.innerHTML = "";
  const rr = resp?.review_result || {};
  const ai = resp?.ai_analysis;
  const findings = rr.findings || [];
  const items = (ai && ai.items && ai.items.length)
    ? ai.items
    : findings.filter((item) => item.status !== "PASS" && item.status !== "NOT_APPLICABLE");
  wrap.appendChild(buildReviewPanels(items, ai, rr, rr.run || {}, rr.report || {}, rr.evidence || [], rr.documents || [], { compact: true }));
}

async function submitFillReview() {
  if (!requireContractFiles("AI审查")) return;
  const btn = $("#btn-fill-review");
  if (btn) btn.disabled = true;
  const wrap = $("#review-result");
  if (wrap) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "审查中…" }),
      el("p", { class: "muted", text: "正在按规则引擎库识别风险，耗时取决于合同页数" }),
    ]));
  }
  try {
    const resp = await runContractReviewForFill();
    renderAiReviewWorkspace(resp);
    toast("AI审查完成", "ok");
  } catch (e) {
    if (wrap) wrap.innerHTML = "";
    toast(`审查失败: ${e.message}`, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openCreditRiskDialog() {
  if (!requireContractFiles("查看客商风险")) return;
  const body = el("div", { class: "review-dialog" }, [
    el("p", { class: "muted", text: "客商风险对接合同主体与资信规则。有企业征信平台时，可将甲方/乙方送去核验。" }),
    el("div", { id: "credit-dialog-body" }, [el("p", { class: "muted", text: "正在汇总客商风险…" })]),
  ]);
  openModal("客商风险", body, { wide: true });
  const host = $("#credit-dialog-body");
  try {
    if (!elementsState.reviewResult) await runContractReviewForFill();
    if (!host) return;
    host.innerHTML = "";
    const items = itemsByModule(elementsState.reviewResult, "资信");
    host.appendChild(el("div", { class: "grid grid-2" }, [
      el("div", { class: "ocr-field" }, [el("div", { class: "k", text: "甲方" }), el("div", { class: "v", text: partyValue("party_a") || "未抽取" })]),
      el("div", { class: "ocr-field" }, [el("div", { class: "k", text: "乙方" }), el("div", { class: "v", text: partyValue("party_b") || "未抽取" })]),
    ]));
    host.appendChild(items.length
      ? el("div", { class: "risk-list mt-8" }, items.map((item, index) => buildAiRiskItem(item, index)))
      : el("p", { class: "muted mt-8", text: "未发现资信风险提示。可在规则引擎库中补充客商授信规则后重新审查。" }));
  } catch (e) {
    if (host) host.innerHTML = "";
    toast(`客商风险分析失败: ${e.message}`, "err");
  }
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
  compareState.result = null;
  if (!compareState.baseFile && elementsState.files[0]) compareState.baseFile = elementsState.files[0];
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
    const resp = await api.upload("/contract-compare", fd);
    compareState.result = resp;
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
    const resp = await api.upload("/contract-review", fd);
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
  const ai = resp?.ai_analysis;
  if (!rr) {
    wrap.appendChild(el("div", { class: "card" }, [
      el("div", { class: "card-title", text: "审查结果" }),
      el("div", { class: "json-view", html: escapeHtml(JSON.stringify(resp, null, 2)) }),
    ]));
    return;
  }

  // 缓存命中提示：输入未变化时后端直接复用上次结果，未重新分析
  if (resp?.cached) {
    wrap.appendChild(el("div", { class: "cache-hit" }, [
      el("span", { text: "✓ 输入未变化，已直接复用上次审查结果（未重新分析，结果与上次一致）" }),
    ]));
  }

  const report = rr.report || {};
  const run = rr.run || {};
  const findings = rr.findings || [];
  const evidence = rr.evidence || [];
  const documents = rr.documents || [];
  const overall = report.overall_status || "UNKNOWN";
  const fm = FINDING_META[overall] || FINDING_META.UNKNOWN;

  // 单一融合清单：后端已将 AI 分析与规则层结论合并（PASS/不适用已隐藏）
  const aiEnabled = !!(ai && ai.items && ai.items.length);
  const items = aiEnabled
    ? ai.items
    : findings.filter((f) => f.status !== "PASS" && f.status !== "NOT_APPLICABLE");
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

  wrap.appendChild(buildReviewPanels(items, ai, rr, run, report, evidence, documents));
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

function buildReviewPanels(items, ai, rr, run, report, evidence, documents, options = {}) {
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
    el("div", { class: "card-title", text: "AI 审查" }, [
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
      el("button", { class: "ppt-btn", text: "重新审查", onclick: submitFillReview }),
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
 * 规则引擎库（用户设定合同风险规则，供 AI 审查命中）
 * ============================================================ */
const AI_RULE_STATUS_META = {
  draft: { label: "待确认", cls: "orange" },
  active: { label: "已启用", cls: "green" },
  disabled: { label: "已停用", cls: "gray" },
};
const RULE_MODULES = ["风险点", "合理性", "内控", "资信"];
const RULE_TOPICS = ["合同类型", "金额", "付款", "发票", "源代码相关（按关键字搜索）", "知识产权", "Qx新技术架构描述相关", "合同主体", "合规性/交付问题", "软件开发服务合同（0税率）重点检查项"];
const RULE_PAGE_SIZES = [10, 20, 50];
let aiRulesState = { topics: RULE_TOPICS, pager: {} };

function isRuleEnabled(rule) {
  return rule.enabled !== false && rule.status === "active";
}

async function renderAiRulesPage(content) {
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
    const data = await api.get("/ai-rules");
    aiRulesState.topics = data.topics && data.topics.length ? data.topics : RULE_TOPICS;
    const packs = data.packs || {};
    const approval = (packs.approval && packs.approval.rules) || [];
    const aiRules = (packs.ai && packs.ai.rules) || [];
    wrap.innerHTML = "";
    wrap.appendChild(buildRulePack({
      mark: "HT",
      title: "合同审批检查标准",
      hint: `共 ${approval.length} 条`,
      empty: "暂无合同审批规则。服务启动后会写入检查标准，也可手动新增。",
      rules: approval,
      groups: (packs.approval && packs.approval.groups) || groupRulesByTopic(approval),
      engineTitle: "规则引擎",
      engineHint: "按检查维度分类，展示权重和高中低分标准",
      guide: "列表中新增、编辑、启用或删除后，规则引擎评分矩阵同步更新。",
      defaultTopic: "合规性/交付问题",
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
    }));
  } catch (e) {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "card" }, [
      el("p", { class: "muted", text: "加载失败：" + (e?.message || e) + "（请确认右上角已填写 X-API-Token）" }),
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
    listHost.appendChild(el("div", { class: "table-wrap" }, [buildApprovalRuleTable(slice.rows, slice.start)]));
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
        pack.hideCreate ? null : el("button", { class: "btn btn-primary btn-sm", text: "新增规则", onclick: () => openRuleForm({ topic: pack.defaultTopic }) }),
        el("button", { class: "btn btn-secondary btn-sm", text: "刷新", onclick: loadAiRules }),
      ]),
      listHost,
    ]),
    buildScoreMatrixCard(pack),
  ]);
}

function buildApprovalRuleTable(rules, start = 0) {
  const head = el("tr", {}, [
    el("th", { class: "col-index", text: "" }),
    el("th", { text: "规则编号" }),
    el("th", { text: "规则名称" }),
    el("th", { text: "规则内容" }),
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
      el("td", { class: "rule-condition", text: rule.condition || "-" }),
      el("td", {}, [
        el("button", {
          class: "enable-toggle" + (enabled ? " on" : ""),
          text: enabled ? "是" : "否",
          onclick: () => toggleRuleEnabled(rule, !enabled),
        }),
      ]),
      el("td", {}, [
        el("button", { class: "link-btn", text: "编辑", onclick: () => openRuleForm(rule) }),
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

function openRuleForm(rule) {
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
      el("label", { text: "规则内容" }),
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
  const payload = {
    title: $("#rule-title")?.value?.trim(),
    code: $("#rule-code")?.value?.trim() || undefined,
    topic: $("#rule-topic")?.value,
    risk_level: $("#rule-level")?.value,
    condition: $("#rule-condition")?.value?.trim(),
    suggested_action: $("#rule-action")?.value?.trim() || null,
    weight: Number($("#rule-weight")?.value || 10),
    high_standard: $("#rule-high")?.value?.trim() || null,
    mid_standard: $("#rule-mid")?.value?.trim() || null,
    low_standard: $("#rule-low")?.value?.trim() || null,
    enabled: true,
    status: "active",
  };
  if (!payload.title) { toast("请填写规则名称", "warn"); return; }
  try {
    if (rule?.id) {
      await api.request("PUT", `/ai-rules/${rule.id}`, {
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      toast("规则已更新", "ok");
    } else {
      await api.postJson("/ai-rules", payload);
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
    await api.postJson(`/ai-rules/${rule.id}/${enabled ? "enable" : "disable"}`, {});
    toast(enabled ? "规则已启用，下次审查生效" : "规则已停用", "ok");
    await loadAiRules();
  } catch (e) {
    toast("操作失败：" + (e?.message || e), "err");
  }
}

async function deleteRule(rule) {
  if (!window.confirm(`确定删除规则「${rule.title}」？列表和规则引擎会同步移除。`)) return;
  try {
    await api.request("DELETE", `/ai-rules/${rule.id}`);
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
  content.appendChild(pageHead("ASYNC QUEUE", "任务中心", "异步 OCR 与合同审查队列，可按状态过滤并查看结果"));

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
    if (resp && (resp.review_result || resp.ai_analysis)) {
      const body = el("div", {});
      openModal("任务结果（合同审查）", body);
      renderReviewViews(body, resp);
      return;
    }
    if (resp && Array.isArray(resp.fields)) {
      elementsState.result = resp;
      closeModal();
      navigate("element-fill");
      toast("抽取完成，点开输入框可选择填充", "ok");
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
