# Contract Review Agent

合同附件审查智能体的代码基础，目标是让每个审核结论都能回到原始文件的页码、条款、文字块或表格单元格，并且可以按同一输入和版本重放。

当前首个里程碑聚焦于：

- 数字 PDF 的只读解析；
- DOCX 段落/表格单元格和 XLSX 工作表/单元格的只读解析；
- 页面、文字块和词级坐标保留；
- 空白/无文字页面的 OCR 待处理标记；
- 可替换 OCR 提供方接口：输出带页坐标的文字块并纳入回放版本；
- 文本证据生成和文件哈希绑定；
- 可序列化的合同事实、规则、审核发现和审核运行模型。
- Excel 自查标准的版本化规则快照；
- 附件缺失和事实值不一致的证据化确定性检查。
- 全规则覆盖执行：未配置的确定性/语义/视觉检查显式输出 `UNKNOWN`；
- 规则/合同知识片段、检索轨迹、人工决定、回放指纹和写一次审计工件。
- PDF 和 DOCX 的统一解析入口；DOCX 保留段落和表格单元格定位，不伪造不可获得的页码。

## 目标架构

```text
原始合同包（只读）
  -> 文档/页面/版面解析
  -> 合同条款与事实抽取
  -> 确定性规则和跨附件比对
  -> RAG 召回依据 + LLM 语义判断
  -> Evidence-first Finding
  -> 人工确认 / 回放 / 报告
```

向量库只用于召回，不作为证据事实的唯一来源。原文件哈希、页面坐标、解析器版本、规则版本和模型版本必须随审核运行保存。

## 本地验证

使用项目环境运行：

```powershell
uv run --extra dev pytest
```

当前也可以使用已提供的 Python 运行时直接执行：

```powershell
python -m pytest
```

命令行入口目前提供两项基础能力：

```powershell
python -m contract_review.cli parse-pdf .\合同.pdf --package-id pkg-001 --query 付款 --output .\parsed.json
python -m contract_review.cli create-run .\合同.pdf --package-id pkg-001 --rules .\data\contract_rules_v0.14.json --output .\run.json
python -m contract_review.cli review .\合同.pdf --package-id pkg-001 --rules .\data\contract_rules_v0.14.json --audit-root .\audit --output .\review.json --report-output .\review.md
```

`parsed.json` 包含文档、页面、文字块、词级坐标和文本证据；`run.json` 包含源文件哈希、规则快照、配置回放指纹和初始运行状态。
`review.json` 是完整审查结果；`review.md` 是供法务/财税/技术审核人阅读的证据化报告；`--audit-root` 会把结果写入不可覆盖目录，并用 `manifest.json` 校验内容哈希。人工决定和最终确认应通过 `JsonAuditStore.append_revision()` 写入 `revisions/`，加载时自动读取最新 revision，历史工件不覆盖。

也可以完全通过 Python 调用，不依赖 Dify：

```python
import os

from contract_review import (
    OpenAICompatibleSemanticReviewer,
    load_rule_bundle,
    run_review_with_semantic_client,
)

rules = load_rule_bundle("data/contract_rules_v0.14.json")
client = OpenAICompatibleSemanticReviewer(
    endpoint="https://your-compatible-endpoint/v1/chat/completions",
    api_key=os.environ["CONTRACT_REVIEW_API_KEY"],
    model_version="your-model-version",
)
result = run_review_with_semantic_client(
    ["合同主文.docx", "技术协议.pdf"],
    package_id="pkg-001",
    rule_bundle=rules,
    client=client,
    provider="your-provider",
    model_version="your-model-version",
    prompt_version="contract-review-prompt-v1",
    contract_type="software",
)
```

模型调用不是必需前置条件：没有模型时，确定性规则仍然执行，语义/视觉/人工规则会明确输出 `UNKNOWN`；接入模型后，模型响应必须携带已存在的 `evidence_id`，并绑定规则、检索上下文、系统指令、提示词版本、模型版本和配置指纹。

同一源文件、规则快照、解析器、配置和模型版本可以重放：

```python
from contract_review import replay_review

replayed = replay_review(original_result, ["合同.pdf"], rule_bundle=rules)
```

如果源文件、规则版本、配置或结果发生变化，重放会明确失败，不会默认为“结果相同”。

## 当前边界

已经实现数字 PDF 的文字和坐标解析、DOCX 段落/表格单元格解析、XLSX 工作表/单元格解析，以及可注入的 OCR 提供方契约；没有配置 OCR 提供方时，扫描 PDF 会被标记为 `needs_ocr`，不会伪造 OCR 结果。当前词法检索是 RAG 的可回放基线，真实 OCR 引擎、向量库、印章识别、生产数据库、权限和 API 仍应通过独立适配器接入。
