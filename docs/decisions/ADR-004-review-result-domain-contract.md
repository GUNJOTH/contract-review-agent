# ADR-004：以 ReviewResult 作为合同审查业务闭环的唯一核心对象

- 状态：Accepted
- 日期：2026-09-12
- 范围：合同审查核心领域、规则执行器和核心 HTTP 接口

## 背景

早期实现把风险清单、合同要素、规则列表和人工处理结果分别建模，应用层需要在多套结构之间复制字段。这样会造成规则判断散落在标题分支、人工操作缺少完整证据上下文，以及兼容接口与真实审查结果逐渐漂移。

合同审查的最小业务闭环应当能够回答四个问题：本次审查基于哪一份合同包和规则快照、抽取了哪些带来源事实、每条规则为什么得到这个结论、人工确认后结果是否仍然可校验和回放。

## 决策

1. `ReviewResult` 是一次审查的聚合根。合同包、文档解析快照、证据、知识块、条款关系、事实、发现、问题结论、人工决定、运行状态和报告都从该对象读取或更新。
2. 旧的风险清单、要素抽取和规则目录接口不再作为产品契约；应用层直接返回 `ReviewResult` 或 `RuleBundle`，标准要素通过 `ReviewResult.facts` 提供。
3. 每条适用规则严格经过 `RetrievalQuery → RetrievalTrace → CandidateEvidence → EvidenceAssessment`；`CandidateEvidence` 仅是候选，`EvidenceAssessment` 记录来源、必要事实锚点和是否可以被下游模块消费，缺失该裁决的结果不能通过审计。
4. `RuleBundle` 通过 `Rule.checker` 显式绑定确定性检查器。`rule_checkers.py` 是金额、税率、付款、发票和附件完整性的唯一业务计算入口；`engine.py` 只编排规则适用性、Playbook 和结果对象，并拒绝未经资格裁决的候选。
5. `facts.py` 先生成带证据引用的结构化 `ContractFact`，且只能消费确定性资格为 `ACCEPT` 的合同候选；检查器不能从上传文件或全局配置重新读取事实。缺少必要事实、附件或可靠解析时返回 `UNKNOWN` 并给出人工动作。
6. 条款构建器可以把同一编号条款的连续正文聚合为一个 `ContractClause`，但必须保留全部 `source_chunk_ids` 和 `evidence_ids`；定义、父子层级和交叉引用统一进入 `ClauseRelation`，未解析引用保留 `UNRESOLVED`。
7. 人工动作接口接收完整 `ReviewResult` 作为版本条件，但服务器按 `run_id` 回读权威快照，在追加决定或推进 `FINALIZED` 前执行审计、证据引用和结果指纹校验。接口不会接受旧风险清单的局部覆盖，也不会绕过 `HUMAN_REVIEW` 状态。

## 不在本 ADR 范围内

鉴权、监控、部署扩缩容和真实 OCR/模型供应商的运维策略不属于本轮核心业务建模；它们只能作为适配层依赖，不能改变上述领域对象和状态契约。

## 后果

- 新规则先增加规则快照中的 `checker` 和对应检查器，再由固定离线夹具覆盖事实、证据和状态；不能在 Router 或模型提示词中添加一次性判断。
- 旧客户端需要迁移到 `review_result.findings`、`review_result.facts` 和 `rule_bundle.rules`；不再为已删除路径维护兼容投影。
- 完整 `ReviewResult` 仍作为人工、比对和修订接口的版本条件，便于客户端携带 `result_fingerprint`；服务器端 `review_result_store.py` 按 `run_id` 保存当前快照并追加后置结果，客户端提交内容不再是状态的权威来源。
