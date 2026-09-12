# 固定合同离线评测集

`contract_review_cases.json` 是版本化的最小合同夹具集。每个夹具包含固定
文字、合同类型、最低发现数、最低条款/关系/义务数量、最低财务事实数量、检查器
状态、问题结论覆盖率、报告状态和证据类型；带 PII 的夹具还验证外部模型门禁必须
返回 `block`。关系数量使用
`min_clause_relations` 声明，未配置时只验证关系对象和引用契约可审计。

评测不会访问 OCR、Redis、Celery 或外部模型：脚本在临时目录生成文字 PDF，
运行确定性审查，然后检查 Schema v2、条款/关系与义务、规则问题覆盖、证据审计、
检索轨迹契约、阶段事件账本和结果指纹重放。

```powershell
$env:UV_CACHE_DIR = '.uv-cache'
uv run --no-sync python scripts/evaluate_contract_fixtures.py
```

夹具变更应与规则快照变更一起评审；不要把真实合同或真实个人信息提交到
该目录。

## 中文专家标注评测集

`expert_contract_review_cases.json` 是面向核心业务能力的中文脱敏业务场景种子集，覆盖一次性预付、交付/验收缺失、自动续期、全面免责、跨文档金额冲突、金额证据不足、完整核心条款和版本差异。它的最小评测单位不是一段合同文本，而是一个完整的 `ExpertCase` 合同包：

```text
合同包
├── package.main_contract                 主合同
├── package.supporting_documents          附件/报价单/技术协议
├── package.version_or_amendment           版本或补充协议
├── package.business_background            业务背景
├── package.enterprise_position            企业立场、底线和升级条件
└── expert_annotation                      专家标注闭环
    ├── metadata                            标注来源、版本和签署状态
    ├── retrieval_annotations                每条规则的检索金标准、候选和切片
    ├── clause_localization                 条款定位
    ├── evidence_scope                      证据范围
    ├── rule_conclusions                    规则结论
    ├── unknown_reasons                     UNKNOWN 原因和证据缺口
    ├── financial_facts                     金额事实
    ├── financial_calculations               金额计算结果
    ├── version_changes                     版本变化
    └── redline_recommendations             红线建议
```

上述结构由 `scripts/expert_eval_schema.py` 的严格 Pydantic 契约校验：所有字段拒绝额外字段，文档、证据、版本和红线标注必须回指合同包中的真实对象。评测器只读取该 v2 契约，不再读取 `text`、`documents`、`annotations` 等旧扁平字段，也不提供旧格式回退；缺失任何必需区段或引用未知对象会在评测开始前失败。

当前文件的 `annotation_status` 为 `seed_requires_legal_signoff`：它是可执行的专家标注结构和虚构/脱敏场景，不冒充已经由真实律师签字的生产验收集。正式上线前应由合同、财务、技术专家复核每条预期状态，并保留标注人、版本、分歧和裁决记录。

独立评测命令只创建临时 DOCX，直接调用 `contract_review.run_review`，不调用应用层
`run_contract_review`，不会访问真实 OCR、外部模型、Redis 或 Celery：

```powershell
$env:UV_CACHE_DIR = '.uv-cache'
uv run --no-sync python scripts/evaluate_expert_contract_cases.py
```

输出按场景和总集合分别给出九个标注区段的指标：检索召回、条款定位、证据引用、规则判断、
`UNKNOWN` 识别、金额事实、金额计算、版本比对和红线建议。`unknown_false_pass` 是
重点安全指标；任何预期 `UNKNOWN` 被实际输出为自动 `PASS` 都使评测失败。版本比较
和红线建议会先挂入核心 `ReviewResult` 的 `version_comparisons`、`revision_sets`，
再进行标注比对，避免评测器维护一套脱离生产领域模型的旁路结果。

检索专项在每个 `RetrievalAnnotation` 上分别统计 Recall@5、Recall@10、Top 10
候选证据引用准确率和错误候选率，并按规则 checker 和否定词、数字、定义词、例外、
跨文档冲突、版本覆盖切片展开明细。`gold_phrases` 与 `gold_document_ids` 是必须命中
的金标准；`acceptable_phrases` 只用于候选相关性判断；`error_candidate_phrases`
用于暴露把相邻业务事实误当成当前规则证据的召回错误。该指标先作为混合检索基线数据，
在专家评测达到稳定指标前，不引入持久化 ANN 服务或重排模型。切片中的
`expected_recall=false` 不是注释：评测器会计算 `slice_detection_at_10` 和
`negative_slice_false_positive_rate_at_10`，直接暴露把否定词、数字、例外、跨文档冲突或
版本覆盖的错误候选误当成金标准的情况。

检索实现的验收边界也固定在 `ReviewResult`：词法候选使用确定性 BM25，保留数字、
否定和定义短语；配置 embedding 时再用固定 `RRF_K=60` 融合向量候选。每条
`RetrievalTrace` 都通过其 `retrieval_query` 保存 `RetrievalFilter`、规则版本、查询
意图、融合方式、来源名次和证据 ID；轨迹命中随后只能投影成 `CandidateEvidence`，
确定性检查器和语义模型都消费同一份候选。过滤范围覆盖文档、条款、来源、版本、适用
规则和证据白名单。评测只验证候选范围和轨迹完整性，
不把命中直接当作规则结论；`Finding` 必须由确定性检查器或通过逐规则合同证据门禁的
语义响应产生。
