# ADR-005：核心业务规则、版本比对与质量门禁

- 状态：Accepted
- 日期：2026-09-12
- 范围：核心合同条款规则、离线评测和 ReviewResult 后置附件

## 背景

合同审核产品的主要风险不是缺少一个模型调用，而是把“没有证据”误当成“没有风险”，以及让付款、交付、验收、续期、终止、违约责任和附件中的事实在不同模块中各自判断。版本比较和红线建议如果停留在应用层临时 DTO，也无法和审核发现、原文证据、人工确认及回放指纹统一。

## 决策

1. 以独立的 `contract_core_rules_v0.15.json` 作为核心业务扩展 Playbook 快照。规则正文、缺失条款策略、建议文本、升级条件和 `checker` 绑定全部版本化；基础 v0.14 快照保持原始来源不被覆盖。
2. 付款、交付、验收、续期、终止和违约责任先形成 `contract_term:*` 事实，再由集中式检查器判断；跨文档一致性只有在多份文档覆盖规则要求的同类事实时才允许判断，证据不完整返回 `UNKNOWN`。
3. 每条适用规则先通过 `RetrievalQuery → RetrievalTrace → CandidateEvidence` 获取候选；关键词扫描只能参与候选生成。确定性事实抽取、规则检查器和语义模型不得访问候选之外的正文，候选为空时缺失判断只能保持 `UNKNOWN`。
4. 任何模型 `PASS` 必须同时通过高置信度、合同原文证据和人工规则门禁；门禁失败统一转 `UNKNOWN`，并记录 `evidence_quality=INSUFFICIENT` 与 `automatic=false`。确定性检查器遇到低置信度事实也采用同一 fail-closed 语义。
5. 文档比对通过 `ContractVersionComparison` 挂入 `ReviewResult`，每条差异生成 `EvidenceType.COMPARISON` 证据；每条差异进一步形成业务义务、付款/责任/交付验收风险、文件优先效力和 Playbook 重触发信息。红线/修订建议通过 `ContractRevisionSet` 挂入同一结果，不直接改写原合同。
6. Playbook 在加载、执行和审计三个边界校验规则 ID、Playbook 自身版本定义、checker、发布状态、发布指纹和 `ReviewResult` Schema 兼容性；规则快照版本与 Playbook 版本保持独立。`publish_playbook_bundle` 只生成不可变发布快照，不在运行时覆盖源文件。
7. 专家评测集采用中文虚构/脱敏业务场景和明确标注结构；在真实领域专家签字集形成前，不将种子集称为生产法律验收。检索专项先输出 Recall@5、Recall@10、证据引用准确率和错误候选切片，再输出条款定位、证据引用、规则判断、`UNKNOWN`、金额计算、版本比对和红线建议指标，重点检查 `unknown_false_pass`。

## 隔离边界

离线评测只调用 `contract_review.run_review`，使用临时 DOCX 文字层和本地规则快照；它不初始化真实 OCR、外部模型、Redis 或 Celery。应用服务的 OCR、语义模型、向量检索和异步队列仍由适配层接入，必须使用独立联调命令验收，不能用离线评测结果替代。

## 后果

- 规则新增遵循“事实抽取 → 检查器 → ReviewResult”的单向数据流，业务判断不会回到 Router 或静态页面。
- 检索质量与业务判断分开验收：Recall@5/10 只证明候选召回，不能直接证明规则结论；只有候选证据与事实、规则结论的引用链完整时才能进入 ReviewResult。
- 缺失条款可能产生 `BLOCK`、`WARN` 或 `UNKNOWN`，取决于已发布 Playbook 的缺失策略；无论哪种结果都保留缺失对象和人工动作，不会静默通过。
- 版本比较和修订提案会改变核心结果指纹，回放按 `post_review_sequence` 重建后置附件；客户端应把 `ReviewResult` 作为唯一事实源。
