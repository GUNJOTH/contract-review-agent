# ADR-002：合同审查结果 Schema v2

## 状态

已接受。

## 背景

旧结果以 `Finding` 为中心，同时保存 `ReviewTransition` 和 `StageEvent` 两套状态
历史。它无法直接表达合同条款、履约义务、规则问题与证据化结论之间的关系，双轨
状态也增加了审计分歧风险。

## 决策

- `ReviewResult.schema_version` 固定为 `2.0`，缺少该字段的旧结果拒绝加载。
- 删除 `ReviewTransition`，状态历史只使用 `StageEvent`；所有事件通过
  `StageEventStore` 追加和读取。
- JSON 审计存储版本升级为 `json-audit-store-0.3.0`，缺少独立事件 JSONL、事件
  数量或摘要的工件拒绝加载。
- `ReviewResult` 必须包含 `ContractClause`、`ContractObligation`、
  `ReviewQuestion` 和 `QuestionAssessment` 四类领域对象。
- `ReviewResult` 必须为每个 `CandidateEvidence` 保存一个 `EvidenceAssessment`；
  缺少资格裁决的候选不能进入事实、Playbook 或规则检查，旧结果不提供该字段时
  不能继续作为审查输入。
- 规则结论使用 `SUPPORTED`、`CONTRADICTED`、`NOT_MENTIONED`、`UNKNOWN` 和
  `NOT_APPLICABLE`，每个结论必须绑定已经持久化的证据 ID。
- 当前条款分段采用“一知识块一最小条款片段”；没有版面证据时不跨块自动合并。
- 当前义务抽取仅保守识别中文强制/禁止表达，无法确认的主体和期限保持空值并进入
  人工复核，不调用模型补造。
- `KnowledgeChunk.source_kind` 明确区分 `contract` 和 `rule`；规则块只提供判断标准，
  语义结论的 `evidence_ids` 只能来自合同正文块。没有合同正文命中时不调用外部模型，
  保留确定性 `UNKNOWN` 结果。

## 影响

这是有意的破坏性 Schema 升级。旧缓存会被丢弃并重新计算，旧审计工件需要从原始
合同重新运行；不提供静默兼容层。收益是领域语义和事件来源单一，后续混合检索、
版本比较、义务履约跟踪和离线评测可以使用稳定对象，而不是继续扩充松散字典。

## 验收

- 相同输入生成稳定的条款、义务、问题、结论和结果指纹。
- 所有领域对象的证据引用通过审计。
- 规则来源证据不能被语义结论当作合同事实引用，且无正文命中时不发生模型调用。
- 旧 Schema 和旧审计存储版本被明确拒绝。
- 阶段事件连续、唯一、时间有序，并终止于运行当前状态。
