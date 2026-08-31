import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = process.argv[2] ?? "D:\\QQ\\合同审批检查自查标准_v0.14.xlsx";
const outputPath = process.argv[3] ?? path.resolve("data/contract_rules_v0.14.json");

const sourceBytes = await fs.readFile(inputPath);
const sourceSha256 = crypto.createHash("sha256").update(sourceBytes).digest("hex");
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheet = workbook.worksheets.getItem("Sheet1");
const values = sheet.getRange("A1:M54").values;

const contractTypeColumns = [
  { name: "软件产品销售", applicabilityColumn: 3, noteColumn: 4 },
  { name: "软件开发/转让服务", applicabilityColumn: 5, noteColumn: 6 },
  { name: "一般商品销售合同", applicabilityColumn: 7, noteColumn: 8 },
  { name: "混合合同", applicabilityColumn: 9, noteColumn: 10 },
  { name: "其它服务合同", applicabilityColumn: 11, noteColumn: 12 },
];

const asText = (value) => (value === null || value === undefined ? "" : String(value).trim());

function stableRuleId(category, title) {
  const digest = crypto.createHash("sha256").update(`${category}\u001f${title}`).digest("hex");
  return `CONTRACT-CHECK-${digest.slice(0, 12)}`.toUpperCase();
}

function classifyMethod(category, title) {
  const text = `${category} ${title}`;
  if (category === "合同类型") return "classification";
  if (["金额", "付款", "发票"].includes(category)) return "deterministic";
  if (category.includes("源代码")) return "keyword";
  if (text.includes("盖骑缝章") || text.includes("印章") || text.includes("签章")) return "visual";
  if (text.includes("产权归属") || text.includes("专利") || text.includes("技术成果归属")) return "human";
  if (category.includes("知识产权")) return "semantic";
  if (category.includes("合同主体") || category.includes("Qx") || category.includes("合规性")) {
    return "semantic";
  }
  return "semantic";
}

function parseApplicability(value, note) {
  const text = asText(value);
  if (!text) return { applicability: "unspecified", expected_value: null, note: asText(note) || null };
  if (text === "√") return { applicability: "required", expected_value: null, note: asText(note) || null };
  if (text === "×") return { applicability: "not_applicable", expected_value: null, note: asText(note) || null };
  return { applicability: "expected_value", expected_value: value, note: asText(note) || null };
}

let currentCategory = "未分类";
const rules = [];
for (let rowIndex = 4; rowIndex < 54; rowIndex += 1) {
  const row = values[rowIndex];
  const legacyId = Number(row[0]);
  const category = asText(row[1]) || currentCategory;
  const title = asText(row[2]);
  if (!title || !Number.isFinite(legacyId)) continue;
  currentCategory = category;

  const applicability = {};
  const appliesTo = [];
  for (const contractType of contractTypeColumns) {
    const parsed = parseApplicability(row[contractType.applicabilityColumn], row[contractType.noteColumn]);
    applicability[contractType.name] = parsed;
    if (parsed.applicability !== "not_applicable" && parsed.applicability !== "unspecified") {
      appliesTo.push(contractType.name);
    }
  }

  rules.push({
    rule_id: stableRuleId(category, title),
    legacy_id: legacyId,
    version: "v0.14",
    title,
    category,
    applies_to: appliesTo,
    condition: null,
    check_method: classifyMethod(category, title),
    expected_value: null,
    risk_level: null,
    applicability,
    required_evidence: [],
    human_review: ["human", "semantic", "visual"].includes(classifyMethod(category, title)),
    source_snapshot: `合同审批检查自查标准_v0.14.xlsx#${sourceSha256}`,
    source_locator: {
      locator_type: "table_cell",
      sheet_name: "Sheet1",
      cell_reference: `C${rowIndex + 1}`,
    },
  });
}

const payload = {
  bundle_id: `contract-rules-v0.14-${sourceSha256.slice(0, 12)}`,
  source_filename: path.basename(inputPath),
  source_sha256: sourceSha256,
  source_sheet: "Sheet1",
  source_range: "A1:M54",
  source_notes: [asText(values[1][0])],
  rules,
  imported_at: new Date().toISOString(),
};

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
console.log(JSON.stringify({ outputPath, sourceSha256, ruleCount: rules.length }));
